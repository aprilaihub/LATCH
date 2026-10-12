#!/usr/bin/env python3
"""Run the bounded WP-014I 30-cell M10 reference closure and V1 children."""

from __future__ import annotations

import argparse
import asyncio
import collections
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
from scripts.run_wp014f_validation_recon import run_cases  # noqa: E402


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


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def resolve(config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    roots_manifest = REPO / config["development_roots_manifest"]
    m8_manifest = REPO / config["m8_manifest"]
    if file_hash(roots_manifest) != config["development_roots_manifest_sha256"]:
        raise ValueError("development_roots_manifest_hash_mismatch")
    if file_hash(m8_manifest) != config["m8_manifest_sha256"]:
        raise ValueError("m8_manifest_hash_mismatch")
    roots_source = read(roots_manifest)
    prefix = Path(roots_source["root_path_prefix"])
    roots = {
        row["root_id"]: {
            **row,
            "root_path": str(prefix / row["filename"]),
        }
        for row in roots_source["roots"]
    }
    fir = dict(config["fir_root"])
    roots[fir["root_id"]] = fir
    m8 = read(m8_manifest)["frozen_actions"]
    extra = config["additional_macros"]
    jobs = []
    for root_id in sorted(key for key in roots if key != fir["root_id"]):
        for action in extra:
            jobs.append({"root_id": root_id, **action})
    for action in [*m8, *extra]:
        jobs.append({
            "root_id": fir["root_id"],
            "action_key": action["action_key"],
            "flow_name": action["flow_name"],
            "heavy_call_upper_bound": int(
                action.get("heavy_call_upper_bound")
                or action.get("heavy_call_range", [0, 0])[1]
            ),
        })
    for row in jobs:
        row["job_id"] = f"{row['root_id']}__{row['flow_name']}"
    if len(jobs) != int(config["expected_jobs"]):
        raise ValueError("expected_job_count_mismatch")
    return roots, jobs


async def run(args: argparse.Namespace) -> int:
    if args.out.exists():
        raise FileExistsError(args.out)
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    config = read(args.config)
    limits = config["limits"]
    if limits["provider_or_llm_calls"] or limits["holdout_or_transfer_optimization_calls"]:
        raise ValueError("wp014i_macro_firewall_not_zero")
    if not 1 <= args.max_parallel <= int(limits["max_parallel"]):
        raise ValueError("macro_parallelism_exceeds_bound")
    roots, jobs = resolve(config)
    if len(jobs) > int(limits["fresh_macro_job_cap"]):
        raise ValueError("fresh_macro_job_cap_exceeded")
    planned_heavy = sum(int(row["heavy_call_upper_bound"]) for row in jobs)
    if planned_heavy > int(limits["implementation_heavy_call_cap"]):
        raise ValueError("implementation_heavy_call_cap_exceeded")
    before = {root_id: file_hash(Path(root["root_path"])) for root_id, root in roots.items()}
    if any(before[root_id] != root["sha256"] for root_id, root in roots.items()):
        raise ValueError("root_hash_mismatch")
    args.out.mkdir(parents=True)
    semaphore = asyncio.Semaphore(args.max_parallel)
    process_limits = ProcessLimits(
        initialization_timeout_seconds=60.0,
        whole_run_timeout_seconds=float(limits["job_timeout_seconds"]),
        graceful_close_seconds=20.0,
        terminate_grace_seconds=10.0,
        kill_grace_seconds=15.0,
        cleanup_timeout_seconds=60.0,
        output_limit_bytes=1024 * 1024,
    )
    started = time.monotonic()

    async def one(job: dict[str, Any]) -> dict[str, Any]:
        root = roots[job["root_id"]]
        job_dir = args.out / "jobs" / job["job_id"]
        command = (
            str(args.python), str(REPO / "scripts" / "run_flow_matrix.py"),
            str(root["root_path"]), "--flows", job["flow_name"],
            "--out", str(job_dir), "--run-mode", "research-dev",
            "--skip-functional-validation",
        )
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        async with semaphore:
            try:
                process = await OwnedProcessRunner(process_limits).run(
                    command, cwd=REPO, env=environment
                )
            except OwnedProcessCancelled as exc:
                process = exc.result
        attempt = attempted_result(job_dir, job["flow_name"])
        child = Path(attempt["output_dcp"]) if attempt.get("output_dcp") else None
        row = {
            **job,
            "root_sha256": root["sha256"],
            "process": process.to_dict(include_output_text=False),
            "attempt": attempt,
            "actual_child": str(child) if child and child.is_file() else None,
            "actual_child_sha256": file_hash(child) if child and child.is_file() else None,
            "provider_or_llm_calls": 0,
            "holdout_or_transfer_optimization_calls": 0,
        }
        write(job_dir / "wp014i-result.json", row)
        return row

    rows = await asyncio.gather(*(one(job) for job in jobs))
    valid = [
        row for row in rows
        if row["attempt"].get("canonical_endpoint") == "VALID_COMPLETION"
        and row["actual_child"]
    ]
    validation_plan = read(REPO / "configs" / "wp014f" / "validation-recon-v1.json")
    validation_plan["boundaries"]["per_case_timeout_seconds"] = int(
        limits["validation_timeout_seconds"]
    )
    cases = []
    for row in valid:
        root = roots[row["root_id"]]
        cases.append({
            "case_id": "m10__" + row["job_id"],
            "root_id": row["root_id"],
            "mechanism": row["flow_name"],
            "golden_dcp": root["root_path"],
            "revised_dcp": row["actual_child"],
            "golden_sha256": root["sha256"],
            "revised_sha256": row["actual_child_sha256"],
            "resolved_input_sha256": {
                "golden": root["sha256"], "revised": row["actual_child_sha256"]
            },
            "sealed_holdout_accesses": 0,
        })
    validation = run_cases(
        cases, validation_plan, args.out / "v1", "multiseed",
        min(int(limits["validation_parallel"]), len(cases)),
    ) if cases else None
    after = {root_id: file_hash(Path(root["root_path"])) for root_id, root in roots.items()}
    core = {
        "schema_version": "dof.wp014i-m10-reference-result.v1",
        "identity": config["identity"],
        "source_commit": git("rev-parse", "HEAD"),
        "config_sha256": file_hash(args.config),
        "planned_jobs": len(jobs),
        "completed_jobs": len(rows),
        "planned_heavy_upper_bound": planned_heavy,
        "queue_wall_seconds": time.monotonic() - started,
        "attempt_endpoint_counts": dict(collections.Counter(
            row["attempt"].get("canonical_endpoint") for row in rows
        )),
        "valid_children": len(valid),
        "v1_terminal_counts": (validation or {}).get("terminal_counts", {}),
        "all_roots_unchanged": before == after,
        "all_processes_clean": all(
            row["process"]["cleanup_ok"] and not row["process"]["orphan_process_tokens"]
            for row in rows
        ) and not (validation or {}).get("owned_process_survivors"),
        "provider_or_llm_calls": 0,
        "holdout_or_transfer_optimization_calls": 0,
        "rows": rows,
        "validation": validation,
    }
    result = core | {"aggregate_sha256": object_hash(core)}
    write(args.out / "aggregate.json", result)
    print(json.dumps({
        key: result[key] for key in (
            "completed_jobs", "attempt_endpoint_counts", "valid_children",
            "v1_terminal_counts", "all_roots_unchanged", "all_processes_clean",
        )
    }, sort_keys=True))
    return 0 if result["all_roots_unchanged"] and result["all_processes_clean"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--config", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=8)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
