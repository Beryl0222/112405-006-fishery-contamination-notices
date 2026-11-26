import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fishery_station.models import Boat, WaterArea  # noqa: E402
from fishery_station.parsing import parse_record  # noqa: E402
from fishery_station.resolution import AreaResolver, BoatResolver  # noqa: E402

REF_NOW = "2026-11-26T12:00:00+08:00"


def areas():
    return AreaResolver([
        WaterArea(id="A-1", name="石湾外口", aliases=("石湾口",)),
        WaterArea(id="A-2", name="南礁"),
        WaterArea(id="A-3", name="石湾礁"),
    ])


def boats():
    return BoatResolver([
        Boat(id="B-1", name="海星", aka=("老陈",)),
        Boat(id="B-2", name="海星", aka=("阿秀",)),
        Boat(id="B-3", name="闽沙渔运06", aka=("周老大",)),
    ])


class AreaResolutionTest(unittest.TestCase):
    def test_canonical_and_explicit_alias(self):
        r = areas()
        self.assertEqual(r.resolve_surface("石湾外口")[0], "A-1")
        self.assertEqual(r.resolve_surface("石湾口")[0], "A-1")
        self.assertEqual(r.resolve_surface("南礁")[0], "A-2")

    def test_similar_name_is_distinct_area(self):
        r = areas()
        self.assertEqual(r.resolve_surface("石湾礁")[0], "A-3")

    def test_bare_shiwan_and_fancheng_are_not_guessed(self):
        r = areas()
        self.assertIsNone(r.resolve_surface("石湾")[0])
        self.assertIsNone(r.resolve_surface("石湾一带")[0])

    def test_primary_area_on_real_records(self):
        r = areas()
        p1 = r.classify(parse_record({"id": "FC-1", "text": "石湾外口昨晚第三网。", "kind": "catch-note"}, REF_NOW))
        aid, surface, status = r.primary_area(p1)
        self.assertEqual(aid, "A-1")
        self.assertEqual(surface, "石湾外口")

        p11 = r.classify(parse_record({"id": "FC-11", "text": "前天还在石湾一带下过一网。", "kind": "voice-transcript"}, REF_NOW))
        self.assertIn("石湾一带", p11.unresolved_area_mentions)
        aid, _, status = r.primary_area(p11)
        self.assertIsNone(aid)
        self.assertEqual(status.value, "unresolved")


class DuplicateBoatTest(unittest.TestCase):
    def test_another_boat_relative_to_context_picks_second_hx(self):
        r = boats()
        p = parse_record({"id": "FC-3", "text": "另一船海星：今晨南礁第二网，货箱 1-3。", "kind": "market-update"}, REF_NOW)
        bid, code, _ = r.resolve_lot_boat(p, context_boat_id="B-1")
        self.assertEqual(bid, "B-2")
        self.assertEqual(code.value, "another_same_name_boat")

    def test_same_name_without_disambiguator_is_ambiguous(self):
        r = boats()
        p = parse_record({"id": "X", "text": "船名海星，有货三箱。", "kind": "catch-note"}, REF_NOW)
        bid, code, _ = r.resolve_lot_boat(p, context_boat_id=None)
        self.assertIsNone(bid)
        self.assertEqual(code.value, "ambiguous_same_name")

    def test_unique_named_boat(self):
        r = boats()
        p = parse_record({"id": "FC-10", "text": "石湾礁昨天下午第一网；船名闽沙渔运06。", "kind": "catch-note"}, REF_NOW)
        bid, code, _ = r.resolve_lot_boat(p)
        self.assertEqual(bid, "B-3")
        self.assertEqual(code.value, "unique_name")

    def test_aka_disambiguates_shared_name_in_mentions(self):
        r = boats()
        ids = r.mention_boats("语音转写（海星船老陈）：昨天那片网也有；也有人说是邻船闽沙渔运06。")
        self.assertIn("B-1", ids)
        self.assertIn("B-3", ids)
        self.assertNotIn("B-2", ids)

    def test_bare_shared_name_without_aka_is_not_guessed_in_mentions(self):
        r = boats()
        ids = r.mention_boats("船名海星。")
        self.assertEqual(ids, [])


if __name__ == "__main__":
    unittest.main()
