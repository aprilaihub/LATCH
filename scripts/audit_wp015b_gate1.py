#!/usr/bin/env python3
"""Audit the corrected QoR-blind WP-015B compatibility scan and Q replay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp015_cells as cells  # noqa: E402
from optimizer_pipeline.dof.hardblock_compatibility import validate_relation  # noqa: E402
from optimizer_pipeline.dof.single_submission.observation_v4 import CommonQuerySurfaceV4  # noqa: E402


MUTATIONS = frozenset({
    "vivado.unroute_all", "vivado.unplace_all", "vivado.place_design",
    "vivado.route_design", "vivado.phys_opt_design", "vivado.write_checkpoint",
    "vivado.wp013_relocate_hardblocks", "vivado.wp014h_apply_allocation",
})


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def run(args: argparse.Namespace) -> int:
    scan, bundle = read(args.scan / "aggregate.json"), read(args.bundle)
    cells.configure()
    base = cells.base
    base.HARDBLOCK_COMPATIBILITY_SCAN_DIR = args.scan
    base.REQUIRE_EXACT_HARDBLOCK_COMPATIBILITY = True
    rows = scan.get("rows", [])
    selected = str(scan.get("selected_admission_root"))
    relation_checks = []
    probe_checks = []
    for row in rows:
        root_id = str(row["root_id"])
        relation_path = args.scan / "relations" / f"{root_id}.json"
        if not relation_path.is_file():
            relation_checks.append({"root_id": root_id, "valid": False, "reason": "relation_missing"})
        else:
            try:
                validate_relation(read(relation_path)["relation"])
                relation_checks.append({"root_id": root_id, "valid": True, "reason": None})
            except Exception as exc:
                relation_checks.append({"root_id": root_id, "valid": False, "reason": f"{type(exc).__name__}:{exc}"})
        summary = read(args.scan / "probes" / root_id / "admission-summary.json")
        operations = [str(item.get("operation")) for item in summary.get("trace", ())]
        probe_checks.append({
            "root_id": root_id,
            "accepted": summary.get("accepted") is True,
            "root_unchanged": summary.get("root_unchanged") is True,
            "qor_fields_absent": not {"pre_timing", "post_timing", "route_valid"}.intersection(summary),
            "qor_operations_absent": "vivado.report_timing_summary" not in operations,
            "mutation_operations_absent": not MUTATIONS.intersection(operations),
            "operations": sorted(set(operations)),
        })
    observation, private = base._load_trial(bundle, args.capture, selected)
    query = CommonQuerySurfaceV4(private["registry"])
    result = query.materialize(
        query_name="critical_hardblock_candidates", top_k=8,
        private_rows=private["rows"]["critical_hardblock_candidates"],
        tool_call_ids=(scan["protocol_id"],), latency_seconds=0.0,
    )
    relation = private["hardblock_compatibility_relation"]
    validate_relation(relation)
    feasible = [
        row for row in rows
        if row.get("probe_accepted") is True
        and row.get("relation", {}).get("feasible") is True
    ]
    resource_order = {"CORE": 0, "LARGE": 1, "VERY_LARGE": 2}
    feasible.sort(key=lambda row: (resource_order.get(str(row["resource_class"]), 99), str(row["root_id"])))
    mechanical_selected = str(feasible[0]["root_id"]) if feasible else None
    checks = {
        "scan_pass": scan.get("status") == "PASS",
        "exact_14_roots": len(rows) == 14 and scan.get("development_root_count") == 14,
        "all_probes_accepted": all(row["accepted"] for row in probe_checks),
        "all_roots_unchanged": all(row["root_unchanged"] for row in probe_checks),
        "zero_qor_fields_or_calls": all(row["qor_fields_absent"] and row["qor_operations_absent"] for row in probe_checks),
        "zero_mutation_operations": all(row["mutation_operations_absent"] for row in probe_checks),
        "all_relations_valid": all(row["valid"] for row in relation_checks),
        "selection_rule_replayed": selected == mechanical_selected,
        "selected_relation_feasible": relation.get("feasible") is True and relation.get("matching", {}).get("matching_size", 0) >= 2,
        "single_q_item_within_limits": len(result.canonical_bytes) <= 4096 and len(result.rows) <= 8 and result.truncated is False,
        "observation_replayed": observation.sha256 == bundle["observations"][selected]["sha256"],
    }
    core = {
        "schema_version": "dof.wp015b-gate1-audit.v1",
        "status": "PASS" if all(checks.values()) else "STOP_SHARED_GATE",
        "checks": checks,
        "scan_protocol_id": scan["protocol_id"],
        "scan_aggregate_sha256": scan["aggregate_sha256"],
        "selected_admission_root": selected,
        "mechanically_replayed_selected_root": mechanical_selected,
        "selected_runtime_relation_sha256": relation["compatibility_relation_sha256"],
        "selected_matching_size": relation["matching"]["matching_size"],
        "model_visible_query_bytes": len(result.canonical_bytes),
        "model_visible_query_rows": len(result.rows),
        "qor_fields_used": [],
        "relation_checks": relation_checks,
        "probe_checks": probe_checks,
    }
    value = core | {"audit_sha256": cells.digest(core)}
    cells.write(args.output, value)
    print(json.dumps({key: value[key] for key in ("status", "selected_admission_root", "selected_matching_size", "model_visible_query_bytes")}, sort_keys=True))
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
