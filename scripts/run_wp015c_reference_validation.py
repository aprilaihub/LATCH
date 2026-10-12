#!/usr/bin/env python3
"""Disk-bounded V1 orchestration for the post-agent M10 reference panel."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.seed_validation import canonical_hash, file_sha256  # noqa: E402
from optimizer_pipeline.dof.validation_scheduler import (  # noqa: E402
    GIB,
    MemoryAwareValidationScheduler,
    ValidationCase,
)
from scripts.run_wp015_seed_parallel_canary import owned_survivors  # noqa: E402
from scripts.run_wp015c_disk_bounded_validation import (  # noqa: E402
    HARD_CEILING,
    MINIMUM_FREE,
    RAM_RESERVE_FRACTION,
    REFILL_CEILING,
    EXPECTED_SHARED_CORE,
    EXPECTED_SOURCE_COMMIT,
    EXPECTED_SOURCE_TREE,
    EXPECTED_VALIDATION_CORE,
    linux_memory_bytes,
    owned_transient_bytes,
    persist_case_and_cleanup,
    persist_preparation_terminal_and_cleanup,
    prepare_case_frozen,
    resume_pending_cleanups,
    run_wave_seeds,
    validation_core,
)


PREPARATION_MEMORY_ESTIMATES = {
    "normal": 8 * GIB,
    "high": 16 * GIB,
    "unknown": 16 * GIB,
}
PREPARATION_DISK_ESTIMATE = 3 * GIB


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def verify_reference_plan(
    *, plan: dict[str, Any], bundle: dict[str, Any], scientific_repo: Path,
) -> dict[str, Any]:
    plan_core = {key: value for key, value in plan.items() if key != "plan_sha256"}
    nondevelopment = {
        root_id for root_id, row in bundle["roots"].items()
        if row["split"] != "DEVELOPMENT_KNOWN"
    }
    cases = plan.get("cases", [])
    core_hash, core_files = validation_core(scientific_repo)
    checks = {
        "plan_internal_hash": plan.get("plan_sha256") == canonical_hash(plan_core),
        "source_aggregate_is_reference": str(plan.get("source_aggregate_sha256") or "") != "",
        "shared_core": plan.get("shared_scientific_core_hash") == EXPECTED_SHARED_CORE,
        "repository_commit": plan.get("repository", {}).get("head") == EXPECTED_SOURCE_COMMIT,
        "repository_tree": plan.get("repository", {}).get("tree") == EXPECTED_SOURCE_TREE,
        "scientific_commit": git(scientific_repo, "rev-parse", "HEAD") == EXPECTED_SOURCE_COMMIT,
        "scientific_tree": git(scientific_repo, "rev-parse", "HEAD^{tree}") == EXPECTED_SOURCE_TREE,
        "scientific_clean": not bool(git(scientific_repo, "status", "--porcelain")),
        "validation_core": core_hash == EXPECTED_VALIDATION_CORE,
        "case_count": plan.get("case_count") == len(cases) and 0 <= len(cases) <= 110,
        "unique_cases": len(cases) == len({row["case_id"] for row in cases}),
        "reference_case_ids": all(row["case_id"].startswith("wp015__wp015-ref__") for row in cases),
        "nondevelopment_roots_only": {row["root_id"] for row in cases}.issubset(nondevelopment),
        "bundle_hash": plan.get("bundle_sha256") == bundle.get("bundle_sha256"),
        "zero_provider_calls": plan.get("provider_or_llm_calls") == 0,
        "zero_optimisation_calls": plan.get("optimization_calls") == 0,
        "zero_new_transfer_accesses": plan.get("transfer_or_holdout_accesses") == 0,
    }
    if not all(checks.values()):
        raise RuntimeError("wp015c_reference_plan_verification_failed:" + json.dumps(checks, sort_keys=True))
    return {"checks": checks, "validation_scientific_core_hash": core_hash,
            "validation_scientific_core_files": core_files}


def run_wave_preparations(
    *, case_ids: list[str], cases: dict[str, dict[str, Any]], plan: dict[str, Any],
    output: Path, scientific_repo: Path, maximum: int, wave_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prepare a reference wave with live memory and disk admission."""
    units = [ValidationCase(
        case_id, cases[case_id]["root_id"], cases[case_id].get("memory_class", "unknown"),
    ) for case_id in case_ids]
    scheduler = MemoryAwareValidationScheduler(
        units, maximum=maximum, target=maximum,
        reserve_fraction=RAM_RESERVE_FRACTION,
        estimates=PREPARATION_MEMORY_ESTIMATES,
    )
    running: dict[concurrent.futures.Future[dict[str, Any]], ValidationCase] = {}
    results: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=maximum) as pool:
        while scheduler.pending or running:
            total_memory, available_memory = linux_memory_bytes()
            free_bytes = shutil.disk_usage(scientific_repo).free
            owned_bytes = owned_transient_bytes(scientific_repo, output)
            if free_bytes < MINIMUM_FREE:
                raise RuntimeError("wp015c_reference_preparation_free_disk_reserve_breach")
            if owned_bytes > HARD_CEILING:
                raise RuntimeError("wp015c_reference_preparation_hard_ceiling_breach")
            proposed = scheduler.refill(
                running=running.values(), total_memory=total_memory,
                available_memory=available_memory,
            )
            admitted: list[ValidationCase] = []
            projected = owned_bytes
            for unit in proposed:
                if projected + PREPARATION_DISK_ESTIMATE <= REFILL_CEILING:
                    admitted.append(unit)
                    projected += PREPARATION_DISK_ESTIMATE
                else:
                    scheduler.pending.insert(0, unit)
            for unit in admitted:
                running[pool.submit(
                    prepare_case_frozen, cases[unit.case_id], plan, output, scientific_repo,
                )] = unit
            events.append({
                "epoch_seconds": time.time(), "wave_id": wave_id,
                "running": len(running), "pending": len(scheduler.pending),
                "admitted": [unit.case_id for unit in admitted],
                "memory_total_bytes": total_memory,
                "memory_available_bytes": available_memory,
                "owned_transient_bytes": owned_bytes,
                "projected_transient_bytes_after_admission": projected,
                "filesystem_free_bytes": free_bytes,
            })
            if not running:
                if scheduler.pending and not admitted:
                    raise RuntimeError("wp015c_reference_preparation_admission_deadlock")
                continue
            done, _ = concurrent.futures.wait(
                running, timeout=2, return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                running.pop(future)
                results.append(future.result())
    return sorted(results, key=lambda row: row["case"]["case_id"]), events


def run(args: argparse.Namespace) -> int:
    started = time.time()
    plan, bundle = read(args.plan), read(args.bundle)
    output, scientific_repo = args.output.resolve(), args.scientific_repo.resolve()
    verification = verify_reference_plan(
        plan=plan, bundle=bundle, scientific_repo=scientific_repo,
    )
    output.mkdir(parents=True, exist_ok=True)
    write(output / "wp015c" / "entry-verification.json", verification)
    cases = {row["case_id"]: row for row in plan["cases"]}
    resume_events = resume_pending_cleanups(
        output=output, scientific_repo=scientific_repo, plan_by_case=cases,
    )
    completed = {
        path.stem
        for path in (output / "wp015c" / "cleanup-receipts").glob("*.json")
        if (output / "wp015c" / "cases" / path.stem / "aggregate.json").is_file()
    }
    prior_waves = [read(path) for path in sorted(
        (output / "wp015c" / "waves").glob("reference-*.json")
    )]
    pending = [case_id for case_id in sorted(cases) if case_id not in completed]
    waves = []
    for offset in range(0, len(pending), args.active_cases):
        case_ids = pending[offset: offset + args.active_cases]
        wave_id = f"reference-{canonical_hash(case_ids)[:12]}"
        bytes_before = owned_transient_bytes(scientific_repo, output)
        total_memory, available_memory = linux_memory_bytes()
        if bytes_before > REFILL_CEILING or bytes_before > HARD_CEILING:
            raise RuntimeError("wp015c_reference_refill_ceiling_breach")
        if shutil.disk_usage(scientific_repo).free < MINIMUM_FREE:
            raise RuntimeError("wp015c_reference_free_disk_reserve_breach")
        if available_memory < int(total_memory * RAM_RESERVE_FRACTION):
            raise RuntimeError("wp015c_reference_memory_reserve_breach")
        preparations, preparation_events = run_wave_preparations(
            case_ids=case_ids, cases=cases, plan=plan, output=output,
            scientific_repo=scientific_repo,
            maximum=args.preparation_parallel, wave_id=wave_id,
        )
        after_prepare = owned_transient_bytes(scientific_repo, output)
        if after_prepare > HARD_CEILING:
            raise RuntimeError("wp015c_reference_preparation_hard_ceiling_breach")
        valid = [row for row in preparations if row.get("prepared_workspace")]
        terminals = [row for row in preparations if not row.get("prepared_workspace")]
        seed_results, queue_events = run_wave_seeds(
            preparations=valid, plan=plan, output=output,
            scientific_repo=scientific_repo, wave_id=wave_id,
            maximum_seed_concurrency=args.seed_parallel,
            target_seed_concurrency=args.seed_parallel,
        ) if valid else ([], [])
        aggregates = []
        for prep in valid:
            case_id = prep["case"]["case_id"]
            aggregates.append(persist_case_and_cleanup(
                prep=prep,
                seed_rows=[row for row in seed_results if row["case_id"] == case_id],
                plan=plan, output=output, scientific_repo=scientific_repo,
                wave_id=wave_id,
            ))
        for prep in terminals:
            aggregates.append(persist_preparation_terminal_and_cleanup(
                prep=prep, plan=plan, output=output,
                scientific_repo=scientific_repo, wave_id=wave_id,
            ))
        survivors = owned_survivors([str(output), str(scientific_repo / "dcp_validation_")])
        wave = {
            "wave_id": wave_id, "case_ids": case_ids,
            "terminal_counts": dict(sorted(collections.Counter(
                row["validation"]["terminal"] for row in aggregates
            ).items())),
            "queue_event_count": len(queue_events),
            "preparation_event_count": len(preparation_events),
            "preparation_peak_running": max(
                (row["running"] for row in preparation_events), default=0,
            ),
            "preparation_minimum_available_memory_bytes": min(
                (row["memory_available_bytes"] for row in preparation_events), default=None,
            ),
            "preparation_minimum_filesystem_free_bytes": min(
                (row["filesystem_free_bytes"] for row in preparation_events), default=None,
            ),
            "owned_transient_bytes_before": bytes_before,
            "owned_transient_bytes_after": owned_transient_bytes(scientific_repo, output),
            "owned_process_survivors": survivors,
        }
        wave["wave_sha256"] = canonical_hash(wave)
        write(output / "wp015c" / "waves" / f"{wave_id}.json", wave)
        waves.append(wave)
        print(json.dumps({
            "wave_id": wave_id, "cases": len(case_ids),
            "terminals": wave["terminal_counts"],
            "owned_gib_after": wave["owned_transient_bytes_after"] / (1024 ** 3),
            "survivors": len(survivors),
        }, sort_keys=True), flush=True)
        if survivors:
            raise RuntimeError("wp015c_reference_owned_process_survivor")
    aggregate_count = len(list((output / "wp015c" / "cases").glob("*/aggregate.json")))
    audit_1 = owned_survivors([str(output), str(scientific_repo / "dcp_validation_")])
    time.sleep(2)
    audit_2 = owned_survivors([str(output), str(scientific_repo / "dcp_validation_")])
    core = {
        "schema_version": "dof.wp015c-reference-wave-run.v1",
        "plan_sha256": plan["plan_sha256"],
        "planned_cases": len(cases), "completed_case_aggregates": aggregate_count,
        "validation_scientific_core_hash": verification["validation_scientific_core_hash"],
        "validation_orchestrator_sha256": file_sha256(Path(__file__)),
        "validation_scheduler_sha256": file_sha256(
            REPO / "optimizer_pipeline" / "dof" / "validation_scheduler.py"
        ),
        "reference_active_cases": args.active_cases,
        "reference_preparation_parallelism": args.preparation_parallel,
        "reference_seed_parallelism": args.seed_parallel,
        "parallelism_authority": (
            "D-WP015C-006_RESOURCE_SENSED_REFERENCE_VALIDATION_20"
            if (
                args.active_cases == 20
                and args.preparation_parallel == 20
                and args.seed_parallel == 20
            )
            else (
                "D-WP015C-005_USER_AUTHORISED_REFERENCE_VALIDATION_20"
                if args.seed_parallel == 20
                else "FROZEN_PLAN_DEFAULT"
            )
        ),
        "resume_cleanup_event_count": len(resume_events),
        "resumed_completed_case_count": len(completed),
        "prior_wave_count": len(prior_waves),
        "waves": [{key: row[key] for key in (
            "wave_id", "case_ids", "terminal_counts", "wave_sha256",
        )} for row in prior_waves + waves],
        "owned_process_audit_1": audit_1, "owned_process_audit_2": audit_2,
        "provider_or_llm_calls": 0, "optimization_calls": 0,
        "wall_seconds": time.time() - started,
    }
    result = core | {"run_sha256": canonical_hash(core)}
    write(output / "wp015c" / "reference-run-summary.json", result)
    return 0 if aggregate_count == len(cases) and not audit_1 and not audit_2 else 2


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scientific-repo", type=Path, required=True)
    parser.add_argument("--active-cases", type=int, default=3)
    parser.add_argument("--preparation-parallel", type=int, default=1)
    parser.add_argument("--seed-parallel", type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.active_cases <= 20:
        raise ValueError("active_cases_must_be_between_1_and_20")
    if not 1 <= args.preparation_parallel <= args.active_cases:
        raise ValueError("preparation_parallel_must_not_exceed_active_cases")
    if not 1 <= args.seed_parallel <= 20:
        raise ValueError("seed_parallel_must_be_between_1_and_20")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
