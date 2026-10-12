#!/usr/bin/env python3
"""Collect QoR-blind clock-region site capacity for strict-G precheck."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


MARKER = re.compile(r"^DOF_WP015A_CAPACITY:([^:]+):([^:]+):(\d+)$")
REGION = re.compile(r"^(?:CLOCKREGION_)?X(\d+)Y(\d+)$")


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def resource_class(site_type: str) -> str | None:
    upper = site_type.upper()
    if upper.startswith("SLICE"):
        return "LOGIC"
    if upper.startswith("RAMB"):
        return "BRAM"
    if upper.startswith("DSP"):
        return "DSP"
    if upper.startswith("URAM"):
        return "URAM"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-root-sha256", required=True)
    parser.add_argument("--vivado", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    if sha256(args.root) != args.expected_root_sha256:
        raise ValueError("wp015a_region_capacity_root_hash_mismatch")
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True)
    tcl = args.output / "capacity.tcl"
    # Only exact DCP and output paths are interpolated; no model data or QoR is
    # present.  Tcl list quoting prevents path interpretation.
    script = "\n".join([
        f"open_checkpoint [list {str(args.root)}]",
        "foreach __cr [lsort [get_clock_regions]] {",
        "  set __crn [get_property NAME $__cr]",
        "  set __sites [get_sites -quiet -of_objects $__cr]",
        "  foreach __type [lsort -unique [get_property SITE_TYPE $__sites]] {",
        "    set __count [llength [filter $__sites \"SITE_TYPE == $__type\"]]",
        "    puts \"DOF_WP015A_CAPACITY:$__crn:$__type:$__count\"",
        "  }",
        "}",
        "close_design",
        "exit",
    ]) + "\n"
    tcl.write_text(script, encoding="utf-8")
    log = args.output / "vivado.log"
    completed = subprocess.run(
        [str(args.vivado), "-mode", "batch", "-nojournal", "-nolog", "-source", str(tcl)],
        cwd=args.output, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=args.timeout_seconds, check=False,
    )
    log.write_text(completed.stdout, encoding="utf-8")
    raw: dict[str, dict[str, int]] = {}
    for line in completed.stdout.splitlines():
        match = MARKER.match(line.strip())
        if match:
            region, site_type, count = match.groups()
            raw.setdefault(region, {})[site_type] = int(count)
    capacities: dict[str, dict[str, int]] = {}
    for region, site_types in raw.items():
        match = REGION.match(region)
        if not match:
            continue
        x, y = map(int, match.groups())
        abstract: dict[str, int] = {"LOGIC": 0, "BRAM": 0, "DSP": 0, "URAM": 0}
        for site_type, count in site_types.items():
            resource = resource_class(site_type)
            if resource:
                abstract[resource] += count
        # Cell resource names are retained because this is the exact vocabulary
        # used by the typed registry and precheck.
        expanded = dict(abstract)
        for name in ("LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6", "FDRE", "FDSE", "FDCE", "FDPE"):
            expanded[name] = abstract["LOGIC"]
        for name in ("RAMB18E2", "RAMB36E2"):
            expanded[name] = abstract["BRAM"]
        capacities[f"{x}:{x}:{y}:{y}"] = expanded
    core: dict[str, Any] = {
        "schema_version": "dof.wp015a-region-capacity.v1",
        "root_sha256": args.expected_root_sha256,
        "returncode": completed.returncode,
        "region_count": len(capacities),
        "capacity_by_region": capacities,
        "qor_fields_used": [],
        "read_only_commands": ["open_checkpoint", "get_clock_regions", "get_sites", "get_property", "filter", "close_design"],
        "vivado_log_sha256": sha256(log),
    }
    raw_bytes = json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    result = core | {"capacity_sha256": hashlib.sha256(raw_bytes).hexdigest()}
    (args.output / "capacity.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"returncode": completed.returncode, "region_count": len(capacities), "qor_fields_used": []}, sort_keys=True))
    return 0 if completed.returncode == 0 and capacities else 2


if __name__ == "__main__":
    raise SystemExit(main())
