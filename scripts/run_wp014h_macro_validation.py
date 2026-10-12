#!/usr/bin/env python3
"""Reconcile immutable Macro campaign endpoints and run required five-seed V1."""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import sys
import time


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp014f_validation_recon as validator  # noqa: E402
from run_wp013_macro_panel import attempted_result  # noqa: E402
from run_wp014h_strict_validation import SEEDS  # noqa: E402


def _verified_reusable_results(
    cases: list[dict], resume_from: Path | None,
) -> tuple[list[dict], list[dict]]:
    """Return immutable completed cases and cases still requiring execution."""
    if resume_from is None:
        return [], cases
    reusable = []
    pending = []
    for case in cases:
        path = resume_from / case["case_id"] / "result.json"
        if not path.is_file():
            pending.append(case)
            continue
        result = validator.read_json(path)
        core = {key: value for key, value in result.items() if key != "result_sha256"}
        if result.get("result_sha256") != validator.object_sha256(core):
            raise ValueError(f"resume_result_hash_mismatch:{case['case_id']}")
        recorded = result.get("case", {})
        expected = case["resolved_input_sha256"]
        if recorded.get("resolved_input_sha256") != expected:
            raise ValueError(f"resume_input_contract_mismatch:{case['case_id']}")
        current = {
            "golden": validator.file_sha256(Path(case["golden_dcp"])),
            "revised": validator.file_sha256(Path(case["revised_dcp"])),
        }
        if current != expected or result.get("input_sha256_after") != expected:
            raise ValueError(f"resume_input_hash_mismatch:{case['case_id']}")
        reusable.append(result)
    return reusable, pending


def _merge_results(
    *, results: list[dict], output: Path, workers: int, started: float,
    reused_from: Path | None,
) -> dict:
    counts = collections.Counter(result["terminal"] for result in results)
    core = {
        "schema_version": "dof.wp014h-resumable-validation-aggregate.v1",
        "mode": "multiseed",
        "planned_cases": len(results),
        "executed_cases": len(results),
        "workers": workers,
        "started_epoch_seconds": started,
        "ended_epoch_seconds": time.time(),
        "queue_wall_seconds": time.time() - started,
        "summed_case_wall_seconds": sum(result["wall_seconds"] for result in results),
        "terminal_counts": dict(sorted(counts.items())),
        "results": results,
        "all_inputs_unchanged": all(
            result["input_sha256_before"] == result["input_sha256_after"]
            for result in results
        ),
        "owned_process_survivors": [
            result["case"]["case_id"] for result in results
            if result["cleanup"].get("audit_2")
        ],
        "reused_completed_cases": sum(
            bool(result.get("wp014h_reused_completed_case")) for result in results
        ),
        "resume_source": str(reused_from) if reused_from else None,
        "provider_or_llm_calls": 0,
        "new_optimization_jobs": 0,
        "sealed_holdout_accesses": 0,
    }
    aggregate = core | {"aggregate_sha256": validator.object_sha256(core)}
    validator.write_json(output / "aggregate.json", aggregate)
    return aggregate


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument(
        "--resume-from", type=Path,
        help="Reuse only hash-verified completed case results from an interrupted run.",
    )
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if not 1 <= args.jobs <= 16:
        raise ValueError("validation_parallelism_must_be_between_1_and_16")
    original = validator.read_json(args.campaign / "aggregate.json")
    bundle = validator.read_json(args.bundle)
    cases = []
    reconciled = []
    for row in original["rows"]:
        job_dir = args.campaign / "jobs" / row["job_id"]
        attempt = attempted_result(job_dir, row["flow"])
        revised = Path(str(attempt.get("output_dcp") or ""))
        updated = dict(row)
        updated["attempt"] = attempt
        updated["endpoint_reconciled_without_rerun"] = True
        updated["actual_child_exists"] = revised.is_file()
        updated["actual_child_sha256"] = validator.file_sha256(revised) if revised.is_file() else None
        reconciled.append(updated)
        if attempt["canonical_endpoint"] != "VALID_COMPLETION" or not revised.is_file():
            continue
        root = bundle["roots"][row["root_id"]]
        cases.append({
            "case_id": row["job_id"],
            "root_id": row["root_id"],
            "mechanism": row["flow"],
            "risk": "topology_changing_rapidwright",
            "golden_dcp": root["root_path"],
            "golden_sha256": root["sha256"],
            "revised_dcp": str(revised),
            "revised_sha256": validator.file_sha256(revised),
            "resolved_input_sha256": {"golden": root["sha256"], "revised": validator.file_sha256(revised)},
        })
    if len(cases) != 4:
        raise RuntimeError(f"four_physically_valid_macro_children_expected:{len(cases)}")
    plan = {
        "functional": {"vectors_per_seed": 200, "precheck_vectors": 100, "seeds": SEEDS},
        "boundaries": {"per_case_timeout_seconds": 10800},
    }
    args.output.mkdir(parents=True)
    started = time.time()
    reused, pending = _verified_reusable_results(cases, args.resume_from)
    reused = [dict(result, wp014h_reused_completed_case=True) for result in reused]
    fresh = []
    if pending:
        partial = validator.run_cases(
            pending, plan, args.output / "fresh", "multiseed",
            min(args.jobs, len(pending)),
        )
        fresh = partial["results"]
    results_by_id = {
        result["case"]["case_id"]: result for result in reused + fresh
    }
    ordered_results = [results_by_id[case["case_id"]] for case in cases]
    aggregate = _merge_results(
        results=ordered_results, output=args.output,
        workers=min(args.jobs, max(1, len(pending))), started=started,
        reused_from=args.resume_from,
    )
    passing = {
        row["case"]["case_id"] for row in aggregate["results"]
        if row["terminal"] == "MULTISEED_FUNCTIONAL_PASS"
    }
    recommendations = {}
    for flow in ("fanout", "lut_cone"):
        flow_rows = [row for row in reconciled if row["flow"] == flow]
        valid = [row for row in flow_rows if row["attempt"]["canonical_endpoint"] == "VALID_COMPLETION"]
        validated = [row for row in valid if row["job_id"] in passing]
        recommendations[flow] = {
            "physical_valid": len(valid),
            "physical_invalid": sum(row["attempt"]["canonical_endpoint"] == "ACTION_INVALID" for row in flow_rows),
            "five_seed_functional_pass": len(validated),
            "technical_candidate": len(valid) >= 2 and len(validated) == len(valid),
            "qor_sign_used_for_admission": False,
            "reliability_caveat": "one LogicNets child was physically invalid" if len(valid) == 2 else None,
        }
    summary = {
        "schema_version": "dof.wp014h-macro-readmission-validation.v1",
        "source_campaign_sha256": original["aggregate_sha256"],
        "source_campaign_rerun": False,
        "endpoint_mapping_repair": "WARNING validation-valid attempts map to valid-artifact endpoint while retaining warnings",
        "cases": len(cases),
        "terminal_counts": aggregate["terminal_counts"],
        "all_inputs_unchanged": aggregate["all_inputs_unchanged"],
        "owned_process_survivors": aggregate["owned_process_survivors"],
        "recommendations": recommendations,
        "macro_universe_candidate": "M10" if all(row["technical_candidate"] for row in recommendations.values()) else "M9" if any(row["technical_candidate"] for row in recommendations.values()) else "M8",
        "final_macro_hash_changed": False,
        "provider_or_llm_calls": 0,
        "sealed_holdout_accesses": 0,
        "validation_aggregate_sha256": aggregate["aggregate_sha256"],
        "reconciled_rows": reconciled,
    }
    validator.write_json(args.output / "macro-readmission-summary.json", summary)
    print(json.dumps({key: summary[key] for key in ("cases", "terminal_counts", "recommendations", "macro_universe_candidate")}, sort_keys=True))
    return 0 if aggregate["all_inputs_unchanged"] and not aggregate["owned_process_survivors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
