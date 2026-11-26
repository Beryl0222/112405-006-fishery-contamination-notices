"""领域对象与状态码。

所有“事实类”对象均为冻结数据类：一经生成不得改写。通知只能通过
台账追加“解除通知”，不能修改或删除原发暂缓通知。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


# ---------------------------------------------------------------------------
# 状态码与中文标签
# ---------------------------------------------------------------------------

class Verdict(StrEnum):
    DETECTED = "detected"
    NOT_DETECTED = "not_detected"


VERDICT_LABELS = {
    Verdict.DETECTED: "检出",
    Verdict.NOT_DETECTED: "未检出",
}


class AreaResolution(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class BoatResolution(StrEnum):
    PINNED = "pinned_by_case_link"          # 资料背景编号直接关联
    UNIQUE_NAME = "unique_name"
    ANOTHER_BOAT = "another_same_name_boat"  # “另一船 + 相对当前船”
    OPERATOR_ALIAS = "operator_alias"
    AMBIGUOUS = "ambiguous_same_name"
    UNKNOWN = "unknown"


class BoxStatus(StrEnum):
    RELEASED_TESTED = "released_tested"
    RELEASED_LIFTED = "released_lifted"
    SUSPENDED_CONFIRMED = "suspended_confirmed"
    HOLD_PENDING_SAMPLE = "hold_pending_sample"
    VERIFICATION_HOLD = "verification_hold"
    CIRCULATING = "circulating"


BOX_STATUS_LABELS = {
    BoxStatus.RELEASED_TESTED: "可流通·合格阴性结果支持",
    BoxStatus.RELEASED_LIFTED: "解除限制·已有解除通知",
    BoxStatus.SUSPENDED_CONFIRMED: "暂缓销售·检出结果支持",
    BoxStatus.HOLD_PENDING_SAMPLE: "暂缓/暂控·同批扩查且缺合格样",
    BoxStatus.VERIFICATION_HOLD: "扩大核查·线索待核实的暂控",
    BoxStatus.CIRCULATING: "可继续流通·无受限记录",
}


class BatchDisposition(StrEnum):
    LOCAL_SUSPENSION = "local_suspension"
    BATCH_SUSPENSION = "batch_suspension"
    EXPANDED_VERIFICATION = "expanded_verification"
    RELEASED = "released"
    CIRCULATE_SAMPLING = "circulate_with_routine_sampling"


BATCH_DISPOSITION_LABELS = {
    BatchDisposition.LOCAL_SUSPENSION: "局部暂缓（部分箱仍受限，其余可流通/已解除）",
    BatchDisposition.BATCH_SUSPENSION: "整批暂缓/暂控",
    BatchDisposition.EXPANDED_VERIFICATION: "扩大核查（关联线索，市场暂控待采样核实）",
    BatchDisposition.RELEASED: "可继续流通（受限范围已解除）",
    BatchDisposition.CIRCULATE_SAMPLING: "可继续流通（无风险证据，列入常规补检）",
}


class NoticeAction(StrEnum):
    SUSPEND = "suspend"
    EXPAND = "expand"
    LIFT = "lift"


NOTICE_ACTION_LABELS = {
    NoticeAction.SUSPEND: "暂缓销售通知",
    NoticeAction.EXPAND: "扩大核查/暂控通知",
    NoticeAction.LIFT: "解除（部分）限制通知",
}


class NoticeState(StrEnum):
    IN_FORCE = "in_force"
    PARTIALLY_LIFTED = "partially_lifted"
    LIFTED = "lifted"
    EFFECTIVE = "effective"  # 解除通知本身


NOTICE_STATE_LABELS = {
    NoticeState.IN_FORCE: "仍在执行",
    NoticeState.PARTIALLY_LIFTED: "部分箱已解除，余量仍执行",
    NoticeState.LIFTED: "所涉范围已全部解除（通知留存，不得删除）",
    NoticeState.EFFECTIVE: "解除通知已发出并生效（永久归档）",
}


class LeadStatus(StrEnum):
    OPEN = "open"
    VERIFIED = "verified"


LEAD_STATUS_LABELS = {
    LeadStatus.OPEN: "待采样核实（禁止据此并批）",
    LeadStatus.VERIFIED: "已经采样核查确认/排除（批次仍独立）",
}


class TaskStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# 背景实体
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WaterArea:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class Boat:
    id: str
    name: str
    registry_no: str | None = None
    operator: str | None = None
    aka: tuple[str, ...] = ()
    role: str | None = None
    contact: str | None = None
    berth: str | None = None
    appears_in: tuple[str, ...] = ()
    note: str | None = None


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HaulMention:
    seq: int | None
    time_phrase: str | None
    event_date: str | None
    part_of_day: str | None
    raw: str


@dataclass(frozen=True)
class ParsedRecord:
    record_id: str
    kind: str
    text: str
    refs: tuple[str, ...] = ()
    lab_no: str | None = None
    cued_boat_name: str | None = None
    another_boat: bool = False
    boat_name_mentions: tuple[str, ...] = ()
    operator_aliases: tuple[str, ...] = ()
    area_mentions: tuple[str, ...] = ()
    unresolved_area_mentions: tuple[str, ...] = ()
    haul: HaulMention | None = None
    extra_hauls: tuple[HaulMention, ...] = ()
    species: tuple[str, ...] = ()
    boxes: tuple[int, ...] = ()
    verdict: Verdict | None = None
    analyte: str | None = None
    tissue: str | None = None
    method: str | None = None
    market_place: str | None = None
    distribution: str | None = None
    claim_quote: str | None = None
    verification_lead_ref: str | None = None
    confirmed_ref: str | None = None
    excluded_ref: str | None = None
    parse_notes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 核心事实对象（全部不可变）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Batch:
    id: str
    ordinal: int
    primary_record_id: str
    source_record_ids: tuple[str, ...]
    boat_id: str | None
    boat_resolution: str
    area_id: str | None
    area_mention: str | None
    area_resolution: str
    event_date: str | None
    part_of_day: str | None
    haul_seq: int | None
    species: tuple[str, ...]
    boxes: tuple[int, ...]
    market_place: str | None = None
    distribution: str | None = None


@dataclass(frozen=True)
class TestResult:
    id: str
    ordinal: int
    record_id: str
    lab_no: str | None
    batch_id: str | None
    box: int | None
    tissue: str | None
    analyte: str | None
    method: str | None
    verdict: Verdict
    scope_statement: str
    qualifies_for_release: bool
    qualification_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Notice:
    id: str
    ordinal: int
    action: NoticeAction
    batch_id: str
    boxes: tuple[int, ...]
    title: str
    reason: str
    issued_after_record_id: str
    evidence_record_ids: tuple[str, ...]
    test_record_ids: tuple[str, ...] = ()
    lifts_notice_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerEvent:
    seq: int
    kind: str  # record_received / notice_issued / verification_recorded
    at_record_id: str
    message: str
    notice_id: str | None = None


@dataclass(frozen=True)
class Lead:
    id: str
    ordinal: int
    source_record_id: str
    quote: str
    hypotheses: tuple[str, ...]
    boat_ids: tuple[str, ...] = ()
    candidate_batch_ids: tuple[str, ...] = ()
    status: LeadStatus = LeadStatus.OPEN
    verification_record_id: str | None = None
    confirmed_batch_ids: tuple[str, ...] = ()
    excluded_batch_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Issue:
    id: str
    ordinal: int
    record_id: str
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class BoxClosure:
    box: int
    record_id: str
    test_id: str
    note: str


@dataclass(frozen=True)
class ActionItem:
    id: str
    ordinal: int
    kind: str  # sampling / lead_verification / clarify_record
    priority: str
    title: str
    reason: str
    suggested_procedure: str
    status: TaskStatus
    batch_id: str | None = None
    boxes_open: tuple[int, ...] = ()
    closures: tuple[BoxClosure, ...] = ()
    return_to: dict | None = None
    verify_with: dict | None = None
    linked_notice_ids: tuple[str, ...] = ()
    closed_by_record_id: str | None = None


@dataclass(frozen=True)
class BoxView:
    batch_id: str
    box: int
    status: BoxStatus
    status_label: str
    test_record_ids: tuple[str, ...]
    active_notice_ids: tuple[str, ...]
    lift_notice_ids: tuple[str, ...]
    missing_qualifying_sample: bool


@dataclass(frozen=True)
class BatchView:
    batch_id: str
    disposition: BatchDisposition
    disposition_label: str
    box_views: tuple[BoxView, ...]
    restricted_boxes: tuple[int, ...]
    released_boxes: tuple[int, ...]
    unsampled_boxes: tuple[int, ...]
    supporting_test_record_ids: tuple[str, ...]
    notice_ids: tuple[str, ...]


@dataclass(frozen=True)
class CaseState:
    case_id: str
    reference_now: str
    disclaimer: str
    policy: dict
    waters: tuple[WaterArea, ...]
    boats: tuple[Boat, ...]
    raw_records: tuple[dict, ...]
    parsed: tuple[ParsedRecord, ...]
    batches: tuple[Batch, ...]
    tests: tuple[TestResult, ...]
    notices: tuple[Notice, ...]
    events: tuple[LedgerEvent, ...]
    leads: tuple[Lead, ...]
    issues: tuple[Issue, ...]
    actions: tuple[ActionItem, ...]
    box_views: tuple[BoxView, ...]
    batch_views: tuple[BatchView, ...]
    evidence_matrix: tuple[dict, ...] = field(default_factory=tuple)
