#!/usr/bin/env python3
"""Run the 30 missing development M10 cells for WP-015 Gate 3."""

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
    OwnedProcessCancelled, OwnedProcessRunner, ProcessLimits,
)
from scripts.run_wp013_macro_panel import attempted_result  # noqa: E402


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def resolve(config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    source_paths = {
        "development_roots": REPO / config["development_roots"],
        "m8_actions": REPO / config["m8_actions"],
        "m10_extension": REPO / config["m10_extension"],
    }
    for key, path in source_paths.items():
        if file_hash(path) != config[key + "_sha256"]:
            raise ValueError(key + "_hash_mismatch")
    roots_source = read(source_paths["development_roots"])
    roots = {
        row["candidate_id"]: {
            "root_id": row["candidate_id"], "family": row["family"],
            "resource_class": row["resource_class"], "root_path": row["dcp"],
            "sha256": row["sha256"],
        } for row in roots_source["roots"]
        if row["candidate_id"] in set(config["fresh_root_ids"])
    }
    actions = []
    for row in read(source_paths["m8_actions"])["frozen_actions"]:
        actions.append({
            "action_key": row["action_key"], "flow_name": row["flow_name"],
            "heavy_call_upper_bound": int(row["heavy_call_range"][1]),
        })
    for row in read(source_paths["m10_extension"])["additional_macros"]:
        actions.append(dict(row))
    jobs = []
    for root_id in config["fresh_root_ids"]:
        for action in actions:
            jobs.append({
                "root_id": root_id, **action,
                "job_id": f"{root_id}__{action['flow_name']}",
                "action_config_sha256": object_hash(action),
            })
    if len(roots) != 3 or len(actions) != 10 or len(jobs) != config["expected_fresh_cells"]:
        raise ValueError("fresh_m10_cross_product_mismatch")
    return roots, jobs


async def run(args: argparse.Namespace) -> int:
    if args.out.exists():
        raise FileExistsError(args.out)
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    config = read(args.config)
    limits = config["limits"]
    if limits["provider_or_llm_calls"] or limits["transfer_or_holdout_optimization_calls"]:
        raise ValueError("gate3_firewall_not_zero")
    if not 1 <= args.max_parallel <= int(limits["max_parallel_weekday"]):
        raise ValueError("weekday_parallelism_exceeds_bound")
    roots, jobs = resolve(config)
    heavy_upper = sum(int(row["heavy_call_upper_bound"]) for row in jobs)
    if heavy_upper > int(limits["implementation_heavy_call_cap"]):
        raise ValueError("heavy_call_cap_exceeded")
    before = {root_id: file_hash(Path(row["root_path"])) for root_id, row in roots.items()}
    if any(before[root_id] != row["sha256"] for root_id, row in roots.items()):
        raise ValueError("fresh_root_hash_mismatch")
    args.out.mkdir(parents=True)
    write(args.out / "resolved-plan.json", {
        "schema_version": "dof.wp015-m10-fresh-plan.v1", "config_sha256": file_hash(args.config),
        "source_commit": git("rev-parse", "HEAD"), "source_tree": git("rev-parse", "HEAD^{tree}"),
        "roots": roots, "jobs": jobs, "planned_heavy_upper_bound": heavy_upper,
    })
    semaphore = asyncio.Semaphore(args.max_parallel)
    process_limits = ProcessLimits(
        initialization_timeout_seconds=60.0,
        whole_run_timeout_seconds=float(limits["job_timeout_seconds"]),
        graceful_close_seconds=20.0, terminate_grace_seconds=10.0,
        kill_grace_seconds=15.0, cleanup_timeout_seconds=60.0,
        output_limit_bytes=1024 * 1024,
    )
    started = time.monotonic()

    async def one(job: dict[str, Any]) -> dict[str, Any]:
        root = roots[job["root_id"]]
        job_dir = args.out / "jobs" / job["job_id"]
        command = (
            str(args.python), str(REPO / "scripts/run_flow_matrix.py"),
            root["root_path"], "--flows", job["flow_name"], "--out", str(job_dir),
            "--run-mode", "research-dev", "--skip-functional-validation",
        )
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        async with semaphore:
            try:
                process = await OwnedProcessRunner(process_limits).run(command, cwd=REPO, env=environment)
            except OwnedProcessCancelled as exc:
                process = exc.result
        attempt = attempted_result(job_dir, job["flow_name"])
        child = Path(attempt["output_dcp"]) if attempt.get("output_dcp") else None
        row = {
            **job, "root_sha256": root["sha256"],
            "process": process.to_dict(include_output_text=False), "attempt": attempt,
            "actual_child": str(child) if child and child.is_file() else None,
            "actual_child_sha256": file_hash(child) if child and child.is_file() else None,
            "provider_or_llm_calls": 0, "transfer_or_holdout_optimization_calls": 0,
        }
        write(job_dir / "wp015-result.json", row)
        return row

    rows = await asyncio.gather(*(one(job) for job in jobs))
    after = {root_id: file_hash(Path(row["root_path"])) for root_id, row in roots.items()}
    core = {
        "schema_version": "dof.wp015-m10-fresh-result.v1", "identity": config["identity"],
        "source_commit": git("rev-parse", "HEAD"), "source_tree": git("rev-parse", "HEAD^{tree}"),
        "config_sha256": file_hash(args.config), "planned_jobs": len(jobs),
        "completed_jobs": len(rows), "planned_heavy_upper_bound": heavy_upper,
        "queue_wall_seconds": time.monotonic() - started,
        "attempt_endpoint_counts": dict(collections.Counter(row["attempt"].get("canonical_endpoint") for row in rows)),
        "valid_children": sum(row["attempt"].get("canonical_endpoint") == "VALID_COMPLETION" and bool(row["actual_child"]) for row in rows),
        "all_roots_unchanged": before == after,
        "all_processes_clean": all(row["process"]["cleanup_ok"] and not row["process"]["orphan_process_tokens"] for row in rows),
        "provider_or_llm_calls": 0, "transfer_or_holdout_optimization_calls": 0,
        "rows": rows,
    }
    result = core | {"aggregate_sha256": object_hash(core)}
    write(args.out / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in (
        "completed_jobs", "attempt_endpoint_counts", "valid_children",
        "all_roots_unchanged", "all_processes_clean", "queue_wall_seconds",
    )}, sort_keys=True))
    return 0 if result["completed_jobs"] == 30 and result["all_roots_unchanged"] and result["all_processes_clean"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--config", type=Path, default=REPO / "configs/wp015/m10-development-closure-v1.json")
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=8)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
