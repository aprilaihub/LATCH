#!/usr/bin/env python3
"""Freeze/execute non-scientific real admissions for WP-012-02 witnesses."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from optimizer_pipeline.dof.action_language_probe import (
    PROBE_IDENTITY,
    place_route_decomposition_program,
    validate_g_ast,
    validate_p_program,
)
from optimizer_pipeline.dof.contracts import DoFLevel
from optimizer_pipeline.dof.mutation_worker_binding import (
    verified_fake_attestation_evidence_sha256,
)
from optimizer_pipeline.dof.real_mutation import (
    build_e0_dispatch_authorization,
    execute_e0_real_gated_plan,
    freeze_e0_real_gated_plan,
    load_e0_real_gated_plan,
    write_e0_gated_plan,
)


ROOTS = {
    "vexriscv_re-place": "8baf6451f9b20b8a82f035d14187c7350529a6ea0330e02d93c7100b19753149",
    "external_compatibility_a": "7fe49fa4a73f43cb20b57fff9c92ecfb2d6a771372b0229b7fa5199fa86c01e1",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _write(path: Path, value: Any) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def _witness(name: str) -> tuple[str, DoFLevel, dict[str, Any]]:
    if name == "p_decomposition":
        return "P", DoFLevel.L3, validate_p_program(place_route_decomposition_program())
    if name == "p_novel_spatial":
        return (
            "P",
            DoFLevel.L4,
            validate_p_program(
                {
                    "program_id": "typed_spatial_real",
                    "steps": [
                        {"op": "SELECT_TARGET", "parameters": {"selector": "sequential_cells", "limit": 16}},
                        {"op": "CREATE_STANDARD_PBLOCK", "parameters": {"pblock_id": "typed_real", "region": "critical_path_window"}},
                    ],
                }
            ),
        )
    if name == "g_ast_wrapper":
        return (
            "G_AST",
            DoFLevel.L4,
            validate_g_ast(
                {
                    "program_id": "generated_real_wrapper",
                    "steps": [
                        {"op": "SELECT", "parameters": {"object_type": "cell", "predicates": [{"property": "IS_SEQUENTIAL", "operator": "EQ", "value": True}, {"property": "SLACK_NS", "operator": "LE", "value": 0.0}], "limit": 16}},
                        {"op": "CREATE_PBLOCK", "parameters": {"pblock_id": "generated_real", "x0": 10, "y0": 10, "x1": 49, "y1": 99}},
                        {"op": "PHYS_OPT", "parameters": {"directive": "Explore"}},
                    ],
                }
            ),
        )
    raise ValueError("unknown_witness")


def _gated(program: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    stage = program["initial_stage"]
    for item in program["compiled_steps"]:
        action = item.get("action_key")
        if not action:
            continue
        if item.get("legacy_symbolic"):
            raise ValueError("legacy_symbolic_action_not_real_admissible")
        final = item.get("expanded", {}).get("final_stage", stage)
        if action == "generated.pblock_region":
            final = stage
        rows.append(
            {
                "step_index": len(rows),
                "step_id": f"probe-step-{len(rows):02d}",
                "action_key": action,
                "parameters": item["parameters"],
                "stage_before": stage,
                "stage_after": final,
            }
        )
        stage = final
    return rows


def freeze(args: argparse.Namespace) -> int:
    if args.root_pseudonym not in ROOTS or args.root_pseudonym.startswith("external_family_holdout"):
        raise SystemExit("development_root_not_allowed")
    observed = hashlib.sha256(args.root_dcp.read_bytes()).hexdigest()
    if observed != ROOTS[args.root_pseudonym]:
        raise SystemExit("development_root_hash_mismatch")
    language, level, program = _witness(args.witness)
    evidence_core = {
        "schema_version": "dof.wp012-02-real-admission-causal-evidence.v1",
        "probe_identity": PROBE_IDENTITY,
        "primary_aggregate_eligible": False,
        "scientific_selection_eligible": False,
        "sealed_holdouts_accessed": False,
        "witness": args.witness,
        "language": language,
        "program_sha256": program["program_sha256"],
        "root_pseudonym": args.root_pseudonym,
        "root_sha256": observed,
        "qor_available_before_commit": False,
        "selection_policy": "root_only_technical_admission",
    }
    evidence = evidence_core | {"causal_evidence_sha256": _hash(evidence_core)}
    proposal_hash = _hash(program)
    gate = {
        "accepted": True,
        "probe_identity": PROBE_IDENTITY,
        "witness": args.witness,
        "program_sha256": program["program_sha256"],
        "primary_aggregate_eligible": False,
    }
    run_id = "wp01202-" + args.witness.replace("_", "-")
    plan = freeze_e0_real_gated_plan(
        args.root_dcp,
        root_pseudonym=args.root_pseudonym,
        level=level,
        decision_id=run_id + "-decision",
        proposal_id=run_id + "-proposal",
        proposal_hash=proposal_hash,
        gate_decision_sha256=_hash(gate),
        gated_steps=_gated(program),
        causal_evidence_sha256=evidence["causal_evidence_sha256"],
        run_id=run_id,
        repository_anchor=REPOSITORY_ROOT,
        python_exec=args.python_exec,
        vivado_exec=args.vivado_exec,
        run_dir=args.run_dir,
        fake_startup_attestation_sha256=verified_fake_attestation_evidence_sha256(args.fake_attestation),
        protocol="e1v2",
        scientific_selection_eligible=False,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    write_e0_gated_plan(plan, args.output / "plan.json")
    _write(args.output / "program.json", program)
    _write(args.output / "causal-evidence.json", evidence)
    _write(args.output / "gate.json", gate)
    _write(args.output / "dispatch-authorization.json", build_e0_dispatch_authorization(plan, evidence))
    print(json.dumps({"status": "FROZEN", "plan_sha256": plan["plan_sha256"], "heavy_calls": plan["limits"]["event_limits"]["IMPLEMENTATION_HEAVY"]}))
    return 0


def execute(args: argparse.Namespace) -> int:
    plan = load_e0_real_gated_plan(args.bundle / "plan.json")
    evidence = json.loads((args.bundle / "causal-evidence.json").read_text())
    authorization = json.loads((args.bundle / "dispatch-authorization.json").read_text())
    result = execute_e0_real_gated_plan(
        plan,
        args.root_dcp,
        args.run_dir,
        dispatch_authorization=authorization,
        causal_evidence=evidence,
        vivado_exec=args.vivado_exec,
        python_exec=args.python_exec,
        repository_anchor=REPOSITORY_ROOT,
    )
    _write(args.bundle / "result.json", result)
    print(json.dumps({"endpoint": result["endpoint"], "root_unchanged": result["root_unchanged"], "real_mutation_invocations": result["real_mutation_invocations"]}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--witness", choices=("p_decomposition", "p_novel_spatial", "g_ast_wrapper"), required=True)
    freeze_parser.add_argument("--root-pseudonym", required=True)
    freeze_parser.add_argument("--root-dcp", type=Path, required=True)
    freeze_parser.add_argument("--run-dir", type=Path, required=True)
    freeze_parser.add_argument("--output", type=Path, required=True)
    freeze_parser.add_argument("--vivado-exec", type=Path, required=True)
    freeze_parser.add_argument("--python-exec", type=Path, required=True)
    freeze_parser.add_argument("--fake-attestation", type=Path, required=True)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--bundle", type=Path, required=True)
    execute_parser.add_argument("--root-dcp", type=Path, required=True)
    execute_parser.add_argument("--run-dir", type=Path, required=True)
    execute_parser.add_argument("--vivado-exec", type=Path, required=True)
    execute_parser.add_argument("--python-exec", type=Path, required=True)
    args = parser.parse_args()
    return freeze(args) if args.command == "freeze" else execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
