#!/usr/bin/env python3
"""Freeze canonical validation evidence from WP-015C case/wave records."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.seed_validation import (  # noqa: E402
    canonical_hash, file_sha256, semantic_seed_result,
)
from scripts.run_wp015_seed_parallel_canary import owned_survivors  # noqa: E402


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def verify_wrapper(path: Path, case: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    wrapper = read(path)
    observed = canonical_hash({
        key: value for key, value in wrapper.items() if key != "case_aggregate_sha256"
    })
    if observed != wrapper.get("case_aggregate_sha256"):
        raise RuntimeError("wp015c_case_aggregate_hash_mismatch:" + case["case_id"])
    if wrapper.get("case") != case:
        raise RuntimeError("wp015c_case_payload_mismatch:" + case["case_id"])
    validation = wrapper.get("validation")
    if not isinstance(validation, dict) or not validation.get("terminal"):
        raise RuntimeError("wp015c_validation_terminal_missing:" + case["case_id"])
    return case["case_id"], wrapper


def verify_input(path_expected: tuple[str, str]) -> tuple[str, dict[str, Any]]:
    path_value, expected = path_expected
    path = Path(path_value)
    observed = file_sha256(path) if path.is_file() else None
    return path_value, {
        "path": path_value, "expected_sha256": expected,
        "observed_sha256": observed, "match": observed == expected,
        "bytes": path.stat().st_size if path.is_file() else None,
    }


def run(args: argparse.Namespace) -> int:
    started = time.time()
    plan = read(args.plan)
    cases = plan["cases"]
    if len(cases) != int(plan["case_count"]):
        raise RuntimeError("wp015c_plan_case_count_mismatch")
    if len({row["case_id"] for row in cases}) != len(cases):
        raise RuntimeError("wp015c_duplicate_case_id")
    wrappers: dict[str, dict[str, Any]] = {}
    for case in cases:
        path = args.wave_root / "wp015c" / "cases" / case["case_id"] / "aggregate.json"
        if not path.is_file():
            raise RuntimeError("wp015c_case_aggregate_missing:" + case["case_id"])
        case_id, wrapper = verify_wrapper(path, case)
        receipt = args.wave_root / "wp015c" / "cleanup-receipts" / f"{case_id}.json"
        if not receipt.is_file() or read(receipt).get("prepared_workspace_deleted") is not True:
            raise RuntimeError("wp015c_cleanup_receipt_missing_or_incomplete:" + case_id)
        wrappers[case_id] = wrapper

    expected_seeds = [int(value) for value in plan["functional"]["seeds"]]
    seed_result_count = 0
    reused_seed_count = 0
    for case_id, wrapper in wrappers.items():
        validation = wrapper["validation"]
        expected_hashes = wrapper.get("seed_result_sha256", {})
        if validation.get("all_seeds_present_exactly_once") is True:
            if len(expected_hashes) != len(expected_seeds):
                raise RuntimeError("wp015c_seed_hash_count_mismatch:" + case_id)
            observed_seeds = []
            for seed_id, semantic_hash in sorted(expected_hashes.items()):
                path = args.wave_root / "seeds" / case_id / seed_id / "seed-result.json"
                if not path.is_file():
                    raise RuntimeError("wp015c_seed_result_missing:" + case_id + ":" + seed_id)
                row = read(path)
                if row.get("semantic_result_sha256") != semantic_hash:
                    raise RuntimeError("wp015c_seed_result_wrapper_hash_mismatch:" + case_id + ":" + seed_id)
                if semantic_hash != canonical_hash(semantic_seed_result(row)):
                    raise RuntimeError("wp015c_seed_result_semantic_hash_mismatch:" + case_id + ":" + seed_id)
                observed_seeds.append(int(row["stimulus_seed"]))
                seed_result_count += 1
                reused_seed_count += int(bool(row.get("batch_reused") or row.get("wp015c_reused")))
            if observed_seeds != expected_seeds:
                raise RuntimeError("wp015c_seed_identity_order_mismatch:" + case_id)
        elif expected_hashes:
            raise RuntimeError("wp015c_preparation_terminal_has_seed_hashes:" + case_id)

    inputs = {}
    for case in cases:
        inputs[str(case["golden_dcp"])] = case["golden_sha256"]
        inputs[str(case["revised_dcp"])] = case["revised_sha256"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.hash_workers) as pool:
        input_rows = dict(pool.map(verify_input, inputs.items()))
    input_unchanged = all(row["match"] for row in input_rows.values())
    if not input_unchanged:
        raise RuntimeError("wp015c_final_input_hash_mismatch")
    audit_1 = owned_survivors([str(args.wave_root), str(args.scientific_repo / "dcp_validation_")])
    time.sleep(2)
    audit_2 = owned_survivors([str(args.wave_root), str(args.scientific_repo / "dcp_validation_")])
    case_aggregates = {
        case_id: wrapper["validation"] for case_id, wrapper in sorted(wrappers.items())
    }
    preparation_terminals = [
        {"case": wrapper["case"], "terminal": wrapper["validation"]["terminal"],
         "terminal_reason": wrapper["validation"].get("terminal_reason")}
        for wrapper in wrappers.values()
        if wrapper["validation"].get("all_seeds_present_exactly_once") is not True
    ]
    core = {
        "schema_version": "dof.wp015-seed-validation-batch.v1",
        "orchestration_schema_version": "dof.wp015c-wave-run.v1",
        "plan_sha256": plan["plan_sha256"],
        "planned_cases": len(cases),
        "prepared_cases": len(cases) - len(preparation_terminals),
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
        "planned_seed_units": len(cases) * len(expected_seeds),
        "completed_seed_units": seed_result_count,
        "reused_preparations": sum(bool(row.get("preparation_reused")) for row in wrappers.values()),
        "reused_seed_units": reused_seed_count,
        "terminal_counts": dict(sorted(collections.Counter(
            row["terminal"] for row in case_aggregates.values()
        ).items())),
        "case_aggregates": case_aggregates,
        "case_wrapper_sha256": {
            case_id: wrapper["case_aggregate_sha256"]
            for case_id, wrapper in sorted(wrappers.items())
        },
        "preparation_terminals": preparation_terminals,
        "unique_input_file_count": len(input_rows),
        "input_hashes_unchanged": input_unchanged,
        "input_hash_audit_sha256": canonical_hash(list(input_rows.values())),
        "owned_process_audit_1": audit_1, "owned_process_audit_2": audit_2,
        "provider_or_llm_calls": 0, "optimization_calls": 0,
        "transfer_or_holdout_accesses": 0,
        "finalize_wall_seconds": time.time() - started,
    }
    result = core | {"aggregate_sha256": canonical_hash(core)}
    write(args.output, result)
    print(json.dumps({key: result[key] for key in (
        "planned_cases", "completed_seed_units", "preparation_terminal_count",
        "terminal_counts", "input_hashes_unchanged", "owned_process_audit_1",
        "owned_process_audit_2", "aggregate_sha256",
    )}, sort_keys=True))
    return 0 if len(case_aggregates) == len(cases) and input_unchanged and not audit_1 and not audit_2 else 2


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--wave-root", type=Path, required=True)
    parser.add_argument("--scientific-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hash-workers", type=int, default=8)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
