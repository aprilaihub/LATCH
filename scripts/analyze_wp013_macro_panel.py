#!/usr/bin/env python3
"""Audit and analyze the completed WP-013 development Macro panel."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import statistics
from typing import Any, Iterable

REPOSITORY = Path(__file__).resolve().parents[1]
FEATURES = (
    "initial_wns_ns", "initial_tns_ns", "failing_endpoints",
    "initial_fmax_mhz", "brams", "dsps", "ffs", "luts",
)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def median(values: Iterable[float]) -> float:
    return statistics.median(list(values))


def bootstrap_mean_ci(values: list[float], *, seed: str, repeats: int = 10000) -> list[float]:
    if not values:
        return [math.nan, math.nan]
    generator = random.Random(seed)
    samples = sorted(
        statistics.mean(generator.choice(values) for _ in values)
        for _ in range(repeats)
    )
    return [samples[int(0.025 * repeats)], samples[int(0.975 * repeats)]]


def best_constant(roots: list[str], actions: list[str], panel: dict[tuple[str, str], dict[str, Any]], oracle: dict[str, float]) -> str:
    return min(
        actions,
        key=lambda action: (
            statistics.mean(oracle[root] - panel[root, action]["final_fmax_mhz"] for root in roots),
            action,
        ),
    )


def fit_stump(roots: list[str], actions: list[str], panel: dict[tuple[str, str], dict[str, Any]], oracle: dict[str, float]) -> dict[str, Any]:
    constant = best_constant(roots, actions, panel, oracle)
    best_key: tuple[Any, ...] = (
        statistics.mean(oracle[root] - panel[root, constant]["final_fmax_mhz"] for root in roots),
        "~constant", 0.0, constant, constant,
    )
    best = {"kind": "constant", "action": constant}
    reference_action = actions[0]
    for feature in FEATURES:
        values = sorted({float(panel[root, reference_action]["features"][feature]) for root in roots})
        for low, high in zip(values, values[1:]):
            threshold = (low + high) / 2.0
            left = [root for root in roots if float(panel[root, reference_action]["features"][feature]) <= threshold]
            right = [root for root in roots if root not in left]
            if not left or not right:
                continue
            left_action = best_constant(left, actions, panel, oracle)
            right_action = best_constant(right, actions, panel, oracle)
            score = statistics.mean(
                oracle[root] - panel[root, left_action if root in left else right_action]["final_fmax_mhz"]
                for root in roots
            )
            key = (score, feature, threshold, left_action, right_action)
            if key < best_key:
                best_key = key
                best = {
                    "kind": "decision_stump", "feature": feature,
                    "operator": "<=", "threshold": threshold,
                    "left_action": left_action, "right_action": right_action,
                }
    return best


def predict(model: dict[str, Any], root: str, actions: list[str], panel: dict[tuple[str, str], dict[str, Any]]) -> str:
    if model["kind"] == "constant":
        return model["action"]
    value = float(panel[root, actions[0]]["features"][model["feature"]])
    return model["left_action"] if value <= model["threshold"] else model["right_action"]


def cross_validate(roots: list[str], actions: list[str], panel: dict[tuple[str, str], dict[str, Any]], oracle: dict[str, float], groups: dict[str, str]) -> dict[str, Any]:
    def folds(by_group: bool) -> list[tuple[str, list[str]]]:
        labels = sorted(set(groups.values())) if by_group else roots
        return [
            (label, [root for root in roots if (groups[root] == label if by_group else root == label)])
            for label in labels
        ]

    payload: dict[str, Any] = {}
    for mode, fold_rows in (("leave_one_benchmark_out", folds(False)), ("leave_one_family_out", folds(True))):
        evaluations = []
        for label, held in fold_rows:
            training = [root for root in roots if root not in held]
            global_action = best_constant(training, actions, panel, oracle)
            stump = fit_stump(training, actions, panel, oracle)
            for root in held:
                stump_action = predict(stump, root, actions, panel)
                evaluations.append({
                    "fold": label, "root_id": root,
                    "global_action": global_action,
                    "global_regret_mhz": oracle[root] - panel[root, global_action]["final_fmax_mhz"],
                    "stump": stump, "stump_action": stump_action,
                    "stump_regret_mhz": oracle[root] - panel[root, stump_action]["final_fmax_mhz"],
                    "oracle_action": max(actions, key=lambda action: panel[root, action]["final_fmax_mhz"]),
                })
        global_regrets = [row["global_regret_mhz"] for row in evaluations]
        stump_regrets = [row["stump_regret_mhz"] for row in evaluations]
        payload[mode] = {
            "rows": evaluations,
            "global": {
                "mean_regret_mhz": statistics.mean(global_regrets),
                "median_regret_mhz": median(global_regrets),
                "top1_hits": sum(row["global_action"] == row["oracle_action"] for row in evaluations),
            },
            "stump": {
                "mean_regret_mhz": statistics.mean(stump_regrets),
                "median_regret_mhz": median(stump_regrets),
                "top1_hits": sum(row["stump_action"] == row["oracle_action"] for row in evaluations),
                "worse_fraction": sum(s > g + 1e-12 for s, g in zip(stump_regrets, global_regrets)) / len(evaluations),
            },
        }
    return payload


def read_attempt(job_dir: Path, flow: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run_db = job_dir / "runs" / f"01_{flow}" / "run.db"
    connection = sqlite3.connect(run_db)
    connection.row_factory = sqlite3.Row
    try:
        attempt = connection.execute("SELECT * FROM attempts ORDER BY scheduled_at DESC LIMIT 1").fetchone()
        validation = connection.execute(
            "SELECT * FROM validations WHERE attempt_id = ? ORDER BY checked_at DESC LIMIT 1", (attempt["attempt_id"],)
        ).fetchone()
        outcome = connection.execute(
            "SELECT * FROM outcomes WHERE attempt_id = ? ORDER BY finished_at DESC LIMIT 1", (attempt["attempt_id"],)
        ).fetchone()
        return dict(validation), json.loads(outcome["metrics_json"])
    finally:
        connection.close()


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def heatmap_svg(path: Path, roots: list[str], actions: list[str], panel: dict[tuple[str, str], dict[str, Any]]) -> None:
    cell_w, cell_h, left, top = 105, 30, 220, 70
    values = [panel[root, action]["fmax_improvement_mhz"] for root in roots for action in actions]
    bound = max(abs(min(values)), abs(max(values)), 1.0)
    width, height = left + cell_w * len(actions) + 20, top + cell_h * len(roots) + 40
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">', '<rect width="100%" height="100%" fill="white"/>', '<g font-family="sans-serif" font-size="11">']
    for index, action in enumerate(actions):
        parts.append(f'<text x="{left + index * cell_w + 4}" y="55" transform="rotate(-25 {left + index * cell_w + 4} 55)">{action}</text>')
    for row_index, root in enumerate(roots):
        y = top + row_index * cell_h
        parts.append(f'<text x="4" y="{y + 20}">{root}</text>')
        for column, action in enumerate(actions):
            value = panel[root, action]["fmax_improvement_mhz"]
            strength = min(1.0, abs(value) / bound)
            color = f'rgb({int(255 - 130 * strength)},{int(255 - (40 if value >= 0 else 170) * strength)},{int(255 - (170 if value >= 0 else 40) * strength)})'
            x = left + column * cell_w
            parts.append(f'<rect x="{x}" y="{y}" width="{cell_w - 2}" height="{cell_h - 2}" fill="{color}"/>')
            parts.append(f'<text x="{x + 5}" y="{y + 19}">{value:.2f}</text>')
    parts.append('</g></svg>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--macros", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    ledger_path = args.panel / "controller-ledger.json"
    ledger = read_object(ledger_path)
    roots_manifest = read_object(args.roots)
    macros_manifest = read_object(args.macros)
    if ledger.get("status") != "COMPLETE" or ledger.get("completed_jobs") != 80:
        raise RuntimeError("panel_not_complete")
    if not ledger.get("all_roots_unchanged") or not ledger.get("all_processes_clean"):
        raise RuntimeError("panel_integrity_failed")
    jobs = {row["job_id"]: row for row in ledger["jobs"]}
    root_metadata = {row["root_id"]: row for row in roots_manifest["roots"]}
    rows = []
    for job_id, process_row in sorted(ledger["results"].items()):
        job = jobs[job_id]
        validation, metrics = read_attempt(args.panel / "jobs" / job_id, job["flow_name"])
        root_qor = metrics["qor_root_metrics"]
        utilization = root_qor.get("utilization_summary") or {}
        features = {
            "initial_wns_ns": root_qor["initial_wns_ns"],
            "initial_tns_ns": root_qor["initial_tns_ns"],
            "failing_endpoints": root_qor["failing_endpoints"],
            "initial_fmax_mhz": root_qor["initial_fmax_mhz"],
            "brams": utilization.get("brams") or 0,
            "dsps": utilization.get("dsps") or 0,
            "ffs": utilization.get("ffs") or 0,
            "luts": utilization.get("luts") or 0,
        }
        rows.append({
            **job, "family": root_metadata[job["root_id"]]["family"],
            "validation_status": validation["status"],
            "canonical_endpoint": "VALID_COMPLETION" if validation["status"] == "CLEAN" else validation["status"],
            "process_endpoint": process_row["process"]["endpoint"],
            "process_cleanup_ok": process_row["process"]["cleanup_ok"],
            "initial_fmax_mhz": metrics["initial_fmax_mhz"],
            "final_fmax_mhz": metrics["final_fmax_mhz"],
            "fmax_improvement_mhz": metrics["fmax_improvement_mhz"],
            "initial_wns_ns": metrics["initial_wns_ns"],
            "final_wns_ns": metrics["final_wns_ns"],
            "duration_seconds": metrics["duration_seconds"],
            "features": features,
        })
    roots = [row["root_id"] for row in roots_manifest["roots"]]
    actions = [row["flow_name"] for row in macros_manifest["frozen_actions"]]
    panel = {(row["root_id"], row["flow_name"]): row for row in rows}
    if len(panel) != len(roots) * len(actions) or any((root, action) not in panel for root in roots for action in actions):
        raise RuntimeError("panel_cross_product_incomplete")
    if any(row["validation_status"] != "CLEAN" for row in rows):
        raise RuntimeError("panel_contains_nonclean_attempt")

    oracle = {root: max(panel[root, action]["final_fmax_mhz"] for action in actions) for root in roots}
    root_summaries = []
    for root in roots:
        winner = max(actions, key=lambda action: panel[root, action]["final_fmax_mhz"])
        root_summaries.append({
            "root_id": root, "family": root_metadata[root]["family"],
            "oracle_action": winner, "oracle_fmax_mhz": oracle[root],
            "baseline_fmax_mhz": panel[root, actions[0]]["initial_fmax_mhz"],
            "oracle_improvement_mhz": oracle[root] - panel[root, actions[0]]["initial_fmax_mhz"],
            "within_root_fmax_spread_mhz": max(panel[root, action]["final_fmax_mhz"] for action in actions) - min(panel[root, action]["final_fmax_mhz"] for action in actions),
        })
    action_summaries = []
    for action in actions:
        action_rows = [panel[root, action] for root in roots]
        improvements = [row["fmax_improvement_mhz"] for row in action_rows]
        runtimes = [row["duration_seconds"] for row in action_rows]
        action_summaries.append({
            "flow_name": action,
            "mean_fmax_improvement_mhz": statistics.mean(improvements),
            "median_fmax_improvement_mhz": median(improvements),
            "bootstrap_mean_improvement_95ci": bootstrap_mean_ci(improvements, seed="wp013:" + action),
            "median_runtime_seconds": median(runtimes),
            "runtime_iqr_seconds": [statistics.quantiles(runtimes, n=4)[0], statistics.quantiles(runtimes, n=4)[2]],
            "oracle_wins": sum(row["oracle_action"] == action for row in root_summaries),
            "valid_count": len(action_rows),
        })
    groups = {}
    for root in roots:
        family = root_metadata[root]["family"]
        groups[root] = "Rosetta" if family.startswith("Rosetta") else "VexRiscv" if family.startswith("VexRiscv") else family
    cv = cross_validate(roots, actions, panel, oracle, groups)
    loo = cv["leave_one_benchmark_out"]
    lof = cv["leave_one_family_out"]
    qualifies = (
        loo["stump"]["mean_regret_mhz"] <= 0.9 * loo["global"]["mean_regret_mhz"]
        and loo["stump"]["worse_fraction"] <= 0.4
        and lof["stump"]["mean_regret_mhz"] <= 0.9 * lof["global"]["mean_regret_mhz"]
        and lof["stump"]["worse_fraction"] <= 0.4
    )
    ranking = sorted(actions, key=lambda action: (
        statistics.mean(oracle[root] - panel[root, action]["final_fmax_mhz"] for root in roots), action
    ))
    full_stump = fit_stump(roots, actions, panel, oracle)
    l1_core = {
        "schema_version": "dof.wp013-l1-heuristic.v1",
        "kind": "decision_stump" if qualifies else "global_frozen_macro_ranking",
        "selection_gate": {
            "complex_candidate_requires_mean_regret_reduction": 0.10,
            "maximum_worse_fold_fraction": 0.40,
            "complex_candidate_qualified": qualifies,
        },
        "global_ranking": ranking,
        "decision_stump": full_stump,
        "selected_rule": full_stump if qualifies else {"kind": "global_frozen_macro_ranking", "choose_first_legal": ranking},
        "model_visible_features": [] if not qualifies else [full_stump["feature"]],
        "forbidden_inputs": ["benchmark_name", "filename", "family", "historical_winner"],
        "training_panel_sha256": file_hash(ledger_path),
    }
    l1 = l1_core | {"heuristic_sha256": canonical_hash(l1_core)}
    audit_core = {
        "schema_version": "research-flow.wp013-macro-panel-audit.v1",
        "source_ledger_sha256": file_hash(ledger_path),
        "jobs": len(rows), "roots": len(roots), "actions": len(actions),
        "cross_product_complete": len(panel) == len(roots) * len(actions),
        "clean_attempts": sum(row["validation_status"] == "CLEAN" for row in rows),
        "succeeded_processes": sum(row["process_endpoint"] == "SUCCEEDED" for row in rows),
        "all_processes_clean": all(row["process_cleanup_ok"] for row in rows),
        "all_roots_unchanged": ledger["all_roots_unchanged"],
        "original_controller_accepted": ledger["accepted"],
        "controller_clean_enum_reclassified": 80,
        "accepted": True,
    }
    audit = audit_core | {"audit_sha256": canonical_hash(audit_core)}
    args.out.mkdir(parents=True, exist_ok=True)
    write_json(args.out / "panel-audit.json", audit)
    write_json(args.out / "panel-rows.json", rows)
    write_json(args.out / "root-summary.json", root_summaries)
    write_json(args.out / "action-summary.json", action_summaries)
    write_json(args.out / "l1-cross-validation.json", cv)
    write_json(args.out / "l1-heuristic-candidate.json", l1)
    write_csv(args.out / "panel-rows.csv", rows, [
        "root_id", "family", "flow_name", "canonical_endpoint", "initial_fmax_mhz",
        "final_fmax_mhz", "fmax_improvement_mhz", "initial_wns_ns", "final_wns_ns",
        "duration_seconds", "heavy_call_upper_bound",
    ])
    write_csv(args.out / "root-summary.csv", root_summaries, list(root_summaries[0]))
    heatmap_svg(args.out / "figures" / "fmax-improvement-heatmap.svg", roots, actions, panel)
    print(json.dumps({
        "audit_sha256": audit["audit_sha256"], "l1_kind": l1["kind"],
        "l1_sha256": l1["heuristic_sha256"], "global_ranking": ranking,
        "loo_global_mean_regret_mhz": loo["global"]["mean_regret_mhz"],
        "loo_stump_mean_regret_mhz": loo["stump"]["mean_regret_mhz"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
