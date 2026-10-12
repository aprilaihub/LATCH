#!/usr/bin/env python3
"""Build functional-valid M8/M10 side-by-side decision evidence."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import statistics
from typing import Any


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def _v1_terminals(aggregate: dict[str, Any]) -> dict[str, str]:
    return {
        row["case"]["case_id"]: row["terminal"] for row in aggregate["results"]
    }


def rows(m8_panel: Path, m8_validation: Path, m10: Path) -> list[dict[str, Any]]:
    m8 = read(m8_panel)
    m8_v1 = _v1_terminals(read(m8_validation / "cases" / "aggregate.json"))
    result = []
    for row in m8:
        case_id = "m8__" + row["job_id"]
        result.append({
            "scenario_source": "M8_EXISTING",
            "root_id": row["root_id"], "action_key": row["action_key"],
            "flow_name": row["flow_name"],
            "gain_mhz": float(row["fmax_improvement_mhz"]),
            "physical_terminal": row["canonical_endpoint"],
            "functional_terminal": m8_v1.get(case_id, "MISSING"),
            "functional_valid": m8_v1.get(case_id) == "MULTISEED_FUNCTIONAL_PASS",
            "runtime_seconds": float(row["duration_seconds"]),
        })
    m10_aggregate = read(m10 / "aggregate.json")
    m10_v1 = _v1_terminals(m10_aggregate["validation"])
    for row in m10_aggregate["rows"]:
        case_id = "m10__" + row["job_id"]
        attempt = row["attempt"]
        result.append({
            "scenario_source": "M10_FRESH",
            "root_id": row["root_id"], "action_key": row["action_key"],
            "flow_name": row["flow_name"],
            "gain_mhz": attempt.get("fmax_improvement_mhz"),
            "physical_terminal": attempt.get("canonical_endpoint"),
            "functional_terminal": m10_v1.get(case_id, "NOT_APPLICABLE"),
            "functional_valid": m10_v1.get(case_id) == "MULTISEED_FUNCTIONAL_PASS",
            "runtime_seconds": float(attempt.get("outer_duration_seconds") or attempt.get("duration_seconds") or row["process"].get("duration_seconds") or 0),
        })
    return result


def scenario(name: str, rows: list[dict[str, Any]], allowed_actions: list[str]) -> dict[str, Any]:
    selected = [row for row in rows if row["action_key"] in allowed_actions]
    roots = sorted({row["root_id"] for row in selected})
    action_rows = {action: [] for action in allowed_actions}
    root_cards = []
    for root_id in roots:
        root_rows = [row for row in selected if row["root_id"] == root_id]
        by_action = {row["action_key"]: row for row in root_rows}
        if set(by_action) != set(allowed_actions):
            raise ValueError(f"{name}_incomplete_action_panel:{root_id}")
        gains = {
            action: max(0.0, float(by_action[action]["gain_mhz"] or 0.0))
            if by_action[action]["functional_valid"] else 0.0
            for action in allowed_actions
        }
        best = max(gains.values())
        threshold = max(0.90 * best, best - 5.0)
        near = [action for action in allowed_actions if gains[action] >= threshold]
        winner = next(action for action in allowed_actions if gains[action] == best)
        root_cards.append({
            "root_id": root_id, "functional_valid_gain_mhz_by_action": gains,
            "oracle_gain_mhz": best, "oracle_action": winner,
            "near_oracle_threshold_mhz": threshold,
            "near_oracle_removed_action_keys": near,
            "reduced_m_retained_action_keys": [action for action in allowed_actions if action not in near],
        })
        for action in allowed_actions:
            action_rows[action].append(gains[action])
    ranking = sorted(allowed_actions, key=lambda action: (-statistics.mean(action_rows[action]), action))
    global_action = ranking[0]
    for card in root_cards:
        card["global_action_gain_mhz"] = card["functional_valid_gain_mhz_by_action"][global_action]
        card["global_action_regret_mhz"] = card["oracle_gain_mhz"] - card["global_action_gain_mhz"]
    summary = {
        "scenario": name, "roots": roots, "actions": allowed_actions,
        "functional_valid_cells": sum(row["functional_valid"] for row in selected),
        "cell_count": len(selected),
        "terminal_counts": dict(collections.Counter(row["functional_terminal"] for row in selected)),
        "fixed_global_macro_prior": ranking,
        "global_action": global_action,
        "global_action_mean_gain_mhz": statistics.mean(action_rows[global_action]),
        "mean_oracle_gain_mhz": statistics.mean(card["oracle_gain_mhz"] for card in root_cards),
        "mean_global_action_regret_mhz": statistics.mean(card["global_action_regret_mhz"] for card in root_cards),
        "median_global_action_regret_mhz": statistics.median(card["global_action_regret_mhz"] for card in root_cards),
        "winner_counts": dict(collections.Counter(card["oracle_action"] for card in root_cards)),
        "per_action": {
            action: {
                "mean_root_fallback_gain_mhz": statistics.mean(action_rows[action]),
                "median_root_fallback_gain_mhz": statistics.median(action_rows[action]),
                "functional_valid_count": sum(
                    row["functional_valid"] for row in selected if row["action_key"] == action
                ),
                "median_runtime_seconds": statistics.median(
                    row["runtime_seconds"] for row in selected if row["action_key"] == action
                ),
            } for action in allowed_actions
        },
        "root_cards": root_cards,
        "near_oracle_rule": "gain=max(0,functional-valid child-root); threshold=max(0.90*best,best-5MHz)",
    }
    return summary | {"scenario_sha256": digest(summary)}


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--m8-panel", type=Path, required=True)
    parser.add_argument("--m8-validation", type=Path, required=True)
    parser.add_argument("--m10", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    all_rows = rows(args.m8_panel, args.m8_validation, args.m10)
    m8_actions = [
        "macro.phys_opt", "macro.place_route", "macro.auto_pblock", "macro.retime",
        "macro.net_swap", "macro.memory_path_opt", "macro.hardblock_relocate",
        "macro.congestion_spread",
    ]
    m10_actions = [*m8_actions, "macro.fanout", "macro.lut_cone"]
    m8 = scenario("M8_ORIGINAL10", [row for row in all_rows if row["scenario_source"] == "M8_EXISTING"], m8_actions)
    m10 = scenario("M10_CORE11", all_rows, m10_actions)
    core = {
        "schema_version": "dof.wp014i-m8-m10-decision-card.v1",
        "decision_owner": "GPT_PRO",
        "codex_final_selection_made": False,
        "qor_sign_used_for_technical_admission": False,
        "m8": m8, "m10": m10,
        "comparison": {
            "additional_actions": ["macro.fanout", "macro.lut_cone"],
            "additional_reference_root": "fir_systolic_transposed_routed",
            "functional_valid_cell_delta": m10["functional_valid_cells"] - m8["functional_valid_cells"],
            "mean_oracle_gain_delta_mhz": m10["mean_oracle_gain_mhz"] - m8["mean_oracle_gain_mhz"],
            "mean_global_regret_delta_mhz": m10["mean_global_action_regret_mhz"] - m8["mean_global_action_regret_mhz"],
        },
    }
    value = core | {"decision_card_sha256": digest(core)}
    args.output.mkdir(parents=True)
    write(args.output / "m8-m10-decision-card.json", value)
    lines = [
        "# WP-014I M8/M10 Decision Card", "",
        "This comparison does not select the final Macro universe or use QoR sign for technical admission.", "",
        "| Scenario | Roots | Valid cells | Global prior | Mean oracle gain | Mean global regret |",
        "|---|---:|---:|---|---:|---:|",
    ]
    for row in (m8, m10):
        lines.append(
            f"| {row['scenario']} | {len(row['roots'])} | {row['functional_valid_cells']}/{row['cell_count']} | "
            f"{row['global_action']} | {row['mean_oracle_gain_mhz']:.3f} MHz | {row['mean_global_action_regret_mhz']:.3f} MHz |"
        )
    lines.extend(["", "Near-oracle masks use the frozen WP-014 rule on functional-valid, root-fallback gains."])
    (args.output / "M8-M10-decision-card.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(value["comparison"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
