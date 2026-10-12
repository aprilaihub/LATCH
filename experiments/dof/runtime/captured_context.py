"""WP015 handle/Q mapping extracted from run_wp013a_e0._registry_rows.

The accepted exact hardblock path is retained; no historical fallback is enabled.
The raw mapping stays local and is never written into a shareable summary.
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping
from ..protocol.handles import TrialHandleRegistry
from optimizer_pipeline.dof.hardblock_compatibility import build_relation, validate_relation
from optimizer_pipeline.scoring import file_sha256
import secrets


def context_from_capture(*, root: Path, capture: Path, target_clock: str):
    root_hash = file_sha256(root)
    measurement = read_object(capture / "measurement.json")
    census = read_object(capture / "rapidwright-net-census.json")
    score = read_object(capture / "root-score" / "score.json")
    if measurement.get("root_sha256") != root_hash or census.get("root_sha256") != root_hash or score.get("artifact", {}).get("sha256") != root_hash:
        raise ValueError("Captured observations do not bind the input root")
    if score["score"]["target_clock"] != target_clock:
        raise ValueError("Captured target clock differs from validation configuration")
    if not census.get("nets"):
        raise ValueError("Required RapidWright fanout observation missing; no reduced condition substituted")
    secret, trial_id = secrets.token_bytes(32), "latch-wp015-migrated"
    registry, rows, missing, regions, bounds, relation = _registry_rows(capture, root_hash,
        measurement["route"]["status"], secret=secret, trial_id=trial_id, hardblock_scan=capture / "hardblock-scan")
    rows["high_fanout_net_details"] = [{"canonical_identity": r["name"], "handle_type": "net",
        "fanout": int(r["fanout"]), "row_semantics": "rapidwright_nonstatic_wire", "source_tool": "rapidwright"}
        for r in census["nets"]]
    missing.pop("high_fanout_net_details", None)
    # Q materialization normally registers on demand. Register every captured
    # row up front too, so legal prepared proposals use the same identities.
    for values in rows.values():
        for row in values:
            registry.register(object_type=row["handle_type"], canonical_identity=row["canonical_identity"],
                attributes={k: v for k, v in row.items() if k not in {"canonical_identity", "handle_type", "source_tool"}},
                source_tool=row.get("source_tool", "vivado"))
    timing = measurement["timing"]
    period, wns = float(score["score"]["target_period_ns"]), timing.get("wns_ns")
    # WP015's model-visible common observation derives FMax from the same
    # captured WNS and period. The separate scorer remains authoritative for
    # post-COMMIT evaluation; do not substitute that result into the prompt.
    fmax = (1000.0 / (period - float(wns))
        if isinstance(wns, (int, float)) and period - float(wns) > 0 else None)
    return {"root_sha256": root_hash, "trial_id": trial_id, "handle_secret_hex": secret.hex(),
        "objects": [{"object_type": o.object_type, "canonical_identity": o.canonical_identity,
            "attributes": dict(o.attributes), "source_tool": o.source_tool} for o in registry.objects()],
        "queries": rows, "query_missing_reasons": missing, "predefined_regions": regions,
        "geometry_bounds": bounds, "hardblock_compatibility_relation": relation, "fanout_admitted": True,
        "observation": {
            "target_clock": {"clock_period_ns": score["score"]["target_period_ns"], "evidence": "read-only same-clock scorer"},
            "timing": {"fmax_mhz": fmax, "wns_ns": wns, "tns_ns": timing.get("tns_ns"), "failing_endpoint_count": timing.get("failing_endpoints")},
            "utilization": measurement["utilization"],
            "implementation": {"placement_state": "PLACED", "route_state": "ROUTED", "route_validity": measurement["route"]["status"]},
            "geometry": {"device_class": "development-device-pseudonym", "clock_region_bounds": list(bounds), "observed_region_count": len(regions)},
            "constraints": {"pblock_count": None, "constrained_cell_count": None, "missing_reason": "read_only_probe_did_not_report_constraints"},
            "congestion": {"coarse_class": "unknown", "hotspot_count": None, "missing_reason": "read_only_probe_did_not_report_congestion"},
            "critical_paths": [{"path_rank": r["path_rank"], "slack_ns": r["slack_ns"], "delay_ns": r["delay_ns"], "status": "object_details_available_via_Q"} for r in rows["critical_path_details"][:8]]}}

def read_object(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Captured JSON must be an object")
    return value

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()

def _registry_rows(
    capture_root: Path, root_hash: str, route_status: str, *,
    secret: bytes, trial_id: str, hardblock_scan: Path
) -> tuple[
    TrialHandleRegistry,
    dict[str, list[dict[str, Any]]],
    dict[str, str | None],
    tuple[str, ...],
    tuple[int, int, int, int],
    dict[str, Any] | None,
]:
    registry = TrialHandleRegistry(
        trial_id=trial_id, root_sha256=root_hash, secret=secret
    )
    captured = read_object(
        capture_root / "contract-probe" / "private" / "resolved-objects.json"
    )
    rows: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "critical_path_details", "critical_object_locations",
            "high_fanout_net_details", "critical_hardblock_candidates",
            "placement_summary", "route_summary",
        )
    }
    missing: dict[str, str | None] = {name: None for name in rows}

    region_handles: list[str] = []
    bounds: list[list[int]] = []
    regions_by_identity: dict[str, str] = {}
    for item in captured.get("device_regions", []):
        match = re.fullmatch(r"(?:CLOCKREGION_)?X(\d+)Y(\d+)", item["identity"])
        if match is None:
            continue
        coordinate = [
            int(match.group(1)), int(match.group(1)),
            int(match.group(2)), int(match.group(2)),
        ]
        attrs = {"bounds": coordinate, "count": 1, "row_semantics": "observed_clock_region"}
        handle = registry.register(
            object_type="region", canonical_identity=item["identity"], attributes=attrs
        )
        regions_by_identity[item["identity"]] = handle
        region_handles.append(handle)
        bounds.append(coordinate)
        rows["placement_summary"].append(
            {"canonical_identity": item["identity"], "handle_type": "region", **attrs}
        )

    for item in captured.get("timing_paths", []):
        parts = item["attribute"].split("\x1f")
        if len(parts) != 4:
            raise RuntimeError("captured_timing_path_shape_invalid")
        slack, delay, startpoint, endpoint = parts
        start_handle = registry.register(
            object_type="pin", canonical_identity=startpoint,
            attributes={"row_semantics": "timing_path_pin"},
        )
        end_handle = registry.register(
            object_type="pin", canonical_identity=endpoint,
            attributes={"row_semantics": "timing_path_pin"},
        )
        attrs = {
            "path_rank": int(item["index"]) + 1,
            "slack_ns": float(slack),
            "delay_ns": float(delay),
            "startpoint_handle": start_handle,
            "endpoint_handle": end_handle,
            "row_semantics": "real_vivado_timing_path",
        }
        rows["critical_path_details"].append(
            {"canonical_identity": item["identity"], "handle_type": "path", **attrs}
        )

    for item in captured.get("critical_cell_locations", []):
        parts = item["attribute"].split("\x1f")
        if len(parts) != 3:
            raise RuntimeError("captured_critical_location_shape_invalid")
        resource, site, region = parts
        site_handle = registry.register(
            object_type="location_site",
            canonical_identity=site or "UNPLACED:" + item["identity"],
            attributes={"site_class": "observed_location"},
        )
        region_handle = regions_by_identity.get(region)
        if region_handle is None:
            region_handle = registry.register(
                object_type="location_region",
                canonical_identity=region or "UNKNOWN_REGION:" + item["identity"],
                attributes={"row_semantics": "location_region"},
            )
        attrs = {
            "path_member": True,
            "path_rank": int(item["index"]) + 1,
            "resource_type": resource,
            "location_status": "PLACED" if site else "UNPLACED",
            "location_site_handle": site_handle,
            "location_region_handle": region_handle,
        }
        rows["critical_object_locations"].append(
            {"canonical_identity": item["identity"], "handle_type": "cell", **attrs}
        )

    for item in captured.get("high_fanout_nets", []):
        attrs = {"fanout": int(item["attribute"]), "row_semantics": "nonclock_net"}
        rows["high_fanout_net_details"].append(
            {"canonical_identity": item["identity"], "handle_type": "net", **attrs}
        )

    hardblock_relation = None
    exact_capture = None
    if hardblock_scan is not None:
        scan = hardblock_scan
        aggregate = read_object(scan / "aggregate.json")
        matching_rows = [
            row for row in aggregate.get("rows", ())
            if isinstance(row, Mapping) and row.get("root_sha256") == root_hash
        ]
        if len(matching_rows) == 1:
            root_id = str(matching_rows[0]["root_id"])
            raw = read_object(scan / "probes" / root_id / "private" / "resolved-objects.json")
            metadata = read_object(scan / "probes" / root_id / "private" / "hardblock-compatibility-queries.json")
            exact_capture = (raw, metadata)
        else:
            raise RuntimeError("Exact hardblock capture for this root is missing or duplicated")

    if exact_capture is not None:
        raw, metadata = exact_capture
        hardblocks = raw.get("critical_hardblocks", [])
        meta_by_index = {
            int(item["target_index"]): item for item in metadata.get("targets", ())
            if isinstance(item, Mapping)
        }
        if len(meta_by_index) != len(hardblocks):
            raise RuntimeError("wp015b_exact_target_query_metadata_mismatch")
        site_rows_by_index: dict[int, list[dict[str, Any]]] = {}
        site_attributes: dict[str, dict[str, Any]] = {}
        target_resources: dict[int, str] = {}
        target_site_query_hashes: dict[int, str] = {}
        for index, item in enumerate(hardblocks):
            observed = str(item["attribute"]).upper()
            resource = (
                "DSP" if "DSP" in observed else "URAM" if "URAM" in observed
                else "BRAM" if "RAMB" in observed else "ANY"
            )
            target_resources[index] = resource
            meta = meta_by_index[index]
            site_rows = raw.get(str(meta["evidence_key"]), [])
            if not isinstance(site_rows, list):
                raise RuntimeError("wp015b_exact_site_rows_invalid")
            observed_query_hash = digest(site_rows)
            if observed_query_hash != meta.get("query_result_sha256"):
                raise RuntimeError("wp015b_exact_site_query_hash_mismatch")
            target_site_query_hashes[index] = observed_query_hash
            site_rows_by_index[index] = site_rows
            for site in site_rows:
                identity = str(site["identity"])
                attrs = site_attributes.setdefault(identity, {
                    "site_class": str(site["attribute"]),
                    "compatible_resource_types": [],
                })
                if attrs["site_class"] != str(site["attribute"]):
                    raise RuntimeError("wp015b_site_type_changed_within_capture")
                if resource not in attrs["compatible_resource_types"]:
                    attrs["compatible_resource_types"].append(resource)
        sites_by_identity = {
            identity: registry.register(
                object_type="site", canonical_identity=identity,
                attributes={
                    "site_class": attrs["site_class"],
                    "compatible_resource_types": sorted(attrs["compatible_resource_types"]),
                },
            )
            for identity, attrs in sorted(site_attributes.items())
        }
        target_rows = []
        target_query_sha = digest(hardblocks)
        query_hashes_by_handle: dict[str, str] = {}
        for index, item in enumerate(hardblocks):
            compatible = [sites_by_identity[str(site["identity"])] for site in site_rows_by_index[index]]
            attrs = {
                "path_member": True,
                "path_rank": int(item["index"]) + 1,
                "resource_type": target_resources[index],
                "compatible_site_handles": compatible,
            }
            handle = registry.register(
                object_type="hardblock", canonical_identity=str(item["identity"]),
                attributes=attrs,
            )
            rows["critical_hardblock_candidates"].append(
                {"canonical_identity": item["identity"], "handle_type": "hardblock", **attrs}
            )
            target_rows.append({
                "handle": handle,
                "target_identity_sha256": hashlib.sha256(str(item["identity"]).encode()).hexdigest(),
                "resource_type": target_resources[index],
                "compatible_site_handles": compatible,
            })
            query_hashes_by_handle[handle] = target_site_query_hashes[index]
        hardblock_relation = build_relation(
            root_sha256=root_hash, stage="ROUTED", target_rows=target_rows,
            target_query_sha256=target_query_sha,
            target_site_query_sha256=query_hashes_by_handle,
        )
        validate_relation(hardblock_relation)
        registry.register(
            object_type="compatibility",
            canonical_identity="wp015b-relation:" + hardblock_relation["compatibility_relation_sha256"],
            attributes={"relation": hardblock_relation}, source_tool="compiler",
        )
    else:
        hardblocks = captured.get("critical_hardblocks", [])
        resource = "ANY"
        if hardblocks:
            observed = hardblocks[0]["attribute"].upper()
            resource = (
                "DSP" if "DSP" in observed else "URAM" if "URAM" in observed
                else "BRAM" if "RAMB" in observed else "ANY"
            )
        site_handles = []
        for item in captured.get("compatible_sites", []):
            attrs = {
                "site_class": item["attribute"],
                "compatible_resource_types": [resource],
            }
            site_handles.append(registry.register(
                object_type="site", canonical_identity=item["identity"], attributes=attrs,
            ))
        for item in hardblocks:
            observed = item["attribute"].upper()
            item_resource = (
                "DSP" if "DSP" in observed else "URAM" if "URAM" in observed
                else "BRAM" if "RAMB" in observed else "ANY"
            )
            attrs = {
                "path_member": True, "path_rank": int(item["index"]) + 1,
                "resource_type": item_resource,
                "compatible_site_handles": site_handles if item is hardblocks[0] else [],
            }
            rows["critical_hardblock_candidates"].append(
                {"canonical_identity": item["identity"], "handle_type": "hardblock", **attrs}
            )

    rows["route_summary"].append(
        {
            "canonical_identity": "route-summary:" + root_hash,
            "handle_type": "net",
            "route_status": route_status,
            "count": 1,
            "row_semantics": "root_read_only_route_summary",
        }
    )
    policies = {
        "critical_path_details": "no_timing_paths_returned",
        "critical_object_locations": "no_critical_cells_returned",
        "high_fanout_net_details": "no_nonclock_nets_returned",
        "critical_hardblock_candidates": "no_critical_hardblocks_returned",
        "placement_summary": "no_clock_regions_returned",
        "route_summary": "route_evidence_missing",
    }
    for name, values in rows.items():
        if not values:
            missing[name] = policies[name]

    geometry = (
        (0, max(item[1] for item in bounds), 0, max(item[3] for item in bounds))
        if bounds else (0, 0, 0, 0)
    )
    return registry, rows, missing, tuple(region_handles), geometry, hardblock_relation
