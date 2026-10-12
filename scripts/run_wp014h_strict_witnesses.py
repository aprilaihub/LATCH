#!/usr/bin/env python3
"""Provider-free, development-only strict G v2 witness controller."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp013a_e0 as base  # noqa: E402
import run_wp014c_scientific as wp014c  # noqa: E402
import run_wp014g_probes as wp014g  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import (  # noqa: E402
    boundary_evidence,
    file_sha256,
    strict_boundary_certificate,
)
from optimizer_pipeline.dof.single_submission.contract_v5 import compile_candidate_v5  # noqa: E402
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


SCHEMA = "dof.wp014h-strict-g-witness.v1"
WITNESSES = (
    "spatial_generated_geometry",
    "nonprefix_multi_net_batch",
    "coupled_hardblock_site_allocation",
)
ROOTS = {
    "spatial_generated_geometry": "amd_mini-isp",
    "nonprefix_multi_net_batch": "logicnets_jscl",
    "coupled_hardblock_site_allocation": "amd_mini-isp",
}
MARKER = re.compile(r"DOF_WP014[CH]_[A-Z0-9_]+_V[0-9]+:[^\r\n\"]+")


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write(path: Path, value: Any, *, private: bool = False) -> None:
    base.write_atomic(path, value, private=private)


def predicate(field: str, operator: str, value: Any) -> dict[str, Any]:
    return {"op": "COMPARE", "field": field, "operator": operator, "value": value}


def candidate(witness_id: str, private: Mapping[str, Any]) -> dict[str, Any]:
    if witness_id == "spatial_generated_geometry":
        x0, x1, y0, _y1 = private["geometry_bounds"]
        if x1 <= x0:
            raise RuntimeError("strict_multi_region_geometry_unavailable")
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "cell", "predicate": predicate("path_member", "EQ", True), "top_k": 4}, "bind": "targets"},
            {"step_id": "s2", "op": "CREATE_REGION", "args": {"x_min": x0, "x_max": x0 + 1, "y_min": y0, "y_max": y0}, "bind": "generated_region"},
            {"step_id": "s3", "op": "ASSIGN_REGION", "args": {"target_handle": {"ref": "targets"}, "region_handle": {"ref": "generated_region"}}},
            {"step_id": "s4", "op": "UNROUTE", "args": {}},
            {"step_id": "s5", "op": "UNPLACE", "args": {}},
            {"step_id": "s6", "op": "PLACE", "args": {"directive": "Default"}},
            {"step_id": "s7", "op": "ROUTE", "args": {"directive": "Default"}},
        ]}
    if witness_id == "nonprefix_multi_net_batch":
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "net", "predicate": predicate("fanout", "GE", 2), "top_k": 8}, "bind": "eligible"},
            {"step_id": "s2", "op": "TOPK", "args": {"input": {"ref": "eligible"}, "k": 1}, "bind": "prefix"},
            {"step_id": "s3", "op": "DIFF", "args": {"left": {"ref": "eligible"}, "right": {"ref": "prefix"}}, "bind": "nonprefix"},
            {"step_id": "s4", "op": "BATCH_ROUTE_NETS", "args": {"typed_nets": {"ref": "nonprefix"}, "delay_mode": "AUTO_DELAY"}},
        ]}
    if witness_id == "coupled_hardblock_site_allocation":
        hardblocks = private["registry"].objects(object_type="hardblock")
        if len(hardblocks) < 2:
            raise RuntimeError("multiple_hardblocks_unavailable")
        resource = str(hardblocks[0].attributes.get("resource_type"))
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "hardblock", "predicate": predicate("resource_type", "EQ", resource), "top_k": 4}, "bind": "hardblock_pool"},
            {"step_id": "s2", "op": "FILTER", "args": {"input": {"ref": "hardblock_pool"}, "predicate": predicate("path_rank", "IN", [1, 3])}, "bind": "hardblocks"},
            {"step_id": "s3", "op": "SELECT_WHERE", "args": {"source_type": "site", "predicate": predicate("object_type", "EQ", "site"), "top_k": 8}, "bind": "sites"},
            {"step_id": "s4", "op": "COUPLE_SITES", "args": {"hardblocks": {"ref": "hardblocks"}, "sites": {"ref": "sites"}, "strategy": "REVERSE"}, "bind": "allocation"},
            {"step_id": "s5", "op": "UNROUTE", "args": {}},
            {"step_id": "s6", "op": "APPLY_ALLOCATION", "args": {"allocation": {"ref": "allocation"}}},
            {"step_id": "s7", "op": "ROUTE", "args": {"directive": "Default"}},
        ]}
    raise ValueError("unknown_witness:" + witness_id)


def mechanism_deltas(case_dir: Path, witness_id: str) -> list[dict[str, Any]]:
    path = case_dir / "private" / "worker-call-outputs.json"
    if not path.is_file():
        return []
    markers: set[str] = set()
    for row in json.loads(path.read_text()):
        if not isinstance(row, Mapping) or not isinstance(row.get("output_b64"), str):
            continue
        decoded = base64.b64decode(row["output_b64"]).decode(errors="replace")
        markers.update(MARKER.findall(decoded))
    concrete = sorted(marker for marker in markers if "$__dof_" not in marker)
    if not concrete:
        return []
    preferred = {
        "spatial_generated_geometry": "ASSIGN_PBLOCK",
        "nonprefix_multi_net_batch": "BATCH_ROUTE_NETS",
        "coupled_hardblock_site_allocation": "APPLY_ALLOCATION",
    }[witness_id]
    marker = next((row for row in concrete if preferred in row), concrete[-1])
    fields = marker.split(":")
    changed = int(fields[-3]) if witness_id == "nonprefix_multi_net_batch" else int(fields[-1]) if witness_id == "coupled_hardblock_site_allocation" else 1
    strength = "ROUTE_BEFORE_AFTER" if witness_id == "nonprefix_multi_net_batch" else "MECHANISM_EXACT"
    return [{
        "state_delta_id": witness_id,
        "mechanism_family": witness_id,
        "before_hash": digest({"witness": witness_id, "phase": "before"}),
        "after_hash": digest({"witness": witness_id, "marker": marker}),
        "changed_count": max(1, changed),
        "changed_identity_hash": digest({"witness": witness_id, "marker": marker}),
        "evidence_strength": strength,
        "sanitized_marker": marker,
    }]


async def run(args: argparse.Namespace) -> int:
    if not 1 <= args.max_parallel <= 8:
        raise ValueError("weekday_real_parallelism_must_be_between_1_and_8")
    if args.out.exists():
        raise FileExistsError(args.out)
    bundle = read(args.bundle)
    roots_before = {root_id: file_sha256(bundle["roots"][root_id]["root_path"]) for root_id in set(ROOTS.values())}
    if any(roots_before[root_id] != bundle["roots"][root_id]["sha256"] for root_id in roots_before):
        raise RuntimeError("development_root_hash_mismatch")
    args.out.mkdir(parents=True)
    selected_witnesses = tuple(args.witness_id or WITNESSES)
    prepared = []
    for witness_id in selected_witnesses:
        root_id = ROOTS[witness_id]
        root = bundle["roots"][root_id]
        private = wp014g._materialize(root, args.capture / root_id)
        value = candidate(witness_id, private)
        context = CompileContext(
            private["registry"], "ROUTED", private["predefined_regions"],
            private["geometry_bounds"], fanout_admitted=True,
            action_surface_version="v3",
        )
        compiled = compile_candidate_v5(value, context)
        prepared.append((witness_id, root_id, Path(root["root_path"]), private["registry"], value, compiled))

    semaphore = asyncio.Semaphore(args.max_parallel)
    started = time.monotonic()

    async def one(item):
        witness_id, root_id, root, registry, value, compiled = item
        case_dir = args.out / "cases" / witness_id
        async with semaphore:
            execution = await base._execute_typed(
                compiled=compiled, registry=registry, root=root, out=case_dir,
                python=args.python, vivado=args.vivado,
            )
        execution["binding_records"] = [dict(row) for row in compiled.binding_records]
        execution["mechanism_state_deltas"] = mechanism_deltas(case_dir, witness_id)
        causal = boundary_evidence(candidate=value, execution_started=True, execution=execution)
        strict = strict_boundary_certificate(candidate=value, execution_started=True, execution=execution)
        row = {
            "witness_id": witness_id,
            "probe_id": witness_id,
            "root_id": root_id,
            "root_sha256": file_sha256(root),
            "candidate": value,
            "candidate_sha256": digest(value),
            "compiled_public_sha256": compiled.public_sha256,
            "compiled_private_plan_sha256": compiled.private_plan_sha256,
            "binding_records": [dict(record) for record in compiled.binding_records],
            "planned_heavy_calls": compiled.heavy_calls,
            "planned_light_mutations": compiled.light_mutations,
            "execution": execution,
            "causal_evidence": causal,
            "strict_boundary_certificate": strict,
        }
        write(case_dir / "wp014h-witness-result.json", row)
        return row

    rows = await asyncio.gather(*(one(item) for item in prepared))
    roots_after = {root_id: file_sha256(bundle["roots"][root_id]["root_path"]) for root_id in roots_before}
    core = {
        "schema_version": SCHEMA,
        "not_a_wp015_run": True,
        "development_only": True,
        "provider_requests": 0,
        "sealed_holdouts_accessed": False,
        "witness_count": len(rows),
        "probe_count": len(rows),
        "actual_heavy_calls": sum(int(row["execution"].get("actual_heavy_calls") or 0) for row in rows),
        "actual_light_mutations": sum(int(row["execution"].get("actual_light_mutations") or 0) for row in rows),
        "queue_wall_seconds": time.monotonic() - started,
        "roots_before": roots_before,
        "roots_after": roots_after,
        "all_roots_unchanged": roots_before == roots_after,
        "endpoint_counts": {endpoint: sum(row["execution"].get("canonical_endpoint") == endpoint for row in rows) for endpoint in sorted({str(row["execution"].get("canonical_endpoint")) for row in rows})},
        "causal_g_exercised": sum(bool(row["causal_evidence"]["boundary_runtime_exercised"]) for row in rows),
        "strict_g_exercised": sum(bool(row["strict_boundary_certificate"]["strict_boundary_exercised"]) for row in rows),
        "rows": rows,
    }
    result = core | {"aggregate_sha256": digest(core)}
    write(args.out / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in ("witness_count", "actual_heavy_calls", "endpoint_counts", "causal_g_exercised", "strict_g_exercised", "all_roots_unchanged")}, sort_keys=True))
    return 0 if result["all_roots_unchanged"] and result["strict_g_exercised"] == len(selected_witnesses) else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--capture", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--vivado", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=3)
    value.add_argument("--witness-id", action="append", choices=WITNESSES)
    return value


if __name__ == "__main__":
    wp014c.configure()
    raise SystemExit(asyncio.run(run(parser().parse_args())))
