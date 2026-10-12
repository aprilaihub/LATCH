#!/usr/bin/env python3
"""Run independent single-flow comparisons for one DCP design."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.core.store import SQLiteRunStore  # noqa: E402


@dataclass
class FlowStatus:
    flow: str
    executed_plan: str
    status: str
    command: List[str]
    run_dir: str
    log_path: str
    output_dcp: str
    exit_code: Optional[int] = None
    validation_status: str = "not_run"
    validation_exit_code: Optional[int] = None
    validation_report: Optional[str] = None
    validation_log: Optional[str] = None
    validation_duration_seconds: Optional[float] = None
    final_fmax_mhz: Optional[float] = None
    fmax_improvement_mhz: Optional[float] = None
    final_wns_ns: Optional[float] = None
    pure_qor_score: Optional[float] = None
    published_best_node_id: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: Optional[float] = None
    assertions: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flow": self.flow,
            "executed_plan": self.executed_plan,
            "status": self.status,
            "command": self.command,
            "run_dir": self.run_dir,
            "log_path": self.log_path,
            "output_dcp": self.output_dcp,
            "exit_code": self.exit_code,
            "validation_status": self.validation_status,
            "validation_exit_code": self.validation_exit_code,
            "validation_report": self.validation_report,
            "validation_log": self.validation_log,
            "validation_duration_seconds": self.validation_duration_seconds,
            "final_fmax_mhz": self.final_fmax_mhz,
            "fmax_improvement_mhz": self.fmax_improvement_mhz,
            "final_wns_ns": self.final_wns_ns,
            "pure_qor_score": self.pure_qor_score,
            "published_best_node_id": self.published_best_node_id,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "assertions": self.assertions,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a single-design independent flow matrix")
    parser.add_argument("input_dcp", type=Path)
    parser.add_argument("--flows", required=True, help="Comma/space-separated flow names")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--vectors", type=int, default=200)
    parser.add_argument("--precheck-vectors", type=int, default=100)
    parser.add_argument("--run-mode", choices=("contest-strict", "research-dev"), default="contest-strict")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--skip-functional-validation", action="store_true")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "configs" / "frontier_expansion_default.json")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-nets", type=int, default=5)
    return parser.parse_args()


def parse_flows(raw: str) -> List[str]:
    flows = [part.strip() for part in re.split(r"[\s,]+", raw or "") if part.strip()]
    if not flows:
        raise ValueError("--flows must contain at least one flow")
    return flows


def flow_plan(flow: str) -> List[str]:
    if flow == "pblock_retime":
        return ["pblock", "retime"]
    return [flow]


def safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "flow"


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


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


def git_value(args: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(["git", *args], cwd=REPO_ROOT, text=True, capture_output=True, check=True)
        return proc.stdout.strip()
    except Exception:
        return None


def build_flow_command(args: argparse.Namespace, flow: str, run_dir: Path, output_dcp: Path) -> List[str]:
    plan = flow_plan(flow)
    command = [
        sys.executable,
        "dcp_optimizer.py",
        str(args.input_dcp),
        "--run-mode",
        str(args.run_mode),
        "--pipeline",
        "fixed",
        "--plan",
        ",".join(plan),
        "--budget-runs",
        str(max(1, len(plan))),
        "--run-dir",
        str(run_dir),
        "--output",
        str(output_dcp),
        "--config",
        str(args.config),
        "--max-nets",
        str(args.max_nets),
    ]
    if args.mock:
        command.append("--mock")
    if args.model:
        command.extend(["--model", args.model])
    return command


def run_logged(command: List[str], log_path: Path) -> tuple[int, float]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with log_path.open("w") as handle:
        handle.write("$ " + " ".join(command) + "\n\n")
        proc = subprocess.run(command, cwd=REPO_ROOT, text=True, stdout=handle, stderr=subprocess.STDOUT)
    return proc.returncode, time.time() - started


def newest_validation_report(before: set[Path]) -> Optional[Path]:
    candidates = sorted(
        [path for path in REPO_ROOT.glob("dcp_validation_*") if path not in before],
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        candidates = sorted(REPO_ROOT.glob("dcp_validation_*"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        return None
    report = candidates[-1] / "validation_report.json"
    return report if report.exists() else None


def classify_validation_status(exit_code: int, report: Optional[Dict[str, Any]]) -> str:
    if exit_code == 2 or (report and report.get("infrastructure_failure") is True):
        return "infrastructure_failed"
    if report is None:
        return "failed" if exit_code else "missing_report"
    if report.get("overall_passed") is True and exit_code == 0:
        return "passed"
    return "failed"


def maybe_validate(args: argparse.Namespace, status: FlowStatus, log_path: Path) -> None:
    if args.mock:
        status.validation_status = "skipped_mock"
        return
    if args.skip_functional_validation:
        status.validation_status = "skipped_by_cli"
        return
    if args.dry_run:
        status.validation_status = "dry_run"
        return
    if status.status != "passed":
        status.validation_status = "skipped_run_failed"
        return

    revised = Path(status.output_dcp)
    if not revised.exists():
        status.validation_status = "skipped_missing_output"
        return

    before = set(REPO_ROOT.glob("dcp_validation_*"))
    validation_command = [
        sys.executable,
        "validate_dcps.py",
        str(args.input_dcp),
        str(revised),
        "--vectors",
        str(args.vectors),
        "--precheck-vectors",
        str(args.precheck_vectors),
    ]
    exit_code, duration = run_logged(validation_command, log_path)
    status.validation_exit_code = exit_code
    status.validation_duration_seconds = duration
    status.validation_log = str(log_path)
    report_path = newest_validation_report(before)
    if report_path is not None:
        report = read_json(report_path)
        status.validation_report = str(report_path)
        status.validation_status = classify_validation_status(exit_code, report)
        status.assertions["validation_overall_passed"] = report.get("overall_passed")
        status.assertions["validation_phase1_passed"] = report.get("phase1_passed")
        status.assertions["validation_phase2_passed"] = report.get("phase2_passed")
        status.assertions["validation_phase2_skipped"] = report.get("phase2_skipped")
        status.assertions["validation_infrastructure_failure"] = report.get("infrastructure_failure")
        status.assertions["validation_infrastructure_reason"] = report.get("infrastructure_reason")
        status.assertions["validation_precheck_vectors"] = report.get("precheck_vectors")
    else:
        status.validation_status = classify_validation_status(exit_code, None)


def enrich_from_run(status: FlowStatus, run_dir: Path) -> None:
    manifest = read_json(run_dir / "result_manifest.json")
    store = SQLiteRunStore(run_dir / "run.db")
    selected = store.selections()[-1]
    status.published_best_node_id = selected.state_id
    state = next(item for item in store.states() if item.state_id == selected.state_id)
    artifact = next(item for item in store.artifacts() if item.artifact_id == selected.artifact_id)
    metrics = state.metadata.to_dict().get("metrics", {})
    status.assertions["published_best_source_dcp"] = artifact.uri
    status.final_fmax_mhz = _as_float(metrics.get("final_fmax_mhz") or metrics.get("initial_fmax_mhz"))
    status.final_wns_ns = _as_float(metrics.get("final_wns_ns") or metrics.get("initial_wns_ns"))
    qor = metrics.get("qor_score") if isinstance(metrics.get("qor_score"), dict) else {}
    status.pure_qor_score = _as_float(qor.get("total_score"))
    components = qor.get("components") if isinstance(qor.get("components"), dict) else {}
    status.fmax_improvement_mhz = _as_float(components.get("delta_fmax_mhz"))
    if store.attempts():
        attempt = store.attempts()[-1]
        validation = next((item for item in store.validations() if item.attempt_id == attempt.attempt_id), None)
        status.assertions["transition_id"] = attempt.attempt_id
        status.assertions["transition_validation_status"] = validation.status.value if validation else None
        status.assertions["transition_final_status"] = "accepted" if selected.state_id == state.state_id else "executed"
    status.assertions["pipeline_success"] = manifest.get("success")
    status.assertions["budget"] = manifest.get("budget")
    store.close()


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def write_summaries(out_root: Path, statuses: List[FlowStatus], args: argparse.Namespace) -> None:
    summary_rows = [status.to_dict() for status in statuses]
    write_json(out_root / "flow_matrix_summary.json", {"schema_version": "flow_matrix_summary.v1", "flows": summary_rows})

    csv_path = out_root / "flow_matrix_summary.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "flow",
                "executed_plan",
                "status",
                "exit_code",
                "validation_status",
                "validation_exit_code",
                "validation_duration_seconds",
                "final_fmax_mhz",
                "fmax_improvement_mhz",
                "final_wns_ns",
                "pure_qor_score",
                "run_dir",
                "output_dcp",
                "validation_report",
            ],
        )
        writer.writeheader()
        for status in statuses:
            data = status.to_dict()
            writer.writerow({key: data.get(key) for key in writer.fieldnames})

    manifest = {
        "schema_version": "flow_matrix_manifest.v1",
        "generated_at": now_stamp(),
        "git_commit": git_value(["rev-parse", "--short", "HEAD"]),
        "git_dirty": bool(git_value(["status", "--short"])),
        "input_dcp": str(args.input_dcp),
        "flows": [status.flow for status in statuses],
        "args": {
            "mock": args.mock,
            "dry_run": args.dry_run,
            "skip_functional_validation": args.skip_functional_validation,
            "vectors": args.vectors,
            "run_mode": args.run_mode,
            "config": str(args.config),
            "model": args.model,
            "max_nets": args.max_nets,
        },
        "summary": {
            "passed": sum(1 for status in statuses if status.status == "passed"),
            "failed": sum(1 for status in statuses if status.status == "failed"),
            "dry_run": sum(1 for status in statuses if status.status == "dry_run"),
        },
        "statuses": summary_rows,
    }
    write_json(out_root / "flow_matrix_manifest.json", manifest)


def main() -> int:
    args = parse_args()
    try:
        flows = parse_flows(args.flows)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.input_dcp.exists() and not args.dry_run:
        print(f"Input DCP not found: {args.input_dcp}", file=sys.stderr)
        return 2

    out_root = args.out
    (out_root / "runs").mkdir(parents=True, exist_ok=True)
    (out_root / "logs").mkdir(parents=True, exist_ok=True)
    (out_root / "status").mkdir(parents=True, exist_ok=True)
    (out_root / "outputs").mkdir(parents=True, exist_ok=True)

    statuses: List[FlowStatus] = []
    for index, flow in enumerate(flows, start=1):
        label = f"{index:02d}_{safe_label(flow)}"
        run_dir = out_root / "runs" / label
        log_path = out_root / "logs" / f"{label}.log"
        validation_log = out_root / "logs" / f"{label}.validation.log"
        output_dcp = out_root / "outputs" / f"{args.input_dcp.stem}_{safe_label(flow)}.dcp"
        command = build_flow_command(args, flow, run_dir, output_dcp)
        status = FlowStatus(
            flow=flow,
            executed_plan=",".join(flow_plan(flow)),
            status="dry_run" if args.dry_run else "pending",
            command=command,
            run_dir=str(run_dir),
            log_path=str(log_path),
            output_dcp=str(output_dcp),
        )

        if args.dry_run:
            run_dir.mkdir(parents=True, exist_ok=True)
            log_path.write_text("$ " + " ".join(command) + "\n")
            status.validation_status = "dry_run"
        else:
            exit_code, duration = run_logged(command, log_path)
            status.exit_code = exit_code
            status.duration_seconds = duration
            status.status = "passed" if exit_code == 0 else "failed"
            if exit_code == 0:
                enrich_from_run(status, run_dir)
            else:
                status.error = f"flow command exited with {exit_code}"
            maybe_validate(args, status, validation_log)

        statuses.append(status)
        write_json(out_root / "status" / f"{label}.status.json", status.to_dict())
        write_summaries(out_root, statuses, args)
        if args.stop_on_failure and status.status == "failed":
            break

    write_summaries(out_root, statuses, args)
    failures = [status for status in statuses if status.status == "failed"]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
