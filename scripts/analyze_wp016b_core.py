#!/usr/bin/env python3
"""Frozen descriptive analysis for WP-016B's model-only Core evidence."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import random
import statistics
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016 import sha256  # noqa: E402
from optimizer_pipeline.dof.wp016b import ARMS, CAPABILITIES, MODELS, ROOTS  # noqa: E402


LABEL = "MODEL-ONLY PROPOSAL-CONSTRUCTION STUDY\nNO PHYSICAL / FUNCTIONAL / FMAX OUTCOME"
LOW, MATCHED, LEGACY = (
    "COMPACT_RETRIEVAL_LOW_RESERVE", "COMPACT_RETRIEVAL_MATCHED_RESERVE", "LEGACY_STATIC",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: list[str] | None = None) -> None:
    values = list(rows); path.parent.mkdir(parents=True, exist_ok=True)
    names = fields or sorted({key for row in values for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore", lineterminator="\n")
        writer.writeheader(); writer.writerows(values)


def exact_sign_p(discordant_positive: int, discordant_negative: int) -> float:
    n = discordant_positive + discordant_negative
    if n == 0: return 1.0
    tail = sum(math.comb(n, k) for k in range(0, min(discordant_positive, discordant_negative) + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_interval(values: list[float], level: float, seed: int) -> tuple[float | None, float | None]:
    if not values: return None, None
    rng = random.Random(seed); samples = []
    for _ in range(10_000):
        samples.append(statistics.mean(rng.choice(values) for _ in values))
    samples.sort(); alpha = (1 - level) / 2
    return samples[int(alpha * len(samples))], samples[min(len(samples) - 1, int((1 - alpha) * len(samples)))]


def flatten(cell: Mapping[str, Any]) -> dict[str, Any]:
    turns = cell.get("turns") or []
    cert = cell.get("proposal_boundary_certificate_v3") or {}
    return {
        "cell_id": cell["cell_id"], "block_id": cell["block_id"], "root_id": cell["root_id"],
        "model_system": cell["model_system"], "capability": cell["capability"],
        "arm": cell["arm"], "repeat": cell["repeat"], "terminal": cell["terminal"],
        "proposal_outcome": cell["proposal_outcome"],
        "primary_success": int(bool(cell["proposal_named_family_valid_commit_v4"])),
        "any_commit": int(cell["terminal"] == "COMMIT_RECEIVED"),
        "proposal_mechanism_family": cert.get("proposal_mechanism_family_v4"),
        "turns": len(turns), "queries": sum(
            1 for turn in turns if (turn.get("parsed_public_endpoint") or {}).get("endpoint") == "QUERY_DESIGN"
            or (turn.get("parsed_public_endpoint") or {}).get("kind") == "QUERY"
        ),
        "input_tokens": sum(int(turn.get("input_tokens") or 0) for turn in turns),
        "output_tokens": sum(int(turn.get("output_tokens") or 0) for turn in turns),
        "reasoning_tokens": sum(int((turn.get("safe_transport_evidence") or {}).get("native_usage", {}).get("reasoning_tokens") or 0) for turn in turns),
        "cost_usd": float(cell.get("known_charged_cost_usd") or 0),
        "claim_boundary": cell["claim_boundary"],
    }


def paired(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keyed = {(r["root_id"], r["model_system"], r["capability"], int(r["repeat"]), r["arm"]): r for r in rows}
    output = []
    for capability in CAPABILITIES:
        for name, treatment, control in (
            ("COMPACT_LOW_MINUS_LEGACY", LOW, LEGACY),
            ("COMPACT_MATCHED_MINUS_COMPACT_LOW", MATCHED, LOW),
        ):
            for root in ROOTS:
                for model in MODELS:
                    for repeat in (1, 2):
                        a = keyed[(root, model, capability, repeat, treatment)]
                        b = keyed[(root, model, capability, repeat, control)]
                        output.append({"contrast": name, "capability": capability, "root_id": root,
                                       "model_system": model, "repeat": repeat,
                                       "treatment_success": a["primary_success"],
                                       "control_success": b["primary_success"],
                                       "difference": a["primary_success"] - b["primary_success"]})
    for root in ROOTS:
        for model in MODELS:
            for repeat in (1, 2):
                g = keyed[(root, model, "G", repeat, MATCHED)]
                p = keyed[(root, model, "P", repeat, MATCHED)]
                output.append({"contrast": "G_MINUS_P_COMPACT_MATCHED", "capability": "G-P",
                               "root_id": root, "model_system": model, "repeat": repeat,
                               "treatment_success": g["primary_success"],
                               "control_success": p["primary_success"],
                               "difference": g["primary_success"] - p["primary_success"]})
    return output


def contrast_summary(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = defaultdict(list)
    for row in pairs: groups[(row["contrast"], row["capability"])].append(row)
    output = []
    for (contrast, capability), values in sorted(groups.items()):
        diffs = [float(row["difference"]) for row in values]
        positive = sum(value > 0 for value in diffs); negative = sum(value < 0 for value in diffs)
        lo80, hi80 = bootstrap_interval(diffs, .80, int(sha256(contrast)[:8], 16))
        lo95, hi95 = bootstrap_interval(diffs, .95, int(sha256(capability + contrast)[:8], 16))
        output.append({"contrast": contrast, "capability": capability, "pairs": len(values),
                       "mean_difference": statistics.mean(diffs), "discordant_positive": positive,
                       "discordant_negative": negative, "concordant": len(values)-positive-negative,
                       "exact_paired_p": exact_sign_p(positive, negative),
                       "bootstrap_80_low": lo80, "bootstrap_80_high": hi80,
                       "bootstrap_95_low": lo95, "bootstrap_95_high": hi95})
    return output


def figures(rows: list[dict[str, Any]], contrasts: list[dict[str, Any]], out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figdir = out / "figures"; figdir.mkdir(parents=True, exist_ok=True)
    arm_short = {LEGACY: "Legacy", LOW: "Compact low", MATCHED: "Compact matched"}
    # 1 heatmap
    heat = []
    for model in MODELS:
        for capability in CAPABILITIES:
            heat.append([statistics.mean(r["primary_success"] for r in rows if r["model_system"] == model
                                          and r["capability"] == capability and r["arm"] == arm) for arm in ARMS])
    fig, ax = plt.subplots(figsize=(8, 4)); image = ax.imshow(heat, vmin=0, vmax=1, cmap="viridis")
    ax.set_xticks(range(3), [arm_short[a] for a in ARMS], rotation=15)
    ax.set_yticks(range(4), [f"{m.split('/')[-1]} / {c}" for m in MODELS for c in CAPABILITIES])
    for y, line in enumerate(heat):
        for x, value in enumerate(line): ax.text(x, y, f"{value:.2f}", ha="center", va="center", color="white")
    fig.colorbar(image, ax=ax, label="Primary success rate"); ax.set_title(LABEL); fig.tight_layout()
    fig.savefig(figdir / "core48_primary_endpoint_heatmap.png", dpi=180); plt.close(fig)
    write_csv(out / "tables/core48_primary_endpoint_heatmap.csv", [
        {"model_system": m, "capability": c, "arm": a,
         "success_rate": statistics.mean(r["primary_success"] for r in rows if r["model_system"] == m and r["capability"] == c and r["arm"] == a)}
        for m in MODELS for c in CAPABILITIES for a in ARMS])

    # 2 paired contrasts
    fig, ax = plt.subplots(figsize=(9, 4)); labels = [f"{r['capability']}\n{r['contrast']}" for r in contrasts]
    values = [r["mean_difference"] for r in contrasts]
    ax.bar(range(len(values)), values, color=["#4c78a8" if v >= 0 else "#e45756" for v in values])
    ax.axhline(0, color="black", lw=.8); ax.set_xticks(range(len(values)), labels, rotation=20, ha="right")
    ax.set_ylabel("Paired success difference"); ax.set_title(LABEL); fig.tight_layout()
    fig.savefig(figdir / "core48_paired_arm_contrasts.png", dpi=180); plt.close(fig)
    write_csv(out / "tables/core48_paired_arm_contrasts.csv", contrasts)

    # 3 terminal composition
    terminals = sorted({r["terminal"] for r in rows}); bottom = [0] * 3
    fig, ax = plt.subplots(figsize=(9, 5))
    terminal_rows = []
    for terminal in terminals:
        counts = [sum(r["terminal"] == terminal for r in rows if r["arm"] == arm) for arm in ARMS]
        ax.bar(range(3), counts, bottom=bottom, label=terminal); bottom = [a+b for a,b in zip(bottom, counts)]
        terminal_rows.extend({"arm": arm, "terminal": terminal, "count": count} for arm, count in zip(ARMS, counts))
    ax.set_xticks(range(3), [arm_short[a] for a in ARMS]); ax.set_ylabel("Cells"); ax.legend(fontsize=7)
    ax.set_title(LABEL); fig.tight_layout(); fig.savefig(figdir / "core48_terminal_composition.png", dpi=180); plt.close(fig)
    write_csv(out / "tables/core48_terminal_composition.csv", terminal_rows)

    # 4 root/model small multiples
    fig, axes = plt.subplots(2, 2, figsize=(10, 7), sharey=True)
    small_rows = []
    for ax, root, model in zip(axes.flat, [r for r in ROOTS for _ in MODELS], list(MODELS) * 2):
        vals = [statistics.mean(r["primary_success"] for r in rows if r["root_id"] == root
                                and r["model_system"] == model and r["arm"] == arm) for arm in ARMS]
        ax.bar(range(3), vals); ax.set_ylim(0, 1); ax.set_xticks(range(3), [arm_short[a] for a in ARMS], rotation=20)
        ax.set_title(f"{root} / {model.split('/')[-1]}")
        small_rows.extend({"root_id": root, "model_system": model, "arm": arm, "success_rate": value}
                          for arm, value in zip(ARMS, vals))
    fig.suptitle(LABEL); fig.tight_layout(); fig.savefig(figdir / "core48_root_model_small_multiples.png", dpi=180); plt.close(fig)
    write_csv(out / "tables/core48_root_model_small_multiples.csv", small_rows)

    # 5 turn/token/cost
    fig, axes = plt.subplots(1, 3, figsize=(12, 4))
    metric_rows = []
    for ax, metric, title in zip(axes, ("turns", "input_tokens", "cost_usd"), ("Turns", "Input tokens", "Cost USD")):
        vals = [[r[metric] for r in rows if r["arm"] == arm] for arm in ARMS]
        ax.boxplot(vals, tick_labels=[arm_short[a] for a in ARMS]); ax.tick_params(axis="x", rotation=20); ax.set_title(title)
        for arm, items in zip(ARMS, vals):
            metric_rows.append({"metric": metric, "arm": arm, "n": len(items), "mean": statistics.mean(items),
                                "median": statistics.median(items), "minimum": min(items), "maximum": max(items)})
    fig.suptitle(LABEL); fig.tight_layout(); fig.savefig(figdir / "core48_turn_token_cost.png", dpi=180); plt.close(fig)
    write_csv(out / "tables/core48_turn_token_cost.csv", metric_rows)


def analyze(result_path: Path, out: Path) -> dict[str, Any]:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") != "PASS_WP016B_CORE" or len(result.get("cells", ())) != 48:
        raise RuntimeError("wp016b_analysis_requires_complete_core")
    rows = [flatten(cell) for cell in result["cells"]]
    pairs = paired(rows); summaries = contrast_summary(pairs)
    write_csv(out / "tables/core48_raw_cells.csv", rows)
    write_csv(out / "tables/core48_paired_outcomes.csv", pairs)
    write_csv(out / "tables/core48_contrast_summary.csv", summaries)
    terminal = Counter(row["terminal"] for row in rows)
    write_csv(out / "tables/core48_typed_terminals.csv", [
        {"terminal": key, "count": value} for key, value in sorted(terminal.items())])
    strata = []
    for dimensions in (("model_system", "capability", "arm"), ("root_id", "capability", "arm"), ("repeat", "capability", "arm")):
        groups = defaultdict(list)
        for row in rows: groups[tuple(row[key] for key in dimensions)].append(row)
        for key, values in groups.items():
            strata.append({**dict(zip(dimensions, key)), "stratum": "×".join(dimensions), "n": len(values),
                           "primary_success_rate": statistics.mean(v["primary_success"] for v in values),
                           "mean_turns": statistics.mean(v["turns"] for v in values),
                           "mean_cost_usd": statistics.mean(v["cost_usd"] for v in values)})
    write_csv(out / "tables/core48_strata.csv", strata)
    failures = []
    for cell in result["cells"]:
        if cell["proposal_named_family_valid_commit_v4"]: continue
        last = (cell.get("turns") or [{}])[-1]
        failures.append({"cell_id": cell["cell_id"], "terminal": cell["terminal"],
                         "public_final_text_utf8": last.get("public_final_text_utf8"),
                         "controller_public_response": last.get("controller_public_response")})
    write_json(out / "public_failure_examples.json", failures[:12])
    figures(rows, summaries, out)
    extension = {
        "schema_version": "wp016b.extension-decision-evidence-card.v1",
        "core_completion_status": result["status"], "core_cells": 48,
        "remaining_paper_writing_time": "HUMAN_INPUT_REQUIRED",
        "root_model_repeat_heterogeneity": strata,
        "arm_contrasts": summaries, "g_specific_result": [r for r in summaries if r["capability"] == "G"],
        "dominant_typed_failure_modes": terminal.most_common(),
        "estimated_extension_cells": 48,
        "estimated_extension_cost_usd": float(result["known_charged_cost_usd"]),
        "estimated_extension_wall_seconds": result["wall_seconds"],
        "expected_information_gain": "ADDITIONAL_FAMILY_HETEROGENEITY; NOT_AUTHORISED",
        "extension_executed": False,
    }
    write_json(out / "extension-decision-evidence-card.json", extension)
    summary = {
        "schema_version": "wp016b.core-analysis-summary.v1", "status": "PASS_ANALYSIS",
        "claim_boundary": LABEL.replace("\n", " / "), "cells": 48,
        "primary_successes": sum(r["primary_success"] for r in rows),
        "terminal_counts": dict(terminal), "contrasts": summaries,
        "total_cost_usd": float(result["known_charged_cost_usd"]),
        "provider_requests": result["provider_requests"], "backend_calls": 0,
        "extension_executed": False,
    }
    write_json(out / "core48_analysis_summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True); args = parser.parse_args()
    if args.out.exists(): raise FileExistsError(args.out)
    summary = analyze(args.result.resolve(strict=True), args.out)
    print(json.dumps(summary, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
