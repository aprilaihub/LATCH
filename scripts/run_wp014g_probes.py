#!/usr/bin/env python3
"""Bounded, provider-free real-tool probes for WP-014G.

The controller deliberately reuses the frozen WP-014C development capture and
the exact Action Surface v3 P consumers.  It creates no model requests and
never touches sealed holdouts.
"""

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
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import run_wp013a_e0 as base  # noqa: E402
import run_wp014c_scientific as wp014c  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import (  # noqa: E402
    boundary_evidence,
    file_sha256,
)
from optimizer_pipeline.dof.single_submission.contract_v5 import (  # noqa: E402
    compile_candidate_v5,
)
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.single_submission.observation_v4 import (  # noqa: E402
    CommonQuerySurfaceV4,
)
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


SCHEMA = "dof.wp014g-real-probe.v1"
PROBE_IDS = (
    "spatial_generated_geometry",
    "net_generated_target_route",
    "hardblock_generated_target_site",
    "property_generated_net_target",
    "net_generated_replication",
)

_MARKER = re.compile(r"DOF_WP014C_[A-Z0-9_]+_V[0-9]+:[^\r\n\"]+")


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


def mechanism_state_deltas(case_dir: Path, *, probe_id: str) -> list[dict[str, Any]]:
    """Project private worker markers into identity-free state-delta evidence."""

    path = case_dir / "private" / "worker-call-outputs.json"
    if not path.is_file():
        return []
    markers: set[str] = set()
    for row in json.loads(path.read_text()):
        if not isinstance(row, Mapping) or not isinstance(row.get("output_b64"), str):
            continue
        try:
            decoded = base64.b64decode(row["output_b64"]).decode(errors="replace")
        except Exception:
            continue
        markers.update(_MARKER.findall(decoded))
    concrete = sorted(marker for marker in markers if "$__dof_" not in marker)
    if not concrete:
        return []
    marker = concrete[-1]
    fields = marker.split(":")
    strength = "MECHANISM_EXACT"
    changed = 1
    if "PROPERTY_V2" in marker:
        strength = "PROPERTY_BEFORE_AFTER"
        before, after = fields[-3], fields[-2]
    elif "ROUTE_NET_V2" in marker:
        strength = "ROUTE_BEFORE_AFTER"
        before, after = fields[-2], fields[-1]
    elif "REPLICATE_NET_V2" in marker:
        strength = "TOPOLOGY_COUNT_BEFORE_AFTER"
        before = ":".join((fields[-4], fields[-2]))
        after = ":".join((fields[-3], fields[-1]))
        changed = abs(int(fields[-3]) - int(fields[-4])) + abs(int(fields[-1]) - int(fields[-2]))
    else:
        before, after = "pre-operation", marker
    return [{
        "state_delta_id": probe_id,
        "mechanism_family": probe_id,
        "before_hash": digest({"probe_id": probe_id, "value": before}),
        "after_hash": digest({"probe_id": probe_id, "value": after}),
        "changed_count": max(1, changed),
        "changed_identity_hash": digest({"probe_id": probe_id, "marker": marker}),
        "evidence_strength": strength,
        "sanitized_marker": marker,
    }]


def _materialize(root: Mapping[str, Any], capture: Path):
    _observation, private = wp014c.common_observation_v3(root, capture)
    queries = CommonQuerySurfaceV4(private["registry"])
    for name, rows in private["rows"].items():
        queries.materialize(
            query_name=name,
            # Frozen common Q permits at most eight rows per request.  Do not
            # let the exploration controller silently widen that information
            # envelope merely because the private capture has more rows.
            top_k=min(8, max(1, len(rows))),
            private_rows=rows,
            tool_call_ids=("wp014g-frozen-development-capture",),
            latency_seconds=0.0,
            missing_reason=private["missing_reasons"].get(name),
        )
    return private


def _predicate(field: str, operator: str, value: Any) -> dict[str, Any]:
    return {"op": "COMPARE", "field": field, "operator": operator, "value": value}


def _candidate(probe_id: str, private: Mapping[str, Any]) -> dict[str, Any]:
    if probe_id == "spatial_generated_geometry":
        x0, _x1, y0, _y1 = private["geometry_bounds"]
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {
                "source_type": "cell", "predicate": _predicate("path_member", "EQ", True), "top_k": 1,
            }, "bind": "targets"},
            {"step_id": "s2", "op": "CREATE_REGION", "args": {
                "x_min": x0, "x_max": x0, "y_min": y0, "y_max": y0,
            }, "bind": "region"},
            {"step_id": "s3", "op": "ASSIGN_REGION", "args": {
                "target_handle": {"ref": "targets"}, "region_handle": {"ref": "region"},
            }},
            {"step_id": "s4", "op": "UNROUTE", "args": {}},
            {"step_id": "s5", "op": "UNPLACE", "args": {}},
            {"step_id": "s6", "op": "PLACE", "args": {"directive": "Default"}},
            {"step_id": "s7", "op": "ROUTE", "args": {"directive": "Default"}},
        ]}
    if probe_id == "net_generated_target_route":
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {
                "source_type": "net", "predicate": _predicate("fanout", "GE", 2), "top_k": 1,
            }, "bind": "target"},
            {"step_id": "s2", "op": "ROUTE_NET", "args": {
                "typed_net": {"ref": "target"}, "delay_mode": "AUTO_DELAY",
            }},
        ]}
    if probe_id == "hardblock_generated_target_site":
        hardblocks = private["registry"].objects(object_type="hardblock")
        sites = private["registry"].objects(object_type="site")
        if not hardblocks or not sites:
            raise RuntimeError("hardblock_or_site_unavailable")
        resource = str(hardblocks[0].attributes.get("resource_type") or "ANY")
        candidates = [
            item.handle for item in sites
            if resource in item.attributes.get("compatible_resource_types", ())
            or "ANY" in item.attributes.get("compatible_resource_types", ())
        ]
        if not candidates:
            raise RuntimeError("compatible_site_unavailable")
        # Pick the final canonical candidate to avoid reproducing the first
        # candidate used by the legacy fixed selector.  Runtime still rejects
        # occupied, incompatible, or no-effect choices.
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {
                "source_type": "hardblock", "predicate": _predicate("resource_type", "EQ", resource), "top_k": 1,
            }, "bind": "target"},
            {"step_id": "s2", "op": "CHOOSE_SITE", "args": {
                "candidate_sites": candidates[:32], "site_index": min(len(candidates), 32) - 1,
            }, "bind": "site"},
            {"step_id": "s3", "op": "UNROUTE", "args": {}},
            {"step_id": "s4", "op": "RELOCATE_HARDBLOCK", "args": {
                "typed_target": {"ref": "target"}, "destination": {"ref": "site"},
            }},
            {"step_id": "s5", "op": "ROUTE", "args": {"directive": "Default"}},
        ]}
    if probe_id == "property_generated_net_target":
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {
                "source_type": "net", "predicate": _predicate("fanout", "GE", 2), "top_k": 1,
            }, "bind": "target"},
            {"step_id": "s2", "op": "SET_NET_DONT_TOUCH", "args": {
                "target_handle": {"ref": "target"}, "value": True,
            }},
            {"step_id": "s3", "op": "UNROUTE", "args": {}},
            {"step_id": "s4", "op": "ROUTE", "args": {"directive": "Default"}},
        ]}
    if probe_id == "net_generated_replication":
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {
                "source_type": "net", "predicate": _predicate("fanout", "GE", 2), "top_k": 1,
            }, "bind": "target"},
            {"step_id": "s2", "op": "UNROUTE", "args": {}},
            {"step_id": "s3", "op": "REPLICATE_NET", "args": {
                "typed_net": {"ref": "target"},
            }},
            {"step_id": "s4", "op": "ROUTE", "args": {"directive": "Default"}},
        ]}
    raise ValueError("unknown_probe_id:" + probe_id)


async def run(args: argparse.Namespace) -> int:
    if args.max_parallel < 1 or args.max_parallel > 8:
        raise SystemExit("weekday_parallelism_must_be_1_to_8")
    if args.out.exists():
        raise FileExistsError(args.out)
    bundle = read(args.bundle)
    selected_probe_ids = tuple(args.probe_id or PROBE_IDS)
    if not selected_probe_ids or any(item not in PROBE_IDS for item in selected_probe_ids):
        raise SystemExit("unknown_or_empty_probe_selection")
    roots_for_probe = {
        "spatial_generated_geometry": "amd_mini-isp",
        "net_generated_target_route": "logicnets_jscl",
        "hardblock_generated_target_site": "amd_mini-isp",
        "property_generated_net_target": "logicnets_jscl",
        "net_generated_replication": "logicnets_jscl",
    }
    roots_before = {
        root_id: file_sha256(bundle["roots"][root_id]["root_path"])
        for root_id in sorted(set(roots_for_probe.values()))
    }
    if any(roots_before[root_id] != bundle["roots"][root_id]["sha256"] for root_id in roots_before):
        raise SystemExit("development_root_hash_mismatch")
    args.out.mkdir(parents=True)
    prepared = []
    for probe_id in selected_probe_ids:
        root_id = roots_for_probe[probe_id]
        root = bundle["roots"][root_id]
        private = _materialize(root, args.capture / root_id)
        candidate = _candidate(probe_id, private)
        context = CompileContext(
            private["registry"], "ROUTED", private["predefined_regions"],
            private["geometry_bounds"], fanout_admitted=True,
            action_surface_version="v3",
        )
        compiled = compile_candidate_v5(candidate, context)
        prepared.append((probe_id, root_id, Path(root["root_path"]), private["registry"], candidate, compiled))

    semaphore = asyncio.Semaphore(args.max_parallel)
    started = time.monotonic()

    async def one(item):
        probe_id, root_id, root, registry, candidate, compiled = item
        case_dir = args.out / "cases" / probe_id
        async with semaphore:
            result = await base._execute_typed(
                compiled=compiled, registry=registry, root=root, out=case_dir,
                python=args.python, vivado=args.vivado,
            )
        result["mechanism_state_deltas"] = mechanism_state_deltas(
            case_dir, probe_id=probe_id,
        )
        bound_calls = [
            row.get("arguments_sha256") for row in result.get("call_trace", [])
            if isinstance(row, Mapping)
            and str(row.get("operation", "")).startswith("vivado.wp014c_")
            and row.get("arguments_sha256")
        ]
        if bound_calls:
            result["selected_object_identity_hash"] = digest(bound_calls)
        if result["mechanism_state_deltas"]:
            result["transformed_object_identity_hash"] = digest([
                row["changed_identity_hash"]
                for row in result["mechanism_state_deltas"]
            ])
        evidence = boundary_evidence(
            candidate=candidate, execution_started=True, execution=result,
        )
        row = {
            "probe_id": probe_id,
            "root_id": root_id,
            "root_sha256": file_sha256(root),
            "candidate": candidate,
            "candidate_sha256": digest(candidate),
            "compiled_public_sha256": compiled.public_sha256,
            "compiled_private_plan_sha256": compiled.private_plan_sha256,
            "planned_heavy_calls": compiled.heavy_calls,
            "planned_light_mutations": compiled.light_mutations,
            "execution": result,
            "causal_evidence": evidence,
            "causal_lineage_sha256": evidence["producer_consumer_lineage"]["lineage_sha256"],
        }
        write(case_dir / "wp014g-probe-result.json", row)
        return row

    rows = await asyncio.gather(*(one(item) for item in prepared))
    roots_after = {
        root_id: file_sha256(bundle["roots"][root_id]["root_path"])
        for root_id in roots_before
    }
    core = {
        "schema_version": SCHEMA,
        "not_a_wp015_run": True,
        "provider_requests": 0,
        "sealed_holdouts_accessed": False,
        "probe_count": len(rows),
        "actual_heavy_calls": sum(int(row["execution"].get("actual_heavy_calls") or 0) for row in rows),
        "actual_light_mutations": sum(int(row["execution"].get("actual_light_mutations") or 0) for row in rows),
        "queue_wall_seconds": time.monotonic() - started,
        "roots_before": roots_before,
        "roots_after": roots_after,
        "all_roots_unchanged": roots_before == roots_after,
        "endpoint_counts": {
            endpoint: sum(row["execution"].get("canonical_endpoint") == endpoint for row in rows)
            for endpoint in sorted({str(row["execution"].get("canonical_endpoint")) for row in rows})
        },
        "causal_g_exercised": sum(bool(row["causal_evidence"]["boundary_runtime_exercised"]) for row in rows),
        "rows": rows,
    }
    value = core | {"aggregate_sha256": digest(core)}
    write(args.out / "aggregate.json", value)
    print(json.dumps({
        "probe_count": len(rows), "actual_heavy_calls": value["actual_heavy_calls"],
        "endpoint_counts": value["endpoint_counts"],
        "causal_g_exercised": value["causal_g_exercised"],
        "all_roots_unchanged": value["all_roots_unchanged"],
    }, sort_keys=True))
    return 0 if value["all_roots_unchanged"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--capture", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--vivado", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=8)
    value.add_argument("--probe-id", action="append", choices=PROBE_IDS)
    return value


if __name__ == "__main__":
    wp014c.configure()
    raise SystemExit(asyncio.run(run(parser().parse_args())))
