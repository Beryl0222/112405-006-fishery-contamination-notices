import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fishery_station.cli import main  # noqa: E402

CASE = ROOT / "data" / "case.json"


class CliTest(unittest.TestCase):
    def test_build_writes_reviewable_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            rc = main(["build", "--case", str(CASE), "--out", str(out)])
            self.assertEqual(rc, 0)
            for name in ("state.json", "report.md", "ledger.jsonl"):
                self.assertTrue((out / name).exists(), name)

            state = json.loads((out / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["case_id"], "fishery-1")
            self.assertIn("notices", state)

            report = (out / "report.md").read_text(encoding="utf-8")
            for keyword in ("局部暂缓", "扩大核查", "解除限制", "补检应回到",
                            "NOTICE-001", "石湾口", "另一船", "未检出"):
                self.assertIn(keyword, report)

            lines = [ln for ln in (out / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if ln]
            self.assertGreaterEqual(len(lines), 20)
            for ln in lines:
                json.loads(ln)  # 每行都是合法 JSON

    def test_check_command(self):
        rc = main(["check", "--case", str(CASE)])
        self.assertEqual(rc, 0)

    def test_module_invocation(self):
        proc = subprocess.run(
            [sys.executable, "-m", "fishery_station", "check", "--case", "data/case.json"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
