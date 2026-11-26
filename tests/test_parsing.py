import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fishery_station.parsing import (  # noqa: E402
    chinese_to_int,
    extract_area_surfaces,
    parse_record,
)

REF_NOW = "2026-11-26T12:00:00+08:00"


class ChineseNumberTest(unittest.TestCase):
    def test_numerals(self):
        self.assertEqual(chinese_to_int("三"), 3)
        self.assertEqual(chinese_to_int("二"), 2)
        self.assertEqual(chinese_to_int("11"), 11)


class BoxAndVerdictParseTest(unittest.TestCase):
    def test_box_range_expands(self):
        p = parse_record({"id": "X", "text": "鲳鱼箱 4-9", "kind": "catch-note"}, REF_NOW)
        self.assertEqual(list(p.boxes), [4, 5, 6, 7, 8, 9])

    def test_huo_xiang_range(self):
        p = parse_record({"id": "X", "text": "另一船海星：货箱 1-3。", "kind": "market-update"}, REF_NOW)
        self.assertEqual(list(p.boxes), [1, 2, 3])
        self.assertTrue(p.another_boat)
        self.assertEqual(p.cued_boat_name, "海星")

    def test_weijianchu_not_misread_as_jianchu(self):
        p = parse_record({"id": "X", "text": "样本未检出目标残留。", "kind": "lab-result"}, REF_NOW)
        self.assertEqual(p.verdict.value, "not_detected")

    def test_jianchu_detected(self):
        p = parse_record({"id": "X", "text": "目标残留结果为检出。", "kind": "lab-result"}, REF_NOW)
        self.assertEqual(p.verdict.value, "detected")


class TissueScopeParseTest(unittest.TestCase):
    def test_tissue_window_uses_target_box_not_later_clause(self):
        text = ("化验单 SYN-LAB-126：应补检要求，对 FC-1 箱 4 肌肉组织按 SYN-LCMS-01 "
                "方法采样检测，目标残留未检出；FC-2 的鳃组织结果不覆盖该肌肉部位。")
        p = parse_record({"id": "FC-12", "text": text, "kind": "lab-result"}, REF_NOW)
        self.assertEqual(p.boxes[0], 4)
        self.assertEqual(p.tissue, "肌肉组织")
        self.assertEqual(p.method, "SYN-LCMS-01")
        self.assertEqual(p.lab_no, "SYN-LAB-126")

    def test_gill_tissue_and_missing_method(self):
        p = parse_record({
            "id": "FC-2",
            "text": "石湾口样本未检出目标残留，样品为 FC-1 箱 4 的鳃组织。",
            "kind": "lab-result",
        }, REF_NOW)
        self.assertEqual(p.tissue, "鳃组织")
        self.assertIsNone(p.method)
        self.assertIn("FC-1", p.refs)


class HaulTimeParseTest(unittest.TestCase):
    def test_last_night_resolves_to_previous_day_night(self):
        p = parse_record({"id": "FC-1", "text": "石湾外口昨晚第三网。", "kind": "catch-note"}, REF_NOW)
        self.assertEqual(p.haul.seq, 3)
        self.assertEqual(p.haul.event_date, "2026-11-25")
        self.assertEqual(p.haul.part_of_day, "夜间")

    def test_nearest_time_word_wins_over_earlier_mention(self):
        text = "昨天那片网也有！昨晚第三网和昨天傍晚第二网。"
        p = parse_record({"id": "FC-7", "text": text, "kind": "voice-transcript"}, REF_NOW)
        self.assertEqual(p.haul.seq, 3)
        self.assertEqual(p.haul.event_date, "2026-11-25")
        self.assertEqual(p.haul.part_of_day, "夜间")
        self.assertEqual(len(p.extra_hauls), 1)
        self.assertEqual(p.extra_hauls[0].seq, 2)
        self.assertEqual(p.extra_hauls[0].part_of_day, "傍晚")

    def test_this_morning(self):
        p = parse_record({"id": "FC-3", "text": "今晨南礁第二网。", "kind": "market-update"}, REF_NOW)
        self.assertEqual(p.haul.event_date, "2026-11-26")
        self.assertEqual(p.haul.part_of_day, "早晨")


class AreaSurfaceParseTest(unittest.TestCase):
    def test_known_and_similar_surfaces(self):
        self.assertIn("石湾外口", extract_area_surfaces("石湾外口昨晚第三网"))
        self.assertIn("石湾口", extract_area_surfaces("石湾口样本"))
        self.assertIn("石湾礁", extract_area_surfaces("石湾礁昨天下午第一网"))

    def test_generic_fan_cheng(self):
        surfaces = extract_area_surfaces("前天还在石湾一带下过一网")
        self.assertIn("石湾一带", surfaces)

    def test_overlap_keeps_longest(self):
        # “在石湾外口”的错误短切分“在石湾”不应产生独立“石湾”噪声
        surfaces = extract_area_surfaces("昨天傍晚在石湾外口的第二网")
        self.assertIn("石湾外口", surfaces)
        self.assertNotIn("石湾", surfaces)


class VerificationParseTest(unittest.TestCase):
    def test_verification_refs(self):
        text = ("采样核查 SV-1：经核对，FC-7 所说“昨天那片网”已确认是 FC-8 所报作业；"
                "与 FC-10 闽沙渔运06 的作业无关。")
        p = parse_record({"id": "FC-13", "text": text, "kind": "verification"}, REF_NOW)
        self.assertEqual(p.verification_lead_ref, "FC-7")
        self.assertEqual(p.confirmed_ref, "FC-8")
        self.assertEqual(p.excluded_ref, "FC-10")


if __name__ == "__main__":
    unittest.main()
