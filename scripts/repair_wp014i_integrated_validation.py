#!/usr/bin/env python3
"""Complete only missing V1 cases from a closed WP-014I integrated preflight."""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import subprocess
from typing import Any

from run_wp014i_integrated_preflight import (
    git, object_sha256, read, resolve_execution_child, verify_embedded_hash, write,
)
from run_wp014i_validation_canary import run_memory_aware
from run_wp014f_validation_recon import file_sha256


def merge_validation(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    results = list(first["results"]) + list(second["results"])
    counts = collections.Counter(row["terminal"] for row in results)
    core = dict(first)
    core.pop("aggregate_sha256", None)
    core.update({
        "planned_cases": len(results), "executed_cases": len(results),
        "terminal_counts": dict(sorted(counts.items())), "results": results,
        "queue_wall_seconds": float(first["queue_wall_seconds"]) + float(second["queue_wall_seconds"]),
        "summed_case_wall_seconds": sum(float(row["wall_seconds"]) for row in results),
        "refill_events": list(first.get("refill_events", [])) + list(second.get("refill_events", [])),
        "refill_pause_count": int(first.get("refill_pause_count", 0)) + int(second.get("refill_pause_count", 0)),
        "peak_process_group_rss_bytes": max(int(first.get("peak_process_group_rss_bytes", 0)), int(second.get("peak_process_group_rss_bytes", 0))),
        "peak_process_group_swap_bytes": max(int(first.get("peak_process_group_swap_bytes", 0)), int(second.get("peak_process_group_swap_bytes", 0))),
        "peak_system_swap_used_bytes": max(int(first.get("peak_system_swap_used_bytes", 0)), int(second.get("peak_system_swap_used_bytes", 0))),
        "total_case_artifact_bytes": int(first.get("total_case_artifact_bytes", 0)) + int(second.get("total_case_artifact_bytes", 0)),
        "all_inputs_unchanged": bool(first["all_inputs_unchanged"] and second["all_inputs_unchanged"]),
        "owned_process_survivors": list(first.get("owned_process_survivors", [])) + list(second.get("owned_process_survivors", [])),
        "validation_continuation": True,
    })
    return core | {"aggregate_sha256": object_sha256(core)}


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    source = read(args.source / "aggregate.json")
    verify_embedded_hash(source, "aggregate_sha256")
    config = read(args.config)
    bundle = read(Path(config["bundle"]))
    prior = source["validation"]
    completed = {row["case"]["source_job_id"] for row in prior["results"]}
    cases = []
    for row in source["rows"]:
        if row.get("physical_terminal") != "VALID_COMPLETION" or row["job_id"] in completed:
            continue
        execution = row["execution"]
        child = resolve_execution_child(execution, args.source / "execution" / row["job_id"])
        if child is None:
            raise ValueError("physical_valid_child_unresolved:" + row["job_id"])
        root = bundle["roots"][row["root_id"]]
        child_sha = file_sha256(child)
        cases.append({
            "case_id": "integrated__" + row["job_id"], "root_id": row["root_id"],
            "memory_class": "normal", "mechanism": row.get("selected_language"),
            "golden_dcp": root["root_path"], "revised_dcp": str(child),
            "golden_sha256": root["sha256"], "revised_sha256": child_sha,
            "resolved_input_sha256": {"golden": root["sha256"], "revised": child_sha},
            "sealed_holdout_accesses": 0, "source_job_id": row["job_id"],
        })
    if len(cases) != 4:
        raise ValueError("wp014i_validation_repair_requires_exactly_four_cases")
    args.out.mkdir(parents=True)
    validation_plan = read(Path("configs/wp014f/validation-recon-v1.json"))
    validation_plan["boundaries"]["per_case_timeout_seconds"] = int(config["limits"]["validation_timeout_seconds"])
    scheduler = {"limits": config["validation_scheduler"] | {"per_case_timeout_seconds": int(config["limits"]["validation_timeout_seconds"])}, "memory_estimates_gib": config["memory_estimates_gib"]}
    supplement = run_memory_aware(cases, validation_plan, args.out / "supplemental-functional", scheduler)
    write(args.out / "supplemental-functional/aggregate.json", supplement)
    merged = merge_validation(prior, supplement)
    write(args.out / "functional-aggregate.json", merged)
    by_job = {row["case"]["source_job_id"]: row for row in merged["results"]}
    rows = []
    for row in source["rows"]:
        updated = dict(row)
        functional = by_job.get(row["job_id"])
        if functional:
            updated["functional_terminal"] = functional["terminal"]
            updated["functional_result_sha256"] = functional["result_sha256"]
            if functional["terminal"] != "MULTISEED_FUNCTIONAL_PASS":
                updated["canonical_terminal"] = functional["terminal"]
        rows.append(updated)
    core = dict(source)
    core.pop("aggregate_sha256", None)
    core.update({
        "identity": "WP-014I-INTEGRATED-PREFLIGHT-03-VALIDATION-CONTINUATION",
        "repository": {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")},
        "source_aggregate_sha256": source["aggregate_sha256"], "rows": rows,
        "validation": merged, "functional_cases_executed": len(by_job),
        "physical_valid_children_all_received_v1": source["physical_valid_children"] == len(by_job),
        "terminal_counts": dict(sorted(collections.Counter(row["canonical_terminal"] for row in rows).items())),
        "owned_process_survivors": merged["owned_process_survivors"],
    })
    core["accepted_for_freeze_review"] = bool(
        core["canonical_jobs"] == 28 and not core["shared_defect_jobs"]
        and core["all_roots_unchanged"] and not core["owned_process_survivors"]
        and core["physical_valid_children_all_received_v1"]
        and core["call_macro_equivalence"]["host_step_equivalent"]
        and core["call_macro_equivalence"]["heavy_cost_equivalent"]
        and core["physical_requests_including_admission"] <= int(config["limits"]["provider_request_cap_including_admission"])
        and core["api_cost_including_admission_usd"] <= float(config["limits"]["api_cost_cap_including_admission_usd"])
        and core["actual_heavy_calls"] <= int(config["limits"]["implementation_heavy_call_cap"])
    )
    aggregate = core | {"aggregate_sha256": object_sha256(core)}
    write(args.out / "aggregate.json", aggregate)
    print(json.dumps({"missing_cases_executed": len(cases), "functional_cases_executed": len(by_job), "terminal_counts": aggregate["terminal_counts"], "accepted_for_freeze_review": aggregate["accepted_for_freeze_review"]}, sort_keys=True))
    return 0 if aggregate["accepted_for_freeze_review"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
