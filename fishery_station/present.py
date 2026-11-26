"""把 CaseState 呈现为可复核的 JSON / Markdown。"""

from __future__ import annotations

import json
from dataclasses import asdict
from enum import Enum
from pathlib import Path

from .models import (
    LeadStatus,
    NOTICE_ACTION_LABELS,
    NOTICE_STATE_LABELS,
    NoticeAction,
    TaskStatus,
)


def _json_default(o):
    if isinstance(o, Enum):
        return o.value
    return str(o)


def state_to_jsonable(state) -> dict:
    return asdict(state)


def dump_state_json(state, path: Path) -> None:
    path.write_text(
        json.dumps(state_to_jsonable(state), ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def write_ledger_jsonl(state, path: Path) -> None:
    notice_by_id = {n.id: n for n in state.notices}
    lines = []
    for ev in state.events:
        row = asdict(ev)
        if ev.notice_id and ev.notice_id in notice_by_id:
            row["notice"] = asdict(notice_by_id[ev.notice_id])
        lines.append(json.dumps(row, ensure_ascii=False, default=_json_default))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Markdown 小工具
# ---------------------------------------------------------------------------

def compress_boxes(boxes) -> str:
    boxes = sorted(int(b) for b in boxes)
    if not boxes:
        return "（无箱号）"
    ranges: list[str] = []
    start = prev = boxes[0]
    for b in boxes[1:]:
        if b == prev + 1:
            prev = b
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = b
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return "、".join(ranges)


def _boat_line(state, boat_id: str | None) -> str:
    if not boat_id:
        return "船只身份未确定（禁止与重名船并批）"
    b = next((x for x in state.boats if x.id == boat_id), None)
    if b is None:
        return boat_id
    aka = f"，人称{b.aka[0]}" if b.aka else ""
    return f"{b.id} {b.name}（{b.registry_no or '无登记号'}；{b.operator or '负责人未登记'}{aka}）"


def _contact_block(snap: dict | None) -> str:
    if not snap:
        return "归还对象未能确定，先由值班采样员核对船名/船号。"
    return (
        f"{snap.get('boat_id')} {snap.get('name')}；"
        f"负责人 {snap.get('operator') or '未登记'}（{snap.get('role') or ''}）；"
        f"{snap.get('contact') or '无联系方式'}；停靠/泊位：{snap.get('berth') or '未登记'}"
    )


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------

def render_markdown(state) -> str:
    L: list[str] = []
    w = L.append

    w(f"# 批次状态与处置复核报告 · {state.case_id}")
    w("")
    w(f"> {state.disclaimer}")
    w(f">")
    w(f"> 时间锚点：{state.reference_now or '未提供（相对日期未换算）'}；"
      f"本报告由记录按受理顺序逐件折叠生成，通知台账只追加、不覆盖。")
    w("")

    bmap = {b.id: b for b in state.batches}
    vmap = {v.batch_id: v for v in state.batch_views}

    # 1. 结论速览 -----------------------------------------------------------
    w("## 一、市场处置结论速览")
    w("")
    w("| 批次 | 来源记录 | 船只（稳定编号） | 水域 | 时间/网次 | 箱号 | 当前处置 | 仍受限箱 | 仍缺认可样箱 |")
    w("|---|---|---|---|---|---|---|---|---|")
    for b in state.batches:
        v = vmap[b.id]
        area = next((a for a in state.waters if a.id == b.area_id), None)
        area_txt = area.name if area else f"⚠ {b.area_mention or '未识别'}（未归属）"
        when = " ".join(x for x in (b.event_date, b.part_of_day,
                                    f"第{b.haul_seq}网" if b.haul_seq else None) if x) or "时间未解析"
        w(
            f"| {b.id} | {b.primary_record_id} | {_boat_line(state, b.boat_id)} "
            f"| {area_txt} | {when} | {compress_boxes(b.boxes)} "
            f"| **{v.disposition_label}** | {compress_boxes(v.restricted_boxes) if v.restricted_boxes else '—'} "
            f"| {compress_boxes(v.unsampled_boxes) if v.unsampled_boxes else '—'} |"
        )
    w("")

    # 2. 箱位状态与证据链 ----------------------------------------------------
    w("## 二、逐箱状态与“检测 → 决定”支持关系")
    w("")
    for b in state.batches:
        v = vmap[b.id]
        w(f"### {b.id}（来源 {', '.join(b.source_record_ids)}）")
        w("")
        w(f"- 物种：{'、'.join(b.species) if b.species else '原文未载明'}")
        w(f"- 去向/位置：{b.market_place or b.distribution or '原文未载明'}")
        w(f"- 当前处置：**{v.disposition_label}**")
        w("")
        w("| 箱号 | 状态 | 支持检测记录 | 当前有效通知 | 解除通知 | 仍缺合格样 |")
        w("|---|---|---|---|---|---|")
        for bv in v.box_views:
            w(
                f"| {bv.box} | {bv.status_label} "
                f"| {', '.join(bv.test_record_ids) if bv.test_record_ids else '无'} "
                f"| {', '.join(bv.active_notice_ids) if bv.active_notice_ids else '—'} "
                f"| {', '.join(bv.lift_notice_ids) if bv.lift_notice_ids else '—'} "
                f"| {'是' if bv.missing_qualifying_sample else '否'} |"
            )
        w("")
        rel_tests = [t for t in state.tests if t.batch_id == b.id]
        if rel_tests:
            w("检测适用范围（检验人员解释口径）：")
            w("")
            for t in rel_tests:
                mark = "可支持放行" if t.qualifies_for_release else "不构成整箱放行依据"
                w(f"- `{t.record_id}` / {t.lab_no or '无化验单编号'} / {t.id}：{t.scope_statement}")
                w(f"  - 判定：**{mark}**")
                for r in t.qualification_reasons:
                    w(f"  - 说明：{r}")
            w("")
        rel_notices = [n for n in state.notices if n.batch_id == b.id]
        if rel_notices:
            w("批次决定链：")
            w("")
            for n in rel_notices:
                derived = _derived_label(state, n)
                lifts = f"，解除对象 {', '.join(n.lifts_notice_ids)}" if n.lifts_notice_ids else ""
                w(f"- {n.id}【{NOTICE_ACTION_LABELS[n.action]}】箱 {compress_boxes(n.boxes)}"
                  f"（{derived}{lifts}），依据记录 {', '.join(n.evidence_record_ids)}"
                  f"，检测 {', '.join(n.test_record_ids) if n.test_record_ids else '无（线索/扩查）'}")
                w(f"  - {n.reason}")
            w("")

    # 3. 通知台账 ------------------------------------------------------------
    w("## 三、通知台账（只追加；已解除的暂缓通知仍永久留痕）")
    w("")
    w("| 序号 | 通知号 | 动作 | 批次 | 箱范围 | 发出时受理记录 | 依据记录 | 检测支持 | 解除引用 | 派生状态 |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for n in state.notices:
        w(
            f"| {n.ordinal} | {n.id} | {NOTICE_ACTION_LABELS[n.action]} | {n.batch_id} "
            f"| {compress_boxes(n.boxes)} | {n.issued_after_record_id} "
            f"| {', '.join(n.evidence_record_ids)} "
            f"| {', '.join(n.test_record_ids) if n.test_record_ids else '—'} "
            f"| {', '.join(n.lifts_notice_ids) if n.lifts_notice_ids else '—'} "
            f"| {_derived_label(state, n)} |"
        )
    w("")
    w("> 说明：追补的阴性结果只会追加新的解除通知并折叠出新的当前状态；"
      "NOTICE-001 等原发暂缓通知不会被删除或改写。")
    w("")

    # 4. 检测支持总索引 ------------------------------------------------------
    w("## 四、检测结果 → 决定 支持索引")
    w("")
    w("| 检测记录 | 化验单 | 结论 | 适用批次/箱 | 部位 | 方法 | 可否支持放行 | 支撑的通知 |")
    w("|---|---|---|---|---|---|---|---|")
    for t in state.tests:
        verdict = "未检出" if t.verdict.value == "not_detected" else "检出"
        if t.qualifies_for_release:
            support = "是（认可部位+方法）"
        elif t.verdict.value == "detected":
            support = "否（检出，支持控制）"
        else:
            support = "否（阴性范围有限）"
        supports = []
        for n in state.notices:
            if t.record_id in n.test_record_ids:
                supports.append(n.id)
        scope = f"{t.batch_id or '未归属'} / 箱{t.box if t.box is not None else '?'}"
        w(
            f"| {t.record_id} | {t.lab_no or '—'} | {verdict} | {scope} "
            f"| {t.tissue or '未载明'} | {t.method or '未载明'} "
            f"| {support} "
            f"| {', '.join(supports) if supports else '—'} |"
        )
    w("")

    # 5. 缺样与补检归还 ------------------------------------------------------
    w("## 五、缺样清单与后续补检归还对象")
    w("")
    open_sampling = [a for a in state.actions if a.kind == "sampling" and a.status == TaskStatus.OPEN]
    closed_sampling = [a for a in state.actions if a.kind == "sampling" and a.status == TaskStatus.CLOSED]
    if open_sampling:
        for a in open_sampling:
            prio = "高（受限/风险关联）" if a.priority == "high" else "常规"
            b = bmap.get(a.batch_id)
            w(f"### {a.id} · {a.title}（优先级：{prio}）")
            w("")
            w(f"- 待补检箱号：**{compress_boxes(a.boxes_open)}**")
            if b:
                w(f"- 对应捕捞：{_boat_line(state, b.boat_id)}；"
                  f"水域 {b.area_id or '未解析'}；{b.event_date or ''} {b.part_of_day or ''} "
                  f"第 {b.haul_seq} 网；首条记录 {b.primary_record_id}")
                if b.market_place:
                    w(f"- 货物位置：{b.market_place}")
            w(f"- **补检应回到：{_contact_block(a.return_to)}**")
            w(f"- 建议操作：{a.suggested_procedure}")
            if a.linked_notice_ids:
                w(f"- 关联通知：{', '.join(a.linked_notice_ids)}")
            if a.closures:
                for c in a.closures:
                    w(f"- 已完成：箱 {c.box} 由 {c.record_id}/{c.test_id} 覆盖（{c.note}）")
            w("")
    else:
        w("所有已建批次的箱位均已有认可结果或检出处置，无开放缺样任务。")
        w("")

    if closed_sampling:
        w("已闭环的补检任务（保留过程，不计入当前缺样）：")
        w("")
        for a in closed_sampling:
            w(f"- {a.id} {a.title}：由 {a.closed_by_record_id} 补齐并关闭；"
              f"历史覆盖 {', '.join(f'箱{c.box}←{c.record_id}' for c in a.closures)}")
        w("")

    # 6. 线索与核查 ----------------------------------------------------------
    w("## 六、模糊表述与线索核查（不核实不并批）")
    w("")
    for lead in state.leads:
        w(f"### {lead.id}（来源 {lead.source_record_id}，状态：{_lead_label(lead.status)}）")
        w("")
        w(f"> 原话：{lead.quote}")
        w("")
        for h in lead.hypotheses:
            w(f"- {h}")
        w(f"- 提及船只：{', '.join(lead.boat_ids) if lead.boat_ids else '未能识别'}")
        w(f"- 候选批次：{', '.join(lead.candidate_batch_ids) if lead.candidate_batch_ids else '（后续记录未匹配）'}")
        if lead.confirmed_batch_ids:
            w(f"- 核查确认：**{', '.join(lead.confirmed_batch_ids)}**（{lead.verification_record_id}）")
        if lead.excluded_batch_ids:
            w(f"- 核查排除：{', '.join(lead.excluded_batch_ids)}（{lead.verification_record_id}）")
        w("- 纪律：即便确认关联，相关网次仍各自保留独立批次编号，只建立风险关联，不做物理并批。")
        w("")

    # 7. 待澄清问题 ----------------------------------------------------------
    open_clarify = [a for a in state.actions if a.kind == "clarify_record" and a.status == TaskStatus.OPEN]
    visible_issues = [i for i in state.issues if i.severity != "note"]
    if visible_issues or open_clarify:
        w("## 七、解析异常与待澄清事项")
        w("")
        for i in visible_issues:
            w(f"- `{i.id}` [{i.code}] 记录 {i.record_id}：{i.message}")
        for a in open_clarify:
            w(f"- **{a.id} {a.title}**：{a.reason}；建议：{a.suggested_procedure}")
            if a.verify_with:
                w(f"  - 核实对象：{_contact_block(a.verify_with)}")
        w("")

    # 8. 实体核对 ------------------------------------------------------------
    w("## 八、实体识别核对（近名水域 / 重名船）")
    w("")
    w("### 水域表")
    w("")
    w("| 水域ID | 规范名 | 显式别名 | 备注 |")
    w("|---|---|---|---|")
    for a in state.waters:
        w(f"| {a.id} | {a.name} | {', '.join(a.aliases) if a.aliases else '—'} | {a.note or ''} |")
    w("")
    w("批次实际落位：")
    w("")
    for b in state.batches:
        status = "已匹配水域表" if b.area_id else "**未解析，未归属**"
        w(f"- {b.id}：原文称谓“{b.area_mention}”→ {b.area_id or '无'}（{status}）")
    w("")
    w("### 船只登记")
    w("")
    w("| 船ID | 船名 | 登记号 | 负责人/别称 | 关联记录 | 备注 |")
    w("|---|---|---|---|---|---|")
    for bt in state.boats:
        w(f"| {bt.id} | {bt.name} | {bt.registry_no or '—'} "
          f"| {bt.operator or '—'}（{'/'.join(bt.aka) if bt.aka else '无别称'}） "
          f"| {', '.join(bt.appears_in) if bt.appears_in else '—'} | {bt.note or ''} |")
    w("")
    w("批次用船识别依据：")
    w("")
    for b in state.batches:
        w(f"- {b.id}（{b.primary_record_id}）→ {b.boat_id or '未确定'}，方式 `{b.boat_resolution}`")
    w("")

    # 9. 三角色行动清单 ------------------------------------------------------
    w("## 九、分角色行动清单")
    w("")
    w("### 采样人员：样本对应哪里、哪次捕捞，补检回到谁")
    w("")
    for a in state.actions:
        if a.kind == "sampling" and a.status == TaskStatus.OPEN:
            tag = "高" if a.priority == "high" else "常规"
            w(f"- [{tag}] {a.batch_id} 箱 {compress_boxes(a.boxes_open)}："
              f"核对船号/水域/日期/网次/箱号后，回到 {_contact_block(a.return_to)}")
        elif a.kind == "lead_verification" and a.status == TaskStatus.OPEN:
            w(f"- [高] {a.title}；核实对象：{_contact_block(a.verify_with)}")
        elif a.kind == "clarify_record" and a.status == TaskStatus.OPEN:
            who = _contact_block(a.verify_with) if a.verify_with else "待值班长指定"
            w(f"- [中] {a.title}；核实对象：{who}")
    w("")
    w("### 检验人员：每个“未检出/检出”的适用范围")
    w("")
    for t in state.tests:
        verdict = "未检出" if t.verdict.value == "not_detected" else "检出"
        scope_note = "符合认可部位+方法，可支持对应箱解除" if t.qualifies_for_release else "不得外推到整箱/整批/整船"
        w(f"- {t.record_id}（{t.lab_no or '无编号'}，{verdict}，{t.tissue or '部位未载明'}，"
          f"方法 {t.method or '未载明'}）→ {t.batch_id or '批次未归属'} 箱 {t.box}：{scope_note}")
    w("")
    w("### 市场处置人员：局部暂缓 / 扩大核查 / 解除流通")
    w("")
    for b in state.batches:
        v = vmap[b.id]
        loc = f"，位置 {b.market_place}" if b.market_place else ""
        w(f"- **{b.id}**{loc}：{v.disposition_label}。")
        w(f"  - 仍受限箱：{compress_boxes(v.restricted_boxes) if v.restricted_boxes else '无'}；"
          f"可流通箱：{compress_boxes(v.released_boxes) if v.released_boxes else '无'}")
        active = [bid for bv in v.box_views for bid in bv.active_notice_ids]
        if active:
            w(f"  - 有效通知：{', '.join(dict.fromkeys(active))}（按通知范围执行，不得扩大到重名船批次）")
    w("")
    w("---")
    w("")
    w("## 十、运行与复核方式")
    w("")
    w("```sh")
    w("# 重新生成 out/state.json、out/report.md、out/ledger.jsonl")
    w("python3 -m fishery_station build --case data/case.json --out out")
    w("")
    w("# 运行全部稳定性与业务不变量测试")
    w("python3 -m unittest discover -s tests -v")
    w("```")
    w("")
    return "\n".join(L)


def _derived_label(state, notice) -> str:
    # 通过 engine 之外的纯折叠函数计算，避免报告层依赖 engine 实例
    if notice.action is NoticeAction.LIFT:
        return NOTICE_STATE_LABELS["effective"]
    lifted_boxes: set[int] = set()
    for l in state.notices:
        if l.action is NoticeAction.LIFT and notice.id in l.lifts_notice_ids:
            lifted_boxes.update(l.boxes)
    target = set(notice.boxes)
    if target <= lifted_boxes:
        return NOTICE_STATE_LABELS["lifted"]
    if lifted_boxes & target:
        return NOTICE_STATE_LABELS["partially_lifted"]
    return NOTICE_STATE_LABELS["in_force"]


def _lead_label(status: LeadStatus) -> str:
    from .models import LEAD_STATUS_LABELS
    return LEAD_STATUS_LABELS[status]
