"""可复核不变量自检。

这些检查把任务说明中的纪律固化为可执行断言，CLI `check` 与单元测试共用。
"""

from __future__ import annotations

from .models import NoticeAction


def invariant_violations(state) -> list[str]:
    problems: list[str] = []

    record_ids = {r["id"] for r in state.raw_records}
    batch_ids = {b.id for b in state.batches}
    notice_ids = {n.id for n in state.notices}

    # 1. 通知引用的记录必须存在
    for n in state.notices:
        for rid in (*n.evidence_record_ids, *n.test_record_ids, n.issued_after_record_id):
            if rid not in record_ids:
                problems.append(f"{n.id} 引用了不存在的记录 {rid}")
        if n.batch_id not in batch_ids:
            problems.append(f"{n.id} 引用了不存在的批次 {n.batch_id}")

    # 2. 解除通知只能引用已存在、同批次、更早发出的限制通知
    ordinal_by_id = {n.id: n.ordinal for n in state.notices}
    for n in state.notices:
        if n.action is not NoticeAction.LIFT:
            continue
        if not n.lifts_notice_ids:
            problems.append(f"{n.id} 是解除通知但没有 lifts_notice_ids")
        for target in n.lifts_notice_ids:
            if target not in notice_ids:
                problems.append(f"{n.id} 解除了不存在的通知 {target}")
                continue
            t = next(x for x in state.notices if x.id == target)
            if t.batch_id != n.batch_id:
                problems.append(f"{n.id} 跨批次解除 {target}")
            if ordinal_by_id[target] >= n.ordinal:
                problems.append(f"{n.id} 不能解除尚未发出的 {target}")
            if not set(n.boxes) <= set(t.boxes):
                problems.append(f"{n.id} 的解除箱 {list(n.boxes)} 超出 {target} 范围 {list(t.boxes)}")

    # 3. 检测结果要么能回到批次，要么必须登记高严重度问题，不允许静默悬空
    issue_messages = " ".join(i.code for i in state.issues)
    for t in state.tests:
        if t.batch_id is None and "LAB_BATCH_UNRESOLVED" not in issue_messages:
            problems.append(f"{t.record_id} 未归属批次但缺少 LAB_BATCH_UNRESOLVED 问题登记")

    # 4. 箱视图与通知折叠必须一致
    for v in state.box_views:
        for nid in v.active_notice_ids:
            n = next((x for x in state.notices if x.id == nid), None)
            if n is None:
                problems.append(f"箱 {v.batch_id}#{v.box} 的活动通知 {nid} 不存在")
            elif v.box not in n.boxes:
                problems.append(f"通知 {nid} 并不覆盖箱 {v.box}")

    # 5. 关闭的任务必须有关闭依据
    for a in state.actions:
        if a.status.value == "closed" and not a.closed_by_record_id:
            problems.append(f"{a.id} 已关闭但缺少 closed_by_record_id")

    # 6. 同一水域称谓若未解析，必须有 AREA_UNRESOLVED 问题
    for b in state.batches:
        if b.area_id is None and b.area_mention:
            if not any(i.code == "AREA_UNRESOLVED" and i.record_id == b.primary_record_id
                       for i in state.issues):
                problems.append(f"{b.id} 水域未解析但缺少 AREA_UNRESOLVED 登记")

    # 7. 船名重复时，不同来源记录若被背景登记指向不同稳定船 ID，
    #    引擎必须原样保留该区分（这里核对批次没有把解析结果置空之外的异常）。
    boat_ids = {bt.id for bt in state.boats}
    for b in state.batches:
        if b.boat_id is not None and b.boat_id not in boat_ids:
            problems.append(f"{b.id} 引用了不存在的船只 {b.boat_id}")

    return problems
