#!/usr/bin/env python3
"""Reduce raw Vivado property metadata to an object-specific candidate allowlist."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
from typing import Any


CANDIDATES: dict[tuple[str, str], dict[str, str]] = {
    ("cell", "LOC"): dict(stage="OPEN_DESIGN", mechanism="explicit site placement", rollback="reset_property LOC in isolated child", risk="HIGH", recommendation="INCLUDE_P_ADVANCED_CONDITIONAL"),
    ("cell", "BEL"): dict(stage="OPEN_DESIGN", mechanism="explicit BEL placement", rollback="reset_property BEL in isolated child", risk="HIGH", recommendation="INCLUDE_P_ADVANCED_CONDITIONAL"),
    ("cell", "IS_LOC_FIXED"): dict(stage="PLACED_OR_ROUTED", mechanism="lock or unlock placed cell location", rollback="restore captured boolean", risk="MEDIUM", recommendation="INCLUDE_P_ADVANCED_CONDITIONAL"),
    ("cell", "IS_BEL_FIXED"): dict(stage="PLACED_OR_ROUTED", mechanism="lock or unlock BEL assignment", rollback="restore captured boolean", risk="MEDIUM", recommendation="INCLUDE_P_ADVANCED_CONDITIONAL"),
    ("cell", "DONT_TOUCH"): dict(stage="ANY", mechanism="preserve cell against optimisation", rollback="restore captured boolean", risk="MEDIUM", recommendation="INCLUDE_P_ADVANCED_CONDITIONAL"),
    ("cell", "USER_SLR_ASSIGNMENT"): dict(stage="PRE_PLACE_OR_PLACED", mechanism="SLR placement guidance", rollback="reset property in isolated child", risk="HIGH", recommendation="INCLUDE_G_AST_EXTENSION"),
    ("cell", "RLOC"): dict(stage="PRE_PLACE", mechanism="relative placement constraint", rollback="reset property in isolated child", risk="HIGH", recommendation="NEEDS_FURTHER_EVIDENCE"),
    ("cell", "LOCK_PINS"): dict(stage="PRE_ROUTE", mechanism="LUT pin locking", rollback="reset property in isolated child", risk="HIGH", recommendation="NEEDS_FURTHER_EVIDENCE"),
    ("cell", "MAX_FANOUT"): dict(stage="SYNTHESIS_OR_OPT", mechanism="fanout constraint", rollback="reset property in isolated child", risk="MEDIUM", recommendation="DEVICE_OR_STAGE_CONDITIONAL"),
    ("cell", "FORCE_MAX_FANOUT"): dict(stage="SYNTHESIS_OR_OPT", mechanism="forced fanout constraint", rollback="reset property in isolated child", risk="HIGH", recommendation="DEVICE_OR_STAGE_CONDITIONAL"),
    ("net", "DONT_TOUCH"): dict(stage="ANY", mechanism="preserve net against optimisation", rollback="restore captured boolean", risk="MEDIUM", recommendation="INCLUDE_P_ADVANCED_CONDITIONAL"),
    ("net", "IS_ROUTE_FIXED"): dict(stage="ROUTED", mechanism="lock existing route", rollback="restore captured boolean", risk="HIGH", recommendation="NEEDS_FURTHER_EVIDENCE"),
    ("net", "FIXED_ROUTE"): dict(stage="ROUTED", mechanism="explicit routing resource sequence", rollback="reset property in isolated child", risk="CRITICAL", recommendation="EXCLUDE_UNSAFE"),
    ("net", "CLOCK_DEDICATED_ROUTE"): dict(stage="PRE_ROUTE", mechanism="clock routing constraint", rollback="restore captured enum", risk="CRITICAL", recommendation="NEEDS_FURTHER_EVIDENCE"),
    ("net", "USER_CROSSING_SLR"): dict(stage="PRE_ROUTE", mechanism="SLR crossing guidance", rollback="reset property in isolated child", risk="HIGH", recommendation="INCLUDE_G_AST_EXTENSION"),
    ("net", "USER_CLOCK_ROOT"): dict(stage="PRE_ROUTE", mechanism="clock-root guidance", rollback="reset property in isolated child", risk="CRITICAL", recommendation="NEEDS_FURTHER_EVIDENCE"),
    ("net", "USER_CLOCK_VTREE_TYPE"): dict(stage="PRE_PLACE", mechanism="clock-tree topology guidance", rollback="reset property in isolated child", risk="CRITICAL", recommendation="DEVICE_OR_FLOW_CONDITIONAL"),
    ("net", "MAX_FANOUT"): dict(stage="SYNTHESIS_OR_OPT", mechanism="fanout constraint", rollback="reset property in isolated child", risk="MEDIUM", recommendation="DEVICE_OR_STAGE_CONDITIONAL"),
    ("net", "FORCE_MAX_FANOUT"): dict(stage="SYNTHESIS_OR_OPT", mechanism="forced fanout constraint", rollback="reset property in isolated child", risk="HIGH", recommendation="DEVICE_OR_STAGE_CONDITIONAL"),
    ("pblock", "CONTAIN_ROUTING"): dict(stage="PRE_ROUTE", mechanism="route containment", rollback="restore captured boolean or delete child pblock", risk="HIGH", recommendation="INCLUDE_P_ADVANCED_CONDITIONAL"),
    ("pblock", "EXCLUDE_PLACEMENT"): dict(stage="PRE_PLACE", mechanism="placement exclusion region", rollback="restore captured boolean or delete child pblock", risk="HIGH", recommendation="INCLUDE_G_AST_EXTENSION"),
    ("pblock", "SNAPPING_MODE"): dict(stage="PBLOCK_DEFINITION", mechanism="pblock geometry snapping", rollback="restore captured enum or delete child pblock", risk="MEDIUM", recommendation="INCLUDE_P_ADVANCED_CONDITIONAL"),
    ("site", "PROHIBIT"): dict(stage="PRE_PLACE", mechanism="exclude bounded sites from placement", rollback="restore captured boolean", risk="HIGH", recommendation="INCLUDE_G_AST_EXTENSION"),
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def property_shape(report: str) -> tuple[str | None, bool | None]:
    type_match = re.search(r"(?m)^\s*Type:\s*(\S+)", report)
    read_only_match = re.search(r"(?m)^\s*Read-only:\s*(true|false)", report, re.IGNORECASE)
    return (
        type_match.group(1) if type_match else None,
        read_only_match.group(1).lower() == "true" if read_only_match else None,
    )


def analyse(aggregate: dict[str, Any]) -> dict[str, Any]:
    roots = aggregate["roots"]
    rows = []
    for (object_type, property_name), policy in CANDIDATES.items():
        observations = []
        for root in roots:
            matches = [row for row in root["capture"]["classes"][object_type]["properties"] if row["property"] == property_name]
            if not matches:
                observations.append({"root_id": root["root_id"], "present": False})
                continue
            row = matches[0]
            property_type, read_only = property_shape(row["report"])
            observations.append({
                "root_id": root["root_id"], "present": True, "type": property_type,
                "read_only": read_only, "legal_values_status": row["legal_values_status"],
                "legal_values": row["legal_values"],
            })
        present = [row for row in observations if row["present"]]
        types = sorted({row["type"] for row in present if row["type"]})
        read_only_values = sorted({row["read_only"] for row in present if row["read_only"] is not None})
        legal_union = sorted({value for row in present for value in row["legal_values"]})
        installed_writable = bool(present) and read_only_values == [False]
        recommendation = policy["recommendation"]
        if not installed_writable:
            recommendation = "EXCLUDE_UNSUPPORTED"
        value_domain = legal_union if legal_union else {
            "bool": [False, True], "string": "TYPED_BOUNDED_TARGET_DEPENDENT",
            "int": "BOUNDED_INTEGER_REQUIRES_PROPERTY_SPECIFIC_LIMIT",
        }.get(types[0] if len(types) == 1 else "", "NEEDS_FURTHER_EVIDENCE")
        rows.append({
            "candidate_id": f"property.{object_type}.{property_name}", "object_type": object_type,
            "property": property_name, "present_roots": len(present), "root_count": len(roots),
            "property_types": types, "writable": installed_writable, "value_domain": value_domain,
            "stage_applicability": policy["stage"], "timing_closure_mechanism": policy["mechanism"],
            "rollback_strategy": policy["rollback"], "risk": policy["risk"],
            "recommended_classification": recommendation,
            "real_mutation_admission": "NOT_YET_ADMITTED", "observations": observations,
        })
    class_summary = {}
    for object_type in {key[0] for key in CANDIDATES} | {"bel"}:
        class_summary[object_type] = {
            "property_count_by_root": {root["root_id"]: root["capture"]["classes"][object_type]["property_count"] for root in roots},
            "candidate_count": sum(row["object_type"] == object_type for row in rows),
        }
    core = {
        "schema_version": "dof.wp014b-property-allowlist-candidates.v1",
        "continuation_identity": "wp014b-action-surface-continuation-v1",
        "source_capture_sha256": aggregate["aggregate_sha256"], "roots": [row["root_id"] for row in roots],
        "class_summary": class_summary, "candidates": rows,
        "generic_set_property_disposition": "EXCLUDE_UNSAFE",
        "bel_class_disposition": "NO_RELEVANT_WRITABLE_CANDIDATE_IDENTIFIED",
    }
    return core | {"analysis_sha256": digest(core)}


def write_csv(result: dict[str, Any], path: Path) -> None:
    fields = ["candidate_id", "object_type", "property", "present_roots", "root_count", "property_types", "writable", "value_domain", "stage_applicability", "timing_closure_mechanism", "rollback_strategy", "risk", "recommended_classification", "real_mutation_admission"]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for row in result["candidates"]:
            writer.writerow({key: json.dumps(row[key], sort_keys=True) if isinstance(row[key], (list, dict)) else row[key] for key in fields})


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    args = parser.parse_args()
    result = analyse(json.loads(args.capture.read_text()))
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    write_csv(result, args.output_csv)
    print(json.dumps({"candidates": len(result["candidates"]), "writable": sum(row["writable"] for row in result["candidates"]), "sha256": result["analysis_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
