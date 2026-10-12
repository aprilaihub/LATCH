#!/usr/bin/env python3
"""Resume-safe global seed scheduler for a frozen WP-015 validation plan."""

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
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.seed_validation import (  # noqa: E402
    aggregate_seed_reports, canonical_hash, file_sha256, run_prepared_seed,
    semantic_seed_result, tree_manifest,
)
from optimizer_pipeline.dof.validation_scheduler import (  # noqa: E402
    GIB, MemoryAwareSeedScheduler, ValidationSeed, linux_memory_bytes,
)
from scripts.run_wp014f_validation_recon import WORKDIR_RE  # noqa: E402
from scripts.run_wp015_seed_parallel_canary import owned_survivors, prepare_case  # noqa: E402


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def _verify_inputs(case: dict[str, Any]) -> None:
    observed = {
        "golden": file_sha256(Path(case["golden_dcp"])),
        "revised": file_sha256(Path(case["revised_dcp"])),
    }
    if observed != case["resolved_input_sha256"]:
        raise ValueError("validation_input_hash_mismatch:" + case["case_id"])


def _reuse_preparation(case: dict[str, Any], output: Path) -> dict[str, Any] | None:
    result_path = output / "prepared" / case["case_id"] / "prepare-result.json"
    if not result_path.is_file():
        return None
    result = read(result_path)
    if result.get("case", {}).get("resolved_input_sha256") != case["resolved_input_sha256"]:
        raise ValueError("prepared_case_input_mismatch:" + case["case_id"])
    workspace = Path(result["prepared_workspace"])
    if not workspace.is_dir():
        raise ValueError("prepared_workspace_missing:" + case["case_id"])
    if canonical_hash(tree_manifest(workspace)) != result["prepared_workspace_manifest_sha256"]:
        raise ValueError("prepared_workspace_hash_mismatch:" + case["case_id"])
    _verify_inputs(case)
    return result


def _prepare_one(case: dict[str, Any], plan: dict[str, Any], output: Path) -> dict[str, Any]:
    reused = _reuse_preparation(case, output)
    if reused:
        return reused | {"batch_reused": True}
    case_dir = output / "prepared" / case["case_id"]
    failure_path = case_dir / "prepare-failure.json"
    if failure_path.is_file():
        return read(failure_path) | {"batch_reused": True}
    if case_dir.exists():
        interrupted = output / "interrupted-preparations" / case["case_id"]
        interrupted.parent.mkdir(parents=True, exist_ok=True)
        if interrupted.exists():
            interrupted = interrupted.with_name(interrupted.name + "-" + str(int(time.time())))
        shutil.move(str(case_dir), str(interrupted))
    try:
        result = prepare_case(case, plan, output)
        return result | {"batch_reused": False}
    except Exception as exc:  # preparation failures are typed per-case evidence
        log_path = output / "prepared" / case["case_id"] / "validator.log"
        log_text = log_path.read_text(errors="replace") if log_path.is_file() else ""
        matches = WORKDIR_RE.findall(log_text)
        validator_dir = Path(matches[-1]) if matches else None
        report_path = validator_dir / "validation_report.json" if validator_dir else None
        report = read(report_path) if report_path and report_path.is_file() else None
        terminal = "VALIDATOR_PREPARATION_FAILURE"
        reason = "preparation_exception_without_complete_validator_report"
        if isinstance(report, dict):
            simulation = report.get("simulation_report")
            simulation = simulation if isinstance(simulation, dict) else {}
            if report.get("phase1_passed") is not True:
                terminal = "FUNCTIONAL_VALIDATION_STRUCTURAL_FAILURE"
                reason = "validator_phase1_failed"
            elif simulation.get("precheck_passed") is False:
                if simulation.get("infrastructure_failure"):
                    terminal = "FUNCTIONAL_VALIDATION_INFRASTRUCTURE_FAILURE"
                    reason = str(simulation.get("infrastructure_reason") or "precheck_infrastructure_failure")
                else:
                    terminal = "FUNCTIONAL_PRECHECK_MISMATCH"
                    reason = "precheck_completed_with_functional_mismatch"
        failure = {
            "case": case, "terminal": terminal, "terminal_reason": reason,
            "exception_type": type(exc).__name__, "exception_message": str(exc),
            "validator_directory": str(validator_dir) if validator_dir else None,
            "validator_report": report,
            "batch_reused": False,
        }
        write(output / "prepared" / case["case_id"] / "prepare-failure.json", failure)
        return failure


def _run_seed_unit(
    unit: ValidationSeed, prep: dict[str, Any], destination: Path,
    vectors: int, timeout_seconds: int,
) -> dict[str, Any]:
    try:
        return run_prepared_seed(
            prepared_workspace=Path(prep["prepared_workspace"]), output=destination,
            stimulus_seed=unit.stimulus_seed, vectors=vectors,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        destination.mkdir(parents=True, exist_ok=True)
        row = {
            "vectors_requested": vectors, "stimulus_seed": unit.stimulus_seed,
            "stimulus_seed_hex": f"0x{unit.stimulus_seed:08X}",
            "cycles_simulated": 0, "mismatch_count": 0,
            "protocol_mismatch_count": 0, "result_pass_seen": False,
            "result_fail_seen": False, "simulator_failed": True,
            "infrastructure_failure": True,
            "infrastructure_reason": "seed_runner_exception:" + type(exc).__name__,
            "returncode": 125, "passed": False, "timed_out": False,
            "wall_seconds": 0.0, "resource": {},
            "exception_message": str(exc),
        }
        row["semantic_result_sha256"] = canonical_hash(semantic_seed_result(row))
        write(destination / "seed-result.json", row)
        return row


def _load_seed_result(path: Path, unit: ValidationSeed) -> dict[str, Any] | None:
    result_path = path / "seed-result.json"
    if not result_path.is_file():
        return None
    row = read(result_path)
    if int(row.get("stimulus_seed", -1)) != unit.stimulus_seed:
        raise ValueError("seed_identity_mismatch:" + unit.case_id + ":" + unit.seed_id)
    if row.get("semantic_result_sha256") != canonical_hash(semantic_seed_result(row)):
        raise ValueError("seed_semantic_hash_mismatch:" + unit.case_id + ":" + unit.seed_id)
    row.update({"case_id": unit.case_id, "root_id": unit.root_id, "seed_id": unit.seed_id, "batch_reused": True})
    return row


def run(args: argparse.Namespace) -> int:
    plan = read(args.plan)
    if plan.get("provider_or_llm_calls") or plan.get("optimization_calls") or plan.get("transfer_or_holdout_accesses"):
        raise ValueError("validation_batch_firewall_not_zero")
    if args.output.exists() and not args.resume:
        raise FileExistsError(args.output)
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    expected_repository = plan.get("repository", {})
    if (
        expected_repository.get("head") != git("rev-parse", "HEAD")
        or expected_repository.get("tree") != git("rev-parse", "HEAD^{tree}")
    ):
        raise RuntimeError("validation_source_repository_mismatch")
    args.output.mkdir(parents=True, exist_ok=args.resume)
    plan_copy = args.output / "resolved-plan.json"
    if plan_copy.is_file():
        if read(plan_copy).get("plan_sha256") != plan["plan_sha256"]:
            raise ValueError("resume_plan_hash_mismatch")
    else:
        write(plan_copy, plan)
    cases = plan["cases"]
    if len(cases) != len({row["case_id"] for row in cases}):
        raise ValueError("duplicate_validation_case")
    for case in cases:
        _verify_inputs(case)

    limits = plan["limits"]
    legacy_plan = {
        "functional": plan["functional"],
        "boundaries": {"per_case_timeout_seconds": limits["per_case_timeout_seconds"]},
    }
    started = time.time()
    preparations: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(limits["preparation_parallel"])) as pool:
        futures = {pool.submit(_prepare_one, case, legacy_plan, args.output): case for case in cases}
        for future in concurrent.futures.as_completed(futures):
            preparations.append(future.result())
    prepared = [row for row in preparations if row.get("prepared_workspace")]
    preparation_terminals = [row for row in preparations if not row.get("prepared_workspace")]
    prep_by_case = {row["case"]["case_id"]: row for row in prepared}

    seeds = [int(value) for value in plan["functional"]["seeds"]]
    all_units = [
        ValidationSeed(
            prep["case"]["case_id"], prep["case"]["root_id"], f"seed-{index:02d}",
            seed, prep["case"]["memory_class"],
        )
        for prep in prepared for index, seed in enumerate(seeds)
    ]
    seed_root = args.output / "seeds"
    seed_results: list[dict[str, Any]] = []
    pending: list[ValidationSeed] = []
    for unit in all_units:
        destination = seed_root / unit.case_id / unit.seed_id
        reused = _load_seed_result(destination, unit) if args.resume else None
        if reused:
            seed_results.append(reused)
        else:
            if destination.exists():
                interrupted = args.output / "interrupted-seeds" / unit.case_id / unit.seed_id
                interrupted.parent.mkdir(parents=True, exist_ok=True)
                if interrupted.exists():
                    interrupted = interrupted.with_name(interrupted.name + "-" + str(int(time.time())))
                shutil.move(str(destination), str(interrupted))
            pending.append(unit)

    estimates = {"normal": 8 * GIB, "high": 64 * GIB, "unknown": 64 * GIB}
    scheduler = MemoryAwareSeedScheduler(
        pending, maximum=16, target=int(limits["seed_parallel"]),
        reserve_fraction=float(limits["reserve_fraction"]), estimates=estimates,
    )
    running: dict[concurrent.futures.Future[dict[str, Any]], ValidationSeed] = {}
    events = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(limits["seed_parallel"])) as pool:
        while scheduler.pending or running:
            total, available = linux_memory_bytes()
            admitted = scheduler.refill(running=running.values(), total_memory=total, available_memory=available)
            events.append({
                "epoch_seconds": time.time(), "running": len(running),
                "pending": len(scheduler.pending) + len(admitted),
                "admitted": [[unit.case_id, unit.seed_id] for unit in admitted],
                "memory_total_bytes": total, "memory_available_bytes": available,
                "reserve_bytes": int(total * float(limits["reserve_fraction"])),
            })
            for unit in admitted:
                prep = prep_by_case[unit.case_id]
                destination = seed_root / unit.case_id / unit.seed_id
                future = pool.submit(
                    _run_seed_unit, unit, prep, destination,
                    int(plan["functional"]["vectors_per_seed"]),
                    int(limits["per_seed_timeout_seconds"]),
                )
                running[future] = unit
            if not running:
                if scheduler.pending:
                    time.sleep(2)
                continue
            done, _ = concurrent.futures.wait(running, timeout=2, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                unit = running.pop(future)
                result = future.result()
                result.update({"case_id": unit.case_id, "root_id": unit.root_id,
                               "seed_id": unit.seed_id, "batch_reused": False})
                seed_results.append(result)

    case_aggregates: dict[str, dict[str, Any]] = {}
    for prep in prepared:
        case_id = prep["case"]["case_id"]
        rows = [row for row in seed_results if row["case_id"] == case_id]
        case_aggregates[case_id] = aggregate_seed_reports(
            prepared_report=prep["validator_report"], seed_reports=rows, expected_seeds=seeds,
        )
    for failure in preparation_terminals:
        terminal = str(failure["terminal"])
        case_aggregates[failure["case"]["case_id"]] = {
            "schema_version": "dof.wp015-seed-validation-aggregate.v1",
            "terminal": terminal, "terminal_reason": failure.get("terminal_reason"),
            "expected_seeds": seeds,
            "all_seeds_present_exactly_once": False,
            "semantic_result_sha256": canonical_hash({
                "terminal": terminal,
                "case_id": failure["case"]["case_id"],
                "exception_type": failure["exception_type"],
                "exception_message": failure["exception_message"],
                "validator_report": failure.get("validator_report"),
            }),
        }
    input_unchanged = all(
        file_sha256(Path(case["golden_dcp"])) == case["golden_sha256"]
        and file_sha256(Path(case["revised_dcp"])) == case["revised_sha256"]
        for case in cases
    )
    markers = [str(args.output)] + [row.get("prepared_workspace", "") for row in prepared]
    audit_1 = owned_survivors(markers)
    time.sleep(2)
    audit_2 = owned_survivors(markers)
    core = {
        "schema_version": "dof.wp015-seed-validation-batch.v1",
        "plan_sha256": plan["plan_sha256"], "planned_cases": len(cases),
        "prepared_cases": len(prepared),
        "preparation_terminal_count": len(preparation_terminals),
        "preparation_failure_count": sum(
            row["terminal"] in {
                "VALIDATOR_PREPARATION_FAILURE",
                "FUNCTIONAL_VALIDATION_INFRASTRUCTURE_FAILURE",
            }
            for row in preparation_terminals
        ),
        "functional_precheck_mismatch_count": sum(
            row["terminal"] == "FUNCTIONAL_PRECHECK_MISMATCH"
            for row in preparation_terminals
        ),
        "planned_seed_units": len(all_units), "completed_seed_units": len(seed_results),
        "reused_preparations": sum(bool(row.get("batch_reused")) for row in prepared),
        "reused_seed_units": sum(bool(row.get("batch_reused")) for row in seed_results),
        "terminal_counts": dict(sorted(collections.Counter(row["terminal"] for row in case_aggregates.values()).items())),
        "case_aggregates": case_aggregates,
        "preparation_terminals": preparation_terminals,
        "queue_events": events, "queue_wall_seconds": time.time() - started,
        "peak_seed_rss_bytes": max((int(row.get("resource", {}).get("max_rss_bytes", 0)) for row in seed_results), default=0),
        "input_hashes_unchanged": input_unchanged,
        "owned_process_audit_1": audit_1, "owned_process_audit_2": audit_2,
        "provider_or_llm_calls": 0, "optimization_calls": 0,
        "transfer_or_holdout_accesses": 0,
    }
    result = core | {"aggregate_sha256": canonical_hash(core)}
    write(args.output / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in (
        "planned_cases", "completed_seed_units", "terminal_counts", "queue_wall_seconds",
        "input_hashes_unchanged", "owned_process_audit_1", "owned_process_audit_2",
    )}, sort_keys=True))
    complete = (
        len(case_aggregates) == len(cases) and len(seed_results) == len(prepared) * len(seeds)
        and input_unchanged and not audit_1 and not audit_2
    )
    return 0 if complete else 2


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
