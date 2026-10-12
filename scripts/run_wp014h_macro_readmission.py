#!/usr/bin/env python3
"""One bounded, development-only fanout/lut_cone technical re-admission campaign."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

from optimizer_pipeline.dof.processes import (  # noqa: E402
    OwnedProcessCancelled,
    OwnedProcessRunner,
    ProcessLimits,
)
from scripts.run_wp013_macro_panel import attempted_result  # noqa: E402


SCHEMA = "dof.wp014h-macro-technical-readmission.v1"
ROOTS = ("vexriscv_re-place", "logicnets_jscl", "corescore_500_mod")
FLOWS = ("fanout", "lut_cone")


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def object_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


async def run(args: argparse.Namespace) -> int:
    if args.out.exists():
        raise FileExistsError(args.out)
    if not 1 <= args.max_parallel <= 8:
        raise ValueError("weekday_parallelism_must_be_between_1_and_8")
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    bundle = read(args.bundle)
    roots = {root_id: bundle["roots"][root_id] for root_id in ROOTS}
    before = {root_id: file_hash(Path(root["root_path"])) for root_id, root in roots.items()}
    if any(before[root_id] != roots[root_id]["sha256"] for root_id in ROOTS):
        raise RuntimeError("development_root_hash_mismatch")
    args.out.mkdir(parents=True)
    jobs = [
        {"job_id": f"{root_id}__{flow}", "root_id": root_id, "flow": flow}
        for root_id in ROOTS for flow in FLOWS
    ]
    semaphore = asyncio.Semaphore(args.max_parallel)
    limits = ProcessLimits(
        initialization_timeout_seconds=60.0,
        whole_run_timeout_seconds=args.timeout_seconds,
        graceful_close_seconds=20.0,
        terminate_grace_seconds=10.0,
        kill_grace_seconds=15.0,
        cleanup_timeout_seconds=60.0,
        output_limit_bytes=1024 * 1024,
    )
    started = time.monotonic()

    async def one(job: dict[str, str]) -> dict[str, Any]:
        root = roots[job["root_id"]]
        job_dir = args.out / "jobs" / job["job_id"]
        command = (
            str(args.python), str(REPO / "scripts" / "run_flow_matrix.py"),
            str(root["root_path"]), "--flows", job["flow"], "--out", str(job_dir),
            "--run-mode", "research-dev", "--skip-functional-validation",
        )
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        async with semaphore:
            try:
                process = await OwnedProcessRunner(limits).run(command, cwd=REPO, env=environment)
            except OwnedProcessCancelled as exc:
                process = exc.result
        attempt = attempted_result(job_dir, job["flow"])
        output = job_dir / "outputs" / f"{Path(root['root_path']).stem}_{job['flow']}.dcp"
        published = file_hash(output) if output.is_file() else None
        fallback_to_root = published == root["sha256"] if published else False
        actual_child = Path(attempt["output_dcp"]) if attempt.get("output_dcp") else None
        result = {
            **job,
            "root_sha256": root["sha256"],
            "process": process.to_dict(include_output_text=False),
            "attempt": attempt,
            "published_output": str(output) if output.is_file() else None,
            "published_output_sha256": published,
            "published_fallback_to_root": fallback_to_root,
            "actual_child_exists": bool(actual_child and actual_child.is_file()),
            "actual_child_sha256": file_hash(actual_child) if actual_child and actual_child.is_file() else None,
            "provider_or_llm_calls": 0,
            "sealed_holdout_accesses": 0,
        }
        write(job_dir / "technical-result.json", result)
        return result

    rows = await asyncio.gather(*(one(job) for job in jobs))
    after = {root_id: file_hash(Path(root["root_path"])) for root_id, root in roots.items()}
    flow_rows = {
        flow: [row for row in rows if row["flow"] == flow]
        for flow in FLOWS
    }
    recommendations = {}
    for flow, selected in flow_rows.items():
        valid = [row for row in selected if row["attempt"].get("canonical_endpoint") == "VALID_COMPLETION" and row["actual_child_exists"]]
        recommendations[flow] = {
            "technical_candidate": "PENDING_V1" if valid else "EXCLUDE_CURRENT_WRAPPER",
            "valid_children": len(valid),
            "clean_fallbacks": sum(row["published_fallback_to_root"] for row in selected),
            "attempt_endpoints": {endpoint: sum(row["attempt"].get("canonical_endpoint") == endpoint for row in selected) for endpoint in sorted({str(row["attempt"].get("canonical_endpoint")) for row in selected})},
            "reason": "five-seed V1 required for every valid child" if valid else "no physically valid non-fallback child produced by the current generic wrapper",
        }
    core = {
        "schema_version": SCHEMA,
        "label": "WP014H_ONE_FRESH_TECHNICAL_READMISSION_CAMPAIGN",
        "roots": list(ROOTS),
        "flows": list(FLOWS),
        "jobs": len(rows),
        "planned_heavy_upper_bound": 12,
        "queue_wall_seconds": time.monotonic() - started,
        "all_roots_unchanged": before == after,
        "all_processes_clean": all(row["process"]["cleanup_ok"] and not row["process"]["orphan_process_tokens"] for row in rows),
        "provider_or_llm_calls": 0,
        "sealed_holdout_accesses": 0,
        "recommendations": recommendations,
        "rows": rows,
    }
    result = core | {"aggregate_sha256": object_hash(core)}
    write(args.out / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in ("jobs", "queue_wall_seconds", "all_roots_unchanged", "all_processes_clean", "recommendations")}, sort_keys=True))
    return 0 if result["all_roots_unchanged"] and result["all_processes_clean"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=6)
    value.add_argument("--timeout-seconds", type=float, default=3600.0)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
