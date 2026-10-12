#!/usr/bin/env python3
"""Run the frozen non-development 11 x M10 panel after agent termination."""

from __future__ import annotations

import argparse
import asyncio
import collections
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Mapping
from zoneinfo import ZoneInfo


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

from optimizer_pipeline.dof.processes import OwnedProcessCancelled, OwnedProcessRunner, ProcessLimits  # noqa: E402
from optimizer_pipeline.dof.single_submission.macro_authority import MACRO_DEFINITIONS  # noqa: E402
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


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def _roots(bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    roots = {
        root_id: dict(row) for root_id, row in bundle["roots"].items()
        if row["split"] != "DEVELOPMENT_KNOWN"
    }
    if len(roots) != 11:
        raise ValueError("wp015_post_agent_root_count_not_11")
    for root_id, row in roots.items():
        path = Path(row["root_path"])
        if not path.is_file() or file_sha256(path) != row["sha256"]:
            raise ValueError("wp015_reference_root_hash_mismatch:" + root_id)
    return roots


def _agent_firewall(scientific: Mapping[str, Any], roots: Mapping[str, Any]) -> None:
    rows = scientific.get("jobs", [])
    if scientific.get("job_count") != 1134 or len(rows) != 1134:
        raise ValueError("wp015_scientific_matrix_not_complete_before_reference")
    for root_id in roots:
        root_rows = [row for row in rows if row["root_id"] == root_id]
        if len(root_rows) != 42 or any(not row.get("canonical_terminal") for row in root_rows):
            raise ValueError("wp015_agent_cells_not_terminal_before_reference:" + root_id)


def _actual_heavy_calls(job_dir: Path, flow: str) -> tuple[int, dict[str, int]]:
    counts = {"place_design": 0, "route_design": 0, "phys_opt_design": 0}
    log = job_dir / "logs" / f"01_{flow}.log"
    if log.is_file():
        pattern = re.compile(r"\[VIVADO\] Calling (place_design|route_design|phys_opt_design) with args:")
        for operation in pattern.findall(log.read_text(errors="replace")):
            counts[operation] += 1
    return sum(counts.values()), counts


async def run(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise FileExistsError(args.output)
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    local_now = datetime.now(ZoneInfo("Europe/London"))
    launch_limit = 16 if local_now.weekday() >= 5 else 8
    if not 1 <= args.max_parallel <= launch_limit:
        raise ValueError("wp015_reference_parallelism_exceeds_day_policy")
    bundle, scientific = read(args.bundle), read(args.scientific)
    roots = _roots(bundle)
    _agent_firewall(scientific, roots)
    actions = list(MACRO_DEFINITIONS.values())
    if len(actions) != 10:
        raise ValueError("wp015_reference_authority_not_m10")
    jobs = [
        {
            "job_id": f"wp015-ref__{root_id}__{action.flow_name}",
            "root_id": root_id, "resource_class": roots[root_id]["resource_class"],
            "action_key": action.action_key, "flow_name": action.flow_name,
            "reserved_heavy_calls": action.heavy_call_cost,
        }
        for root_id in roots for action in actions
    ]
    if len(jobs) != 110 or len({row["job_id"] for row in jobs}) != 110:
        raise ValueError("wp015_reference_inventory_not_110")
    before = {root_id: file_sha256(Path(row["root_path"])) for root_id, row in roots.items()}
    args.output.mkdir(parents=True)
    limits = ProcessLimits(
        initialization_timeout_seconds=60.0, whole_run_timeout_seconds=7200.0,
        graceful_close_seconds=20.0, terminate_grace_seconds=10.0,
        kill_grace_seconds=15.0, cleanup_timeout_seconds=60.0,
        output_limit_bytes=1024 * 1024,
    )
    semaphore = asyncio.Semaphore(args.max_parallel)
    started = time.monotonic()

    async def one(job: Mapping[str, Any]) -> dict[str, Any]:
        job_dir = args.output / "jobs" / job["job_id"]
        command = (
            str(args.python), str(REPO / "scripts/run_flow_matrix.py"),
            roots[job["root_id"]]["root_path"], "--flows", job["flow_name"],
            "--out", str(job_dir), "--run-mode", "research-dev",
            "--skip-functional-validation",
        )
        async with semaphore:
            try:
                process = await OwnedProcessRunner(limits).run(command, cwd=REPO, env=dict(os.environ))
            except OwnedProcessCancelled as exc:
                process = exc.result
        attempt = attempted_result(job_dir, job["flow_name"])
        actual_heavy, heavy_counts = _actual_heavy_calls(job_dir, job["flow_name"])
        child = Path(attempt["output_dcp"]) if attempt.get("output_dcp") else None
        physical = attempt.get("canonical_endpoint")
        result = dict(job) | {
            "canonical_terminal": physical, "physical_terminal": physical,
            "attempt": attempt, "execution": attempt,
            "process": process.to_dict(include_output_text=False),
            "resolved_child_path": str(child) if child and child.is_file() else None,
            "resolved_child_sha256": file_sha256(child) if child and child.is_file() else None,
            "selected_language": "M", "functional_terminal": (
                "PENDING_V1" if physical == "VALID_COMPLETION" and child and child.is_file() else None
            ),
            "provider_requests": 0, "cost_usd": 0.0,
            "actual_heavy_calls": actual_heavy,
            "actual_heavy_call_counts": heavy_counts,
            "heavy_reservation_respected": actual_heavy <= int(job["reserved_heavy_calls"]),
        }
        write(job_dir / "cell-result.json", result)
        return result

    rows = await asyncio.gather(*(one(job) for job in jobs))
    after = {root_id: file_sha256(Path(row["root_path"])) for root_id, row in roots.items()}
    core = {
        "schema_version": "dof.wp015-post-agent-reference-physical.v1",
        "protocol_id": bundle["protocol_id"], "bundle_sha256": bundle["bundle_sha256"],
        "shared_scientific_core_hash": bundle["shared_scientific_core_hash"],
        "scientific_source_commit": bundle["repository"]["head_commit"],
        "scientific_source_tree": bundle["repository"]["head_tree"],
        "job_count": len(rows), "terminal_counts": dict(sorted(collections.Counter(
            row["canonical_terminal"] for row in rows
        ).items())),
        "actual_heavy_calls": sum(int(row["actual_heavy_calls"]) for row in rows),
        "physical_valid_children": sum(row.get("functional_terminal") == "PENDING_V1" for row in rows),
        "all_roots_unchanged": before == after,
        "all_processes_clean": all(row["process"]["cleanup_ok"] and not row["process"]["orphan_process_tokens"] for row in rows),
        "all_heavy_reservations_respected": all(row["heavy_reservation_respected"] for row in rows),
        "provider_requests": 0, "api_cost_usd": 0.0,
        "queue_wall_seconds": time.monotonic() - started, "jobs": rows,
    }
    result = core | {"aggregate_sha256": digest(core)}
    write(args.output / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in (
        "job_count", "terminal_counts", "physical_valid_children",
        "all_roots_unchanged", "all_processes_clean", "queue_wall_seconds",
    )}, sort_keys=True))
    return 0 if result["all_roots_unchanged"] and result["all_processes_clean"] and result["all_heavy_reservations_respected"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--scientific", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=8)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
