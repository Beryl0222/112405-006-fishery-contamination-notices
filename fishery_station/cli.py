"""命令行入口：

    python3 -m fishery_station build --case data/case.json --out out
    python3 -m fishery_station check --case data/case.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .caseio import load_case
from .checks import invariant_violations
from .engine import build_case
from .present import dump_state_json, render_markdown, write_ledger_jsonl


def cmd_build(args: argparse.Namespace) -> int:
    case_path = Path(args.case)
    out_dir = Path(args.out)
    case = load_case(case_path)
    state = build_case(case)

    out_dir.mkdir(parents=True, exist_ok=True)
    dump_state_json(state, out_dir / "state.json")
    write_ledger_jsonl(state, out_dir / "ledger.jsonl")
    (out_dir / "report.md").write_text(render_markdown(state), encoding="utf-8")

    problems = invariant_violations(state)
    print(f"案例 {state.case_id}：受理记录 {len(state.raw_records)} 条，"
          f"批次 {len(state.batches)} 个，检测 {len(state.tests)} 份，"
          f"通知 {len(state.notices)} 份，线索 {len(state.leads)} 条，"
          f"开放待办 {sum(1 for a in state.actions if a.status.value == 'open')} 项。")
    for bv in state.batch_views:
        print(f"  - {bv.batch_id}: {bv.disposition.value} | {bv.disposition_label}")
    print(f"输出目录：{out_dir}/ (state.json, report.md, ledger.jsonl)")
    if problems:
        print("不变量自检发现问题：", file=sys.stderr)
        for p in problems:
            print(f"  ! {p}", file=sys.stderr)
        return 1
    print("不变量自检：全部通过。")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    state = build_case(load_case(args.case))
    problems = invariant_violations(state)
    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        return 1
    print(f"OK: {state.case_id} 的 {len(state.batches)} 个批次、"
          f"{len(state.notices)} 份通知均满足复核不变量。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fishery_station", description="沿岸检验站批次归属与放行范围工作台")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="处理案例并输出 state.json / report.md / ledger.jsonl")
    b.add_argument("--case", default="data/case.json", help="案例 JSON 路径")
    b.add_argument("--out", default="out", help="输出目录")
    b.set_defaults(func=cmd_build)

    c = sub.add_parser("check", help="只运行业务不变量自检")
    c.add_argument("--case", default="data/case.json")
    c.set_defaults(func=cmd_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"找不到案例文件：{exc.filename}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"案例 JSON 解析失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
