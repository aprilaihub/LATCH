#!/usr/bin/env python3
"""Run research-mode frontier LLM tuning sweeps from artifact-level configs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.tuning import (  # noqa: E402
    ExperimentPreflightError,
    load_experiment_spec,
    preflight_experiment,
    render_comparison_report,
    run_experiment,
)
from experiments.tuning.collector import aggregate_rows, collect_run_rows, write_csv  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a frontier LLM tuning experiment")
    parser.add_argument("--experiment", type=Path, required=True, help="Experiment config path")
    parser.add_argument("--out", type=Path, default=None, help="Override experiment output root")
    parser.add_argument("--python", dest="python_exe", default=None, help="Python executable for child frontier runs")
    parser.add_argument("--dry-run", action="store_true", help="Write matrix/status without running child commands")
    parser.add_argument("--preflight", action="store_true", help="Validate the expanded experiment and exit before running")
    parser.add_argument("--strict-experiment", action="store_true", help="Fail runs on provider fallback, unavailable provider calls, or config mismatch")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec = load_experiment_spec(args.experiment)
    if args.preflight:
        report = preflight_experiment(spec, repo_root=REPO_ROOT, output_root=args.out)
        print(report.format_text())
        return 0 if report.ok else 1
    try:
        result = run_experiment(
            spec,
            repo_root=REPO_ROOT,
            output_root=args.out,
            python_exe=args.python_exe,
            dry_run=args.dry_run,
            strict_experiment=args.strict_experiment,
        )
    except ExperimentPreflightError as exc:
        print(exc.report.format_text(), file=sys.stderr)
        return 1
    experiment_root = result["experiment_root"]
    rows = collect_run_rows(result["runs"])
    aggregate = aggregate_rows(rows)
    write_csv(experiment_root / "run_results.csv", rows)
    write_csv(experiment_root / "aggregate_results.csv", aggregate)
    (experiment_root / "comparison_report.md").write_text(render_comparison_report(spec.experiment_id, aggregate))
    print(f"Experiment root: {experiment_root}")
    print(f"Run rows:        {len(rows)}")
    print(f"Aggregate rows:  {len(aggregate)}")
    print(f"Report:          {experiment_root / 'comparison_report.md'}")
    failed = [item for item in result["results"] if item.status == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
