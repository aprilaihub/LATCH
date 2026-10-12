#!/usr/bin/env python3
"""Public-safe parser for the WP-012-02 private read-only Vivado logs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
from typing import Any


FIELDS = {
    "tool_version": "O",
    "part": "O",
    "clocks": "O",
    "timing_summary": "O",
    "critical_paths": "Q",
    "critical_object_locations": "Q",
    "utilization": "O",
    "route_status": "O",
    "congestion": "Q",
    "high_fanout_nets": "Q",
    "slice_geometry": "O",
    "clock_regions": "O",
    "slrs": "O",
    "pblocks": "Q",
    "constraints_summary": "O",
}
RW_FIELDS = {
    "design_and_device_info": "O",
    "placement_summary": "Q",
    "net_route_summary": "Q",
    "critical_path_spread": "EXCLUDE",
    "critical_net_detour": "EXCLUDE",
}


def _pseudo(root: str, field: str, value: bytes) -> dict[str, Any]:
    text = value.decode("utf-8", errors="replace")
    digest = hashlib.sha256((root + "\0" + field).encode() + value).hexdigest()
    # Only aggregates and schema evidence are public.  Names and report bodies
    # remain in the private external logs.
    return {
        "private_value_sha256": digest,
        "private_size_bytes": len(value),
        "line_count": text.count("\n") + bool(text),
        "contains_absolute_path": bool(re.search(r"/(?:home|tmp|opt)/", text)),
    }


def parse(root: str, path: Path) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("WP01202_FIELD\t"):
            continue
        _, field, latency, status, encoded = line.split("\t", 4)
        raw = base64.b64decode(encoded)
        found[field] = {
            "root": root,
            "field": field,
            "class": FIELDS.get(field, "INFRASTRUCTURE"),
            "status": status,
            "latency_ms": int(latency),
            "external_tool_calls": 1 if field == "open_checkpoint" else 0,
            "stable_schema": field in FIELDS,
            "pseudonymisation_feasible": field not in {"timing_summary", "utilization", "route_status", "congestion"} or True,
            **_pseudo(root, field, raw),
        }
    for field, classification in FIELDS.items():
        found.setdefault(
            field,
            {
                "root": root,
                "field": field,
                "class": classification,
                "status": "MISSING",
                "latency_ms": None,
                "external_tool_calls": 0,
                "stable_schema": False,
                "pseudonymisation_feasible": False,
                "private_value_sha256": None,
                "private_size_bytes": 0,
                "line_count": 0,
                "contains_absolute_path": False,
            },
        )
    return [found[key] for key in sorted(FIELDS)]


def parse_rapidwright(root: str, path: Path) -> list[dict[str, Any]]:
    found = {}
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("WP01202_RW_FIELD\t"):
            continue
        _, field, latency, status, encoded = line.split("\t", 4)
        if field not in RW_FIELDS:
            continue
        raw = base64.b64decode(encoded)
        found[field] = {
            "root": root,
            "field": "rapidwright." + field,
            "class": RW_FIELDS[field],
            "status": status,
            "latency_ms": int(latency),
            "external_tool_calls": 0,
            "stable_schema": status in {"OK", "UNAVAILABLE"},
            "pseudonymisation_feasible": True,
            **_pseudo(root, "rapidwright." + field, raw),
        }
    return [found[key] for key in sorted(found)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", nargs=2, metavar=("ROOT", "LOG"), required=True)
    parser.add_argument("--rapidwright-input", action="append", nargs=2, metavar=("ROOT", "LOG"), default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [row for root, path in args.input for row in parse(root, Path(path))]
    rows.extend(
        row
        for root, path in args.rapidwright_input
        for row in parse_rapidwright(root, Path(path))
    )
    roots = sorted({row["root"] for row in rows})
    fields = []
    all_fields = {**FIELDS, **{"rapidwright." + key: value for key, value in RW_FIELDS.items()}}
    for field in sorted(all_fields):
        selected = [row for row in rows if row["field"] == field]
        fields.append(
            {
                "field": field,
                "recommended_class": all_fields[field],
                "success_roots": sum(row["status"] == "OK" for row in selected),
                "root_count": len(roots),
                "latency_ms": {row["root"]: row["latency_ms"] for row in selected},
                "statuses": {row["root"]: row["status"] for row in selected},
                "stable_schema_both": all(row["stable_schema"] and row["status"] == "OK" for row in selected),
                "pseudonymisation_feasible_both": all(row["pseudonymisation_feasible"] for row in selected),
            }
        )
    payload = {
        "schema_version": "dof.wp012-02-common-observation-query-audit.v1",
        "probe_identity": "ACTION_LANGUAGE_DESIGN_PROBE_V1",
        "primary_aggregate_eligible": False,
        "roots": roots,
        "fields": fields,
        "rows": rows,
        "raw_names_public": False,
        "private_logs_preserved_externally": True,
        "design_mutation": False,
        "rapidwright_note": "Path-spread/detour are excluded until cross-tool hierarchical object identities are stable; absence is recorded rather than reconstructed from names.",
        "sealed_holdouts_accessed": False,
    }
    payload["audit_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
