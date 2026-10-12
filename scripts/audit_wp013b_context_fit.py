#!/usr/bin/env python3
"""Materialise WP-013B maximum-Q schedules and prove the v2.2 byte fit."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path: sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path: sys.path.insert(0, str(REPO / "scripts"))

import run_wp013b_e0 as wp  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v3 import (  # noqa: E402
    MAX_INPUT_BYTES, QUERY_RESULT_MAX_BYTES, QUERY_SESSION_MAX_BYTES,
    QUERY_SESSION_MAX_ROWS, contract_for_level,
)
from optimizer_pipeline.dof.single_submission.observation_v3 import CommonQuerySurfaceV3  # noqa: E402
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--provider-admission", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wp.configure_base()
    plan = wp.base.read_object(args.plan)
    admission = wp.base.read_object(args.provider_admission)
    schedule = [(name, 16) for name in wp.QUERY_NAMES] + [("critical_path_details", 15), ("critical_hardblock_candidates", 15)]
    roots = []
    for root in plan["roots"]:
        observation, private = wp.base._common_observation(root, args.capture / root["root_id"])
        surface = CommonQuerySurfaceV3(private["registry"])
        results = []
        for name, top_k in schedule:
            item = surface.materialize(query_name=name, top_k=top_k, private_rows=private["rows"][name], tool_call_ids=("wp013b-frozen-read-only-capture",), latency_seconds=0.0, missing_reason=private["missing_reasons"].get(name))
            results.append(item.public_payload())
        admitted = tuple(root.get("admitted_action_keys", wp.M_ACTION_KEYS))
        contract = contract_for_level("L4", admitted_action_keys=admitted)
        feedback = [{"error_code": "E" * 64, "location": "L" * 128, "expected_shape_or_schema_ref": "S" * 128, "remaining_turns": 4 - index, "remaining_feedback_rounds": 3 - index, "remaining_query_budget": 8} for index in range(3)]
        final_turn = {"final_submission_turn": 4, "allowed_kinds_this_turn": ["COMMIT", "NO_VALID_SUBMISSION", "STOP"], "turns_remaining": 1, "remaining_feedback_rounds": 0, "remaining_query_budget": 0}
        sections = {"language_contract": contract, "common_observation": dict(observation.payload), "query_results": results, "maximum_feedback_objects": feedback, "final_turn_envelope": final_turn}
        context = {"schema_version": "dof.wp013b-worst-case-context.v1", **sections}
        section_evidence = {key: {"canonical_bytes": len(canonical_json_bytes(value)), "sha256": digest(value)} for key, value in sections.items()}
        actual_q_bytes = sum(len(canonical_json_bytes(item)) for item in results)
        conservative = len(canonical_json_bytes(context)) - len(canonical_json_bytes(results)) + QUERY_SESSION_MAX_BYTES
        checks = {"eight_queries": len(results) == 8, "top_k_schedule_maximal": all(top_k in {15, 16} for _, top_k in schedule), "individual_results_bounded": all(len(canonical_json_bytes(item)) <= QUERY_RESULT_MAX_BYTES for item in results), "aggregate_rows_bounded": surface.materialization_usage["returned_rows"] <= QUERY_SESSION_MAX_ROWS, "aggregate_bytes_bounded": surface.materialization_usage["canonical_result_bytes"] <= QUERY_SESSION_MAX_BYTES, "actual_context_fits": len(canonical_json_bytes(context)) < MAX_INPUT_BYTES, "conservative_full_q_reserve_fits": conservative < MAX_INPUT_BYTES}
        roots.append({"root_id": root["root_id"], "root_sha256": root["sha256"], "regime": root.get("regime"), "section_evidence": section_evidence, "actual_query_payload_bytes": actual_q_bytes, "query_usage": surface.materialization_usage, "canonical_context_bytes": len(canonical_json_bytes(context)), "canonical_context_sha256": digest(context), "conservative_24576_query_reserve_context_bytes": conservative, "checks": checks, "accepted": all(checks.values())})
    l4_sessions = [row for row in admission.get("sessions", []) if row.get("level") == "L4"]
    provider_checks = {model: any(row.get("model") == model and row.get("max_q_result_count") == 8 and row.get("total_input_size_bytes", MAX_INPUT_BYTES + 1) < MAX_INPUT_BYTES and row.get("physical_requests") == 1 for row in l4_sessions) for model in wp.MODELS}
    value = {"schema_version": "dof.wp013b-context-fit-audit.v1", "input_ceiling_bytes": MAX_INPUT_BYTES, "schedule": [{"name": name, "top_k": top_k} for name, top_k in schedule], "roots": roots, "provider_max_q_admission": provider_checks, "provider_admission_sha256": digest(admission), "accepted": len(roots) == 2 and all(row["accepted"] for row in roots) and all(provider_checks.values())}
    value["audit_sha256"] = digest(value)
    args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": value["accepted"], "roots": [{"root_id": row["root_id"], "bytes": row["canonical_context_bytes"], "conservative": row["conservative_24576_query_reserve_context_bytes"]} for row in roots], "provider": provider_checks}, sort_keys=True))
    return 0 if value["accepted"] else 2


if __name__ == "__main__": raise SystemExit(main())
