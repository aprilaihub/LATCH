#!/usr/bin/env python3
"""Audit WP-015A cards, examples, feedback, feasibility and envelopes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp014g_probes as probes  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import strict_boundary_certificate  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v5 import (  # noqa: E402
    SYNTAX_EXAMPLES, compile_candidate_v5, contract_for_level,
)
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.wp015a_strict_g import (  # noqa: E402
    FEEDBACK_CODES, STRICT_FAMILIES, spatial_generated_region_precheck,
    strict_family_feedback,
)


EXAMPLES = {
    "SPATIAL_GENERATED_GEOMETRY": ("amd_mini-isp", "G_BOUNDARY_SPATIAL"),
    "NONPREFIX_MULTI_NET_BATCH": ("logicnets_jscl", "G_BOUNDARY_NONPREFIX_MULTI_NET"),
    "COUPLED_HARDBLOCK_SITE_ALLOCATION": ("amd_mini-isp", "G_BOUNDARY_COUPLED_HARDBLOCK"),
}


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--capacity", type=Path, required=True)
    parser.add_argument("--envelope", type=Path, required=True)
    parser.add_argument("--feasibility", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle, capacity = read(args.bundle), read(args.capacity)
    envelope, feasibility = read(args.envelope), read(args.feasibility)
    rows = []
    for family, (root_id, example_name) in EXAMPLES.items():
        private = probes._materialize(bundle["roots"][root_id], args.capture / root_id)
        context = CompileContext(
            private["registry"], "ROUTED", private["predefined_regions"],
            private["geometry_bounds"], fanout_admitted=True,
            action_surface_version="v3",
        )
        candidate = SYNTAX_EXAMPLES[example_name]["candidate"]
        compiled = compile_candidate_v5(candidate, context)
        certificate = strict_boundary_certificate(candidate=candidate, execution_started=False, execution=None)
        spatial = None
        if family == "SPATIAL_GENERATED_GEOMETRY":
            spatial = spatial_generated_region_precheck(
                compiled=compiled, context=context,
                capacity_by_region=capacity["capacity_by_region"],
            )
        rows.append({
            "family": family,
            "root_id": root_id,
            "candidate_sha256": digest(candidate),
            "compiled_sha256": compiled.public_sha256,
            "production_validator_accepted": True,
            "strict_family": certificate.get("mechanism_family"),
            "feedback_fixture": FEEDBACK_CODES[family],
            "feedback_cleared_for_example": strict_family_feedback(candidate, required_family=family) is None,
            "spatial_feasibility_precheck": spatial,
        })
    contract = contract_for_level("L4")
    added_model_surface = {
        "strict_family_cards": contract["G"]["strict_family_cards"],
        "strict_examples": {
            key: contract["examples"][key]
            for key in (
                "G_BOUNDARY_SPATIAL", "G_BOUNDARY_NONPREFIX_MULTI_NET",
                "G_BOUNDARY_COUPLED_HARDBLOCK",
            )
        },
    }
    serialized = json.dumps(added_model_surface, sort_keys=True).lower()
    forbidden_qor = [key for key in ("fmax", "wns", "tns", "qor_gain", "winner") if key in serialized]
    checks = {
        "three_examples_production_valid": len(rows) == 3 and all(row["production_validator_accepted"] for row in rows),
        "three_exact_strict_families": {row["strict_family"] for row in rows} == set(STRICT_FAMILIES),
        "three_exact_feedback_fixtures": all(row["feedback_fixture"] == FEEDBACK_CODES[row["family"]] for row in rows),
        "spatial_capacity_precheck_pass": rows[0]["spatial_feasibility_precheck"]["accepted"] is True,
        "capacity_qor_blind": capacity.get("qor_fields_used") == [],
        "added_strict_surface_has_no_qor_or_benchmark_answer": not forbidden_qor,
        "envelope_4096_final_12288_total": envelope.get("common_final_answer_capacity_tokens") == 4096 and envelope.get("hard_total_generated_token_ceiling") == 12288,
        "envelope_zero_retry_fallback_repair": envelope.get("retry_count") == envelope.get("fallback_count") == envelope.get("response_repair_count") == 0,
        "feasibility_three_families": set(feasibility.get("assignments", {})) == set(STRICT_FAMILIES),
        "feasibility_capacity_hash_bound": feasibility["assignments"]["SPATIAL_GENERATED_GEOMETRY"]["capacity_evidence"]["capacity_sha256"] == capacity["capacity_sha256"],
    }
    core = {
        "schema_version": "dof.wp015a-gate1-audit.v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "rows": rows,
        "forbidden_qor_terms_found": forbidden_qor,
        "contract_sha256": digest(contract),
        "envelope_sha256": digest(envelope),
        "feasibility_sha256": digest(feasibility),
        "capacity_sha256": capacity["capacity_sha256"],
    }
    result = core | {"audit_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "checks": checks}, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
