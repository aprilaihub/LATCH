#!/usr/bin/env python3
"""Resume the frozen WP-015 post-agent M10 panel without rerunning terminals.

The frozen launcher is intentionally one-shot.  This continuation wrapper keeps
its job construction, command, timeout and result normalization unchanged while
adding durable per-job recovery after an external controller interruption.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping


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


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def load_frozen_launcher(scientific_repo: Path) -> Any:
    source = scientific_repo / "scripts" / "run_wp015_post_agent_references.py"
    spec = importlib.util.spec_from_file_location("wp015_frozen_reference_launcher", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("frozen_reference_launcher_not_importable")
    sys.path[:0] = [str(scientific_repo), str(scientific_repo / "scripts")]
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def owned_survivors(markers: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    own_pid = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            status = (entry / "status").read_text(errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
        if uid_line and int(uid_line.split()[1]) == os.getuid() and any(marker in command for marker in markers):
            rows.append({"pid": int(entry.name), "command_sha256": hashlib.sha256(command.encode()).hexdigest()})
    return sorted(rows, key=lambda row: row["pid"])


def frozen_jobs(module: Any, bundle: Mapping[str, Any], scientific: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    roots = module._roots(bundle)
    module._agent_firewall(scientific, roots)
    actions = list(module.MACRO_DEFINITIONS.values())
    if len(actions) != 10:
        raise ValueError("wp015_reference_authority_not_m10")
    jobs = [
        {
            "job_id": f"wp015-ref__{root_id}__{action.flow_name}",
            "root_id": root_id,
            "resource_class": roots[root_id]["resource_class"],
            "action_key": action.action_key,
            "flow_name": action.flow_name,
            "reserved_heavy_calls": action.heavy_call_cost,
        }
        for root_id in roots for action in actions
    ]
    if len(jobs) != 110 or len({row["job_id"] for row in jobs}) != 110:
        raise ValueError("wp015_reference_inventory_not_110")
    return roots, jobs


def validate_existing_result(job: Mapping[str, Any], path: Path, module: Any) -> dict[str, Any]:
    row = read(path)
    for key in ("job_id", "root_id", "action_key", "flow_name", "reserved_heavy_calls"):
        if row.get(key) != job[key]:
            raise ValueError(f"existing_reference_identity_mismatch:{job['job_id']}:{key}")
    if row.get("provider_requests") != 0 or row.get("cost_usd") != 0.0:
        raise ValueError("existing_reference_provider_accounting_mismatch:" + job["job_id"])
    child = row.get("resolved_child_path")
    if child and (not Path(child).is_file() or file_sha256(Path(child)) != row.get("resolved_child_sha256")):
        raise ValueError("existing_reference_child_hash_mismatch:" + job["job_id"])
    actual, counts = module._actual_heavy_calls(path.parent, job["flow_name"])
    if actual != row.get("actual_heavy_calls") or counts != row.get("actual_heavy_call_counts"):
        raise ValueError("existing_reference_heavy_accounting_mismatch:" + job["job_id"])
    return row


def completed_artifact_paths(job_dir: Path, flow: str) -> list[Path]:
    paths = [
        job_dir / "flow_matrix_summary.json",
        job_dir / "flow_matrix_manifest.json",
        job_dir / "status" / f"01_{flow}.status.json",
        job_dir / "runs" / f"01_{flow}" / "result_manifest.json",
    ]
    return paths if all(path.is_file() for path in paths) else []


def recover_completed_result(
    job: Mapping[str, Any], job_dir: Path, module: Any, *, survivor_audit: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifacts = completed_artifact_paths(job_dir, job["flow_name"])
    if not artifacts:
        raise ValueError("reference_artifacts_not_complete:" + job["job_id"])
    attempt = module.attempted_result(job_dir, job["flow_name"])
    actual_heavy, heavy_counts = module._actual_heavy_calls(job_dir, job["flow_name"])
    child = Path(attempt["output_dcp"]) if attempt.get("output_dcp") else None
    physical = attempt.get("canonical_endpoint")
    summary = read(job_dir / "flow_matrix_summary.json")
    flow_rows = summary.get("flows", [])
    if len(flow_rows) != 1 or flow_rows[0].get("flow") != job["flow_name"]:
        raise ValueError("reference_summary_not_single_expected_flow:" + job["job_id"])
    flow_row = flow_rows[0]
    artifact_rows = [
        {"path": str(path), "sha256": file_sha256(path), "bytes": path.stat().st_size}
        for path in artifacts
    ]
    if child and child.is_file():
        artifact_rows.append({"path": str(child), "sha256": file_sha256(child), "bytes": child.stat().st_size})
    receipt_core = {
        "schema_version": "dof.wp015c-reference-controller-recovery.v1",
        "job": dict(job),
        "classification": "BACKEND_COMPLETE_CELL_RECORD_INTERRUPTED",
        "artifacts": artifact_rows,
        "survivor_audit": survivor_audit,
        "recovered_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    receipt = receipt_core | {"receipt_sha256": digest(receipt_core)}
    process = {
        "schema_version": "dof.wp015c-recovered-process-evidence.v1",
        "runner_version": "wp015c.post-agent-reference-resume.v1",
        "endpoint": "RECOVERED_COMPLETED_AFTER_CONTROLLER_LOSS",
        "returncode": flow_row.get("exit_code"),
        "duration_seconds": flow_row.get("duration_seconds"),
        "command_hash": digest(flow_row.get("command", [])),
        "session_owned": True,
        "cleanup_ok": not survivor_audit,
        "cleanup_errors": [],
        "observed_process_tokens": [],
        "orphan_process_tokens": [],
        "recovery_receipt_sha256": receipt["receipt_sha256"],
    }
    result = dict(job) | {
        "canonical_terminal": physical,
        "physical_terminal": physical,
        "attempt": attempt,
        "execution": attempt,
        "process": process,
        "resolved_child_path": str(child) if child and child.is_file() else None,
        "resolved_child_sha256": file_sha256(child) if child and child.is_file() else None,
        "selected_language": "M",
        "functional_terminal": "PENDING_V1" if physical == "VALID_COMPLETION" and child and child.is_file() else None,
        "provider_requests": 0,
        "cost_usd": 0.0,
        "actual_heavy_calls": actual_heavy,
        "actual_heavy_call_counts": heavy_counts,
        "heavy_reservation_respected": actual_heavy <= int(job["reserved_heavy_calls"]),
        "controller_recovery": {
            "classification": receipt["classification"],
            "receipt_sha256": receipt["receipt_sha256"],
        },
    }
    return result, receipt


async def run(args: argparse.Namespace) -> int:
    if not args.output.is_dir():
        raise FileNotFoundError("reference_output_not_found:" + str(args.output))
    if git(args.scientific_repo, "status", "--porcelain"):
        raise RuntimeError("scientific_repository_must_be_clean")
    bundle, scientific = read(args.bundle), read(args.scientific)
    if git(args.scientific_repo, "rev-parse", "HEAD") != bundle["repository"]["head_commit"]:
        raise ValueError("scientific_source_commit_mismatch")
    if git(args.scientific_repo, "rev-parse", "HEAD^{tree}") != bundle["repository"]["head_tree"]:
        raise ValueError("scientific_source_tree_mismatch")
    # D-WP015C-004: user-authorised reference-collection-only amendment.
    # Seed validation remains capped at the frozen value of 16.
    launch_limit = 20
    if not 1 <= args.max_parallel <= launch_limit:
        raise ValueError("wp015_reference_parallelism_exceeds_day_policy")
    module = load_frozen_launcher(args.scientific_repo)
    roots, jobs = frozen_jobs(module, bundle, scientific)
    before = {root_id: file_sha256(Path(row["root_path"])) for root_id, row in roots.items()}
    markers = [str(args.output), str(args.scientific_repo)]
    entry_survivors = owned_survivors(markers)
    if entry_survivors:
        raise RuntimeError("owned_reference_processes_survived_at_resume_entry")

    rows: dict[str, dict[str, Any]] = {}
    recovered_ids: list[str] = []
    pending_recoveries: list[tuple[Mapping[str, Any], Path, dict[str, Any], dict[str, Any]]] = []
    never_started: list[dict[str, Any]] = []
    entry_jobs: list[dict[str, Any]] = []
    for job in jobs:
        job_dir = args.output / "jobs" / job["job_id"]
        cell_path = job_dir / "cell-result.json"
        if cell_path.is_file():
            row = validate_existing_result(job, cell_path, module)
            rows[job["job_id"]] = row
            entry_jobs.append({"job_id": job["job_id"], "state": "CELL_RESULT_DURABLE", "sha256": file_sha256(cell_path)})
        elif job_dir.is_dir() and completed_artifact_paths(job_dir, job["flow_name"]):
            row, receipt = recover_completed_result(job, job_dir, module, survivor_audit=entry_survivors)
            pending_recoveries.append((job, cell_path, row, receipt))
            recovered_ids.append(job["job_id"])
            entry_jobs.append({"job_id": job["job_id"], "state": "BACKEND_COMPLETE_CELL_RECORD_RECOVERED", "receipt_sha256": receipt["receipt_sha256"]})
        elif job_dir.exists():
            raise RuntimeError("partial_reference_job_requires_explicit_audit:" + job["job_id"])
        else:
            never_started.append(job)
            entry_jobs.append({"job_id": job["job_id"], "state": "NOT_STARTED"})
    entry_core = {
        "schema_version": "dof.wp015c-reference-resume-entry.v1",
        "scientific_source_commit": bundle["repository"]["head_commit"],
        "scientific_source_tree": bundle["repository"]["head_tree"],
        "frozen_launcher_sha256": file_sha256(args.scientific_repo / "scripts" / "run_wp015_post_agent_references.py"),
        "root_sha256": before,
        "entry_survivors": entry_survivors,
        "jobs": entry_jobs,
        "durable_count": len(rows),
        "recovered_count": len(recovered_ids),
        "not_started_count": len(never_started),
        "reference_collection_parallelism": args.max_parallel,
        "parallelism_authority": "D-WP015C-004_USER_AUTHORISED_REFERENCE_ONLY",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    audit_dir = args.output / "resume-entry-audits"
    audit_sequence = len(list(audit_dir.glob("entry-*.json"))) + 1 if audit_dir.is_dir() else 1
    write(audit_dir / f"entry-{audit_sequence:03d}.json", entry_core | {"audit_sha256": digest(entry_core)})
    for job, cell_path, row, receipt in pending_recoveries:
        write(args.output / "recovery-receipts" / f"{job['job_id']}.json", receipt)
        write(cell_path, row)
        rows[job["job_id"]] = row

    limits = module.ProcessLimits(
        initialization_timeout_seconds=60.0,
        whole_run_timeout_seconds=7200.0,
        graceful_close_seconds=20.0,
        terminate_grace_seconds=10.0,
        kill_grace_seconds=15.0,
        cleanup_timeout_seconds=60.0,
        output_limit_bytes=1024 * 1024,
    )
    semaphore = asyncio.Semaphore(args.max_parallel)
    started = time.monotonic()

    async def one(job: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        job_dir = args.output / "jobs" / job["job_id"]
        command = (
            str(args.python), str(args.scientific_repo / "scripts" / "run_flow_matrix.py"),
            roots[job["root_id"]]["root_path"], "--flows", job["flow_name"],
            "--out", str(job_dir), "--run-mode", "research-dev", "--skip-functional-validation",
        )
        async with semaphore:
            write(args.output / "launch-receipts" / f"{job['job_id']}.json", {
                "schema_version": "dof.wp015c-reference-launch.v1",
                "job": dict(job), "command_sha256": digest(command),
                "launched_at_utc": datetime.now(timezone.utc).isoformat(),
            })
            try:
                process = await module.OwnedProcessRunner(limits).run(command, cwd=args.scientific_repo, env=dict(os.environ))
            except module.OwnedProcessCancelled as exc:
                process = exc.result
        attempt = module.attempted_result(job_dir, job["flow_name"])
        actual_heavy, heavy_counts = module._actual_heavy_calls(job_dir, job["flow_name"])
        child = Path(attempt["output_dcp"]) if attempt.get("output_dcp") else None
        physical = attempt.get("canonical_endpoint")
        result = dict(job) | {
            "canonical_terminal": physical, "physical_terminal": physical,
            "attempt": attempt, "execution": attempt,
            "process": process.to_dict(include_output_text=False),
            "resolved_child_path": str(child) if child and child.is_file() else None,
            "resolved_child_sha256": file_sha256(child) if child and child.is_file() else None,
            "selected_language": "M",
            "functional_terminal": "PENDING_V1" if physical == "VALID_COMPLETION" and child and child.is_file() else None,
            "provider_requests": 0, "cost_usd": 0.0,
            "actual_heavy_calls": actual_heavy,
            "actual_heavy_call_counts": heavy_counts,
            "heavy_reservation_respected": actual_heavy <= int(job["reserved_heavy_calls"]),
        }
        write(job_dir / "cell-result.json", result)
        return job["job_id"], result

    for job_id, row in await asyncio.gather(*(one(job) for job in never_started)):
        rows[job_id] = row
    ordered = [rows[job["job_id"]] for job in jobs]
    after = {root_id: file_sha256(Path(row["root_path"])) for root_id, row in roots.items()}
    final_survivors_1 = owned_survivors(markers)
    time.sleep(2)
    final_survivors_2 = owned_survivors(markers)
    core = {
        "schema_version": "dof.wp015-post-agent-reference-physical.v1",
        "protocol_id": bundle["protocol_id"], "bundle_sha256": bundle["bundle_sha256"],
        "shared_scientific_core_hash": bundle["shared_scientific_core_hash"],
        "scientific_source_commit": bundle["repository"]["head_commit"],
        "scientific_source_tree": bundle["repository"]["head_tree"],
        "job_count": len(ordered),
        "terminal_counts": dict(sorted(collections.Counter(row["canonical_terminal"] for row in ordered).items())),
        "actual_heavy_calls": sum(int(row["actual_heavy_calls"]) for row in ordered),
        "physical_valid_children": sum(row.get("functional_terminal") == "PENDING_V1" for row in ordered),
        "all_roots_unchanged": before == after,
        "all_processes_clean": all(row["process"]["cleanup_ok"] and not row["process"]["orphan_process_tokens"] for row in ordered) and not final_survivors_1 and not final_survivors_2,
        "all_heavy_reservations_respected": all(row["heavy_reservation_respected"] for row in ordered),
        "provider_requests": 0, "api_cost_usd": 0.0,
        "queue_wall_seconds": time.monotonic() - started,
        "controller_resume": {
            "durable_reused": len(ordered) - len(recovered_ids) - len(never_started),
            "completed_artifacts_recovered": len(recovered_ids),
            "freshly_executed": len(never_started),
            "reference_collection_parallelism": args.max_parallel,
            "parallelism_authority": "D-WP015C-004_USER_AUTHORISED_REFERENCE_ONLY",
            "recovered_job_ids": recovered_ids,
            "final_survivor_audit_1": final_survivors_1,
            "final_survivor_audit_2": final_survivors_2,
        },
        "jobs": ordered,
    }
    result = core | {"aggregate_sha256": digest(core)}
    write(args.output / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in (
        "job_count", "terminal_counts", "physical_valid_children", "actual_heavy_calls",
        "all_roots_unchanged", "all_processes_clean", "queue_wall_seconds", "controller_resume",
    )}, sort_keys=True))
    return 0 if result["all_roots_unchanged"] and result["all_processes_clean"] and result["all_heavy_reservations_respected"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--scientific", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--scientific-repo", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=8)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
