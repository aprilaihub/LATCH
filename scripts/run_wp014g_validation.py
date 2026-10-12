#!/usr/bin/env python3
"""Five-seed V1 validation for retained WP-014G real-probe children."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import run_wp014f_validation_recon as validator  # noqa: E402


SEEDS = [0xDEADBEEF, 0xE604362F, 0xDBAF2937, 0x5F0B7E3A, 0x23B7FC5D]


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--probes", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 8:
        raise ValueError("wp014g_weekday_jobs_must_be_between_1_and_8")
    if args.output.exists():
        raise FileExistsError(args.output)
    probes = validator.read_json(args.probes / "aggregate.json")
    bundle = validator.read_json(args.bundle)
    cases = []
    for row in probes["rows"]:
        if row["execution"].get("canonical_endpoint") != "VALID_COMPLETION":
            continue
        root = bundle["roots"][row["root_id"]]
        child = args.probes / "cases" / row["probe_id"] / "child.dcp"
        case = {
            "case_id": row["probe_id"],
            "root_id": row["root_id"],
            "mechanism": row["probe_id"],
            "risk": "generated",
            "golden_dcp": root["root_path"],
            "golden_sha256": root["sha256"],
            "revised_dcp": str(child),
            "revised_sha256": row["execution"]["child"]["sha256"],
            "resolved_input_sha256": {
                "golden": root["sha256"],
                "revised": row["execution"]["child"]["sha256"],
            },
        }
        if not child.is_file() or validator.file_sha256(child) != case["revised_sha256"]:
            raise RuntimeError("probe_child_hash_mismatch:" + row["probe_id"])
        cases.append(case)
    if not cases:
        raise RuntimeError("no_valid_probe_children")
    plan = {
        "functional": {
            "vectors_per_seed": 200,
            "precheck_vectors": 100,
            "seeds": SEEDS,
        },
        "boundaries": {"per_case_timeout_seconds": 10800},
    }
    aggregate = validator.run_cases(
        cases, plan, args.output, "multiseed", min(args.jobs, len(cases)),
    )
    summary = {
        "schema_version": "dof.wp014g-five-seed-validation.v1",
        "probe_aggregate_sha256": probes["aggregate_sha256"],
        "planned_cases": len(cases),
        "terminal_counts": aggregate["terminal_counts"],
        "queue_wall_seconds": aggregate["queue_wall_seconds"],
        "summed_case_wall_seconds": aggregate["summed_case_wall_seconds"],
        "all_inputs_unchanged": aggregate["all_inputs_unchanged"],
        "owned_process_survivors": aggregate["owned_process_survivors"],
        "provider_or_llm_calls": 0,
        "sealed_holdout_accesses": 0,
        "accepted": (
            aggregate["terminal_counts"].get("MULTISEED_FUNCTIONAL_PASS") == len(cases)
            and aggregate["all_inputs_unchanged"]
            and not aggregate["owned_process_survivors"]
        ),
        "aggregate_sha256": aggregate["aggregate_sha256"],
    }
    validator.write_json(args.output / "wp014g-validation-summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
