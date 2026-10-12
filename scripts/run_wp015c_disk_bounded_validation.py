#!/usr/bin/env python3
"""Disk-bounded, resume-safe orchestration for the frozen WP-015 V1 validator.

This module deliberately lives outside the WP-015 validation scientific core.
It calls the frozen validator and seed semantic functions without changing them.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.seed_validation import (  # noqa: E402
    aggregate_seed_reports,
    canonical_hash,
    file_sha256,
    semantic_seed_result,
    tree_manifest,
)
from optimizer_pipeline.dof.validation_scheduler import (  # noqa: E402
    GIB,
    MemoryAwareSeedScheduler,
    ValidationSeed,
    linux_memory_bytes,
)
from scripts.run_wp014f_validation_recon import WORKDIR_RE  # noqa: E402
from scripts.run_wp015_seed_parallel_canary import (  # noqa: E402
    owned_survivors,
    terminate_process_group,
)
from scripts.run_wp015_seed_validation_batch import (  # noqa: E402
    _load_seed_result,
    _run_seed_unit,
)


EXPECTED_PLAN_HASH = "486bf03f1493efcd8bd907d33c7aa19dbd6cde3d5100c1603cab12876e3e094b"
EXPECTED_SOURCE_COMMIT = "32d852a702c4386f71322c4c260baf99d43dd7d4"
EXPECTED_SOURCE_TREE = "621d2748e96dadb69c456f8bdb6107a96867ec88"
EXPECTED_SHARED_CORE = "3302733b869645a3f10225f56c870577ef6d5ee6ee673c65112e5ddb42d1b570"
EXPECTED_VALIDATION_CORE = "1af9a6de7395040ee82bf6c6aa8e4e29cfe4635447fd2cfe2e4f7a25b2068076"
VALIDATION_CORE_FILES = (
    "validate_dcps.py",
    "optimizer_pipeline/dof/seed_validation.py",
    "optimizer_pipeline/dof/validation_scheduler.py",
    "scripts/run_wp015_seed_parallel_canary.py",
    "scripts/run_wp015_seed_validation_batch.py",
    "scripts/finalize_wp015_cell_validation.py",
)
HARD_CEILING = 300 * GIB
REFILL_CEILING = 220 * GIB
WAVE_TARGET = 64 * GIB
MINIMUM_FREE = 50 * GIB
RAM_RESERVE_FRACTION = 0.20
MAX_SEED_CONCURRENCY = 16
MAX_PREPARATION_CONCURRENCY = 8


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def logical_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    return total


def validation_core(scientific_repo: Path) -> tuple[str, list[dict[str, str]]]:
    rows = [
        {"path": relative, "sha256": file_sha256(scientific_repo / relative)}
        for relative in VALIDATION_CORE_FILES
    ]
    return canonical_hash(rows), rows


def owned_validator_directories(scientific_repo: Path) -> list[Path]:
    return sorted(
        path.resolve()
        for path in scientific_repo.glob("dcp_validation_*")
        if path.is_dir()
    )


def owned_transient_bytes(scientific_repo: Path, output: Path) -> int:
    total = sum(logical_bytes(path) for path in owned_validator_directories(scientific_repo))
    seed_root = output / "seeds"
    if seed_root.exists():
        total += sum(logical_bytes(path) for path in seed_root.glob("**/workspace") if path.is_dir())
    return total


def verify_owned_validator_path(path: Path, scientific_repo: Path) -> Path:
    resolved = path.resolve()
    root = scientific_repo.resolve()
    if resolved.parent != root or not resolved.name.startswith("dcp_validation_"):
        raise RuntimeError("refuse_unowned_validator_path:" + str(path))
    return resolved


def verify_inputs(case: dict[str, Any]) -> dict[str, str]:
    observed = {
        "golden": file_sha256(Path(case["golden_dcp"])),
        "revised": file_sha256(Path(case["revised_dcp"])),
    }
    if observed != case["resolved_input_sha256"]:
        raise RuntimeError("validation_input_hash_mismatch:" + case["case_id"])
    return observed


def verify_entry(
    *, plan: dict[str, Any], entry: dict[str, Any], scientific_repo: Path,
) -> dict[str, Any]:
    core_hash, core_rows = validation_core(scientific_repo)
    checks = {
        "plan_sha256": plan.get("plan_sha256") == EXPECTED_PLAN_HASH,
        "plan_source_commit": plan.get("repository", {}).get("head") == EXPECTED_SOURCE_COMMIT,
        "plan_source_tree": plan.get("repository", {}).get("tree") == EXPECTED_SOURCE_TREE,
        "plan_shared_core": plan.get("shared_scientific_core_hash") == EXPECTED_SHARED_CORE,
        "scientific_commit": git(scientific_repo, "rev-parse", "HEAD") == EXPECTED_SOURCE_COMMIT,
        "scientific_tree": git(scientific_repo, "rev-parse", "HEAD^{tree}") == EXPECTED_SOURCE_TREE,
        "scientific_clean": not bool(git(scientific_repo, "status", "--porcelain")),
        "validation_scientific_core": core_hash == EXPECTED_VALIDATION_CORE,
        "entry_audit_plan_count": entry.get("plan_case_count") == 634,
        "entry_audit_inputs": entry.get("all_input_hashes_match") is True,
        "entry_audit_seed_count": entry.get("completed_seed_units_at_entry") == 0,
        "entry_audit_hash": entry.get("entry_audit_sha256") == canonical_hash(
            {key: value for key, value in entry.items() if key != "entry_audit_sha256"}
        ),
        "zero_provider_calls": plan.get("provider_or_llm_calls") == 0,
        "zero_optimisation_calls": plan.get("optimization_calls") == 0,
        "zero_transfer_accesses": plan.get("transfer_or_holdout_accesses") == 0,
    }
    if not all(checks.values()):
        raise RuntimeError("wp015c_entry_verification_failed:" + json.dumps(checks, sort_keys=True))
    return {"checks": checks, "validation_scientific_core_hash": core_hash,
            "validation_scientific_core_files": core_rows}


def cleanup_interrupted_entry(
    *, plan_by_case: dict[str, dict[str, Any]], entry: dict[str, Any],
    scientific_repo: Path, output: Path,
) -> dict[str, Any]:
    receipt_path = output / "wp015c" / "entry-interrupted-cleanup.json"
    if receipt_path.is_file():
        receipt = read(receipt_path)
        if receipt.get("entry_audit_sha256") != entry["entry_audit_sha256"]:
            raise RuntimeError("interrupted_cleanup_entry_hash_mismatch")
        return receipt
    rows = [row for row in entry["cases"] if row["status"] == "PREPARATION_INTERRUPTED"]
    if len(rows) != 36:
        raise RuntimeError("interrupted_entry_count_mismatch")
    events = []
    for row in sorted(rows, key=lambda value: value["case_id"]):
        case_id = row["case_id"]
        if case_id not in plan_by_case:
            raise RuntimeError("interrupted_case_not_in_plan:" + case_id)
        validator = row.get("validator_directory")
        validator_path = verify_owned_validator_path(Path(validator), scientific_repo) if validator else None
        bytes_before = logical_bytes(validator_path) if validator_path and validator_path.exists() else 0
        metadata = output / "prepared" / case_id
        archive = output / "wp015c" / "interrupted-entry" / case_id
        event = {
            "case_id": case_id,
            "validator_directory": str(validator_path) if validator_path else None,
            "validator_bytes_before": bytes_before,
            "metadata_path": str(metadata),
            "archive_path": str(archive),
            "entry_audit_sha256": entry["entry_audit_sha256"],
        }
        write(output / "wp015c" / "cleanup-intents" / f"entry-{case_id}.json", event)
        if validator_path and validator_path.exists():
            shutil.rmtree(validator_path)
        if metadata.exists():
            archive.parent.mkdir(parents=True, exist_ok=True)
            if archive.exists():
                raise RuntimeError("interrupted_archive_already_exists_without_receipt:" + case_id)
            shutil.move(str(metadata), str(archive))
        event.update({
            "validator_deleted": not bool(validator_path and validator_path.exists()),
            "metadata_archived": archive.is_dir(),
            "completed_epoch_seconds": time.time(),
        })
        if not event["validator_deleted"] or not event["metadata_archived"]:
            raise RuntimeError("interrupted_cleanup_incomplete:" + case_id)
        events.append(event)
    receipt = {
        "schema_version": "dof.wp015c-interrupted-cleanup.v1",
        "entry_audit_sha256": entry["entry_audit_sha256"],
        "event_count": len(events), "events": events,
        "reclaimed_logical_bytes": sum(row["validator_bytes_before"] for row in events),
    }
    receipt["receipt_sha256"] = canonical_hash(receipt)
    write(receipt_path, receipt)
    return receipt


def prepare_case_frozen(
    case: dict[str, Any], plan: dict[str, Any], output: Path, scientific_repo: Path,
) -> dict[str, Any]:
    case_dir = output / "prepared" / case["case_id"]
    if case_dir.exists():
        result_path = case_dir / "prepare-result.json"
        if result_path.is_file():
            result = read(result_path)
            workspace = Path(result["prepared_workspace"])
            observed = canonical_hash(tree_manifest(workspace)) if workspace.is_dir() else None
            if (
                result.get("case", {}).get("resolved_input_sha256") == case["resolved_input_sha256"]
                and observed == result.get("prepared_workspace_manifest_sha256")
            ):
                verify_inputs(case)
                return result | {"wp015c_reused": True}
        raise RuntimeError("unresolved_existing_preparation:" + case["case_id"])
    case_dir.mkdir(parents=True, exist_ok=False)
    log_path = case_dir / "validator.log"
    seeds = [int(value) for value in plan["functional"]["seeds"]]
    command = [
        sys.executable, str(scientific_repo / "validate_dcps.py"),
        case["golden_dcp"], case["revised_dcp"],
        "--vectors", str(plan["functional"]["vectors_per_seed"]),
        "--precheck-vectors", str(plan["functional"]["precheck_vectors"]),
        "--prepare-seed-workspace-only",
    ]
    for seed in seeds:
        command.extend(["--seed", hex(seed)])
    before = verify_inputs(case)
    started = time.time()
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command, cwd=scientific_repo, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, env=os.environ.copy(),
        )
        try:
            returncode = process.wait(timeout=int(plan["limits"]["per_case_timeout_seconds"]))
            cleanup = []
        except subprocess.TimeoutExpired:
            cleanup = terminate_process_group(process)
            returncode = 124
    log_text = log_path.read_text(errors="replace")
    matches = WORKDIR_RE.findall(log_text)
    validator_dir = Path(matches[-1]) if matches else None
    report_path = validator_dir / "validation_report.json" if validator_dir else None
    report = read(report_path) if report_path and report_path.is_file() else None
    if (
        returncode != 0 or report is None or not report.get("phase1_passed")
        or not report.get("seed_workspace_prepared")
        or not (report.get("simulation_report") or {}).get("precheck_passed")
    ):
        terminal = "VALIDATOR_PREPARATION_FAILURE"
        reason = "preparation_exception_without_complete_validator_report"
        if isinstance(report, dict):
            simulation = report.get("simulation_report") or {}
            if report.get("phase1_passed") is not True:
                terminal, reason = "FUNCTIONAL_VALIDATION_STRUCTURAL_FAILURE", "validator_phase1_failed"
            elif simulation.get("precheck_passed") is False:
                if simulation.get("infrastructure_failure"):
                    terminal = "FUNCTIONAL_VALIDATION_INFRASTRUCTURE_FAILURE"
                    reason = str(simulation.get("infrastructure_reason") or "precheck_infrastructure_failure")
                else:
                    terminal, reason = "FUNCTIONAL_PRECHECK_MISMATCH", "precheck_completed_with_functional_mismatch"
        failure = {
            "case": case, "terminal": terminal, "terminal_reason": reason,
            "returncode": returncode, "validator_directory": str(validator_dir) if validator_dir else None,
            "validator_report": report, "input_sha256_before": before,
            "input_sha256_after": verify_inputs(case), "cleanup_events": cleanup,
            "wall_seconds": time.time() - started,
        }
        write(case_dir / "prepare-failure.json", failure)
        return failure
    workspace = Path(report["simulation_report"]["prepared_seed_workspace"])
    manifest = tree_manifest(workspace)
    write(case_dir / "prepared-workspace-manifest.json", manifest)
    result = {
        "case": case, "returncode": returncode, "wall_seconds": time.time() - started,
        "validator_directory": str(validator_dir), "prepared_workspace": str(workspace),
        "prepared_workspace_manifest_sha256": canonical_hash(manifest),
        "prepared_workspace_file_count": len(manifest),
        "prepared_workspace_bytes": sum(int(row.get("bytes", 0)) for row in manifest),
        "validator_report": report, "input_sha256_before": before,
        "input_sha256_after": verify_inputs(case), "cleanup_events": cleanup,
        "wp015c_reused": False,
    }
    write(case_dir / "prepare-result.json", result)
    return result


def reusable_preparation(
    case: dict[str, Any], output: Path, entry_row: dict[str, Any],
) -> dict[str, Any]:
    result = read(output / "prepared" / case["case_id"] / "prepare-result.json")
    workspace = Path(result["prepared_workspace"])
    if not workspace.is_dir():
        raise RuntimeError("retained_workspace_missing:" + case["case_id"])
    checks = entry_row.get("checks", {})
    if entry_row.get("status") != "PREPARED_REUSABLE" or not checks or not all(checks.values()):
        raise RuntimeError("retained_workspace_not_entry_verified:" + case["case_id"])
    if (
        entry_row.get("prepared_workspace") != str(workspace.resolve())
        or entry_row.get("prepared_workspace_tree_sha256")
        != result.get("prepared_workspace_manifest_sha256")
        or entry_row.get("validator_directory")
        != str(Path(result["validator_directory"]).resolve())
    ):
        raise RuntimeError("retained_workspace_entry_binding_drift:" + case["case_id"])
    if result.get("case", {}).get("resolved_input_sha256") != case["resolved_input_sha256"]:
        raise RuntimeError("retained_case_payload_drift:" + case["case_id"])
    # Full workspace and DCP hashes were independently recomputed in the
    # immutable Gate-0 entry audit.  They are recomputed once more after seed
    # execution, immediately before the durable cleanup receipt; avoiding a
    # redundant pre-execution pass is orchestration-only and materially reduces
    # retained-drain wall time.
    return result | {"wp015c_reused": True}


def seed_units(preparations: Iterable[dict[str, Any]], seeds: list[int]) -> list[ValidationSeed]:
    return [
        ValidationSeed(
            prep["case"]["case_id"], prep["case"]["root_id"], f"seed-{index:02d}",
            seed, prep["case"]["memory_class"],
        )
        for prep in preparations for index, seed in enumerate(seeds)
    ]


def run_wave_seeds(
    *, preparations: list[dict[str, Any]], plan: dict[str, Any], output: Path,
    scientific_repo: Path, wave_id: str,
    maximum_seed_concurrency: int = MAX_SEED_CONCURRENCY,
    target_seed_concurrency: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if maximum_seed_concurrency < 1:
        raise ValueError("maximum_seed_concurrency_must_be_positive")
    requested_target = (
        int(plan["limits"]["seed_parallel"])
        if target_seed_concurrency is None
        else target_seed_concurrency
    )
    if requested_target < 1 or requested_target > maximum_seed_concurrency:
        raise ValueError("target_seed_concurrency_out_of_range")
    seeds = [int(value) for value in plan["functional"]["seeds"]]
    prep_by_case = {row["case"]["case_id"]: row for row in preparations}
    results: list[dict[str, Any]] = []
    pending: list[ValidationSeed] = []
    for unit in seed_units(preparations, seeds):
        destination = output / "seeds" / unit.case_id / unit.seed_id
        reused = _load_seed_result(destination, unit)
        if reused:
            results.append(reused)
        else:
            if destination.exists():
                quarantine = output / "wp015c" / "interrupted-seeds" / unit.case_id / unit.seed_id
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                if quarantine.exists():
                    raise RuntimeError("duplicate_interrupted_seed_quarantine:" + unit.case_id + ":" + unit.seed_id)
                shutil.move(str(destination), str(quarantine))
            pending.append(unit)
    estimates = {"normal": 8 * GIB, "high": 64 * GIB, "unknown": 64 * GIB}
    scheduler = MemoryAwareSeedScheduler(
        pending, maximum=maximum_seed_concurrency,
        target=requested_target,
        reserve_fraction=RAM_RESERVE_FRACTION, estimates=estimates,
    )
    running: dict[concurrent.futures.Future[dict[str, Any]], ValidationSeed] = {}
    events: list[dict[str, Any]] = []
    baseline_owned = owned_transient_bytes(scientific_repo, output)
    active_clone_bytes: dict[ValidationSeed, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=maximum_seed_concurrency) as pool:
        while scheduler.pending or running:
            total_memory, available_memory = linux_memory_bytes()
            free_bytes = shutil.disk_usage(scientific_repo).free
            if free_bytes < MINIMUM_FREE:
                raise RuntimeError("wp015c_minimum_free_disk_breach")
            proposed = scheduler.refill(
                running=running.values(), total_memory=total_memory,
                available_memory=available_memory,
            )
            admitted = []
            rejected = []
            projected = baseline_owned + sum(active_clone_bytes.values())
            for unit in proposed:
                clone_bytes = int(prep_by_case[unit.case_id].get("prepared_workspace_bytes") or 0)
                if projected + clone_bytes <= HARD_CEILING:
                    admitted.append(unit)
                    projected += clone_bytes
                else:
                    rejected.append(unit)
            if rejected:
                scheduler.pending = rejected + scheduler.pending
            for unit in admitted:
                if baseline_owned > HARD_CEILING:
                    # In inherited drain-only mode only bounded active seed clones may grow.
                    if not prep_by_case[unit.case_id].get("wp015c_reused"):
                        raise RuntimeError("new_preparation_admitted_above_hard_ceiling")
                destination = output / "seeds" / unit.case_id / unit.seed_id
                future = pool.submit(
                    _run_seed_unit, unit, prep_by_case[unit.case_id], destination,
                    int(plan["functional"]["vectors_per_seed"]),
                    int(plan["limits"]["per_seed_timeout_seconds"]),
                )
                running[future] = unit
                active_clone_bytes[unit] = int(
                    prep_by_case[unit.case_id].get("prepared_workspace_bytes") or 0
                )
            events.append({
                "epoch_seconds": time.time(), "wave_id": wave_id,
                "running": len(running), "pending": len(scheduler.pending),
                "admitted": [[unit.case_id, unit.seed_id] for unit in admitted],
                "memory_total_bytes": total_memory, "memory_available_bytes": available_memory,
                "owned_transient_bytes_baseline": baseline_owned,
                "projected_owned_transient_bytes_after_admission": projected,
                "filesystem_free_bytes": free_bytes,
            })
            if not running:
                if scheduler.pending:
                    if not admitted:
                        raise RuntimeError("wp015c_seed_disk_or_memory_admission_deadlock")
                    time.sleep(2)
                continue
            done, _ = concurrent.futures.wait(
                running, timeout=2, return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                unit = running.pop(future)
                active_clone_bytes.pop(unit, None)
                result = future.result()
                result.update({
                    "case_id": unit.case_id, "root_id": unit.root_id,
                    "seed_id": unit.seed_id, "wp015c_reused": False,
                })
                destination = output / "seeds" / unit.case_id / unit.seed_id
                clone = destination / "workspace"
                if clone.exists():
                    # A typed seed-runner exception may return after cloning but
                    # before the frozen runner's normal clone cleanup.  The
                    # semantic result is already durable, so remove only that
                    # exact transient clone and retain a receipt.
                    clone_bytes = logical_bytes(clone)
                    shutil.rmtree(clone)
                    write(output / "wp015c" / "seed-clone-cleanup" / unit.case_id / f"{unit.seed_id}.json", {
                        "case_id": unit.case_id, "seed_id": unit.seed_id,
                        "semantic_result_sha256": result["semantic_result_sha256"],
                        "clone_path": str(clone), "clone_logical_bytes": clone_bytes,
                        "clone_deleted": not clone.exists(),
                        "completed_epoch_seconds": time.time(),
                    })
                results.append(result)
    return results, events


def resume_pending_cleanups(
    *, output: Path, scientific_repo: Path, plan_by_case: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Finish aggregate-durable cleanup without rerunning a seed."""
    events = []
    for aggregate_path in sorted((output / "wp015c" / "cases").glob("*/aggregate.json")):
        case_id = aggregate_path.parent.name
        receipt_path = output / "wp015c" / "cleanup-receipts" / f"{case_id}.json"
        if receipt_path.is_file():
            continue
        if case_id not in plan_by_case:
            raise RuntimeError("pending_cleanup_case_not_in_plan:" + case_id)
        aggregate = read(aggregate_path)
        observed = canonical_hash({
            key: value for key, value in aggregate.items() if key != "case_aggregate_sha256"
        })
        if observed != aggregate.get("case_aggregate_sha256"):
            raise RuntimeError("pending_cleanup_aggregate_hash_mismatch:" + case_id)
        intent_path = output / "wp015c" / "cleanup-intents" / f"{case_id}.json"
        if not intent_path.is_file():
            raise RuntimeError("pending_cleanup_intent_missing:" + case_id)
        intent = read(intent_path)
        validator_value = intent.get("validator_directory")
        validator = verify_owned_validator_path(Path(validator_value), scientific_repo) if validator_value else None
        if validator and validator.exists():
            shutil.rmtree(validator)
        case = plan_by_case[case_id]
        receipt = {
            "schema_version": "dof.wp015c-cleanup-receipt.v1",
            "case_id": case_id, "wave_id": aggregate.get("wave_id"),
            "case_aggregate_sha256": aggregate["case_aggregate_sha256"],
            "cleanup_intent_sha256": intent.get("intent_sha256"),
            "validator_directory": str(validator) if validator else None,
            "prepared_workspace_manifest_sha256": intent.get("prepared_workspace_manifest_sha256"),
            "validator_logical_bytes_before": intent.get("validator_logical_bytes_before", 0),
            "prepared_workspace_deleted": not bool(validator and validator.exists()),
            "validator_directory_deleted": not bool(validator and validator.exists()),
            "input_sha256_after_cleanup": verify_inputs(case),
            "resumed_cleanup_only": True,
            "completed_epoch_seconds": time.time(),
        }
        receipt["receipt_sha256"] = canonical_hash(receipt)
        write(receipt_path, receipt)
        events.append(receipt)
    return events


def persist_case_and_cleanup(
    *, prep: dict[str, Any], seed_rows: list[dict[str, Any]], plan: dict[str, Any],
    output: Path, scientific_repo: Path, wave_id: str,
) -> dict[str, Any]:
    case = prep["case"]
    case_id = case["case_id"]
    aggregate_path = output / "wp015c" / "cases" / case_id / "aggregate.json"
    receipt_path = output / "wp015c" / "cleanup-receipts" / f"{case_id}.json"
    if aggregate_path.is_file() and receipt_path.is_file():
        aggregate = read(aggregate_path)
        receipt = read(receipt_path)
        if aggregate.get("case_aggregate_sha256") != canonical_hash(
            {key: value for key, value in aggregate.items() if key != "case_aggregate_sha256"}
        ):
            raise RuntimeError("persisted_case_aggregate_hash_mismatch:" + case_id)
        if receipt.get("prepared_workspace_deleted") is not True:
            raise RuntimeError("cleanup_receipt_not_terminal:" + case_id)
        return aggregate
    expected_seeds = [int(value) for value in plan["functional"]["seeds"]]
    if len(seed_rows) != len(expected_seeds):
        raise RuntimeError("case_seed_count_incomplete:" + case_id)
    for row in seed_rows:
        if row.get("semantic_result_sha256") != canonical_hash(semantic_seed_result(row)):
            raise RuntimeError("case_seed_semantic_hash_mismatch:" + case_id)
    validation = aggregate_seed_reports(
        prepared_report=prep["validator_report"], seed_reports=seed_rows,
        expected_seeds=expected_seeds,
    )
    if validation["all_seeds_present_exactly_once"] is not True:
        raise RuntimeError("case_seed_identity_not_exact:" + case_id)
    inputs_after = verify_inputs(case)
    core = {
        "schema_version": "dof.wp015c-case-aggregate.v1",
        "plan_sha256": plan["plan_sha256"], "case": case,
        "wave_id": wave_id,
        "prepared_workspace_manifest_sha256": prep["prepared_workspace_manifest_sha256"],
        "preparation_reused": bool(prep.get("wp015c_reused")),
        "validation": validation,
        "seed_result_sha256": {
            row["seed_id"]: row["semantic_result_sha256"]
            for row in sorted(seed_rows, key=lambda value: value["seed_id"])
        },
        "input_sha256_after": inputs_after,
    }
    aggregate = core | {"case_aggregate_sha256": canonical_hash(core)}
    write(aggregate_path, aggregate)
    if read(aggregate_path) != aggregate:
        raise RuntimeError("case_aggregate_atomic_persistence_failure:" + case_id)
    validator_dir = verify_owned_validator_path(Path(prep["validator_directory"]), scientific_repo)
    workspace = Path(prep["prepared_workspace"])
    if validator_dir not in workspace.resolve().parents:
        raise RuntimeError("prepared_workspace_not_inside_validator_dir:" + case_id)
    manifest_before = canonical_hash(tree_manifest(workspace))
    if manifest_before != prep["prepared_workspace_manifest_sha256"]:
        raise RuntimeError("prepared_workspace_changed_before_cleanup:" + case_id)
    bytes_before = logical_bytes(validator_dir)
    free_before = shutil.disk_usage(scientific_repo).free
    intent = {
        "schema_version": "dof.wp015c-cleanup-intent.v1",
        "case_id": case_id, "wave_id": wave_id,
        "case_aggregate_sha256": aggregate["case_aggregate_sha256"],
        "validator_directory": str(validator_dir),
        "prepared_workspace": str(workspace),
        "prepared_workspace_manifest_sha256": manifest_before,
        "validator_logical_bytes_before": bytes_before,
        "input_sha256_before_cleanup": inputs_after,
        "intent_epoch_seconds": time.time(),
    }
    intent["intent_sha256"] = canonical_hash(intent)
    write(output / "wp015c" / "cleanup-intents" / f"{case_id}.json", intent)
    shutil.rmtree(validator_dir)
    free_after = shutil.disk_usage(scientific_repo).free
    receipt = {
        "schema_version": "dof.wp015c-cleanup-receipt.v1",
        "case_id": case_id, "wave_id": wave_id,
        "case_aggregate_sha256": aggregate["case_aggregate_sha256"],
        "cleanup_intent_sha256": intent["intent_sha256"],
        "validator_directory": str(validator_dir),
        "prepared_workspace_manifest_sha256": manifest_before,
        "validator_logical_bytes_before": bytes_before,
        "filesystem_free_bytes_before": free_before,
        "filesystem_free_bytes_after": free_after,
        "filesystem_free_delta_bytes": free_after - free_before,
        "prepared_workspace_deleted": not workspace.exists(),
        "validator_directory_deleted": not validator_dir.exists(),
        "input_sha256_after_cleanup": verify_inputs(case),
        "completed_epoch_seconds": time.time(),
    }
    if not receipt["prepared_workspace_deleted"] or not receipt["validator_directory_deleted"]:
        raise RuntimeError("prepared_workspace_cleanup_failed:" + case_id)
    receipt["receipt_sha256"] = canonical_hash(receipt)
    write(receipt_path, receipt)
    return aggregate


def persist_preparation_terminal_and_cleanup(
    *, prep: dict[str, Any], plan: dict[str, Any], output: Path,
    scientific_repo: Path, wave_id: str,
) -> dict[str, Any]:
    case = prep["case"]
    case_id = case["case_id"]
    terminal = str(prep["terminal"])
    core = {
        "schema_version": "dof.wp015c-case-aggregate.v1",
        "plan_sha256": plan["plan_sha256"], "case": case, "wave_id": wave_id,
        "preparation_reused": False, "validation": {
            "schema_version": "dof.wp015-seed-validation-aggregate.v1",
            "terminal": terminal, "terminal_reason": prep.get("terminal_reason"),
            "expected_seeds": [int(value) for value in plan["functional"]["seeds"]],
            "all_seeds_present_exactly_once": False,
            "semantic_result_sha256": canonical_hash({
                "terminal": terminal, "case_id": case_id,
                "terminal_reason": prep.get("terminal_reason"),
                "validator_report": prep.get("validator_report"),
            }),
        },
        "seed_result_sha256": {}, "input_sha256_after": verify_inputs(case),
    }
    aggregate = core | {"case_aggregate_sha256": canonical_hash(core)}
    write(output / "wp015c" / "cases" / case_id / "aggregate.json", aggregate)
    validator_value = prep.get("validator_directory")
    deleted = True
    reclaimed = 0
    if validator_value:
        validator_dir = verify_owned_validator_path(Path(validator_value), scientific_repo)
        reclaimed = logical_bytes(validator_dir)
        intent = {
            "case_id": case_id, "terminal": terminal,
            "validator_directory": str(validator_dir),
            "validator_logical_bytes_before": reclaimed,
            "case_aggregate_sha256": aggregate["case_aggregate_sha256"],
        }
        write(output / "wp015c" / "cleanup-intents" / f"{case_id}.json", intent)
        if validator_dir.exists():
            shutil.rmtree(validator_dir)
        deleted = not validator_dir.exists()
    receipt = {
        "schema_version": "dof.wp015c-cleanup-receipt.v1",
        "case_id": case_id, "wave_id": wave_id,
        "case_aggregate_sha256": aggregate["case_aggregate_sha256"],
        "validator_logical_bytes_before": reclaimed,
        "prepared_workspace_deleted": deleted,
        "validator_directory_deleted": deleted,
        "preparation_terminal": terminal,
        "completed_epoch_seconds": time.time(),
    }
    receipt["receipt_sha256"] = canonical_hash(receipt)
    write(output / "wp015c" / "cleanup-receipts" / f"{case_id}.json", receipt)
    return aggregate


def select_canary(
    reusable: list[dict[str, Any]], plan_by_case: dict[str, dict[str, Any]],
    entry: dict[str, Any],
) -> list[str]:
    sizes = {
        Path(row["path"]).resolve(): int(row["logical_bytes"])
        for row in entry["owned_directories"]
    }
    candidates = []
    for row in reusable:
        case = plan_by_case[row["case_id"]]
        validator = Path(row["validator_directory"]).resolve()
        candidates.append((sizes.get(validator, 0), case))
    candidates.sort(key=lambda value: (-value[0], value[1]["root_id"], value[1]["case_id"]))
    selected: list[dict[str, Any]] = []
    high = next((case for _, case in candidates if case["memory_class"] == "high"), None)
    if high:
        selected.append(high)
    normal = next((
        case for _, case in candidates
        if case["memory_class"] == "normal"
        and case["root_id"] not in {row["root_id"] for row in selected}
        and case.get("mechanism") not in {row.get("mechanism") for row in selected}
    ), None)
    if normal is None:
        normal = next((
            case for _, case in candidates
            if case["memory_class"] == "normal"
            and case["root_id"] not in {row["root_id"] for row in selected}
        ), None)
    if normal:
        selected.append(normal)
    for _, case in candidates:
        if case in selected or case["root_id"] in {row["root_id"] for row in selected}:
            continue
        selected.append(case)
        if len(selected) == 3:
            break
    if len(selected) < 3:
        for _, case in candidates:
            if case not in selected:
                selected.append(case)
            if len(selected) == 3:
                break
    if (
        len(selected) != 3
        or len({row["root_id"] for row in selected}) < 2
        or len({row.get("mechanism") for row in selected}) < 2
        or "high" not in {row["memory_class"] for row in selected}
        or "normal" not in {row["memory_class"] for row in selected}
    ):
        raise RuntimeError("canary_coverage_unavailable")
    return [row["case_id"] for row in selected]


def run(args: argparse.Namespace) -> int:
    started = time.time()
    plan = read(args.plan)
    entry = read(args.entry_audit)
    output = args.output.resolve()
    scientific_repo = args.scientific_repo.resolve()
    entry_verification = verify_entry(plan=plan, entry=entry, scientific_repo=scientific_repo)
    output.mkdir(parents=True, exist_ok=True)
    write(output / "wp015c" / "entry-verification.json", entry_verification)
    plan_by_case = {row["case_id"]: row for row in plan["cases"]}
    interrupted_cleanup = cleanup_interrupted_entry(
        plan_by_case=plan_by_case, entry=entry, scientific_repo=scientific_repo,
        output=output,
    )
    resume_cleanup_events = resume_pending_cleanups(
        output=output, scientific_repo=scientific_repo, plan_by_case=plan_by_case,
    )
    completed = {
        path.stem
        for path in (output / "wp015c" / "cleanup-receipts").glob("*.json")
        if (output / "wp015c" / "cases" / path.stem / "aggregate.json").is_file()
    }
    reusable_rows = [
        row for row in entry["cases"]
        if row["status"] == "PREPARED_REUSABLE" and row["case_id"] not in completed
    ]
    if args.phase == "canary":
        canonical_selection = output / "wp015c" / "canary-selection.json"
        if canonical_selection.is_file():
            selected_ids = read(canonical_selection)["case_ids"]
        else:
            selected_ids = select_canary(reusable_rows, plan_by_case, entry)
            selection = {"case_ids": selected_ids, "entry_audit_sha256": entry["entry_audit_sha256"]}
            selection["selection_sha256"] = canonical_hash(selection)
            write(canonical_selection, selection)
        case_ids = [case_id for case_id in selected_ids if case_id not in completed]
    else:
        entry_sizes = {
            Path(row["path"]).resolve(): int(row["logical_bytes"])
            for row in entry["owned_directories"]
        }
        reusable_rows.sort(key=lambda row: (
            -entry_sizes.get(Path(row["validator_directory"]).resolve(), 0),
            plan_by_case[row["case_id"]]["root_id"], row["case_id"],
        ))
        retained_ids = [row["case_id"] for row in reusable_rows]
        remaining_ids = sorted(set(plan_by_case) - completed - set(retained_ids))
        case_ids = retained_ids + remaining_ids
    if args.case_limit is not None:
        case_ids = case_ids[: args.case_limit]
    if args.phase == "canary":
        wave_groups = [case_ids]
    else:
        retained_selected = [case_id for case_id in case_ids if any(
            row["case_id"] == case_id for row in reusable_rows
        )]
        remaining_selected = [case_id for case_id in case_ids if case_id not in set(retained_selected)]
        wave_groups = [
            retained_selected[index:index + args.active_cases]
            for index in range(0, len(retained_selected), args.active_cases)
        ] + [
            remaining_selected[index:index + min(3, args.active_cases)]
            for index in range(0, len(remaining_selected), min(3, args.active_cases))
        ]
    queue_events = []
    wave_summaries = []
    for wave_index, wave_case_ids in enumerate(wave_groups):
        # Content-derived identity prevents a resumed queue from overwriting
        # earlier durable wave receipts after completed cases are removed.
        wave_id = f"{args.phase}-{canonical_hash(wave_case_ids)[:12]}"
        bytes_before = owned_transient_bytes(scientific_repo, output)
        free_before = shutil.disk_usage(scientific_repo).free
        total_memory, available_memory = linux_memory_bytes()
        if free_before < MINIMUM_FREE or available_memory < int(total_memory * RAM_RESERVE_FRACTION):
            raise RuntimeError("wp015c_resource_reserve_breach_before_wave")
        retained_by_id = {row["case_id"]: row for row in reusable_rows}
        if all(case_id in retained_by_id for case_id in wave_case_ids):
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave_case_ids)) as pool:
                preparations = list(pool.map(
                    lambda case_id: reusable_preparation(
                        plan_by_case[case_id], output, retained_by_id[case_id]
                    ),
                    wave_case_ids,
                ))
        else:
            preparations = []
            for case_id in wave_case_ids:
                case = plan_by_case[case_id]
                current_bytes = owned_transient_bytes(scientific_repo, output)
                if current_bytes > REFILL_CEILING:
                    raise RuntimeError("preparation_refill_above_ceiling")
                if current_bytes > HARD_CEILING:
                    raise RuntimeError("preparation_above_hard_ceiling")
                prep = prepare_case_frozen(case, plan, output, scientific_repo)
                after_prepare = owned_transient_bytes(scientific_repo, output)
                if after_prepare > HARD_CEILING:
                    raise RuntimeError("preparation_caused_hard_ceiling_breach")
                if after_prepare - current_bytes > WAVE_TARGET:
                    raise RuntimeError("single_preparation_exceeded_wave_target")
                preparations.append(prep)
        valid_preps = [row for row in preparations if row.get("prepared_workspace")]
        terminals = [row for row in preparations if not row.get("prepared_workspace")]
        seed_results, events = run_wave_seeds(
            preparations=valid_preps, plan=plan, output=output,
            scientific_repo=scientific_repo, wave_id=wave_id,
        ) if valid_preps else ([], [])
        queue_events.extend(events)
        aggregates = []
        if valid_preps:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(valid_preps)) as pool:
                futures = [pool.submit(
                    persist_case_and_cleanup,
                    prep=prep,
                    seed_rows=[row for row in seed_results if row["case_id"] == prep["case"]["case_id"]],
                    plan=plan, output=output, scientific_repo=scientific_repo,
                    wave_id=wave_id,
                ) for prep in valid_preps]
                aggregates.extend(future.result() for future in futures)
        for prep in terminals:
            aggregates.append(persist_preparation_terminal_and_cleanup(
                prep=prep, plan=plan, output=output,
                scientific_repo=scientific_repo, wave_id=wave_id,
            ))
        bytes_after = owned_transient_bytes(scientific_repo, output)
        survivors = owned_survivors([str(output), str(scientific_repo / "dcp_validation_")])
        wave = {
            "wave_id": wave_id, "case_ids": wave_case_ids,
            "terminal_counts": dict(sorted(collections.Counter(
                row["validation"]["terminal"] for row in aggregates
            ).items())),
            "owned_transient_bytes_before": bytes_before,
            "owned_transient_bytes_after": bytes_after,
            "owned_transient_delta_bytes": bytes_after - bytes_before,
            "filesystem_free_bytes_before": free_before,
            "filesystem_free_bytes_after": shutil.disk_usage(scientific_repo).free,
            "memory_total_bytes": total_memory,
            "memory_available_bytes_before": available_memory,
            "owned_process_survivors": survivors,
            "completed_epoch_seconds": time.time(),
        }
        wave["wave_sha256"] = canonical_hash(wave)
        write(output / "wp015c" / "waves" / f"{wave_id}.json", wave)
        wave_summaries.append(wave)
        print(json.dumps({
            "wave_id": wave_id, "cases": len(wave_case_ids),
            "terminals": wave["terminal_counts"],
            "owned_gib_before": bytes_before / GIB,
            "owned_gib_after": bytes_after / GIB,
            "survivors": len(survivors),
        }, sort_keys=True), flush=True)
        if survivors:
            raise RuntimeError("owned_process_survivor_after_wave")
        if bytes_before > HARD_CEILING and bytes_after > bytes_before + GIB:
            raise RuntimeError("drain_only_owned_bytes_material_growth")
    all_aggregates = [read(path) for path in sorted((output / "wp015c" / "cases").glob("*/aggregate.json"))]
    audit_1 = owned_survivors([str(output), str(scientific_repo / "dcp_validation_")])
    time.sleep(2)
    audit_2 = owned_survivors([str(output), str(scientific_repo / "dcp_validation_")])
    core = {
        "schema_version": "dof.wp015c-wave-run.v1",
        "phase": args.phase, "plan_sha256": plan["plan_sha256"],
        "entry_audit_sha256": entry["entry_audit_sha256"],
        "validation_scientific_core_hash": entry_verification["validation_scientific_core_hash"],
        "validation_orchestrator_hash": file_sha256(Path(__file__)),
        "selected_case_count": len(case_ids),
        "total_persisted_case_count": len(all_aggregates),
        "terminal_counts": dict(sorted(collections.Counter(
            row["validation"]["terminal"] for row in all_aggregates
        ).items())),
        "interrupted_entry_cleanup_sha256": interrupted_cleanup["receipt_sha256"],
        "resumed_cleanup_event_count": len(resume_cleanup_events),
        "waves": [{key: row[key] for key in (
            "wave_id", "case_ids", "terminal_counts", "owned_transient_bytes_before",
            "owned_transient_bytes_after", "wave_sha256",
        )} for row in wave_summaries],
        "queue_event_count": len(queue_events),
        "owned_transient_bytes_final": owned_transient_bytes(scientific_repo, output),
        "owned_process_audit_1": audit_1, "owned_process_audit_2": audit_2,
        "provider_or_llm_calls": 0, "optimization_calls": 0,
        "wall_seconds": time.time() - started,
    }
    result = core | {"run_sha256": canonical_hash(core)}
    write(output / "wp015c" / f"{args.phase}-run-summary.json", result)
    if args.phase == "canary":
        complete = len(case_ids) == 3 and len(all_aggregates) >= 3
    else:
        complete = len(all_aggregates) == len(plan_by_case)
    return 0 if complete and not audit_1 and not audit_2 else 2


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entry-audit", type=Path, required=True)
    parser.add_argument("--scientific-repo", type=Path, required=True)
    parser.add_argument("--phase", choices=("canary", "drain"), required=True)
    parser.add_argument("--active-cases", type=int, default=3)
    parser.add_argument("--case-limit", type=int)
    args = parser.parse_args()
    if not 1 <= args.active_cases <= 8:
        raise ValueError("active_cases_must_be_between_1_and_8")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
