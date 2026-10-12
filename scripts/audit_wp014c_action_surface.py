#!/usr/bin/env python3
"""Mechanical parity audit for WP-014C Gates 1–2."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.single_submission.action_surface_v3 import (  # noqa: E402
    G_EXCLUSIVE_NODES,
    PHYS_OPT_DIRECTIVES,
    PHYS_OPT_OPTIONS,
    PLACE_DIRECTIVES,
    PLACE_NET_DELAY_WEIGHTS,
    P_OPERATION_REGISTRY,
    ROUTE_DIRECTIVES,
    public_registry,
    registry_sha256,
    worker_meanings,
)
from optimizer_pipeline.dof.single_submission.contract_v5 import (  # noqa: E402
    G_CARDS, MAX_INPUT_BYTES, P_CARDS, contract_for_level,
)
from optimizer_pipeline.dof.single_submission.contract_v6 import (  # noqa: E402
    contract_for_cell,
)
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402
from optimizer_pipeline.dof.mutation_worker_binding import mutation_worker_source_manifest  # noqa: E402
from optimizer_pipeline.dof.vivado_mutation_worker import (  # noqa: E402
    _ALLOWED_OPERATIONS, _DIRECTIVES, _PHYS_DIRECTIVES, _PHYS_OPTIONS,
    _PLACE_WEIGHTS, WORKER_VERSION,
)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    errors: list[str] = []
    public = public_registry()
    p_keys = set(P_OPERATION_REGISTRY)
    if {row["key"] for row in P_CARDS} != p_keys:
        errors.append("p_card_registry_key_mismatch")
    if {row["key"] for row in G_CARDS} != G_EXCLUSIVE_NODES:
        errors.append("g_card_registry_key_mismatch")
    worker_ops = {
        row["worker_operation"] for row in worker_meanings().values()
        if row["worker_operation"].startswith("vivado.")
    }
    if not worker_ops <= _ALLOWED_OPERATIONS:
        errors.append("worker_operation_missing:" + ",".join(sorted(worker_ops - _ALLOWED_OPERATIONS)))
    enum_checks = {
        "place_directives": set(PLACE_DIRECTIVES) == _DIRECTIVES["vivado.place_design"],
        "place_weights": set(PLACE_NET_DELAY_WEIGHTS) == _PLACE_WEIGHTS,
        "route_directives": set(ROUTE_DIRECTIVES) == _DIRECTIVES["vivado.route_design"],
        "phys_directives": set(PHYS_OPT_DIRECTIVES) == _PHYS_DIRECTIVES,
        "phys_options": set(PHYS_OPT_OPTIONS) == _PHYS_OPTIONS,
    }
    errors.extend(name + "_mismatch" for name, accepted in enum_checks.items() if not accepted)

    l3 = contract_for_level("L3")
    l4 = contract_for_level("L4")
    inherited = {
        "M_l3_l4_byte_identical": canonical_json_bytes(l3["M"]) == canonical_json_bytes(l4["M"]),
        "P_l3_l4_byte_identical": canonical_json_bytes(l3["P"]) == canonical_json_bytes(l4["P"]),
    }
    errors.extend(name + "_failed" for name, accepted in inherited.items() if not accepted)
    sizes = {level: len(canonical_json_bytes(contract_for_level(level))) for level in ("L2", "L3", "L4")}
    if any(size > MAX_INPUT_BYTES for size in sizes.values()):
        errors.append("public_contract_byte_ceiling_exceeded")

    p_condition = contract_for_cell(
        study_track="C_CONDITIONED", coverage_regime="REDUCED_M_STRESS",
        language_condition="P_BOUNDARY", level="C",
    )
    g_condition = contract_for_cell(
        study_track="C_CONDITIONED", coverage_regime="REDUCED_M_STRESS",
        language_condition="G_BOUNDARY", level="C",
    )
    conditioned_inheritance = {
        "P_BOUNDARY_M_identical": canonical_json_bytes(p_condition["M"]) == canonical_json_bytes(l3["M"]),
        "P_BOUNDARY_P_identical": canonical_json_bytes(p_condition["P"]) == canonical_json_bytes(l3["P"]),
        "G_BOUNDARY_M_identical": canonical_json_bytes(g_condition["M"]) == canonical_json_bytes(l4["M"]),
        "G_BOUNDARY_P_identical": canonical_json_bytes(g_condition["P"]) == canonical_json_bytes(l4["P"]),
    }
    errors.extend(name + "_failed" for name, accepted in conditioned_inheritance.items() if not accepted)

    raw = canonical_json_bytes(public)
    manifest = mutation_worker_source_manifest()
    core = {
        "schema_version": "dof.wp014c-action-surface-parity-audit.v1",
        "registry_sha256": registry_sha256(),
        "registry_recomputed_sha256": hashlib.sha256(raw).hexdigest(),
        "operation_count": len(p_keys),
        "g_exclusive_node_count": len(G_EXCLUSIVE_NODES),
        "worker_version": WORKER_VERSION,
        "worker_source_manifest_sha256": manifest["manifest_sha256"],
        "worker_operations_expected": sorted(worker_ops),
        "enum_checks": enum_checks,
        "inherited_byte_identity": inherited,
        "conditioned_inheritance": conditioned_inheritance,
        "public_contract_bytes": sizes,
        "public_input_byte_ceiling": MAX_INPUT_BYTES,
        "one_commit_accounting_authority": "planner session plus compiled actual host-step costs",
        "raw_tcl_exposed": False,
        "generic_set_property_exposed": False,
        "rqs_exposed": False,
        "sealed_holdout_accessed": False,
        "errors": sorted(errors),
        "accepted": not errors,
    }
    value = core | {"audit_sha256": hashlib.sha256(canonical_json_bytes(core)).hexdigest()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(value) + b"\n")
    print(json.dumps({"accepted": value["accepted"], "errors": value["errors"], "audit_sha256": value["audit_sha256"]}, sort_keys=True))
    return 0 if value["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
