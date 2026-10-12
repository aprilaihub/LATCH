"""Mock-only entry for the common L0--L3 experiment contract.

This entry is intentionally independent of the contest application.  It never
loads dotenv files, contest profiles, the Xilinx backend, or provider clients.
Real-tool binding belongs to a later work package.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
from pathlib import Path
import sys
from typing import Any


def build_dof_experiment_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fpga-dof-experiment",
        description="Run one validated common DoF condition using the mock-only WP-002 runtime.",
    )
    parser.add_argument("spec", type=Path, help="Strict JSON experiment specification.")
    parser.add_argument("--run-dir", type=Path, required=True, help="New output directory for this run.")
    parser.add_argument(
        "--execution-mode",
        choices=("mock",),
        default="mock",
        help="WP-002 supports mock execution only.",
    )
    return parser


def run_dof_experiment_entry(
    args: argparse.Namespace,
    *,
    runner: Callable[[Path, Path], dict[str, Any]] | None = None,
) -> int:
    if str(getattr(args, "execution_mode", "")) != "mock":
        raise ValueError("WP-002 DoF experiment entry permits mock execution only")
    if runner is None:
        from optimizer_pipeline.dof.runner import run_mock_experiment

        runner = run_mock_experiment
    summary = runner(Path(args.spec), Path(args.run_dir))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_dof_experiment_parser()
    args = parser.parse_args(argv)
    try:
        return run_dof_experiment_entry(args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"fpga-dof-experiment: {exc}", file=sys.stderr)
        return 2
