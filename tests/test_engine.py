import copy
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fishery_station.caseio import load_case  # noqa: E402
from fishery_station.checks import invariant_violations  # noqa: E402
from fishery_station.engine import StationEngine, build_case  # noqa: E402
from fishery_station.models import (  # noqa: E402
    BatchDisposition,
    BoxStatus,
    NoticeAction,
    NoticeState,
    TaskStatus,
)

CASE = load_case(ROOT / "data" / "case.json")


class EndToEndCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = build_case(CASE)

    def test_material_counts(self):
        s = self.state
        self.assertEqual(len(s.raw_records), 13)
        self.assertEqual(len(s.batches), 4)
        self.assertEqual(len(s.tests), 6)
        self.assertEqual(len(s.notices), 6)
        self.assertEqual(len(s.leads), 1)

    def test_duplicate_boat_names_become_distinct_batches_and_boats(self):
        s = self.state
        b1 = next(b for b in s.batches if b.primary_record_id == "FC-1")
        b2 = next(b for b in s.batches if b.primary_record_id == "FC-3")
        self.assertEqual(b1.boat_id, "B-1")
        self.assertEqual(b2.boat_id, "B-2")
        self.assertNotEqual(b1.boat_id, b2.boat_id)
        self.assertEqual(b1.area_id, "A-1")
        self.assertEqual(b2.area_id, "A-2")
        self.assertNotEqual(b1.id, b2.id)

    def test_similar_water_name_not_merged(self):
        s = self.state
        b4 = next(b for b in s.batches if b.primary_record_id == "FC-10")
        self.assertEqual(b4.area_id, "A-3")
        self.assertNotEqual(b4.area_id, "A-1")
        # FC-11 的泛称“石湾一带”必须保持未解析并登记
        self.assertTrue(any(i.code == "AREA_UNRESOLVED" and i.record_id == "FC-11"
                            for i in s.issues))

    def test_lot001_final_local_suspension_boxes_7_and_9(self):
        s = self.state
        v = next(v for v in s.batch_views if v.batch_id == "LOT-001")
        self.assertEqual(v.disposition, BatchDisposition.LOCAL_SUSPENSION)
        self.assertEqual(list(v.restricted_boxes), [7, 9])
        self.assertEqual(list(v.unsampled_boxes), [9])
        statuses = {bv.box: bv.status for bv in v.box_views}
        self.assertEqual(statuses[7], BoxStatus.SUSPENDED_CONFIRMED)
        self.assertEqual(statuses[9], BoxStatus.HOLD_PENDING_SAMPLE)
        for box in (4, 5, 6, 8):
            self.assertEqual(statuses[box], BoxStatus.RELEASED_LIFTED)

    def test_fc2_weijianchu_scope_is_limited_to_gill_box4(self):
        s = self.state
        t = next(t for t in s.tests if t.record_id == "FC-2")
        self.assertEqual(t.batch_id, "LOT-001")
        self.assertEqual(t.box, 4)
        self.assertEqual(t.tissue, "鳃组织")
        self.assertIsNone(t.method)
        self.assertFalse(t.qualifies_for_release)
        self.assertTrue(t.qualification_reasons)
        # 适用范围陈述必须明确禁止外推
        self.assertIn("不自动延伸", t.scope_statement)

        # 箱 4 在 FC-12 补检前不能被 FC-2 放行：最终由 FC-12 合格结果解除
        t12 = next(t for t in s.tests if t.record_id == "FC-12")
        self.assertTrue(t12.qualifies_for_release)

    def test_other_batches_dispositions(self):
        s = self.state
        d = {v.batch_id: v.disposition for v in s.batch_views}
        self.assertEqual(d["LOT-002"], BatchDisposition.CIRCULATE_SAMPLING)
        self.assertEqual(d["LOT-003"], BatchDisposition.EXPANDED_VERIFICATION)
        self.assertEqual(d["LOT-004"], BatchDisposition.CIRCULATE_SAMPLING)

    def test_notice_chain_and_derived_states(self):
        s = self.state
        n1 = next(n for n in s.notices if n.id == "NOTICE-001")
        self.assertEqual(n1.action, NoticeAction.SUSPEND)
        self.assertEqual(list(n1.boxes), [4, 5, 6, 7, 8, 9])
        self.assertEqual(list(n1.test_record_ids), ["FC-4"])
        self.assertEqual(_derived(s, n1), NoticeState.PARTIALLY_LIFTED)

        lifts = {n.id: n for n in s.notices if n.action == NoticeAction.LIFT}
        self.assertEqual(set(lifts), {"NOTICE-002", "NOTICE-003", "NOTICE-005", "NOTICE-006"})
        for n in lifts.values():
            self.assertEqual(list(n.lifts_notice_ids), ["NOTICE-001"])
            self.assertEqual(_derived(s, n), NoticeState.EFFECTIVE)

        n4 = next(n for n in s.notices if n.id == "NOTICE-004")
        self.assertEqual(n4.action, NoticeAction.EXPAND)
        self.assertEqual(list(n4.evidence_record_ids), ["FC-7", "FC-8"])
        self.assertEqual(_derived(s, n4), NoticeState.IN_FORCE)

    def test_lead_verified_but_batches_never_merged(self):
        s = self.state
        lead = s.leads[0]
        self.assertEqual(lead.status.value, "verified")
        self.assertEqual(list(lead.confirmed_batch_ids), ["LOT-003"])
        self.assertEqual(list(lead.excluded_batch_ids), ["LOT-004"])
        # 核查确认后仍然是 4 个独立批次
        self.assertEqual(len(s.batches), 4)
        lot3 = next(b for b in s.batches if b.id == "LOT-003")
        self.assertEqual(lot3.primary_record_id, "FC-8")

    def test_missing_samples_and_return_to_owners(self):
        s = self.state
        open_sampling = {a.batch_id: a for a in s.actions
                         if a.kind == "sampling" and a.status == TaskStatus.OPEN}
        self.assertEqual(set(open_sampling), {"LOT-001", "LOT-002", "LOT-003", "LOT-004"})
        self.assertEqual(list(open_sampling["LOT-001"].boxes_open), [9])
        self.assertEqual(list(open_sampling["LOT-002"].boxes_open), [1, 2, 3])

        self.assertEqual(open_sampling["LOT-001"].return_to["boat_id"], "B-1")
        self.assertIn("138-0000-0107", open_sampling["LOT-001"].return_to["contact"])
        self.assertEqual(open_sampling["LOT-002"].return_to["boat_id"], "B-2")
        self.assertIn("138-0000-0226", open_sampling["LOT-002"].return_to["contact"])
        self.assertEqual(open_sampling["LOT-003"].return_to["boat_id"], "B-1")
        self.assertEqual(open_sampling["LOT-004"].return_to["boat_id"], "B-3")

        closed = [a for a in s.actions if a.kind == "sampling" and a.status == TaskStatus.CLOSED]
        self.assertEqual(closed, [])  # LOT-001 仍有箱 9，故整个采样任务未关闭

    def test_lead_verification_task_closed_by_fc13(self):
        s = self.state
        tasks = [a for a in s.actions if a.kind == "lead_verification"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status, TaskStatus.CLOSED)
        self.assertEqual(tasks[0].closed_by_record_id, "FC-13")

    def test_evidence_matrix_links_tests_to_decisions(self):
        s = self.state
        rows = s.evidence_matrix
        n1_row = next(r for r in rows if r.get("notice_id") == "NOTICE-001")
        self.assertEqual(n1_row["supporting_test_record_ids"], ["FC-4"])
        self.assertEqual(n1_row["derived_state_code"], "partially_lifted")

        batch_row = next(r for r in rows
                         if r["row_type"] == "batch_current_decision" and r["batch_id"] == "LOT-001")
        self.assertEqual(batch_row["decision_code"], "local_suspension")
        self.assertEqual(batch_row["boxes_still_missing_qualifying_sample"], [9])
        self.assertIn("FC-4", batch_row["supporting_test_record_ids"])

    def test_invariants_hold(self):
        self.assertEqual(invariant_violations(self.state), [])


def _derived(state, notice):
    # 报告/测试共用的纯折叠口径
    from fishery_station.present import _derived_label  # noqa: F401
    lifted = set()
    for l in state.notices:
        if l.action is NoticeAction.LIFT and notice.id in l.lifts_notice_ids:
            lifted.update(l.boxes)
    target = set(notice.boxes)
    if notice.action is NoticeAction.LIFT:
        return NoticeState.EFFECTIVE
    if target <= lifted:
        return NoticeState.LIFTED
    if lifted & target:
        return NoticeState.PARTIALLY_LIFTED
    return NoticeState.IN_FORCE


class AppendOnlyLedgerTest(unittest.TestCase):
    def test_supplementary_results_never_erase_original_notice(self):
        eng = StationEngine(CASE)
        records = CASE["records"]
        for r in records[:4]:       # 截至 FC-4：发出 NOTICE-001
            eng.ingest(r)
        before = eng.snapshot()
        self.assertEqual([n.id for n in before.notices], ["NOTICE-001"])
        original = before.notices[0]
        events_prefix = list(before.events)

        # 冻结对象不可改写
        with self.assertRaises(dataclasses.FrozenInstanceError):
            original.title = "试图篡改已发出的通知"  # type: ignore[misc]

        for r in records[4:]:
            eng.ingest(r)
        after = eng.snapshot()

        # 原通知仍在、内容逐字段未变
        self.assertIn(original, after.notices)
        self.assertEqual(next(n for n in after.notices if n.id == "NOTICE-001"), original)
        # 事件台账前缀逐事件未变，只允许追加
        self.assertEqual(list(after.events[:len(events_prefix)]), events_prefix)
        self.assertGreater(len(after.events), len(events_prefix))
        # 当前风险范围确实被追补结果改变
        v = next(v for v in after.batch_views if v.batch_id == "LOT-001")
        self.assertEqual(list(v.restricted_boxes), [7, 9])
        self.assertEqual(_derived(after, original), NoticeState.PARTIALLY_LIFTED)


class OriginalSeedOnlyTest(unittest.TestCase):
    """只用初始三条记录时，关键纪律同样成立。"""

    def test_seed_three_records(self):
        mini = {
            "case_id": "seed",
            "reference_now": "2026-11-26T12:00:00+08:00",
            "records": [
                {"id": "FC-1", "text": "石湾外口昨晚第三网，鲳鱼箱 4-9；船名海星。", "kind": "catch-note"},
                {"id": "FC-2", "text": "石湾口样本未检出目标残留，样品为 FC-1 箱 4 的鳃组织。", "kind": "lab-result"},
                {"id": "FC-3", "text": "另一船海星：今晨南礁第二网，货箱 1-3。", "kind": "market-update"},
            ],
            "waters": [
                {"id": "A-1", "name": "石湾外口", "aliases": ["石湾口"]},
                {"id": "A-2", "name": "南礁"},
            ],
            "policy": {},
        }
        s = build_case(mini)
        self.assertEqual(len(s.batches), 2)
        lot1, lot2 = s.batches
        self.assertEqual(lot1.area_id, "A-1")
        self.assertEqual(lot2.area_id, "A-2")
        # 没有船登记表时不得瞎并，必须留 BOAT_UNRESOLVED
        self.assertTrue(any(i.code == "BOAT_UNRESOLVED" for i in s.issues))
        # FC-2 仅覆盖箱 4 鳃组织，不支持放行
        t = s.tests[0]
        self.assertFalse(t.qualifies_for_release)
        v1 = next(v for v in s.batch_views if v.batch_id == "LOT-001")
        self.assertEqual(set(v1.unsampled_boxes), {4, 5, 6, 7, 8, 9})
        # 没有阳性结果就没有暂缓通知，也不能把 FC-3 的箱并入 FC-1
        self.assertEqual(s.notices, ())
        v2 = next(v for v in s.batch_views if v.batch_id == "LOT-002")
        self.assertEqual(set(x.box for x in v2.box_views), {1, 2, 3})
        self.assertEqual(invariant_violations(s), [])


class SupplementaryResultNarrowsExpandedHoldTest(unittest.TestCase):
    def test_qualified_negative_lifts_one_box_of_expand_notice(self):
        case = copy.deepcopy(CASE)
        case["records"].append({
            "id": "FC-14",
            "kind": "lab-result",
            "text": "化验单 SYN-LAB-130：FC-8 箱 1 肌肉组织按 SYN-LCMS-01 方法检测，目标残留未检出。",
        })
        s = build_case(case)
        n4 = next(n for n in s.notices if n.id == "NOTICE-004")
        lifts = [n for n in s.notices if n.action == NoticeAction.LIFT and "NOTICE-004" in n.lifts_notice_ids]
        self.assertEqual(len(lifts), 1)
        self.assertEqual(list(lifts[0].boxes), [1])
        self.assertEqual(_derived(s, n4), NoticeState.PARTIALLY_LIFTED)

        v = next(v for v in s.batch_views if v.batch_id == "LOT-003")
        self.assertEqual(list(v.restricted_boxes), [2, 3])
        self.assertEqual(v.box_views[0].status, BoxStatus.RELEASED_LIFTED)
        # 原扩大核查通知仍在台账
        self.assertIn(n4, s.notices)
        self.assertEqual(invariant_violations(s), [])


class DeterminismTest(unittest.TestCase):
    def test_repeated_builds_are_equal(self):
        from fishery_station.present import state_to_jsonable
        import json
        a = json.dumps(state_to_jsonable(build_case(CASE)), ensure_ascii=False, sort_keys=True, default=str)
        b = json.dumps(state_to_jsonable(build_case(CASE)), ensure_ascii=False, sort_keys=True, default=str)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
