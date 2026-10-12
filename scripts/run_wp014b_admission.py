#!/usr/bin/env python3
"""Execute the frozen WP-014B real-admission matrix with bounded process groups."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any


REPO = Path(__file__).resolve().parents[1]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def decode(value: str) -> str:
    return base64.b64decode(value).decode("utf-8", "replace")


def run_bounded(command: list[str], cwd: Path, timeout: float) -> tuple[int | None, bool, bytes, bytes]:
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, False, stdout, stderr
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try: stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); stdout, stderr = process.communicate()
        return None, True, stdout, stderr


def parse(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"heavy_calls": [], "targets": [], "qor": {}}
    for raw in path.read_text(errors="replace").splitlines():
        fields = raw.split("\t")
        if fields[0] == "WP014B_OPEN": result["open"] = {"status": int(fields[1]), "detail": decode(fields[2])}
        elif fields[0] == "WP014B_PART": result["part"] = fields[1]
        elif fields[0] == "WP014B_HEAVY_START": result["heavy_calls"].append({"label": fields[1], "start_ms": int(fields[2]), "end_ms": None, "status": None})
        elif fields[0] == "WP014B_HEAVY_END":
            call = next(row for row in reversed(result["heavy_calls"]) if row["label"] == fields[1] and row["end_ms"] is None)
            call.update(end_ms=int(fields[2]), status=int(fields[3]), detail=decode(fields[4]))
        elif fields[0] == "WP014B_TARGET":
            names = decode(fields[3]).splitlines() if decode(fields[3]) else []
            result["targets"].append({"type": fields[1], "count": int(fields[2]),
                "handle_sha256": [hashlib.sha256(name.encode()).hexdigest() for name in names]})
        elif fields[0] == "WP014B_QOR":
            result["qor"][fields[1]] = {"path_status": int(fields[2]), "wns_ns": number(fields[3]),
                "datapath_delay_ns": number(fields[4]), "timing_status": int(fields[5]),
                "timing_report_sha256": hashlib.sha256(decode(fields[6]).encode()).hexdigest(),
                "route_status": int(fields[7]), "route_report": decode(fields[8])}
        elif fields[0] == "WP014B_ACTION":
            result["action"] = {"kind": fields[1], "value": decode(fields[2]), "start_ms": int(fields[3]),
                "end_ms": int(fields[4]), "status": int(fields[5]), "detail": decode(fields[6])}
        elif fields[0] == "WP014B_CHILD": result["child"] = {"status": int(fields[1]), "detail": decode(fields[2])}
    return result


def number(value: str) -> float | None:
    try: return float(value)
    except ValueError: return None


def endpoint(parsed: dict[str, Any] | None, timeout: bool, returncode: int | None) -> str:
    if timeout: return "TIMEOUT"
    if returncode not in {0, None} or not parsed or parsed.get("open", {}).get("status") != 0: return "TOOL_EXECUTION_FAILURE"
    action = parsed.get("action", {})
    if action.get("status") != 0:
        detail = action.get("detail", "")
        if "WP014B_NO_APPLICABLE_TARGET" in detail: return "NO_APPLICABLE_TARGET"
        if re.search(r"unknown option|invalid.*directive|invalid option|bad value", detail, re.I): return "UNSUPPORTED_SYNTAX"
        if re.search(r"not applicable|unsupported.*device|advanced flow|device", detail, re.I): return "DEVICE_OR_STAGE_INCOMPATIBLE"
        return "TOOL_EXECUTION_FAILURE"
    if parsed.get("child", {}).get("status") != 0: return "TOOL_EXECUTION_FAILURE"
    before = parsed.get("qor", {}).get("before", {}).get("wns_ns")
    after = parsed.get("qor", {}).get("after", {}).get("wns_ns")
    return "VALID_QOR_GAIN" if before is not None and after is not None and after > before else "VALID_NO_QOR_GAIN"


def run_job(vivado: Path, row: dict[str, Any], root: dict[str, Any], prefix: Path, output: Path) -> dict[str, Any]:
    job_dir = output / "jobs" / row["job_id"]
    job_dir.mkdir(parents=True, exist_ok=False)
    root_path, child = prefix / root["filename"], job_dir / "child.dcp"
    tsv = job_dir / "evidence.tsv"
    command = [str(vivado), "-mode", "batch", "-nojournal", "-notrace", "-log", str(job_dir / "vivado.log"),
               "-source", str(REPO / "scripts/wp014b_admission_job.tcl"), "-tclargs",
               str(root_path), str(tsv), row["action_kind"], row["action_value"], str(child)]
    start = time.monotonic(); returncode, timed_out, stdout, stderr = run_bounded(command, job_dir, row["timeout_seconds"])
    (job_dir / "stdout.txt").write_bytes(stdout); (job_dir / "stderr.txt").write_bytes(stderr)
    parsed = parse(tsv) if tsv.exists() else None
    result = {
        **row, "root_family": root["family"], "root_expected_sha256": root["sha256"],
        "root_before_sha256": hashlib.sha256(root_path.read_bytes()).hexdigest(),
        "root_after_sha256": hashlib.sha256(root_path.read_bytes()).hexdigest(),
        "elapsed_seconds": time.monotonic() - start, "process_returncode": returncode,
        "timeout": timed_out, "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(), "parsed": parsed,
        "actual_heavy_calls": len(parsed["heavy_calls"]) if parsed else 0,
        "endpoint": endpoint(parsed, timed_out, returncode),
        "child_available": child.exists(), "child_sha256": hashlib.sha256(child.read_bytes()).hexdigest() if child.exists() else None,
    }
    (job_dir / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True); parser.add_argument("--max-workers", type=int, default=8)
    args = parser.parse_args()
    plan, roots_manifest = json.loads(args.plan.read_text()), json.loads(args.roots.read_text())
    if plan["continuation_identity"] != "wp014b-action-surface-continuation-v1": raise SystemExit("identity_mismatch")
    if plan["counts"]["planned_heavy_calls"] > 96 or args.max_workers > plan["concurrency"]["active_max"]: raise SystemExit("resource_cap_invalid")
    vivado_raw = os.environ.get("VIVADO_EXEC");
    if not vivado_raw: raise SystemExit("VIVADO_EXEC_missing_source_env")
    vivado = Path(vivado_raw).resolve(strict=True); roots = {row["root_id"]: row for row in roots_manifest["roots"]}
    args.output.mkdir(parents=True, exist_ok=False); started = time.monotonic(); results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {pool.submit(run_job, vivado, row, roots[row["root_id"]], Path(roots_manifest["root_path_prefix"]), args.output): row for row in plan["jobs"]}
        for future in as_completed(futures):
            result = future.result(); results.append(result)
            print(json.dumps({"job_id": result["job_id"], "endpoint": result["endpoint"], "heavy": result["actual_heavy_calls"], "elapsed": round(result["elapsed_seconds"], 3)}), flush=True)
    results.sort(key=lambda row: row["job_id"]); wall = time.monotonic() - started
    endpoint_counts: dict[str, int] = {}
    for row in results: endpoint_counts[row["endpoint"]] = endpoint_counts.get(row["endpoint"], 0) + 1
    core = {"schema_version": "dof.wp014b-real-admission-aggregate.v1", "continuation_identity": plan["continuation_identity"],
            "plan_sha256": plan["plan_sha256"], "source_commit": subprocess.run(("git", "rev-parse", "HEAD"), cwd=REPO, check=True, capture_output=True, text=True).stdout.strip(),
            "wall_seconds": wall, "planned_jobs": len(plan["jobs"]), "completed_jobs": len(results),
            "planned_heavy_calls": plan["counts"]["planned_heavy_calls"], "actual_heavy_calls": sum(row["actual_heavy_calls"] for row in results),
            "endpoint_counts": endpoint_counts, "root_integrity": all(row["root_expected_sha256"] == row["root_before_sha256"] == row["root_after_sha256"] for row in results),
            "openrouter_requests": 0, "scientific_repeat_1": False, "sealed_holdout_access": False, "results": results}
    aggregate = core | {"aggregate_sha256": digest(core)}
    (args.output / "aggregate.json").write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
    print(json.dumps({key: aggregate[key] for key in ("completed_jobs", "actual_heavy_calls", "endpoint_counts", "root_integrity", "wall_seconds", "aggregate_sha256")}, sort_keys=True))
    return 0 if aggregate["root_integrity"] and aggregate["actual_heavy_calls"] <= 96 and wall <= 43200 else 2


if __name__ == "__main__": raise SystemExit(main())
