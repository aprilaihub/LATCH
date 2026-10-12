#!/usr/bin/env python3
"""Hash-freeze the WP-015 shared scientific core after bootstrap gates pass."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


REPO = Path(__file__).resolve().parents[1]
COMPONENT_GROUPS = {
    "macro_authority_and_execution": (
        "optimizer_pipeline/dof/single_submission/macro_authority.py",
        "optimizer_pipeline/backends/xilinx/legacy.py",
        "scripts/run_flow_matrix.py",
    ),
    "pg_registry_compiler_runtime": (
        "optimizer_pipeline/dof/single_submission/action_surface_v3.py",
        "optimizer_pipeline/dof/single_submission/language.py",
        "optimizer_pipeline/dof/single_submission/contract_v5.py",
        "optimizer_pipeline/dof/single_submission/contract_v6.py",
        "optimizer_pipeline/dof/hardblock_compatibility.py",
        "optimizer_pipeline/dof/vivado_mutation_worker.py",
    ),
    "observation_query_handles": (
        "optimizer_pipeline/dof/e0_job.py",
        "optimizer_pipeline/dof/preflight.py",
        "optimizer_pipeline/dof/single_submission/handles.py",
        "optimizer_pipeline/dof/single_submission/observation_v4.py",
        "optimizer_pipeline/dof/single_submission/planner_v5.py",
        "optimizer_pipeline/dof/single_submission/specs.py",
    ),
    "scorer": (
        "optimizer_pipeline/dof/e1.py",
        "optimizer_pipeline/dof/scientific_evidence.py",
    ),
    "validation": (
        "validate_dcps.py",
        "optimizer_pipeline/dof/seed_validation.py",
        "optimizer_pipeline/dof/validation_scheduler.py",
        "scripts/run_wp015_seed_parallel_canary.py",
        "scripts/run_wp015_seed_validation_batch.py",
        "scripts/finalize_wp015_cell_validation.py",
    ),
    "providers_and_cost": (
        "optimizer_pipeline/dof/provider.py",
        "optimizer_pipeline/dof/multi_provider.py",
        "optimizer_pipeline/dof/gemini_direct.py",
        "optimizer_pipeline/dof/wp015_provider.py",
        ".research-flow/source/WP015-Model-and-Provider-Freeze-v1.json",
        ".research-flow/source/WP015-Pricing-Snapshot-2026-08-14.json",
    ),
    "process_artifact_provenance": (
        "optimizer_pipeline/dof/processes.py",
        "optimizer_pipeline/dof/process_supervisor.py",
        "optimizer_pipeline/dof/artifacts.py",
        "optimizer_pipeline/dof/provenance.py",
    ),
    "protocol_inventory_controller": (
        ".research-flow/source/WP015-Lean-Scientific-Protocol-v1-CN-EN.md",
        ".research-flow/source/WP015-Lean-Matrix-v1.json",
        ".research-flow/source/WP015-Benchmark-Portfolio-Freeze-v1.json",
        ".research-flow/source/WP015-Resource-and-Stop-Policy-v1.md",
        ".research-flow/source/WP015-Analysis-and-Figure-Contract-v2.md",
        "configs/wp015/benchmark-timing-contract-v1.json",
        "scripts/build_wp015_m10_panel.py",
        "scripts/run_wp013_admission.py",
        "scripts/run_wp014c_admission.py",
        "scripts/freeze_wp015_inventory.py",
        "scripts/prepare_wp015_bundle.py",
        "scripts/run_wp015_rapidwright_census.py",
        "scripts/run_wp015_cells.py",
        "scripts/audit_wp015_canary.py",
        "scripts/run_wp015_post_agent_references.py",
        "scripts/analyze_wp015.py",
        "scripts/freeze_wp015_environment.py",
        "scripts/package_wp015_handoff.py",
    ),
}


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def component_hashes() -> dict[str, Any]:
    groups = {}
    all_paths = []
    for name, paths in COMPONENT_GROUPS.items():
        rows = []
        for relative in paths:
            path = REPO / relative
            if not path.is_file():
                raise FileNotFoundError("core_component_missing:" + relative)
            rows.append({"path": relative, "sha256": file_sha256(path)})
            all_paths.append(relative)
        groups[name] = {"files": rows, "group_sha256": digest(rows)}
    if len(all_paths) != len(set(all_paths)):
        raise ValueError("core_component_path_duplicated")
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--m10-freeze", type=Path, required=True)
    parser.add_argument("--stress", type=Path, required=True)
    parser.add_argument("--gate1", type=Path, required=True)
    parser.add_argument("--gate2", type=Path, required=True)
    parser.add_argument("--wp015b-gate1", type=Path, required=True)
    parser.add_argument("--wp015b-gate2", type=Path, required=True)
    parser.add_argument("--wp015b-gate3", type=Path, required=True)
    parser.add_argument("--wp015b-gate4", type=Path, required=True)
    parser.add_argument("--test-report", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    required = {
        "m10_freeze": args.m10_freeze, "stress": args.stress, "gate1": args.gate1,
        "gate2": args.gate2, "test_report": args.test_report,
        "environment": args.environment,
        "wp015b_gate1": args.wp015b_gate1,
        "wp015b_gate2": args.wp015b_gate2,
        "wp015b_gate3": args.wp015b_gate3,
        "wp015b_gate4": args.wp015b_gate4,
    }
    for label, path in required.items():
        if not path.is_file():
            raise FileNotFoundError(label + "_missing:" + str(path))
    gate1 = json.loads(args.gate1.read_text())
    gate2 = json.loads(args.gate2.read_text())
    wp015b_gates = {
        index: json.loads(path.read_text())
        for index, path in enumerate((
            args.wp015b_gate1, args.wp015b_gate2,
            args.wp015b_gate3, args.wp015b_gate4,
        ), start=1)
    }
    m10 = json.loads(args.m10_freeze.read_text())
    gate1_pass = (
        gate1.get("conditions") == ["M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY"]
        and gate1.get("compatible_by_condition") == {
            "G_STRICT_BOUNDARY": 1, "M_ONLY": 1, "P_BOUNDARY": 1,
        }
        and gate1.get("backend_dispatches") == 0
        and gate1.get("physical_model_requests") <= 12
        and gate1.get("retry_count") == gate1.get("fallback_count") == gate1.get("repair_count") == 0
    )
    if not gate1_pass or gate2.get("gate") != "PASS":
        raise ValueError("bootstrap_gate_not_passed")
    if any(value.get("status") != "PASS" for value in wp015b_gates.values()):
        raise ValueError("wp015b_exact_hardblock_gate_not_passed")
    if m10.get("cell_count") != 140 or m10.get("action_count") != 10:
        raise ValueError("m10_freeze_not_complete")
    groups = component_hashes()
    resource_upper_bounds = {
        "physical_model_requests": 12 + 28 * 4 + 1134 * 4,
        "implementation_heavy_calls": 30 * 4 + 28 * 4 + 1134 * 4 + 110 * 4,
        "functional_child_cases": 6 + 140 + 28 + 1134 + 110,
    }
    if (
        resource_upper_bounds["physical_model_requests"] > 5200
        or resource_upper_bounds["implementation_heavy_calls"] > 6500
        or resource_upper_bounds["functional_child_cases"] > 1500
    ):
        raise ValueError("wp015_static_resource_upper_bound_exceeds_hard_cap")
    core = {
        "schema_version": "dof.wp015-shared-scientific-core.v1",
        "protocol_id": "WP015_LEAN_R1_V3",
        "base_protocol_id": "WP015_LEAN_R1",
        "scientific_source_commit": git("rev-parse", "HEAD"),
        "scientific_source_tree": git("rev-parse", "HEAD^{tree}"),
        "component_groups": groups,
        "evidence_hashes": {label: file_sha256(path) for label, path in required.items()},
        "m10_freeze_sha256": m10["freeze_sha256"],
        "resource_upper_bound_proof": resource_upper_bounds,
        "provider_retries": 0, "provider_fallbacks": 0,
        "response_repairs": 0, "postcommit_resubmissions": 0,
        "sealed_holdout_access_before_launch": False,
    }
    result = core | {"shared_scientific_core_hash": digest(core)}
    write(args.output, result)
    print(json.dumps({
        "scientific_source_commit": result["scientific_source_commit"],
        "scientific_source_tree": result["scientific_source_tree"],
        "component_group_count": len(groups),
        "shared_scientific_core_hash": result["shared_scientific_core_hash"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
