"""批次状态引擎：逐记录受理，维护只追加台账与可折叠的当前状态。

不变量：
- Notice / LedgerEvent 全部冻结且只能追加，没有 update/delete 接口；
- 后续“解除通知”通过 lifts_notice_ids 引用原暂缓通知，原通知仍在台账中；
- 模糊表述只生成 Lead / Issue / ActionItem，绝不自动合并批次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import (
    ActionItem,
    AreaResolution,
    Batch,
    BatchDisposition,
    BatchView,
    BoatResolution,
    BoxClosure,
    BoxStatus,
    BoxView,
    CaseState,
    Issue,
    Lead,
    LeadStatus,
    LedgerEvent,
    Notice,
    NoticeAction,
    NoticeState,
    ParsedRecord,
    TaskStatus,
    TestResult,
    Verdict,
    BATCH_DISPOSITION_LABELS,
    BOX_STATUS_LABELS,
    NOTICE_ACTION_LABELS,
    NOTICE_STATE_LABELS,
)
from .parsing import parse_record
from .resolution import AreaResolver, BoatResolver


@dataclass
class _TaskBuilder:
    ordinal: int
    kind: str
    priority: str
    title: str
    reason: str
    suggested_procedure: str
    batch_id: str | None = None
    boxes_open: set[int] = field(default_factory=set)
    closures: list[BoxClosure] = field(default_factory=list)
    return_to: dict | None = None
    verify_with: dict | None = None
    linked_notice_ids: set[str] = field(default_factory=set)
    status: TaskStatus = TaskStatus.OPEN
    closed_by_record_id: str | None = None


@dataclass
class _LeadBuilder:
    ordinal: int
    source_record_id: str
    quote: str
    hypotheses: tuple[str, ...]
    boat_ids: list[str] = field(default_factory=list)
    candidate_batch_ids: list[str] = field(default_factory=list)
    strong_batch_ids: set[str] = field(default_factory=set)
    status: LeadStatus = LeadStatus.OPEN
    verification_record_id: str | None = None
    confirmed_batch_ids: list[str] = field(default_factory=list)
    excluded_batch_ids: list[str] = field(default_factory=list)


class StationEngine:
    def __init__(self, case: dict):
        self.case = case
        self.case_id = case.get("case_id", "case")
        self.reference_now = case.get("reference_now", "")
        self.disclaimer = case.get("disclaimer", "全部为合成演练资料。")
        self.policy = case.get("policy", {})

        from .models import Boat, WaterArea
        self.areas = tuple(
            WaterArea(
                id=w["id"], name=w["name"],
                aliases=tuple(w.get("aliases", [])),
                note=w.get("note"),
            ) for w in case.get("waters", [])
        )
        self.boats = tuple(
            Boat(
                id=b["id"], name=b["name"],
                registry_no=b.get("registry_no"),
                operator=b.get("operator"),
                aka=tuple(b.get("aka", [])),
                role=b.get("role"),
                contact=b.get("contact"),
                berth=b.get("berth"),
                appears_in=tuple(b.get("appears_in", [])),
                note=b.get("note"),
            ) for b in case.get("boats", [])
        )
        self.area_resolver = AreaResolver(self.areas)
        self.boat_resolver = BoatResolver(self.boats)

        self.raw_records: list[dict] = []
        self.parsed: list[ParsedRecord] = []
        self.batches: list[Batch] = []
        self.tests: list[TestResult] = []
        self.notices: list[Notice] = []
        self.events: list[LedgerEvent] = []
        self.issues: list[Issue] = []
        self._leads: list[_LeadBuilder] = []
        self._tasks: list[_TaskBuilder] = []

        self._n_batch = 0
        self._n_test = 0
        self._n_notice = 0
        self._n_event = 0
        self._n_issue = 0
        self._n_lead = 0
        self._n_task = 0
        self._context_boat_id: str | None = None

    # ------------------------------------------------------------------ utils
    def _next_event_seq(self) -> int:
        self._n_event += 1
        return self._n_event

    def _add_issue(self, record_id: str, code: str, message: str, severity: str = "warning") -> None:
        self._n_issue += 1
        self.issues.append(Issue(
            id=f"ISSUE-{self._n_issue:03d}",
            ordinal=self._n_issue,
            record_id=record_id,
            code=code,
            severity=severity,
            message=message,
        ))

    def _record_event(self, kind: str, record_id: str, message: str,
                      notice_id: str | None = None) -> LedgerEvent:
        ev = LedgerEvent(
            seq=self._next_event_seq(), kind=kind, at_record_id=record_id,
            message=message, notice_id=notice_id,
        )
        self.events.append(ev)
        return ev

    def _find_batch_by_source_record(self, record_id: str) -> Batch | None:
        for b in self.batches:
            if b.primary_record_id == record_id or record_id in b.source_record_ids:
                return b
        return None

    def _resolve_ref_batch(self, parsed: ParsedRecord) -> Batch | None:
        for ref in parsed.refs:
            b = self._find_batch_by_source_record(ref)
            if b is not None:
                return b
        return None

    # ------------------------------------------------------------ notice logic
    def active_restriction_ids(self, batch_id: str, box: int) -> list[str]:
        """折叠台账：返回当前仍有效的限制类通知 id。"""
        active: list[str] = []
        for n in self.notices:
            if n.batch_id != batch_id or box not in n.boxes:
                continue
            if n.action not in (NoticeAction.SUSPEND, NoticeAction.EXPAND):
                continue
            lifted = any(
                l.action == NoticeAction.LIFT
                and l.batch_id == batch_id
                and box in l.boxes
                and n.id in l.lifts_notice_ids
                for l in self.notices
            )
            if not lifted:
                active.append(n.id)
        return active

    def _issue_notice(self, action: NoticeAction, batch: Batch, boxes: tuple[int, ...],
                      record_id: str, title: str, reason: str,
                      evidence: tuple[str, ...], test_records: tuple[str, ...] = (),
                      lifts: tuple[str, ...] = ()) -> Notice:
        self._n_notice += 1
        notice = Notice(
            id=f"NOTICE-{self._n_notice:03d}",
            ordinal=self._n_notice,
            action=action,
            batch_id=batch.id,
            boxes=tuple(sorted(boxes)),
            title=title,
            reason=reason,
            issued_after_record_id=record_id,
            evidence_record_ids=tuple(evidence),
            test_record_ids=tuple(test_records),
            lifts_notice_ids=tuple(lifts),
        )
        self.notices.append(notice)
        label = NOTICE_ACTION_LABELS[action]
        self._record_event(
            "notice_issued", record_id,
            f"发出{label} {notice.id}：{batch.id} 箱 {list(notice.boxes)}。{title}",
            notice_id=notice.id,
        )
        if action in (NoticeAction.SUSPEND, NoticeAction.EXPAND):
            for t in self._tasks:
                if t.batch_id == batch.id and t.status is TaskStatus.OPEN:
                    t.linked_notice_ids.add(notice.id)
                    t.priority = "high"
        return notice

    # --------------------------------------------------------------- tasks
    def _add_sampling_task(self, batch: Batch) -> None:
        self._n_task += 1
        task = _TaskBuilder(
            ordinal=self._n_task,
            kind="sampling",
            priority="normal",
            title=f"{batch.id} 缺样/补检与覆盖核对",
            reason="批次建立后尚无覆盖全部箱位的认可检测；需逐箱落实样品。",
            suggested_procedure=self.policy.get(
                "default_sampling_suggestion",
                "按箱采集肌肉/可食部并用认可方法检测目标残留。",
            ),
            batch_id=batch.id,
            boxes_open=set(batch.boxes),
            return_to=self.boat_resolver.contact_snapshot(batch.boat_id),
        )
        self._tasks.append(task)

    def _sampling_task(self, batch_id: str) -> _TaskBuilder | None:
        for t in self._tasks:
            if t.kind == "sampling" and t.batch_id == batch_id:
                return t
        return None

    # ------------------------------------------------------------- ingestion
    def ingest(self, record: dict) -> None:
        self.raw_records.append(record)
        ref_now = self.reference_now
        parsed = self.area_resolver.classify(parse_record(record, ref_now))
        self.parsed.append(parsed)
        rid = parsed.record_id
        self._record_event("record_received", rid, f"受理 {rid}（{parsed.kind}）：{parsed.text[:42]}…")

        for note in parsed.parse_notes:
            self._add_issue(rid, "PARSE_NOTE", note, severity="note")
        for surface in parsed.unresolved_area_mentions:
            self._add_issue(
                rid, "AREA_UNRESOLVED",
                f"水域称谓‘{surface}’不在 waters 规范名/显式别名表中；"
                f"禁止按‘石湾’等子串并入 A-1，需采样人员核对具体口门。",
                severity="warning",
            )

        kind = parsed.kind
        if kind in ("catch-note", "market-update"):
            self._ingest_lot_record(parsed)
        elif kind == "lab-result":
            self._ingest_lab(parsed)
        elif kind == "voice-transcript":
            self._ingest_voice(parsed)
        elif kind == "verification":
            self._ingest_verification(parsed)

    def _ingest_lot_record(self, parsed: ParsedRecord) -> None:
        rid = parsed.record_id
        area_id, area_surface, area_status = self.area_resolver.primary_area(parsed)
        boat_id, boat_code, boat_detail = self.boat_resolver.resolve_lot_boat(
            parsed, context_boat_id=self._context_boat_id,
        )
        if boat_id is not None:
            self._context_boat_id = boat_id
        elif parsed.cued_boat_name or "船名" in parsed.text or "海星" in parsed.text:
            self._add_issue(
                rid, "BOAT_UNRESOLVED",
                f"船只身份未能唯一确定（{boat_detail}）；相关货批不得与重名船批次合并。",
                severity="high",
            )

        if area_status is AreaResolution.UNRESOLVED and area_surface:
            # 有称谓但不在别名表：AREA_UNRESOLVED 已在 ingest 主流程登记
            pass
        elif area_id is None:
            self._add_issue(
                rid, "AREA_MISSING",
                "记录没有可识别的水域称谓，批次无法判断捕捞位置，需向船方补问。",
                severity="warning",
            )

        self._n_batch += 1
        haul = parsed.haul
        batch = Batch(
            id=f"LOT-{self._n_batch:03d}",
            ordinal=self._n_batch,
            primary_record_id=rid,
            source_record_ids=(rid,),
            boat_id=boat_id,
            boat_resolution=boat_code.value if isinstance(boat_code, BoatResolution) else str(boat_code),
            area_id=area_id,
            area_mention=area_surface,
            area_resolution=area_status.value,
            event_date=haul.event_date if haul else None,
            part_of_day=haul.part_of_day if haul else None,
            haul_seq=haul.seq if haul else None,
            species=parsed.species,
            boxes=tuple(parsed.boxes),
            market_place=parsed.market_place,
            distribution=parsed.distribution,
        )
        self.batches.append(batch)
        self._add_sampling_task(batch)
        self._record_event(
            "batch_created", rid,
            f"建立批次 {batch.id}：船 {boat_id or '未确定'} / 水域 {area_id or area_surface or '未确定'} "
            f"/ 第 {batch.haul_seq} 网 / 箱 {list(batch.boxes)}。",
        )
        self._match_open_leads_to_batch(batch, parsed)

        if area_status is AreaResolution.UNRESOLVED:
            self._add_clarify_task_for_area(parsed, batch)

    def _add_clarify_task_for_area(self, parsed: ParsedRecord, batch: Batch | None) -> None:
        if batch is not None and batch.area_mention:
            title = f"核实 {parsed.record_id} 的水域称谓‘{batch.area_mention}’"
            reason = "记录中的水域是泛称/近名，未在别名表登记，不能直接落位到 A-1 或 A-3。"
        else:
            title = f"补清 {parsed.record_id} 的捕捞水域"
            reason = "记录未给出可识别水域，批次缺少捕捞位置，不能判断与其他水域批次是否同域。"
        self._n_task += 1
        self._tasks.append(_TaskBuilder(
            ordinal=self._n_task,
            kind="clarify_record",
            priority="normal",
            title=title,
            reason=reason,
            suggested_procedure="向记录中的船只核对坐标、相邻标志与潮汐口门，确认后再补登水域编号。",
            batch_id=batch.id if batch else None,
            return_to=self.boat_resolver.contact_snapshot(batch.boat_id) if batch else None,
        ))

    # ------------------------------------------------------------------- lab
    def _qualify_negative(self, parsed: ParsedRecord) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        accepted_tissues = tuple(self.policy.get("release_accepted_tissues", ()))
        accepted_methods = tuple(self.policy.get("release_accepted_methods", ()))
        if parsed.tissue not in accepted_tissues:
            reasons.append(
                f"受检部位‘{parsed.tissue or '未载明'}’不属于演练认可的放行部位 "
                f"{list(accepted_tissues)}；该阴性结果只能说明该部位/样品的情况，不能支持整箱放行"
            )
        if not parsed.method:
            reasons.append("原文未载明检测方法，无法确认是否属于认可方法")
        elif parsed.method not in accepted_methods:
            reasons.append(
                f"方法 {parsed.method} 不在认可方法 {list(accepted_methods)} 内"
            )
        return (len(reasons) == 0, tuple(reasons))

    def _ingest_lab(self, parsed: ParsedRecord) -> None:
        rid = parsed.record_id
        batch = self._resolve_ref_batch(parsed)
        box = parsed.boxes[0] if parsed.boxes else None
        if batch is None:
            self._add_issue(
                rid, "LAB_BATCH_UNRESOLVED",
                "化验单没有通过记录编号引用到任何已建批次；只能作为独立结果留存，"
                "不得用于支持任何批次决定，需采样人员核对样品出处。",
                severity="high",
            )
        if box is None:
            self._add_issue(rid, "LAB_BOX_UNRESOLVED", "化验单未识别到受检箱号。", severity="high")

        verdict = parsed.verdict
        if verdict is None:
            self._add_issue(rid, "VERDICT_UNKNOWN", "未能识别检出/未检出结论。", severity="high")
            return

        self._n_test += 1
        test_id = f"T-{self._n_test:03d}"

        target = f"{batch.id} 箱 {box}" if batch and box is not None else (
            f"{batch.id}" if batch else "批次未确定"
        )
        verdict_word = "未检出" if verdict is Verdict.NOT_DETECTED else "检出"
        scope = (
            f"{parsed.lab_no or rid} 的“{verdict_word}”结论仅对实际送检样品有效："
            f"来源 {target}，组织/部位为 {parsed.tissue or '原文未载明'}，"
            f"指标 {parsed.analyte or '原文未载明'}，方法 {parsed.method or '原文未载明'}；"
            f"不自动延伸到同批其他箱、其他组织、其他网次、重名船或整船货。"
        )

        qualifies = False
        reasons: tuple[str, ...] = ()
        if verdict is Verdict.NOT_DETECTED:
            qualifies, reasons = self._qualify_negative(parsed)
            if not qualifies:
                self._add_issue(
                    rid, "NEGATIVE_SCOPE_LIMITED",
                    "；".join(reasons) + f"。因此 {rid} 不支持解除 {target} 的销售限制，需按认可部位/方法补检。",
                    severity="note",
                )
        else:
            reasons = ("检出结果不支持放行，应支持对相应范围采取控制措施。",)

        test = TestResult(
            id=test_id, ordinal=self._n_test, record_id=rid, lab_no=parsed.lab_no,
            batch_id=batch.id if batch else None, box=box,
            tissue=parsed.tissue, analyte=parsed.analyte, method=parsed.method,
            verdict=verdict, scope_statement=scope,
            qualifies_for_release=qualifies, qualification_reasons=reasons,
        )
        self.tests.append(test)
        self._record_event(
            "result_received", rid,
            f"登记检测 {test_id}：{target} / {parsed.tissue or '部位未载明'} / "
            f"{NOTICE_ACTION_LABELS and ('未检出' if verdict is Verdict.NOT_DETECTED else '检出')} / "
            f"可支持放行={qualifies}。",
        )

        if batch is None or box is None:
            return

        task = self._sampling_task(batch.id)

        if verdict is Verdict.DETECTED:
            # 检出箱不再是“缺样”，而是执法控制对象
            if task and box in task.boxes_open:
                task.boxes_open.discard(box)
            if self.policy.get("detected_triggers_whole_lot_suspend", True):
                already = any(
                    n.batch_id == batch.id and n.action is NoticeAction.SUSPEND
                    for n in self.notices
                )
                if not already and batch.boxes:
                    self._issue_notice(
                        NoticeAction.SUSPEND, batch, batch.boxes, rid,
                        title=f"{batch.id} 整网货批暂缓销售，逐箱定位风险",
                        reason=(
                            f"{rid}（{parsed.lab_no or '化验单'}）在箱 {box} "
                            f"{parsed.tissue or ''} 检出{parsed.analyte or '目标指标'}；"
                            f"同一网次其余箱在排查/补检完成前一并暂缓。"
                        ),
                        evidence=(rid,), test_records=(rid,),
                    )
            return

        # 未检出
        if qualifies:
            if task and box in task.boxes_open:
                task.boxes_open.discard(box)
                closure = BoxClosure(
                    box=box, record_id=rid, test_id=test_id,
                    note="认可部位与方法的未检出结果，支持该箱解除限制",
                )
                task.closures.append(closure)
                if not task.boxes_open and task.status is TaskStatus.OPEN:
                    task.status = TaskStatus.CLOSED
                    task.closed_by_record_id = rid
            active = self.active_restriction_ids(batch.id, box)
            if active:
                covering = [n for n in self.notices if n.id in active]
                self._issue_notice(
                    NoticeAction.LIFT, batch, (box,), rid,
                    title=f"解除箱 {box} 的限制（局部解除，其余范围不变）",
                    reason=(
                        f"{rid}（{parsed.lab_no or '化验单'}）对箱 {box} "
                        f"{parsed.tissue}、按 {parsed.method} 检测"
                        f"{parsed.analyte or '目标指标'}未检出，符合演练放行条件；"
                        f"仅解除箱 {box}，原通知其余箱继续执行。"
                    ),
                    evidence=(rid,), test_records=(rid,),
                    lifts=tuple(sorted({n.id for n in covering})),
                )

    # ----------------------------------------------------------------- voice
    def _ingest_voice(self, parsed: ParsedRecord) -> None:
        rid = parsed.record_id
        mentioned_boats = self.boat_resolver.mention_boats(parsed.text)

        if parsed.claim_quote:
            self._n_lead += 1
            hypotheses = (
                "解释一：指向同一潮汐、同一船相邻网次（待采样核对网次/潮汐记录）",
                "解释二：指向邻船的另一次独立作业（船名、水域、时间均需核对）",
                "在核实前两种解释不得相互替代，也不得把相关批次合并。",
            )
            lead = _LeadBuilder(
                ordinal=self._n_lead,
                source_record_id=rid,
                quote=parsed.claim_quote,
                hypotheses=hypotheses,
                boat_ids=mentioned_boats,
            )
            self._leads.append(lead)
            self._record_event(
                "lead_opened", rid,
                f"登记模糊关联线索 LEAD-{lead.ordinal:03d}：原话“{lead.quote}”；"
                f"提及船只 {mentioned_boats}。线索不是已核实事实，禁止据此并批。",
            )
            self._n_task += 1
            self._tasks.append(_TaskBuilder(
                ordinal=self._n_task,
                kind="lead_verification",
                priority="high",
                title=f"核实 LEAD-{lead.ordinal:03d}“昨天那片网”的真实指向",
                reason="语音表述存在同潮相邻网次/邻船独立作业两种解释，需采样人员回溯样品对应位置与网次。",
                suggested_procedure=(
                    "找到 B-1 船长核对潮汐、网次时间与定位；同时向被提及的邻船 B-3 "
                    "核对同一时段作业水域；形成书面核查记录（引用 FC 编号）后再决定扩查范围。"
                ),
                verify_with=self.boat_resolver.contact_snapshot(mentioned_boats[0]) if mentioned_boats else None,
            ))
            self._match_lead_to_existing_batches(lead, parsed)
            return

        # 无明确线索句式，但描述了说不清楚的作业（如 FC-11：泛称水域、无箱号）
        describes_trip = bool(re.search(r"[一二两三四五六七八九十\d]+\s*网", parsed.text))
        need_clarify = (
            bool(parsed.unresolved_area_mentions)
            or (parsed.haul is not None and not parsed.boxes)
            or (describes_trip and not parsed.boxes)
        )
        if need_clarify:
            self._n_task += 1
            boat_snap = self.boat_resolver.contact_snapshot(mentioned_boats[0]) if len(mentioned_boats) == 1 else None
            self._tasks.append(_TaskBuilder(
                ordinal=self._n_task,
                kind="clarify_record",
                priority="normal",
                title=f"补清 {rid} 所述无法建批的作业信息",
                reason="记录提到一次作业，但水域为泛称/近名或缺少箱号，尚不能建立可检验批次。",
                suggested_procedure="向当事船补问日期、水域、网次、物种、箱数与去向，再决定是否建批抽检。",
                verify_with=boat_snap,
            ))

    def _haul_match(self, lead_record: ParsedRecord | None, batch: Batch) -> tuple[bool, str]:
        """判断批次与线索中的时间/网次描述是否强匹配。"""
        if lead_record is None or batch.event_date is None:
            return False, "缺少可比对的时间/网次"
        haul_mentions = [lead_record.haul, *lead_record.extra_hauls]
        for h in haul_mentions:
            if h is None:
                continue
            if h.event_date == batch.event_date and (h.seq is None or h.seq == batch.haul_seq):
                if h.seq == batch.haul_seq and batch.haul_seq is not None:
                    return True, f"线索所述 {batch.event_date} 第 {batch.haul_seq} 网与本批完全对应"
                return False, f"日期 {batch.event_date} 相符但网次不明确"
        return False, "时间/网次不对应"

    def _match_lead_to_existing_batches(self, lead: _LeadBuilder, voice_parsed: ParsedRecord) -> None:
        for b in self.batches:
            boat_ok = b.boat_id in lead.boat_ids if b.boat_id else False
            strong, why = self._haul_match(voice_parsed, b)
            if strong and boat_ok:
                lead.candidate_batch_ids.append(b.id)
                lead.strong_batch_ids.add(b.id)
            elif boat_ok:
                lead.candidate_batch_ids.append(b.id)

    def _match_open_leads_to_batch(self, batch: Batch, lot_parsed: ParsedRecord) -> None:
        for lead in self._leads:
            if lead.status is not LeadStatus.OPEN:
                continue
            voice = next((p for p in self.parsed if p.record_id == lead.source_record_id), None)
            boat_ok = batch.boat_id in lead.boat_ids if batch.boat_id else False
            strong, why = self._haul_match(voice, batch)
            if batch.id not in lead.candidate_batch_ids and (boat_ok or strong):
                lead.candidate_batch_ids.append(batch.id)
            if strong and boat_ok:
                lead.strong_batch_ids.add(batch.id)
                if self.policy.get("unverified_link_triggers_market_hold", True) and batch.boxes:
                    self._issue_notice(
                        NoticeAction.EXPAND, batch, batch.boxes, lot_parsed.record_id,
                        title="按模糊关联线索扩大核查，相关箱先暂停销售待采样",
                        reason=(
                            f"{lead.source_record_id} 称“{lead.quote}”；新到记录 "
                            f"{lot_parsed.record_id} {why}，且船主别称一致。"
                            f"但线索尚未经采样核查，故只作扩大核查暂控，不等同检出确认。"
                        ),
                        evidence=(lead.source_record_id, lot_parsed.record_id),
                    )

    # ---------------------------------------------------------- verification
    def _ingest_verification(self, parsed: ParsedRecord) -> None:
        rid = parsed.record_id
        self._record_event(
            "verification_recorded", rid,
            f"受理采样核查结论：确认 {parsed.confirmed_ref}，排除 {parsed.excluded_ref}。",
        )
        lead_ref = parsed.verification_lead_ref
        lead = next((l for l in self._leads if l.source_record_id == lead_ref), None)
        if lead is None:
            self._add_issue(rid, "VERIFICATION_NO_LEAD", "核查记录没有找到对应线索。", severity="warning")
            return

        conf_batch = self._find_batch_by_source_record(parsed.confirmed_ref) if parsed.confirmed_ref else None
        excl_batch = self._find_batch_by_source_record(parsed.excluded_ref) if parsed.excluded_ref else None
        if conf_batch:
            lead.confirmed_batch_ids = [conf_batch.id]
            if conf_batch.id not in lead.candidate_batch_ids:
                lead.candidate_batch_ids.append(conf_batch.id)
        if excl_batch:
            lead.excluded_batch_ids = [excl_batch.id]
            if excl_batch.id not in lead.candidate_batch_ids:
                lead.candidate_batch_ids.append(excl_batch.id)
        lead.status = LeadStatus.VERIFIED
        lead.verification_record_id = rid

        for t in self._tasks:
            if t.kind == "lead_verification" and t.status is TaskStatus.OPEN:
                # 只有一条线索任务（本演练）；关闭并记录结论
                t.status = TaskStatus.CLOSED
                t.closed_by_record_id = rid
                t.reason += f" 已由 {rid} 核查：确认 {conf_batch.id if conf_batch else parsed.confirmed_ref}，" \
                            f"排除 {excl_batch.id if excl_batch else parsed.excluded_ref}；批次仍保持独立编号。"

    # ------------------------------------------------------------- snapshots
    def notice_derived_state(self, notice: Notice) -> NoticeState:
        if notice.action is NoticeAction.LIFT:
            return NoticeState.EFFECTIVE
        lifted_boxes: set[int] = set()
        for l in self.notices:
            if l.action is not NoticeAction.LIFT or notice.id not in l.lifts_notice_ids:
                continue
            if l.batch_id != notice.batch_id:
                continue
            lifted_boxes.update(l.boxes)
        target = set(notice.boxes)
        if target <= lifted_boxes:
            return NoticeState.LIFTED
        if lifted_boxes & target:
            return NoticeState.PARTIALLY_LIFTED
        return NoticeState.IN_FORCE

    def _build_box_views(self) -> list[BoxView]:
        views: list[BoxView] = []
        for b in self.batches:
            for box in b.boxes:
                tests = [t.record_id for t in self.tests if t.batch_id == b.id and t.box == box]
                detected = any(
                    t.batch_id == b.id and t.box == box and t.verdict is Verdict.DETECTED
                    for t in self.tests
                )
                qneg = any(
                    t.batch_id == b.id and t.box == box
                    and t.verdict is Verdict.NOT_DETECTED and t.qualifies_for_release
                    for t in self.tests
                )
                covering = [
                    n for n in self.notices
                    if n.batch_id == b.id and box in n.boxes
                    and n.action in (NoticeAction.SUSPEND, NoticeAction.EXPAND)
                ]
                lift_ids = [
                    n.id for n in self.notices
                    if n.action is NoticeAction.LIFT and n.batch_id == b.id and box in n.boxes
                ]
                active_ids = self.active_restriction_ids(b.id, box)
                active_notices = [n for n in covering if n.id in active_ids]

                if not active_ids:
                    if covering:
                        status = BoxStatus.RELEASED_LIFTED
                    elif qneg:
                        status = BoxStatus.RELEASED_TESTED
                    else:
                        status = BoxStatus.CIRCULATING
                else:
                    if detected:
                        status = BoxStatus.SUSPENDED_CONFIRMED
                    elif any(n.action is NoticeAction.EXPAND for n in active_notices) and \
                            not any(n.action is NoticeAction.SUSPEND for n in active_notices):
                        status = BoxStatus.VERIFICATION_HOLD
                    else:
                        status = BoxStatus.HOLD_PENDING_SAMPLE

                views.append(BoxView(
                    batch_id=b.id, box=box, status=status,
                    status_label=BOX_STATUS_LABELS[status],
                    test_record_ids=tuple(tests),
                    active_notice_ids=tuple(active_ids),
                    lift_notice_ids=tuple(lift_ids),
                    missing_qualifying_sample=(not detected and not qneg),
                ))
        return views

    def _build_batch_views(self, box_views: list[BoxView]) -> list[BatchView]:
        views: list[BatchView] = []
        by_batch: dict[str, list[BoxView]] = {}
        for v in box_views:
            by_batch.setdefault(v.batch_id, []).append(v)

        for b in self.batches:
            bvs = by_batch.get(b.id, [])
            restricted = [v.box for v in bvs if v.active_notice_ids]
            released = [v.box for v in bvs if not v.active_notice_ids]
            unsampled = [v.box for v in bvs if v.missing_qualifying_sample]
            test_ids = tuple(dict.fromkeys(
                t.record_id for t in self.tests if t.batch_id == b.id
            ))
            notice_ids = tuple(n.id for n in self.notices if n.batch_id == b.id)

            if not restricted:
                if any(v.status in (BoxStatus.RELEASED_LIFTED, BoxStatus.RELEASED_TESTED) for v in bvs):
                    disp = BatchDisposition.RELEASED
                else:
                    disp = BatchDisposition.CIRCULATE_SAMPLING
            else:
                enforcement = any(
                    v.status in (BoxStatus.SUSPENDED_CONFIRMED, BoxStatus.HOLD_PENDING_SAMPLE)
                    for v in bvs if v.active_notice_ids
                )
                if enforcement:
                    disp = (BatchDisposition.LOCAL_SUSPENSION
                            if len(restricted) < len(bvs) else BatchDisposition.BATCH_SUSPENSION)
                else:
                    disp = BatchDisposition.EXPANDED_VERIFICATION

            views.append(BatchView(
                batch_id=b.id, disposition=disp,
                disposition_label=BATCH_DISPOSITION_LABELS[disp],
                box_views=tuple(sorted(bvs, key=lambda v: v.box)),
                restricted_boxes=tuple(restricted),
                released_boxes=tuple(released),
                unsampled_boxes=tuple(unsampled),
                supporting_test_record_ids=test_ids,
                notice_ids=notice_ids,
            ))
        return views

    def _build_evidence_matrix(self, batch_views: list[BatchView]) -> tuple[dict, ...]:
        rows: list[dict] = []
        bmap = {v.batch_id: v for v in batch_views}
        for b in self.batches:
            v = bmap[b.id]
            rows.append({
                "row_type": "batch_current_decision",
                "batch_id": b.id,
                "source_record_ids": list(b.source_record_ids),
                "decision_code": v.disposition.value,
                "decision": v.disposition_label,
                "restricted_boxes": list(v.restricted_boxes),
                "released_boxes": list(v.released_boxes),
                "boxes_still_missing_qualifying_sample": list(v.unsampled_boxes),
                "supporting_test_record_ids": list(v.supporting_test_record_ids),
                "supporting_notice_ids": list(v.notice_ids),
            })
        for n in self.notices:
            state = self.notice_derived_state(n)
            rows.append({
                "row_type": "notice_decision",
                "notice_id": n.id,
                "action_code": n.action.value,
                "action": NOTICE_ACTION_LABELS[n.action],
                "batch_id": n.batch_id,
                "boxes": list(n.boxes),
                "derived_state_code": state.value,
                "derived_state": NOTICE_STATE_LABELS[state],
                "issued_after_record_id": n.issued_after_record_id,
                "evidence_record_ids": list(n.evidence_record_ids),
                "supporting_test_record_ids": list(n.test_record_ids),
                "lifts_notice_ids": list(n.lifts_notice_ids),
                "reason": n.reason,
            })
        return tuple(rows)

    def snapshot(self) -> CaseState:
        box_views = self._build_box_views()
        batch_views = self._build_batch_views(box_views)
        matrix = self._build_evidence_matrix(batch_views)

        actions = tuple(
            ActionItem(
                id=f"TASK-{t.ordinal:03d}",
                ordinal=t.ordinal,
                kind=t.kind,
                priority=t.priority,
                title=t.title,
                reason=t.reason,
                suggested_procedure=t.suggested_procedure,
                status=t.status,
                batch_id=t.batch_id,
                boxes_open=tuple(sorted(t.boxes_open)),
                closures=tuple(t.closures),
                return_to=t.return_to,
                verify_with=t.verify_with,
                linked_notice_ids=tuple(sorted(t.linked_notice_ids)),
                closed_by_record_id=t.closed_by_record_id,
            )
            for t in self._tasks
        )
        leads = tuple(
            Lead(
                id=f"LEAD-{l.ordinal:03d}", ordinal=l.ordinal,
                source_record_id=l.source_record_id, quote=l.quote,
                hypotheses=l.hypotheses,
                boat_ids=tuple(l.boat_ids),
                candidate_batch_ids=tuple(l.candidate_batch_ids),
                status=l.status,
                verification_record_id=l.verification_record_id,
                confirmed_batch_ids=tuple(l.confirmed_batch_ids),
                excluded_batch_ids=tuple(l.excluded_batch_ids),
            )
            for l in self._leads
        )
        return CaseState(
            case_id=self.case_id,
            reference_now=self.reference_now,
            disclaimer=self.disclaimer,
            policy=self.policy,
            waters=self.areas,
            boats=self.boats,
            raw_records=tuple(self.raw_records),
            parsed=tuple(self.parsed),
            batches=tuple(self.batches),
            tests=tuple(self.tests),
            notices=tuple(self.notices),
            events=tuple(self.events),
            leads=leads,
            issues=tuple(self.issues),
            actions=actions,
            box_views=tuple(box_views),
            batch_views=tuple(batch_views),
            evidence_matrix=matrix,
        )


def build_case(case: dict) -> CaseState:
    engine = StationEngine(case)
    for record in case["records"]:
        engine.ingest(record)
    return engine.snapshot()
