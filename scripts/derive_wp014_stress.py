#!/usr/bin/env python3
"""Derive the preregistered WP-014 reduced-M masks from the accepted panel."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
M8 = (
    "macro.phys_opt", "macro.place_route", "macro.auto_pblock", "macro.retime",
    "macro.net_swap", "macro.memory_path_opt", "macro.hardblock_relocate",
    "macro.congestion_spread",
)
PRIORITY = (
    "logicnets_jscl", "corescore_500_mod", "finn_radioml", "amd_mini-isp",
    "rosetta_optical-flow", "rosetta_digit-recognition", "vexriscv_re-place",
    "vexriscv_re-place_v2", "rosetta_3d-rendering", "rosetta_spam-filter",
)
MECHANISM = {
    "macro.place_route", "macro.auto_pblock", "macro.memory_path_opt",
    "macro.hardblock_relocate",
}
SPATIAL = {"macro.place_route", "macro.auto_pblock"}
HARDBLOCK = {"macro.memory_path_opt", "macro.hardblock_relocate"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical(value)
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def derive(panel_path: Path, roots_path: Path) -> dict[str, Any]:
    rows = json.loads(panel_path.read_text())
    roots_value = json.loads(roots_path.read_text())
    roots = {row["root_id"]: row for row in roots_value["roots"]}
    if len(rows) != 80 or len({(r["root_id"], r["action_key"]) for r in rows}) != 80:
        raise ValueError("accepted_macro_panel_not_complete_80_cells")
    by_root: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_root.setdefault(row["root_id"], []).append(row)
    if set(by_root) != set(roots):
        raise ValueError("macro_panel_root_set_mismatch")

    derivations = []
    for root_id in PRIORITY:
        root_rows = by_root[root_id]
        if {row["action_key"] for row in root_rows} != set(M8):
            raise ValueError("macro_panel_action_set_mismatch:" + root_id)
        if any(row["root_sha256"] != roots[root_id]["sha256"] for row in root_rows):
            raise ValueError("macro_panel_root_hash_mismatch:" + root_id)
        gains = {
            row["action_key"]: max(
                0.0, float(row["final_fmax_mhz"]) - float(row["initial_fmax_mhz"])
            )
            for row in root_rows
        }
        best = max(gains.values())
        threshold = max(0.90 * best, best - 5.0)
        removed = tuple(key for key in M8 if gains[key] >= threshold)
        retained = tuple(key for key in M8 if key not in removed)
        periods = {
            round(1000.0 / float(row["initial_fmax_mhz"]) + float(row["initial_wns_ns"]), 12)
            for row in root_rows
        }
        if len(periods) != 1:
            raise ValueError("target_period_inconsistent:" + root_id)
        eligible = best >= 5.0 and len(retained) >= 3 and bool(set(removed) & MECHANISM)
        derivations.append(
            {
                "root_id": root_id,
                "family": roots[root_id]["family"],
                "root_sha256": roots[root_id]["sha256"],
                "root_filename": roots[root_id]["filename"],
                "target_period_ns": next(iter(periods)),
                "root_fmax_mhz": float(root_rows[0]["initial_fmax_mhz"]),
                "gain_mhz_by_action": gains,
                "best_gain_mhz": best,
                "threshold_mhz": threshold,
                "removed_action_keys": list(removed),
                "retained_action_keys": list(retained),
                "eligible": eligible,
                "mechanism_flags": {
                    "spatial_or_flow_removed": bool(set(removed) & SPATIAL),
                    "hardblock_or_memory_removed": bool(set(removed) & HARDBLOCK),
                },
            }
        )

    selected = [row for row in derivations if row["eligible"]][:4]
    if len(selected) != 4:
        raise ValueError("fewer_than_four_eligible_stress_roots")
    for flag in ("spatial_or_flow_removed", "hardblock_or_memory_removed"):
        if not any(row["mechanism_flags"][flag] for row in selected):
            replacement = next(
                (row for row in derivations if row["eligible"] and row not in selected and row["mechanism_flags"][flag]),
                None,
            )
            if replacement is None:
                raise ValueError("mechanism_coverage_unsatisfied:" + flag)
            selected[-1] = replacement
    if {row["root_id"] for row in selected} != {
        "logicnets_jscl", "corescore_500_mod", "finn_radioml", "amd_mini-isp"
    }:
        raise ValueError("fixed_priority_selection_unexpected")

    core = {
        "schema_version": "dof.wp014-stress-masks.v1",
        "protocol_epoch": "dof.dual-track.v1",
        "rule_version": "wp014-near-oracle-removal.v1",
        "rule": "gain=max(0,child-root); threshold=max(0.90*best,best-5MHz); W=gain>=threshold; M'=M8\\W",
        "selection_priority": list(PRIORITY),
        "macro_panel_path": str(panel_path),
        "macro_panel_sha256": file_digest(panel_path),
        "development_roots_path": str(roots_path.relative_to(REPO)),
        "development_roots_sha256": file_digest(roots_path),
        "m8": list(M8),
        "derivation_table": derivations,
        "selected_stress_roots": selected,
        "selected_root_ids": [row["root_id"] for row in selected],
        "provider_requests_before_freeze": 0,
        "model_disclosure": "retained_action_keys_only; removed keys, gains and rationale are private to analysis",
        "sealed_holdouts_accessed": False,
    }
    return core | {"stress_masks_sha256": digest(core)}


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--roots", type=Path, default=REPO / "configs/wp013/development-roots-v1.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = derive(args.panel.resolve(strict=True), args.roots.resolve(strict=True))
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"selected_root_ids": value["selected_root_ids"], "stress_masks_sha256": value["stress_masks_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
