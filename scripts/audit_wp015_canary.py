#!/usr/bin/env python3
"""Mechanically audit the final 28-cell WP-015 canary before Gate 5."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
from typing import Any


STRICT_FAMILIES = {
    "SPATIAL_GENERATED_GEOMETRY", "NONPREFIX_MULTI_NET_BATCH",
    "COUPLED_HARDBLOCK_SITE_ALLOCATION",
}


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> int:
    value = read(args.aggregate)
    rows = value.get("jobs", [])
    counts = collections.Counter((row["model_key"], row["condition"]) for row in rows)
    g_rows = [row for row in rows if row["condition"] == "G_STRICT_BOUNDARY"]
    submitted_families = {
        row.get("strict_boundary_certificate", {}).get("mechanism_family")
        for row in g_rows if row.get("strict_boundary_certificate", {}).get("mechanism_family")
    }
    physical_children = [row for row in rows if row.get("physical_terminal") == "VALID_COMPLETION"]
    checks = {
        "exact_28_unique": len(rows) == len({row["job_id"] for row in rows}) == 28,
        "seven_by_four_balanced": len(counts) == 28 and all(count == 1 for count in counts.values()),
        "no_shared_controller_terminal": not any(row["canonical_terminal"] == "CONTROLLER_FAILURE" for row in rows),
        "no_profile_circuit_breaker": not any(row["canonical_terminal"] == "PROFILE_CIRCUIT_BREAKER_PAUSED" for row in rows),
        "all_physical_children_received_v1": all(
            row.get("functional_terminal") not in {None, "PENDING_V1"} for row in physical_children
        ),
        "heavy_call_bound": all(int(row.get("actual_heavy_calls") or 0) <= 4 for row in rows),
        "strict_condition_matches_scientific_any_family_semantics": all(
            row.get("strict_mechanism") is None for row in g_rows
        ),
        "zero_retry_fallback_repair": (
            value.get("retry_count") == value.get("fallback_count") == value.get("repair_count") == 0
        ),
        "zero_postcommit_resubmission": value.get("postcommit_resubmission_count") == 0,
    }
    core = {
        "schema_version": "dof.wp015-final-canary-audit.v1",
        "source_aggregate_sha256": value["aggregate_sha256"],
        "checks": checks, "status": "PASS" if all(checks.values()) else "STOP_SHARED_GATE",
        "terminal_counts": value.get("terminal_counts"),
        "functional_terminal_counts": value.get("functional_terminal_counts"),
        "submitted_strict_families": sorted(submitted_families),
        "strict_family_rotation_not_required": True,
        "strict_runtime_exercised": sum(bool(row.get("strict_boundary_certificate", {}).get("strict_boundary_exercised")) for row in g_rows),
        "local_failure_terminals_preserved": dict(collections.Counter(row["canonical_terminal"] for row in rows)),
    }
    result = core | {"audit_sha256": digest(core)}
    write(args.output, result)
    print(json.dumps({"status": result["status"], "checks": checks, "audit_sha256": result["audit_sha256"]}, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--aggregate", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
