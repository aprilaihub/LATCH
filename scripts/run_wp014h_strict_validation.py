#!/usr/bin/env python3
"""Five-seed V1 gate for physically valid WP-014H strict witnesses."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp014f_validation_recon as validator  # noqa: E402


SEEDS = [0xDEADBEEF, 0xE604362F, 0xDBAF2937, 0x5F0B7E3A, 0x23B7FC5D]


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--witnesses", type=Path, action="append", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 16:
        raise ValueError("validation_parallelism_must_be_between_1_and_16")
    if args.output.exists():
        raise FileExistsError(args.output)
    bundle = validator.read_json(args.bundle)
    cases = []
    aggregate_hashes = []
    for witness_dir in args.witnesses:
        witnesses = validator.read_json(witness_dir / "aggregate.json")
        aggregate_hashes.append(witnesses["aggregate_sha256"])
        for row in witnesses["rows"]:
            if row["execution"].get("canonical_endpoint") != "VALID_COMPLETION":
                continue
            if not row["strict_boundary_certificate"].get("strict_boundary_exercised"):
                continue
            root = bundle["roots"][row["root_id"]]
            child = witness_dir / "cases" / row["witness_id"] / "child.dcp"
            case = {
                "case_id": row["witness_id"],
                "root_id": row["root_id"],
                "mechanism": row["strict_boundary_certificate"]["mechanism_family"],
                "risk": "generated_strict",
                "golden_dcp": root["root_path"],
                "golden_sha256": root["sha256"],
                "revised_dcp": str(child),
                "revised_sha256": row["execution"]["child"]["sha256"],
                "resolved_input_sha256": {"golden": root["sha256"], "revised": row["execution"]["child"]["sha256"]},
            }
            if not child.is_file() or validator.file_sha256(child) != case["revised_sha256"]:
                raise RuntimeError("witness_child_hash_mismatch:" + row["witness_id"])
            cases.append(case)
    unique = {case["case_id"]: case for case in cases}
    cases = [unique[key] for key in sorted(unique)]
    if len(cases) != 3:
        raise RuntimeError(f"three_physically_valid_strict_witnesses_required:{len(cases)}")
    plan = {
        "functional": {"vectors_per_seed": 200, "precheck_vectors": 100, "seeds": SEEDS},
        "boundaries": {"per_case_timeout_seconds": 10800},
    }
    aggregate = validator.run_cases(cases, plan, args.output, "multiseed", min(args.jobs, len(cases)))
    summary = {
        "schema_version": "dof.wp014h-strict-witness-validation.v1",
        "witness_aggregate_sha256": aggregate_hashes,
        "planned_cases": len(cases),
        "terminal_counts": aggregate["terminal_counts"],
        "queue_wall_seconds": aggregate["queue_wall_seconds"],
        "summed_case_wall_seconds": aggregate["summed_case_wall_seconds"],
        "all_inputs_unchanged": aggregate["all_inputs_unchanged"],
        "owned_process_survivors": aggregate["owned_process_survivors"],
        "provider_or_llm_calls": 0,
        "sealed_holdout_accesses": 0,
        "accepted": aggregate["terminal_counts"].get("MULTISEED_FUNCTIONAL_PASS") == 3 and aggregate["all_inputs_unchanged"] and not aggregate["owned_process_survivors"],
        "aggregate_sha256": aggregate["aggregate_sha256"],
    }
    validator.write_json(args.output / "wp014h-validation-summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
