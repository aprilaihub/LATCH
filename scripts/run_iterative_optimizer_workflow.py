#!/usr/bin/env python3
"""Thin flag-style driver for iterative flow-matrix optimisation suites."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_FLOWS = [
    "fanout",
    "pblock",
    "vexriscv",
    "phys_opt",
    "auto_pblock",
    "lut_cone",
    "retime",
    "net_swap",
    "memory_path_opt",
    "hardblock_relocate",
    "congestion_spread",
    "memory_path_opt_rw",
    "hardblock_relocate_rw",
    "congestion_spread_rw",
    "pblock_retime",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run iterative flow-matrix optimisation for one or more DCPs")
    parser.add_argument("input_dcps", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--flows", default=",".join(DEFAULT_FLOWS), help="Comma/space-separated logical optimiser names")
    parser.add_argument("--vectors", type=int, default=100)
    parser.add_argument("--run-mode", choices=("contest-strict", "research-dev"), default="research-dev")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-functional-validation", action="store_true")
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument("--pure-qor-threshold", type=float, default=0.0)
    return parser.parse_args()


def parse_flows(raw: str) -> List[str]:
    flows = [part.strip() for part in re.split(r"[\s,]+", raw or "") if part.strip()]
    if not flows:
        raise ValueError("--flows must contain at least one optimiser")
    return flows


def safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "item"


def benchmark_name(path: Path) -> str:
    stem = re.sub(r"_optimized-\d{8}_\d{6}$", "", path.stem)
    return re.sub(r"_2025\.1$", "", stem)


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def run_logged(command: Sequence[str], log_path: Path) -> tuple[int, float]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with log_path.open("w") as handle:
        handle.write("$ " + " ".join(str(part) for part in command) + "\n\n")
        proc = subprocess.run(list(command), cwd=REPO_ROOT, text=True, stdout=handle, stderr=subprocess.STDOUT)
    return proc.returncode, time.time() - started


def git_value(args: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(["git", *args], cwd=REPO_ROOT, text=True, capture_output=True, check=True)
        return proc.stdout.strip()
    except Exception:
        return None


def build_matrix_command(args: argparse.Namespace, input_dcp: Path, remaining: List[str], out: Path) -> List[str]:
    command = [
        sys.executable,
        "scripts/run_flow_matrix.py",
        str(input_dcp),
        "--flows",
        ",".join(remaining),
        "--out",
        str(out),
        "--vectors",
        str(args.vectors),
        "--run-mode",
        str(args.run_mode),
    ]
    if args.mock:
        command.append("--mock")
    if args.dry_run:
        command.append("--dry-run")
    if args.skip_functional_validation:
        command.append("--skip-functional-validation")
    return command


def matrix_rows_to_sheet_rows(benchmark: str, round_index: int, matrix_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in matrix_rows:
        rows.append(
            {
                "benchmark_name": benchmark,
                "round_index": round_index,
                "optimiser": row.get("flow"),
                "executed_plan": row.get("executed_plan"),
                "fmax_improvement_mhz": row.get("fmax_improvement_mhz"),
                "final_fmax_mhz": row.get("final_fmax_mhz"),
                "optimisation_runtime_seconds": row.get("duration_seconds"),
                "verification_runtime_seconds": row.get("validation_duration_seconds"),
                "overall_pure_qor_score": row.get("pure_qor_score"),
                "validation_status": row.get("validation_status"),
                "selected": False,
                "input_dcp": (row.get("command") or [None, None, None])[2],
                "output_dcp": row.get("output_dcp"),
                "run_dir": row.get("run_dir"),
                "optimizer_log": row.get("log_path"),
                "validation_log": row.get("validation_log"),
                "status": row.get("status"),
                "exit_code": row.get("exit_code"),
                "validation_exit_code": row.get("validation_exit_code"),
                "validation_report": row.get("validation_report"),
            }
        )
    return rows


def selectable(row: Dict[str, Any], threshold: float) -> bool:
    if row.get("status") != "passed":
        return False
    if row.get("validation_status") not in {"passed", "skipped_mock", "skipped_by_cli"}:
        return False
    output_dcp = row.get("output_dcp")
    if not output_dcp or not Path(str(output_dcp)).exists():
        return False
    score = as_float(row.get("overall_pure_qor_score"))
    return score is not None and score > threshold


def select_best(rows: List[Dict[str, Any]], threshold: float) -> Optional[Dict[str, Any]]:
    candidates = [row for row in rows if selectable(row, threshold)]
    if not candidates:
        return None
    order = {str(row.get("optimiser")): index for index, row in enumerate(rows)}

    def key(row: Dict[str, Any]) -> tuple[float, float, float, int]:
        score = as_float(row.get("overall_pure_qor_score")) or float("-inf")
        fmax = as_float(row.get("final_fmax_mhz")) or float("-inf")
        runtime = as_float(row.get("optimisation_runtime_seconds"))
        runtime_key = -runtime if runtime is not None else float("-inf")
        return (score, fmax, runtime_key, -order.get(str(row.get("optimiser")), 0))

    return max(candidates, key=key)


def run_benchmark(args: argparse.Namespace, input_dcp: Path, out_root: Path, all_flows: List[str]) -> Dict[str, Any]:
    bench = benchmark_name(input_dcp)
    bench_dir = out_root / safe_label(bench)
    current_dcp = input_dcp
    remaining = list(all_flows)
    rows: List[Dict[str, Any]] = []
    selected_sequence: List[str] = []
    stop_reason = "all_optimisers_exhausted"
    round_index = 1

    while remaining:
        if args.max_rounds is not None and round_index > max(0, args.max_rounds):
            stop_reason = "max_rounds_reached"
            break

        round_dir = bench_dir / f"round_{round_index:02d}"
        matrix_dir = round_dir / "flow_matrix"
        command = build_matrix_command(args, current_dcp, remaining, matrix_dir)
        exit_code, duration = run_logged(command, round_dir / "flow_matrix_command.log")
        matrix = read_json(matrix_dir / "flow_matrix_summary.json")
        matrix_rows = matrix.get("flows") if isinstance(matrix.get("flows"), list) else []
        sheet_rows = matrix_rows_to_sheet_rows(bench, round_index, matrix_rows)
        for row in sheet_rows:
            row["matrix_exit_code"] = exit_code
            row["matrix_runtime_seconds"] = duration
            row["matrix_dir"] = str(matrix_dir)
        rows.extend(sheet_rows)

        selected = None if args.dry_run else select_best(sheet_rows, args.pure_qor_threshold)
        if selected is None:
            stop_reason = "dry_run_complete" if args.dry_run else "no_positive_candidate"
            write_round_summary(round_dir, command, matrix_dir, sheet_rows, None, stop_reason)
            break

        selected["selected"] = True
        selected_sequence.append(str(selected["optimiser"]))
        current_dcp = Path(str(selected["output_dcp"]))
        remaining = [flow for flow in remaining if flow != selected["optimiser"]]
        write_round_summary(round_dir, command, matrix_dir, sheet_rows, selected, "selected")
        write_benchmark_outputs(bench_dir, input_dcp, current_dcp, rows, selected_sequence, stop_reason, remaining)
        round_index += 1

    if not remaining and stop_reason != "max_rounds_reached":
        stop_reason = "all_optimisers_exhausted"

    final_best = bench_dir / "final_best.dcp"
    if not args.dry_run and current_dcp.exists():
        final_best.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(current_dcp, final_best)

    return write_benchmark_outputs(bench_dir, input_dcp, current_dcp, rows, selected_sequence, stop_reason, remaining, final_best)


def write_round_summary(
    round_dir: Path,
    command: List[str],
    matrix_dir: Path,
    rows: List[Dict[str, Any]],
    selected: Optional[Dict[str, Any]],
    stop_reason: str,
) -> None:
    write_json(
        round_dir / "round_summary.json",
        {
            "schema_version": "iterative_round_summary.v1",
            "command": command,
            "matrix_dir": str(matrix_dir),
            "stop_reason": stop_reason,
            "selected_optimiser": selected.get("optimiser") if selected else None,
            "selected_output_dcp": selected.get("output_dcp") if selected else None,
            "rows": rows,
        },
    )


def write_benchmark_outputs(
    bench_dir: Path,
    input_dcp: Path,
    current_dcp: Path,
    rows: List[Dict[str, Any]],
    selected_sequence: List[str],
    stop_reason: str,
    remaining: List[str],
    final_best: Optional[Path] = None,
) -> Dict[str, Any]:
    summary = {
        "schema_version": "iterative_benchmark_summary.v1",
        "benchmark_name": benchmark_name(input_dcp),
        "input_dcp": str(input_dcp),
        "current_best_dcp": str(current_dcp),
        "final_best_dcp": str(final_best) if final_best and final_best.exists() else None,
        "stop_reason": stop_reason,
        "rounds_run": max((int(row.get("round_index") or 0) for row in rows), default=0),
        "selected_sequence": selected_sequence,
        "remaining_optimisers": remaining,
        "rows": rows,
    }
    write_json(bench_dir / "benchmark_summary.json", summary)
    write_rows_csv(bench_dir / "benchmark_summary.csv", rows)
    return summary


def write_rows_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "benchmark_name",
        "round_index",
        "optimiser",
        "executed_plan",
        "fmax_improvement_mhz",
        "final_fmax_mhz",
        "optimisation_runtime_seconds",
        "verification_runtime_seconds",
        "overall_pure_qor_score",
        "validation_status",
        "selected",
        "input_dcp",
        "output_dcp",
        "run_dir",
        "optimizer_log",
        "validation_log",
        "status",
        "exit_code",
        "validation_exit_code",
        "validation_report",
        "matrix_dir",
        "matrix_exit_code",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def write_suite_outputs(out_root: Path, summaries: List[Dict[str, Any]], args: argparse.Namespace, flows: List[str]) -> None:
    rows = [row for summary in summaries for row in summary.get("rows", [])]
    write_rows_csv(out_root / "suite_summary.csv", rows)
    write_json(
        out_root / "suite_summary.json",
        {
            "schema_version": "iterative_suite_summary.v1",
            "generated_at": now_iso(),
            "git_commit": git_value(["rev-parse", "--short", "HEAD"]),
            "git_dirty": bool(git_value(["status", "--short"])),
            "input_dcps": [str(path) for path in args.input_dcps],
            "flows": flows,
            "args": {
                "out": str(out_root),
                "vectors": args.vectors,
                "run_mode": args.run_mode,
                "mock": args.mock,
                "dry_run": args.dry_run,
                "skip_functional_validation": args.skip_functional_validation,
                "max_rounds": args.max_rounds,
                "pure_qor_threshold": args.pure_qor_threshold,
            },
            "benchmarks": summaries,
            "summary": {
                "benchmarks": len(summaries),
                "candidate_rows": len(rows),
                "selected_rows": sum(1 for row in rows if row.get("selected")),
                "failed_rows": sum(1 for row in rows if row.get("status") == "failed"),
                "dry_run_rows": sum(1 for row in rows if row.get("status") == "dry_run"),
            },
        },
    )


def as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    args = parse_args()
    try:
        flows = parse_flows(args.flows)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.max_rounds is not None and args.max_rounds < 0:
        print("--max-rounds must be non-negative", file=sys.stderr)
        return 2

    missing = [path for path in args.input_dcps if not path.exists()]
    if missing and not args.dry_run:
        for path in missing:
            print(f"Input DCP not found: {path}", file=sys.stderr)
        return 2

    out_root = args.out or (REPO_ROOT / "experiments" / f"iterative_suite_{now_stamp()}")
    out_root.mkdir(parents=True, exist_ok=True)
    summaries = [run_benchmark(args, input_dcp, out_root, flows) for input_dcp in args.input_dcps]
    write_suite_outputs(out_root, summaries, args, flows)
    hard_failures = [
        row
        for summary in summaries
        for row in summary.get("rows", [])
        if row.get("matrix_exit_code") not in {0, None} and not Path(str(row.get("matrix_dir", "")) + "/flow_matrix_summary.json").exists()
    ]
    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
