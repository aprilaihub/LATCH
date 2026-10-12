#!/usr/bin/env python3
"""Run the memory-aware WP-014I sixteen-case validation canary."""

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

from optimizer_pipeline.dof.validation_scheduler import (  # noqa: E402
    GIB, MemoryAwareValidationScheduler, ValidationCase, linux_memory_bytes,
)
from scripts.run_wp014f_validation_recon import (  # noqa: E402
    file_sha256, object_sha256, resolve_cases, run_case,
)


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


def _self_case(selection: dict[str, Any], dcp: Path, sha256: str) -> dict[str, Any]:
    if not dcp.is_file() or file_sha256(dcp) != sha256:
        raise ValueError("canary_input_hash_mismatch:" + selection["case_id"])
    return {
        **selection,
        "mechanism": selection.get("mechanism", "golden_self"),
        "golden_dcp": str(dcp),
        "revised_dcp": str(dcp),
        "golden_sha256": sha256,
        "revised_sha256": sha256,
        "resolved_input_sha256": {"golden": sha256, "revised": sha256},
        "sealed_holdout_accesses": int(selection.get("validation_only_external_access", False)),
    }


def resolve(config: dict[str, Any]) -> list[dict[str, Any]]:
    legacy_plan_path = REPO / config["legacy_plan"]
    legacy_plan = read(legacy_plan_path)
    legacy = {row["case_id"]: row for row in resolve_cases(legacy_plan)}
    rows: list[dict[str, Any]] = []
    for selection in config["cases"]:
        kind = selection["kind"]
        if kind == "legacy":
            row = dict(legacy[selection["source_case_id"]])
            row.update(selection)
            row.pop("source_case_id", None)
        elif kind == "direct_self":
            row = _self_case(selection, Path(selection["dcp"]), selection["sha256"])
        elif kind == "synthetic_self":
            candidate_id = selection["candidate_id"]
            result_path = (
                Path(config["synthetic_run"]) / "final" / candidate_id / "final-result.json"
            )
            result = read(result_path)
            if result.get("terminal") != "SYNTHETIC_V0_SCORE_READY":
                raise ValueError("synthetic_input_not_v0_ready:" + candidate_id)
            row = _self_case(
                selection, Path(result["final_dcp"]), result["final_dcp_sha256"]
            )
            row["synthetic_certificate_sha256"] = object_sha256({
                "period_ns": result.get("period_ns"),
                "target": result.get("target"),
                "final_probe": result.get("final_probe"),
            })
        else:
            raise ValueError("unknown_canary_case_kind:" + str(kind))
        rows.append(row)
    expected = int(config["limits"]["maximum_complete_cases"])
    if len(rows) != expected or len({row["case_id"] for row in rows}) != expected:
        raise ValueError("canary_requires_exactly_16_unique_cases")
    if sum(row["memory_class"] == "high" for row in rows) < 2:
        raise ValueError("canary_requires_at_least_two_high_memory_cases")
    return rows


def run_memory_aware(
    cases: list[dict[str, Any]], plan: dict[str, Any], output: Path,
    config: dict[str, Any], *, resume: bool = False,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=resume)
    limits = config["limits"]
    estimates = {
        key: int(value) * GIB for key, value in config["memory_estimates_gib"].items()
    }
    by_id = {row["case_id"]: row for row in cases}
    results: list[dict[str, Any]] = []
    reused_case_ids: list[str] = []
    interrupted_artifacts: list[str] = []
    if resume:
        interrupted_root = output.parent / "interrupted-attempts"
        for case_id, case in by_id.items():
            case_dir = output / case_id
            result_path = case_dir / "result.json"
            if result_path.is_file():
                result = read(result_path)
                claimed = result.get("result_sha256")
                unsigned = dict(result)
                unsigned.pop("result_sha256", None)
                if claimed != hashlib.sha256(
                    json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest():
                    raise ValueError("resume_result_hash_mismatch:" + case_id)
                if result.get("case", {}).get("case_id") != case_id:
                    raise ValueError("resume_case_identity_mismatch:" + case_id)
                expected_hashes = case["resolved_input_sha256"]
                if (
                    result.get("input_sha256_before") != expected_hashes
                    or result.get("input_sha256_after") != expected_hashes
                ):
                    raise ValueError("resume_input_hash_mismatch:" + case_id)
                results.append(result)
                reused_case_ids.append(case_id)
            elif case_dir.exists():
                interrupted_root.mkdir(parents=True, exist_ok=True)
                destination = interrupted_root / case_id
                suffix = 1
                while destination.exists():
                    destination = interrupted_root / f"{case_id}-{suffix:02d}"
                    suffix += 1
                shutil.move(str(case_dir), destination)
                interrupted_artifacts.append(str(destination))
    completed = set(reused_case_ids)
    descriptors = [
        ValidationCase(row["case_id"], row["root_id"], row["memory_class"])
        for row in cases if row["case_id"] not in completed
    ]
    scheduler = MemoryAwareValidationScheduler(
        descriptors,
        maximum=int(limits["maximum_complete_cases"]),
        target=int(limits["target_concurrency"]),
        reserve_fraction=float(limits["reserve_fraction"]),
        estimates=estimates,
    )
    running: dict[concurrent.futures.Future[dict[str, Any]], ValidationCase] = {}
    refill_events: list[dict[str, Any]] = []
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=int(limits["maximum_complete_cases"])
    ) as pool:
        while scheduler.pending or running:
            total, available = linux_memory_bytes()
            admitted = scheduler.refill(
                running=running.values(), total_memory=total,
                available_memory=available,
            )
            refill_events.append({
                "epoch_seconds": time.time(),
                "running_before": len(running),
                "pending_before": len(scheduler.pending) + len(admitted),
                "admitted_case_ids": [row.case_id for row in admitted],
                "memory_total_bytes": total,
                "memory_available_bytes": available,
                "reserve_bytes": int(total * float(limits["reserve_fraction"])),
                "paused_for_memory": bool(scheduler.pending and not admitted),
            })
            for descriptor in admitted:
                future = pool.submit(
                    run_case, by_id[descriptor.case_id], plan, output, "multiseed"
                )
                running[future] = descriptor
            if not running:
                if scheduler.pending:
                    time.sleep(5)
                    continue
                break
            done, _ = concurrent.futures.wait(
                running, timeout=5, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                results.append(future.result())
                del running[future]
    counts = collections.Counter(row["terminal"] for row in results)
    telemetry = [
        row["resource"].get("process_group_telemetry", {}) for row in results
    ]
    core = {
        "schema_version": "dof.wp014i-validation-canary-result.v1",
        "planned_cases": len(cases),
        "executed_cases": len(results),
        "resume": resume,
        "reused_case_ids": sorted(reused_case_ids),
        "newly_executed_case_ids": sorted(
            row["case"]["case_id"] for row in results
            if row["case"]["case_id"] not in completed
        ),
        "interrupted_artifacts": interrupted_artifacts,
        "repository_at_aggregate": {
            "head": git("rev-parse", "HEAD"),
            "tree": git("rev-parse", "HEAD^{tree}"),
        },
        "terminal_counts": dict(sorted(counts.items())),
        "queue_wall_seconds": time.time() - started,
        "summed_case_wall_seconds": sum(row["wall_seconds"] for row in results),
        "refill_events": refill_events,
        "refill_pause_count": sum(row["paused_for_memory"] for row in refill_events),
        "peak_process_group_rss_bytes": max(
            (row.get("peak_process_group_rss_bytes", 0) for row in telemetry), default=0
        ),
        "peak_process_group_swap_bytes": max(
            (row.get("peak_process_group_swap_bytes", 0) for row in telemetry), default=0
        ),
        "peak_system_swap_used_bytes": max(
            (row.get("peak_system_swap_used_bytes", 0) for row in telemetry), default=0
        ),
        "total_case_artifact_bytes": sum(
            row.get("case_directory_bytes_after", 0) for row in telemetry
        ),
        "all_inputs_unchanged": all(
            row["input_sha256_before"] == row["input_sha256_after"] for row in results
        ),
        "owned_process_survivors": [
            row["case"]["case_id"] for row in results if row["cleanup"].get("audit_2")
        ],
        "results": results,
        "provider_or_llm_calls": 0,
        "optimization_calls": 0,
        "external_access_purpose": "VALIDATION_ONLY_NO_OPTIMIZATION",
    }
    return core | {"aggregate_sha256": object_sha256(core)}


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.output.exists() and not args.resume:
        raise FileExistsError(args.output)
    if args.resume and not (args.output / "resolved-plan.json").is_file():
        raise FileNotFoundError("resume_resolved_plan_missing:" + str(args.output))
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    config = read(args.config)
    limits = config["limits"]
    if limits["provider_or_llm_calls"] or limits["optimization_calls"]:
        raise ValueError("canary_firewall_not_zero")
    if int(limits["maximum_complete_cases"]) != 16:
        raise ValueError("canary_requires_frozen_maximum_16")
    if float(limits["reserve_fraction"]) < 0.20:
        raise ValueError("canary_reserve_below_20_percent")
    cases = resolve(config)
    plan = read(REPO / config["legacy_plan"])
    plan["boundaries"]["per_case_timeout_seconds"] = int(
        limits["per_case_timeout_seconds"]
    )
    if not args.resume:
        args.output.mkdir(parents=True)
        write(args.output / "resolved-plan.json", {
            "schema_version": "dof.wp014i-validation-canary-resolved.v1",
            "config_sha256": file_sha256(args.config),
            "repository": {
                "head": git("rev-parse", "HEAD"),
                "tree": git("rev-parse", "HEAD^{tree}"),
            },
            "cases": cases,
        })
    else:
        resolved = read(args.output / "resolved-plan.json")
        if resolved.get("config_sha256") != file_sha256(args.config):
            raise ValueError("resume_config_hash_mismatch")
        prior_cases = {row["case_id"]: row for row in resolved.get("cases", [])}
        current_cases = {row["case_id"]: row for row in cases}
        if prior_cases != current_cases:
            raise ValueError("resume_resolved_cases_mismatch")
    aggregate = run_memory_aware(
        cases, plan, args.output / "cases", config, resume=args.resume
    )
    write(args.output / "aggregate.json", aggregate)
    print(json.dumps({
        key: aggregate[key] for key in (
            "executed_cases", "terminal_counts", "queue_wall_seconds",
            "refill_pause_count", "peak_process_group_rss_bytes",
            "peak_system_swap_used_bytes", "owned_process_survivors",
        )
    }, sort_keys=True))
    expected = {
        row["case_id"]: row.get("expected_terminal") for row in cases
        if row.get("expected_terminal")
    }
    observed = {row["case"]["case_id"]: row["terminal"] for row in aggregate["results"]}
    expected_ok = all(observed.get(case_id) == terminal for case_id, terminal in expected.items())
    return 0 if (
        aggregate["executed_cases"] == 16
        and not aggregate["owned_process_survivors"]
        and aggregate["all_inputs_unchanged"]
        and expected_ok
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
