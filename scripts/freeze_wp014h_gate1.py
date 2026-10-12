#!/usr/bin/env python3
"""Create compact, identity-free WP-014H G-v2 strict witness freeze artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.single_submission.action_surface_v3 import public_registry, registry_sha256  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v5 import CONTRACT_VERSION, contract_hash  # noqa: E402


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def object_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--witness-v1", type=Path, required=True)
    parser.add_argument("--hardblock-v2", type=Path, required=True)
    parser.add_argument("--hardblock-v3", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    first = read(args.witness_v1 / "aggregate.json")
    second = read(args.hardblock_v2 / "aggregate.json")
    third = read(args.hardblock_v3 / "aggregate.json")
    validation = read(args.validation / "wp014h-validation-summary.json")
    rows = []
    for source in (first, third):
        for row in source["rows"]:
            certificate = row["strict_boundary_certificate"]
            if not certificate["strict_boundary_exercised"]:
                continue
            cert = certificate["certificates"][0]
            rows.append({
                "witness_id": row["witness_id"],
                "mechanism_family": certificate["mechanism_family"],
                "canonical_endpoint": row["execution"]["canonical_endpoint"],
                "selected_object_count": cert["selected_object_count"],
                "transformed_object_count": cert["transformed_object_count"],
                "child_artifact_hash": cert["child_artifact_hash"],
                "binding_content_hash": cert["binding_content_hash"],
                "actual_target_identity_hash": cert["actual_target_identity_hash"],
                "certificate_sha256": certificate["certificate_sha256"],
                "exact_worker_dispatch_bound": bool(cert["worker_dispatch_id"]),
            })
    if len(rows) != 3 or validation.get("terminal_counts") != {"MULTISEED_FUNCTIONAL_PASS": 3}:
        raise RuntimeError("strict_gate_not_closed")
    registry = public_registry()
    registry_artifact = {
        "schema_version": "dof.wp014h-g-v2-registry-freeze.v1",
        "registry": registry,
        "registry_sha256": registry_sha256(),
    }
    write(args.out / "g-v2-registry.json", registry_artifact)
    code_paths = [
        "optimizer_pipeline/dof/single_submission/action_surface_v3.py",
        "optimizer_pipeline/dof/single_submission/language.py",
        "optimizer_pipeline/dof/single_submission/contract_v5.py",
        "optimizer_pipeline/dof/scientific_evidence.py",
        "optimizer_pipeline/dof/vivado_mutation_worker.py",
    ]
    summary = {
        "schema_version": "dof.wp014h-g-v2-strict-witness-summary.v1",
        "architecture": "O1_TYPED_SSA_WITH_CAUSAL_CERTIFICATE_OVERLAY",
        "witnesses": sorted(rows, key=lambda row: row["witness_id"]),
        "witness_count": 3,
        "mechanism_distinct_count": len({row["mechanism_family"] for row in rows}),
        "validation_terminal_counts": validation["terminal_counts"],
        "validation_aggregate_sha256": validation["aggregate_sha256"],
        "all_roots_and_children_unchanged": validation["all_inputs_unchanged"],
        "owned_process_survivors": validation["owned_process_survivors"],
        "witness_execution_attempts": 5,
        "actual_implementation_heavy_calls": first["actual_heavy_calls"] + second["actual_heavy_calls"] + third["actual_heavy_calls"],
        "failed_attempts_retained": [
            {"identity": "strict-witnesses-v1/coupled_hardblock_site_allocation", "classification": "TOOL_FAILURE_TARGET_COUPLING_DEFECT"},
            {"identity": "strict-hardblock-v2", "classification": "FRAMEWORK_EFFECT_MARKER_PARSE_DEFECT"},
        ],
        "provider_or_llm_calls": 0,
        "sealed_holdout_accesses": 0,
        "source_aggregate_hashes": [first["aggregate_sha256"], second["aggregate_sha256"], third["aggregate_sha256"]],
    }
    summary["summary_sha256"] = object_hash(summary)
    write(args.out / "g-v2-strict-witness-summary.json", summary)
    freeze = {
        "schema_version": "dof.wp014h-g-v2-freeze.v1",
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "contract_version": CONTRACT_VERSION,
        "contract_hashes": {level: contract_hash(level) for level in ("L2", "L3", "L4")},
        "registry_sha256": registry_sha256(),
        "code_sha256": {path: file_hash(REPO / path) for path in code_paths},
        "strict_witness_summary_sha256": summary["summary_sha256"],
        "gate": "GO",
        "claim_boundary": "technical strict reachability and sampled functional readiness only; no QoR or L4 superiority claim",
    }
    freeze["freeze_sha256"] = object_hash(freeze)
    write(args.out / "g-v2-freeze.json", freeze)
    print(json.dumps({"gate": freeze["gate"], "freeze_sha256": freeze["freeze_sha256"], "witnesses": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
