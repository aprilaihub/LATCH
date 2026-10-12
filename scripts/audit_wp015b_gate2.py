#!/usr/bin/env python3
"""Mechanically audit exact compiler/validator/worker compatibility closure."""

from __future__ import annotations

import argparse
import base64
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp015_cells as cells  # noqa: E402
from optimizer_pipeline.dof.hardblock_compatibility import (  # noqa: E402
    pair_hash, validate_allocation, validate_relation,
)
from optimizer_pipeline.dof.single_submission.contract_v5 import compile_candidate_v5  # noqa: E402
from optimizer_pipeline.dof.single_submission.language import CompileContext, ConstructionError  # noqa: E402
from optimizer_pipeline.dof.vivado_mutation_worker import (  # noqa: E402
    build_fixed_tcl, parse_request_line, request_bytes,
)


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def run(args: argparse.Namespace) -> int:
    cells.configure()
    cells.configure_wp015b_hardblock_compatibility(args.scan)
    bundle = read(args.bundle)
    selected = str(read(args.scan / "aggregate.json")["selected_admission_root"])
    _observation, private = cells.base._load_trial(bundle, args.capture, selected)
    relation = private["hardblock_compatibility_relation"]
    validate_relation(relation)
    eligible = [row for row in relation["targets"] if row["compatible_site_handles"]]
    ranks = sorted(
        private["registry"].resolve(row["target_handle"], expected_type="hardblock").attributes["path_rank"]
        for row in eligible
    )[:2]
    candidate = {"language": "G", "steps": [
        {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "hardblock", "predicate": {"op": "COMPARE", "field": "path_rank", "operator": "IN", "value": ranks}, "top_k": 2}, "bind": "hardblocks"},
        {"step_id": "s2", "op": "SELECT_WHERE", "args": {"source_type": "site", "predicate": {"op": "COMPARE", "field": "object_type", "operator": "EQ", "value": "site"}, "top_k": 16}, "bind": "sites"},
        {"step_id": "s3", "op": "COUPLE_SITES", "args": {"hardblocks": {"ref": "hardblocks"}, "sites": {"ref": "sites"}, "strategy": "FORWARD"}, "bind": "allocation"},
        {"step_id": "s4", "op": "UNROUTE", "args": {}},
        {"step_id": "s5", "op": "APPLY_ALLOCATION", "args": {"allocation": {"ref": "allocation"}}},
        {"step_id": "s6", "op": "ROUTE", "args": {"directive": "Default"}},
    ]}
    context = CompileContext(
        private["registry"], "ROUTED", private["predefined_regions"],
        private["geometry_bounds"], fanout_admitted=bool(private.get("fanout_admitted")),
        hardblock_compatibility_relation=relation,
    )
    compiled = compile_candidate_v5(candidate, context)
    apply_steps = [step for step in compiled.host_steps if step.operation == "vivado.wp014h_apply_allocation"]
    if len(apply_steps) != 1:
        raise RuntimeError("exact_apply_step_not_unique")
    step = apply_steps[0]
    allocation = private["registry"].resolve(str(step.public_args["allocation"]), expected_type="allocation")
    validate_allocation(
        allocation.attributes, relation,
        root_sha256=private["registry"].root_sha256, stage="ROUTED",
    )
    targets = [private["registry"].resolve(handle, expected_type="hardblock") for handle in step.public_args["targets"]]
    sites = [private["registry"].resolve(handle, expected_type="site") for handle in step.public_args["sites"]]
    target_identity_sha256 = cells.digest([item.canonical_identity for item in targets])
    dispatch_pairs_sha256 = cells.digest([
        {"target": target.canonical_identity, "site": site.canonical_identity}
        for target, site in zip(targets, sites)
    ])
    worker_args = {
        "cell_names_b64": [b64(item.canonical_identity) for item in targets],
        "site_names_b64": [b64(item.canonical_identity) for item in sites],
        "target_site_pair_hashes": list(step.public_args["target_site_pair_hashes"]),
        "compatibility_relation_sha256": step.public_args["compatibility_relation_sha256"],
        "feasibility_query_sha256": step.public_args["feasibility_query_sha256"],
        "allocation_sha256": step.public_args["allocation_sha256"],
        "target_identity_sha256": target_identity_sha256,
        "dispatch_pairs_sha256": dispatch_pairs_sha256,
    }
    request = parse_request_line(request_bytes(
        "vivado.wp014h_apply_allocation", "wp015b-gate2",
        timeout_seconds=900.0, arguments=worker_args,
    ).rstrip())
    tcl = build_fixed_tcl(request)
    tampered_relation_rejected = False
    try:
        compile_candidate_v5(candidate, replace(context, hardblock_compatibility_relation=dict(relation) | {"compatibility_relation_sha256": "0" * 64}))
    except ConstructionError:
        tampered_relation_rejected = True
    checks = {
        "relation_valid": True,
        "exact_matching_size_two": allocation.attributes["matching_size"] == 2,
        "all_pairs_members": all(
            site.handle in next(row["compatible_site_handles"] for row in relation["targets"] if row["target_handle"] == target.handle)
            for target, site in zip(targets, sites)
        ),
        "pair_hashes_exact": list(step.public_args["target_site_pair_hashes"]) == [pair_hash(target.handle, site.handle) for target, site in zip(targets, sites)],
        "allocation_root_stage_bound": allocation.attributes["root_sha256"] == private["registry"].root_sha256 and allocation.attributes["stage"] == "ROUTED",
        "worker_request_exact_proof": request.arguments == worker_args,
        "worker_emits_exact_evidence_marker": "DOF_WP015B_ALLOCATION_EVIDENCE_V1" in tcl and target_identity_sha256 in tcl and dispatch_pairs_sha256 in tcl,
        "tampered_relation_rejected": tampered_relation_rejected,
        "no_backend_dispatch": True,
    }
    core = {
        "schema_version": "dof.wp015b-gate2-audit.v1",
        "status": "PASS" if all(checks.values()) else "STOP_SHARED_GATE",
        "checks": checks, "selected_admission_root": selected,
        "candidate_sha256": cells.digest(candidate),
        "compiled_public_sha256": compiled.public_sha256,
        "compatibility_relation_sha256": relation["compatibility_relation_sha256"],
        "feasibility_query_sha256": allocation.attributes["feasibility_query_sha256"],
        "allocation_sha256": allocation.attributes["allocation_sha256"],
        "target_site_pair_hashes": list(allocation.attributes["target_site_pair_hashes"]),
        "matching_size": allocation.attributes["matching_size"],
        "backend_dispatches": 0,
    }
    value = core | {"audit_sha256": cells.digest(core)}
    cells.write(args.output, value)
    print(json.dumps({key: value[key] for key in ("status", "selected_admission_root", "matching_size")}, sort_keys=True))
    return 0 if value["status"] == "PASS" else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--scan", type=Path, required=True)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--capture", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
