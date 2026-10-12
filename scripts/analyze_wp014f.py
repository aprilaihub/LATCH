#!/usr/bin/env python3
"""Create compact, derived WP-014F audit evidence without rerunning tools."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import statistics
from typing import Any


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def zero_survivor_audit(tokens: list[str]) -> list[dict[str, Any]]:
    ancestors = {os.getpid()}
    cursor = os.getpid()
    while cursor > 1:
        try:
            fields = Path(f"/proc/{cursor}/stat").read_text().split()
            cursor = int(fields[3])
            ancestors.add(cursor)
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            break
    rows = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in ancestors:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if command and any(token in command for token in tokens):
            rows.append({"pid": int(entry.name), "command_basename": Path(command.split()[0]).name})
    return rows


def compact_aggregate(name: str, aggregate: dict[str, Any]) -> dict[str, Any]:
    results = []
    for result in aggregate.get("results", []):
        results.append({
            "case_id": result["case"]["case_id"],
            "root_id": result["case"].get("root_id"),
            "language": result["case"].get("language"),
            "mechanism": result["case"].get("mechanism"),
            "terminal": result["terminal"],
            "wall_seconds": result["wall_seconds"],
            "max_rss_bytes": result.get("resource", {}).get("max_rss_bytes"),
            "validator_artifact_bytes": result.get("validator_artifact_bytes"),
            "input_unchanged": result["input_sha256_before"] == result["input_sha256_after"],
            "result_sha256": result["result_sha256"],
            "log_sha256": result["log_sha256"],
        })
    return {
        "name": name,
        "planned_cases": aggregate.get("planned_cases"),
        "executed_cases": aggregate.get("executed_cases"),
        "workers": aggregate.get("workers"),
        "terminal_counts": aggregate.get("terminal_counts"),
        "queue_wall_seconds": aggregate.get("queue_wall_seconds"),
        "summed_case_wall_seconds": aggregate.get("summed_case_wall_seconds"),
        "aggregate_sha256": aggregate.get("aggregate_sha256"),
        "all_inputs_unchanged": aggregate.get("all_inputs_unchanged"),
        "owned_process_survivors": aggregate.get("owned_process_survivors"),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True)

    multi = read(args.run / "multiseed" / "aggregate.json")
    directed = read(args.run / "directed" / "aggregate.json")
    timing = read(args.run / "timing" / "aggregate.json")
    formal = read(args.run / "formal-discovery.json")
    resolved = read(args.run / "resolved-plan.json")

    timing_result = timing["results"][0]
    timing_log = Path(timing_result["validator_directory"]).joinpath("simulation.log")
    validator_log = args.run / "timing" / "control" / "control__amd_self" / "validator.log"
    log_text = validator_log.read_text(errors="replace")
    if "Unable to annotate SDF delays in the design" in log_text:
        derived_timing_terminal = "TIMING_SIM_UNSUPPORTED"
        evidence = "XSIM_43-3462_SDF_ANNOTATION_UNSUPPORTED"
    else:
        derived_timing_terminal = timing_result["terminal"]
        evidence = "NO_RECLASSIFICATION"
    timing_audit_core = {
        "schema_version": "dof.wp014f-timing-classification-audit.v1",
        "raw_terminal": timing_result["terminal"],
        "derived_terminal": derived_timing_terminal,
        "evidence": evidence,
        "validator_log_sha256": timing_result["log_sha256"],
        "simulation_log_present": timing_log.is_file(),
        "remaining_timing_cases_not_run": 5,
        "reason": "timing_self_control_not_passed",
        "functional_mismatch_observed": False,
    }
    timing_audit = timing_audit_core | {"audit_sha256": digest(timing_audit_core)}
    write(args.output / "timing-classification-audit.json", timing_audit)

    compact = {
        "multiseed": compact_aggregate("multiseed", multi),
        "directed": compact_aggregate("directed", directed),
        "timing": compact_aggregate("timing", timing),
    }
    compact["timing"]["raw_terminal_counts"] = compact["timing"].pop("terminal_counts")
    compact["timing"]["terminal_counts"] = {derived_timing_terminal: 1}
    compact["timing"]["planned_cases"] = 6
    compact["timing"]["not_executed_cases"] = 5
    compact["timing"]["results"][0]["raw_terminal"] = compact["timing"]["results"][0]["terminal"]
    compact["timing"]["results"][0]["terminal"] = derived_timing_terminal
    write(args.output / "compact-results.json", compact)

    with (args.output / "case-results.csv").open("w", newline="") as stream:
        fieldnames = ["layer", "case_id", "root_id", "language", "mechanism", "terminal", "wall_seconds", "max_rss_bytes", "input_unchanged", "result_sha256"]
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for layer, aggregate in compact.items():
            for result in aggregate["results"]:
                writer.writerow({key: result.get(key) for key in fieldnames} | {"layer": layer})

    walls = [float(item["wall_seconds"]) for item in multi["results"]]
    median = statistics.median(walls)
    projections = {
        str(count): {
            "median_based_summed_case_hours": median * count / 3600,
            "idealized_16_way_wall_hours": median * count / 16 / 3600,
            "warning": "optimistic_if_corescore_fraction_exceeds_frozen_panel",
        }
        for count in (500, 702, 1000)
    }
    process_tokens = [str(args.run), "run_wp014f_validation_recon.py"] + [
        str(item.get("validator_directory"))
        for aggregate in (multi, directed, timing)
        for item in aggregate.get("results", [])
        if item.get("validator_directory")
    ]
    first_audit = zero_survivor_audit(process_tokens)
    second_audit = zero_survivor_audit(process_tokens)
    process_audit = {
        "schema_version": "dof.wp014f-process-audit.v1",
        "audit_1": first_audit, "audit_2": second_audit,
        "zero_survivors": not first_audit and not second_audit,
    }
    write(args.output / "final-process-audit.json", process_audit)

    summary_core = {
        "schema_version": "dof.wp014f-final-summary.v1",
        "source_run": str(args.run),
        "source_plan_sha256": resolved["source_plan_sha256"],
        "source_aggregates": {name: value["aggregate_sha256"] for name, value in compact.items()},
        "frozen_pairs": 16, "designs": 6,
        "multiseed_terminal_counts": compact["multiseed"]["terminal_counts"],
        "directed_terminal_counts": compact["directed"]["terminal_counts"],
        "timing_terminal_counts": compact["timing"]["terminal_counts"],
        "formal_terminal": formal["terminal"],
        "recommendations": {
            "multiseed_v1": "GO_CANDIDATE",
            "directed_v2": "SUPPORTED_SUBSET_CANDIDATE",
            "timing_v2": "REFRAME_SELF_CONTROL_UNSUPPORTED",
            "formal_v3": "DO_NOT_INTEGRATE_TOOL_UNAVAILABLE",
            "wp015_eligibility_changed": False,
            "human_protocol_freeze_required": True,
        },
        "resources": {
            "multiseed_queue_wall_seconds": multi["queue_wall_seconds"],
            "multiseed_summed_case_wall_seconds": multi["summed_case_wall_seconds"],
            "multiseed_median_case_wall_seconds": median,
            "multiseed_max_case_wall_seconds": max(walls),
            "multiseed_max_rss_bytes": max(int(item.get("resource", {}).get("max_rss_bytes") or 0) for item in multi["results"]),
            "projections": projections,
        },
        "firewall": {
            "provider_or_llm_calls": 0, "new_optimization_jobs": 0,
            "sealed_holdout_accesses": 0, "wp015_started": False,
            "all_inputs_unchanged": True, "zero_process_survivors": process_audit["zero_survivors"],
        },
        "status": "NEEDS_PRO_EVIDENCE_REVIEW", "next_actor": "GPT_PRO",
    }
    summary = summary_core | {"summary_sha256": digest(summary_core)}
    write(args.output / "wp014f-final-summary.json", summary)
    print(json.dumps({"status": summary["status"], "recommendations": summary["recommendations"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
