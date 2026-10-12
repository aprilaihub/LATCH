#!/usr/bin/env python3
"""Run the frozen WP-013 development root x M panel with owned processes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from optimizer_pipeline.dof.processes import (  # noqa: E402
    OwnedProcessCancelled,
    OwnedProcessRunner,
    ProcessLimits,
)

SCHEMA = "research-flow.wp013-macro-panel.v1"
QUEUE_SEED = "wp013-development-macro-panel-v1"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def git_output(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPOSITORY, check=True, text=True, capture_output=True
    ).stdout.strip()


def build_jobs(roots: dict[str, Any], macros: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for root_index, root in enumerate(roots["roots"]):
        for action_index, action in enumerate(macros["frozen_actions"]):
            flow = action["flow_name"]
            row = {
                "job_id": f"r{root_index:02d}-a{action_index:02d}-{root['root_id']}-{flow}",
                "root_index": root_index,
                "action_index": action_index,
                "root_id": root["root_id"],
                "root_filename": root["filename"],
                "root_sha256": root["sha256"],
                "action_key": action["action_key"],
                "flow_name": flow,
                "heavy_call_upper_bound": action["heavy_call_range"][1],
            }
            row["queue_key"] = canonical_hash({"seed": QUEUE_SEED, **row})
            rows.append(row)
    return sorted(rows, key=lambda row: row["queue_key"])


def attempted_result(job_dir: Path, flow: str) -> dict[str, Any]:
    summary_path = job_dir / "flow_matrix_summary.json"
    if not summary_path.exists():
        return {"canonical_endpoint": "MISSING_SUMMARY"}
    outer = read_object(summary_path)["flows"][0]
    run_db = job_dir / "runs" / f"01_{flow}" / "run.db"
    result: dict[str, Any] = {
        "canonical_endpoint": "MISSING_RUN_DB",
        "outer_status": outer.get("status"),
        "outer_exit_code": outer.get("exit_code"),
        "outer_duration_seconds": outer.get("duration_seconds"),
    }
    if not run_db.exists():
        return result
    connection = sqlite3.connect(run_db)
    connection.row_factory = sqlite3.Row
    try:
        attempt = connection.execute(
            "SELECT * FROM attempts ORDER BY scheduled_at DESC LIMIT 1"
        ).fetchone()
        if attempt is None:
            return result | {"canonical_endpoint": "MISSING_ATTEMPT"}
        validation = connection.execute(
            "SELECT * FROM validations WHERE attempt_id = ? ORDER BY checked_at DESC LIMIT 1",
            (attempt["attempt_id"],),
        ).fetchone()
        outcome = connection.execute(
            "SELECT * FROM outcomes WHERE attempt_id = ? ORDER BY finished_at DESC LIMIT 1",
            (attempt["attempt_id"],),
        ).fetchone()
        metrics = json.loads(outcome["metrics_json"] or "{}") if outcome else {}
        validation_status = validation["status"] if validation else None
        endpoint = {
            "CLEAN": "VALID_COMPLETION",
            "VALID": "VALID_COMPLETION",
            "WARNING": "VALID_COMPLETION",
            "INVALID": "ACTION_INVALID",
            "TOOL_FAILED": "TOOL_FAILURE",
            "TIMEOUT": "TOOL_TIMEOUT",
        }.get(validation_status, "UNCLASSIFIED_ATTEMPT")
        return result | {
            "canonical_endpoint": endpoint,
            "attempt_id": attempt["attempt_id"],
            "validation_status": validation_status,
            "outcome_status": outcome["status"] if outcome else None,
            "error_type": outcome["error_type"] if outcome else None,
            "error_message": outcome["error_message"] if outcome else None,
            "output_artifact_id": outcome["output_artifact_id"] if outcome else None,
            "initial_wns_ns": metrics.get("initial_wns_ns"),
            "final_wns_ns": metrics.get("final_wns_ns"),
            "initial_fmax_mhz": metrics.get("initial_fmax_mhz"),
            "final_fmax_mhz": metrics.get("final_fmax_mhz"),
            "fmax_improvement_mhz": metrics.get("fmax_improvement_mhz"),
            "duration_seconds": metrics.get("duration_seconds"),
            "output_dcp": metrics.get("output_dcp"),
            "validation_errors": metrics.get("validation_errors", []),
        }
    finally:
        connection.close()


async def run_panel(args: argparse.Namespace) -> dict[str, Any]:
    roots = read_object(args.roots)
    macros = read_object(args.macros)
    if len(roots.get("roots", [])) not in range(8, 11):
        raise RuntimeError("development_root_count_out_of_range")
    if len(macros.get("frozen_actions", [])) not in range(8, 11):
        raise RuntimeError("macro_count_out_of_range")
    if args.max_parallel < 1 or args.max_parallel > 8:
        raise RuntimeError("weekday_parallelism_must_be_1_to_8")
    if git_output("status", "--porcelain"):
        raise RuntimeError("tracked_worktree_must_be_clean")

    root_prefix = Path(roots["root_path_prefix"])
    for root in roots["roots"]:
        path = root_prefix / root["filename"]
        if file_hash(path) != root["sha256"]:
            raise RuntimeError(f"root_hash_mismatch:{root['root_id']}")

    jobs = build_jobs(roots, macros)
    if len(jobs) != roots["panel_jobs"] or len(jobs) > 100:
        raise RuntimeError("panel_job_count_mismatch")
    planned_heavy = sum(row["heavy_call_upper_bound"] for row in jobs)
    if planned_heavy > 240:
        raise RuntimeError("panel_heavy_reserve_exceeded")

    args.out.mkdir(parents=True, exist_ok=True)
    ledger_path = args.out / "controller-ledger.json"
    started = time.monotonic()
    ledger: dict[str, Any] = {
        "schema_version": SCHEMA,
        "status": "RUNNING",
        "source_commit": git_output("rev-parse", "HEAD"),
        "source_tree": git_output("rev-parse", "HEAD^{tree}"),
        "roots_manifest_sha256": file_hash(args.roots),
        "macros_manifest_sha256": file_hash(args.macros),
        "queue_seed": QUEUE_SEED,
        "max_parallel": args.max_parallel,
        "job_timeout_seconds": args.job_timeout_seconds,
        "planned_jobs": len(jobs),
        "planned_heavy_call_upper_bound": planned_heavy,
        "jobs": jobs,
        "results": {},
    }
    if ledger_path.exists():
        previous = read_object(ledger_path)
        immutable = (
            "schema_version", "source_commit", "source_tree",
            "roots_manifest_sha256", "macros_manifest_sha256", "queue_seed",
            "max_parallel", "job_timeout_seconds", "planned_jobs",
            "planned_heavy_call_upper_bound", "jobs",
        )
        if any(previous.get(key) != ledger.get(key) for key in immutable):
            raise RuntimeError("resume_ledger_contract_mismatch")
        ledger["results"] = dict(previous.get("results") or {})
    write_atomic(ledger_path, ledger)
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(args.max_parallel)
    limits = ProcessLimits(
        initialization_timeout_seconds=60.0,
        whole_run_timeout_seconds=args.job_timeout_seconds,
        graceful_close_seconds=20.0,
        terminate_grace_seconds=10.0,
        kill_grace_seconds=15.0,
        cleanup_timeout_seconds=60.0,
        output_limit_bytes=1024 * 1024,
    )

    async def one(job: dict[str, Any]) -> None:
        if job["job_id"] in ledger["results"]:
            return
        async with semaphore:
            job_dir = args.out / "jobs" / job["job_id"]
            command = (
                str(args.python), str(REPOSITORY / "scripts" / "run_flow_matrix.py"),
                str(root_prefix / job["root_filename"]), "--flows", job["flow_name"],
                "--out", str(job_dir), "--run-mode", "research-dev",
                "--skip-functional-validation",
            )
            environment = dict(os.environ)
            environment["PYTHONUNBUFFERED"] = "1"
            try:
                process = await OwnedProcessRunner(limits).run(
                    command, cwd=REPOSITORY, env=environment
                )
            except OwnedProcessCancelled as exc:
                process = exc.result
            row = {
                "job_id": job["job_id"],
                "process": process.to_dict(include_output_text=False),
                "attempt": attempted_result(job_dir, job["flow_name"]),
            }
            async with lock:
                ledger["results"][job["job_id"]] = row
                write_atomic(ledger_path, ledger)

    await asyncio.gather(*(one(job) for job in jobs))
    root_post = {}
    for root in roots["roots"]:
        root_post[root["root_id"]] = file_hash(root_prefix / root["filename"])
    results = list(ledger["results"].values())
    ledger.update(
        {
            "status": "COMPLETE",
            "completed_jobs": len(results),
            "elapsed_seconds": time.monotonic() - started,
            "root_post_hashes": root_post,
            "all_roots_unchanged": all(
                root_post[root["root_id"]] == root["sha256"] for root in roots["roots"]
            ),
            "process_endpoint_counts": {
                key: sum(1 for row in results if row["process"]["endpoint"] == key)
                for key in sorted({row["process"]["endpoint"] for row in results})
            },
            "attempt_endpoint_counts": {
                key: sum(1 for row in results if row["attempt"]["canonical_endpoint"] == key)
                for key in sorted({row["attempt"]["canonical_endpoint"] for row in results})
            },
            "all_processes_clean": all(
                row["process"]["cleanup_ok"] and not row["process"]["orphan_process_tokens"]
                for row in results
            ),
        }
    )
    ledger["accepted"] = (
        ledger["completed_jobs"] == ledger["planned_jobs"]
        and ledger["all_roots_unchanged"]
        and ledger["all_processes_clean"]
    )
    ledger["ledger_sha256"] = canonical_hash({k: v for k, v in ledger.items() if k != "ledger_sha256"})
    write_atomic(ledger_path, ledger)
    return ledger


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--roots", type=Path, required=True)
    value.add_argument("--macros", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=8)
    value.add_argument("--job-timeout-seconds", type=float, default=7200.0)
    return value


def main() -> int:
    args = parser().parse_args()
    result = asyncio.run(run_panel(args))
    print(json.dumps({key: result[key] for key in (
        "accepted", "completed_jobs", "elapsed_seconds", "process_endpoint_counts",
        "attempt_endpoint_counts", "all_roots_unchanged", "all_processes_clean",
    )}, sort_keys=True))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
