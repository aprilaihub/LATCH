#!/usr/bin/env python3
"""Frozen Expansion-96 and combined-144 descriptive analysis for WP-016B."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016_canary import file_sha256  # noqa: E402
from optimizer_pipeline.dof.wp016b import ARMS, CAPABILITIES  # noqa: E402
from optimizer_pipeline.dof.wp016b_expansion import (  # noqa: E402
    CORE_ROOTS, MODELS, NEW_ROOTS,
)
from scripts.analyze_wp016b_core import (  # noqa: E402
    bootstrap_interval, exact_sign_p,
)
from scripts.run_wp016b_expansion import (  # noqa: E402
    CORE_DERIVED_SHA256, CORE_RAW_SHA256,
)


LABEL = "MODEL-ONLY PROPOSAL-CONSTRUCTION STUDY\nNO PHYSICAL / FUNCTIONAL / FMAX OUTCOME"
LEGACY = "LEGACY_STATIC"
LOW = "COMPACT_RETRIEVAL_LOW_RESERVE"
MATCHED = "COMPACT_RETRIEVAL_MATCHED_RESERVE"
ROOTS = (*CORE_ROOTS, *NEW_ROOTS)
EXPANSION_A2_RAW_SHA256 = "74d25366d1713040d390ce610aa05cc1408f4b4c326a58679bbaf16b4fa9b0a9"
LEGACY_CONTRAST_CSV_SHA256 = "dd8b92c06867aa746c2742909db6a8a596a7b900d747d3706d51c84c407d5e90"
LEGACY_PAIRED_OUTCOMES_CSV_SHA256 = "93549fb3fd249519968e617425b123660136a8f6128c6667957fe0ba546f4da8"
COST_QUANTUM = Decimal("0.0000001")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    rows = list(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _journal_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_expansion_reasoning_receipts(
    journal_path: Path,
) -> tuple[dict[tuple[str, int, str], int], list[dict[str, Any]], str]:
    journal = journal_path / "journal" if (journal_path / "journal").is_dir() else journal_path
    paths = sorted(journal.glob("*__request-complete.json"))
    if not paths:
        raise RuntimeError("expansion_request_complete_receipts_missing")
    mapping: dict[tuple[str, int, str], int] = {}
    rows: list[dict[str, Any]] = []
    for path in paths:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("kind") != "request-complete":
            raise RuntimeError(f"invalid_request_complete_receipt:{path}")
        usage = receipt.get("native_usage") or {}
        value = usage.get("reasoning_tokens")
        if value is None:
            raise RuntimeError(f"reasoning_tokens_missing_from_receipt:{path}")
        key = (
            str(receipt["cell_id"]), int(receipt["turn"]),
            str(receipt["client_request_id"]),
        )
        if key in mapping:
            raise RuntimeError(f"duplicate_request_complete_identity:{key}")
        mapping[key] = int(value)
        rows.append({
            "cell_id": key[0], "turn": key[1], "client_request_id": key[2],
            "reasoning_tokens": int(value),
        })
    return mapping, rows, _journal_digest(paths)


def flatten(
    cell: Mapping[str, Any], *, phase: str, root_stratum: str,
    reasoning_by_turn: Mapping[tuple[str, int, str], int] | None = None,
) -> dict[str, Any]:
    turns = list(cell.get("turns") or ())
    certificate = cell.get("proposal_boundary_certificate_v3") or {}
    latencies = [float(turn["request_latency_seconds"]) for turn in turns
                 if turn.get("request_latency_seconds") is not None]
    reasoning_values: list[int] = []
    if reasoning_by_turn is not None:
        for turn in turns:
            key = (
                str(cell["cell_id"]), int(turn["turn"]),
                str(turn["client_request_id"]),
            )
            if key not in reasoning_by_turn:
                raise RuntimeError(f"reasoning_receipt_join_missing:{key}")
            reasoning_values.append(int(reasoning_by_turn[key]))
    complete_latency = bool(turns) and len(latencies) == len(turns)
    return {
        "phase": phase,
        "cell_id": cell["cell_id"], "block_id": cell["block_id"],
        "root_id": cell["root_id"], "root_stratum": root_stratum,
        "model_system": cell["model_system"], "capability": cell["capability"],
        "arm": cell["arm"], "repeat": int(cell["repeat"]),
        "terminal": cell["terminal"], "proposal_outcome": cell["proposal_outcome"],
        "primary_success": int(bool(cell["proposal_named_family_valid_commit_v4"])),
        "any_commit": int(cell["terminal"] == "COMMIT_RECEIVED"),
        "family_proven": int(bool(cell["proposal_named_family_valid_commit_v4"])),
        "family_unproven_commit": int(
            cell["terminal"] == "COMMIT_RECEIVED"
            and not cell["proposal_named_family_valid_commit_v4"]
        ),
        "proposal_mechanism_family": certificate.get("proposal_mechanism_family_v4"),
        "turns": len(turns),
        "queries": sum((turn.get("parsed_public_endpoint") or {}).get("endpoint") == "QUERY_DESIGN"
                       or (turn.get("parsed_public_endpoint") or {}).get("kind") == "QUERY"
                       for turn in turns),
        "input_tokens": sum(int(turn.get("input_tokens") or 0) for turn in turns),
        "output_tokens": sum(int(turn.get("output_tokens") or 0) for turn in turns),
        "reasoning_tokens": sum(reasoning_values) if reasoning_by_turn is not None else None,
        "reasoning_token_turns_observed": len(reasoning_values),
        "reasoning_token_turns_missing": len(turns) - len(reasoning_values),
        "request_latency_seconds": sum(latencies) if complete_latency else None,
        "request_latency_turns_observed": len(latencies),
        "request_latency_turns_missing": len(turns) - len(latencies),
        "mean_turn_latency_seconds": statistics.mean(latencies) if latencies else None,
        # The accepted Core per-cell strings inherited binary-float display tails.
        # Frozen pricing is seven-decimal USD, so normalise at that exact decimal
        # quantum before any derived aggregation.
        "cost_usd": str(
            Decimal(str(cell.get("known_charged_cost_usd") or "0"))
            .quantize(COST_QUANTUM)
        ),
        "request_count": int(cell.get("request_count") or len(turns)),
        "claim_boundary": cell["claim_boundary"],
    }


def _groups(rows: list[dict[str, Any]], dimensions: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row[name] for name in dimensions)].append(row)
    output = []
    for key, values in sorted(grouped.items(), key=lambda item: str(item[0])):
        latency_values = [row["request_latency_seconds"] for row in values
                          if row["request_latency_seconds"] is not None]
        reasoning_values = [row["reasoning_tokens"] for row in values
                            if row["reasoning_tokens"] is not None]
        latency_complete = len(latency_values) == len(values)
        reasoning_complete = len(reasoning_values) == len(values)
        output.append({
            **dict(zip(dimensions, key, strict=True)), "n": len(values),
            "primary_successes": sum(row["primary_success"] for row in values),
            "primary_success_rate": statistics.mean(row["primary_success"] for row in values),
            "any_commit_rate": statistics.mean(row["any_commit"] for row in values),
            "mean_turns": statistics.mean(row["turns"] for row in values),
            "mean_cost_usd": str(
                sum((Decimal(row["cost_usd"]) for row in values), Decimal("0"))
                / Decimal(len(values))
            ),
            "mean_request_latency_seconds": (
                statistics.mean(latency_values) if latency_complete else None
            ),
            "request_latency_observed_cells": len(latency_values),
            "request_latency_missing_cells": len(values) - len(latency_values),
            "request_latency_scope": (
                "OBSERVED_ALL" if latency_complete else
                "NOT_RECORDED" if not latency_values else "PARTIAL_NOT_POOLED"
            ),
            "reasoning_tokens_sum": sum(reasoning_values) if reasoning_complete else None,
            "reasoning_tokens_observed_cells": len(reasoning_values),
            "reasoning_tokens_missing_cells": len(values) - len(reasoning_values),
        })
    return output


def paired(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keyed = {(r["root_id"], r["model_system"], r["capability"], r["repeat"], r["arm"]): r
             for r in rows}
    expected = len(ROOTS) * len(MODELS) * len(CAPABILITIES) * 2 * len(ARMS)
    if len(keyed) != expected:
        raise RuntimeError(f"combined_pairing_inventory_invalid:{len(keyed)}:{expected}")
    output: list[dict[str, Any]] = []
    for capability in CAPABILITIES:
        for name, treatment, control in (
            ("COMPACT_LOW_MINUS_LEGACY", LOW, LEGACY),
            ("COMPACT_MATCHED_MINUS_COMPACT_LOW", MATCHED, LOW),
        ):
            for root in ROOTS:
                for model in MODELS:
                    for repeat in (1, 2):
                        treatment_row = keyed[(root, model, capability, repeat, treatment)]
                        control_row = keyed[(root, model, capability, repeat, control)]
                        output.append({
                            "contrast": name, "capability": capability,
                            "root_id": root, "model_system": model, "repeat": repeat,
                            "root_stratum": treatment_row["root_stratum"],
                            "treatment_success": treatment_row["primary_success"],
                            "control_success": control_row["primary_success"],
                            "difference": treatment_row["primary_success"] - control_row["primary_success"],
                        })
    for root in ROOTS:
        for model in MODELS:
            for repeat in (1, 2):
                g = keyed[(root, model, "G", repeat, MATCHED)]
                p = keyed[(root, model, "P", repeat, MATCHED)]
                output.append({
                    "contrast": "G_MINUS_P_COMPACT_MATCHED", "capability": "G-P",
                    "root_id": root, "model_system": model, "repeat": repeat,
                    "root_stratum": g["root_stratum"],
                    "treatment_success": g["primary_success"],
                    "control_success": p["primary_success"],
                    "difference": g["primary_success"] - p["primary_success"],
                })
    return output


def contrast_summary(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in pairs:
        grouped[(row["contrast"], row["capability"])].append(row)
    output = []
    for (contrast, capability), values in sorted(grouped.items()):
        diffs = [float(value["difference"]) for value in values]
        positive = sum(value > 0 for value in diffs)
        negative = sum(value < 0 for value in diffs)
        seed80 = int(hashlib.sha256(f"{contrast}|{capability}|80".encode()).hexdigest()[:8], 16)
        seed95 = int(hashlib.sha256(f"{contrast}|{capability}|95".encode()).hexdigest()[:8], 16)
        lo80, hi80 = bootstrap_interval(diffs, .80, seed80)
        lo95, hi95 = bootstrap_interval(diffs, .95, seed95)
        output.append({
            "contrast": contrast, "capability": capability, "pairs": len(values),
            "mean_difference": statistics.mean(diffs),
            "discordant_positive": positive, "discordant_negative": negative,
            "concordant": len(values) - positive - negative,
            "exact_paired_p": exact_sign_p(positive, negative),
            "bootstrap_80_low": lo80, "bootstrap_80_high": hi80,
            "bootstrap_95_low": lo95, "bootstrap_95_high": hi95,
        })
    return output


def _figure_setup():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def figures(rows: list[dict[str, Any]], contrasts: list[dict[str, Any]], out: Path) -> None:
    plt = _figure_setup()
    figdir = out / "figures"
    tabledir = out / "tables"
    figdir.mkdir(parents=True, exist_ok=True)
    arm_short = {LEGACY: "Legacy", LOW: "Compact low", MATCHED: "Compact matched"}

    heat_rows = _groups(rows, ("model_system", "capability", "arm"))
    heat_lookup = {(r["model_system"], r["capability"], r["arm"]): r["primary_success_rate"] for r in heat_rows}
    heat = [[heat_lookup[(m, c, a)] for a in ARMS] for m in MODELS for c in CAPABILITIES]
    fig, ax = plt.subplots(figsize=(8, 5)); image = ax.imshow(heat, vmin=0, vmax=1, cmap="viridis")
    ax.set_xticks(range(3), [arm_short[a] for a in ARMS], rotation=15)
    ax.set_yticks(range(6), [f"{m.split('/')[-1]} / {c}" for m in MODELS for c in CAPABILITIES])
    for y, line in enumerate(heat):
        for x, value in enumerate(line): ax.text(x, y, f"{value:.2f}", ha="center", va="center", color="white")
    fig.colorbar(image, ax=ax); ax.set_title(LABEL); fig.tight_layout()
    fig.savefig(figdir / "combined144_primary_endpoint_heatmap.png", dpi=180); plt.close(fig)
    write_csv(tabledir / "combined144_primary_endpoint_heatmap.csv", heat_rows)

    gradient = _groups(rows, ("model_system", "capability"))
    fig, ax = plt.subplots(figsize=(8, 4))
    x = range(len(MODELS)); width = .35
    for offset, capability in ((-.5, "P"), (.5, "G")):
        values = [next(r["primary_success_rate"] for r in gradient if r["model_system"] == m and r["capability"] == capability) for m in MODELS]
        ax.bar([i + offset * width for i in x], values, width, label=capability)
    ax.set_xticks(list(x), [m.split('/')[-1] for m in MODELS]); ax.set_ylim(0, 1); ax.legend(); ax.set_title(LABEL)
    fig.tight_layout(); fig.savefig(figdir / "combined144_model_gradient.png", dpi=180); plt.close(fig)
    write_csv(tabledir / "combined144_model_gradient.csv", gradient)

    fig, ax = plt.subplots(figsize=(9, 4)); labels = [f"{r['capability']}\n{r['contrast']}" for r in contrasts]
    values = [r["mean_difference"] for r in contrasts]
    ax.bar(range(len(values)), values, color=["#4c78a8" if value >= 0 else "#e45756" for value in values])
    ax.axhline(0, color="black", lw=.8); ax.set_xticks(range(len(values)), labels, rotation=20, ha="right")
    ax.set_ylabel("Paired success difference"); ax.set_title(LABEL); fig.tight_layout()
    fig.savefig(figdir / "combined144_paired_arm_contrasts.png", dpi=180); plt.close(fig)
    write_csv(tabledir / "combined144_paired_arm_contrasts.csv", contrasts)

    terminal_rows = _groups(rows, ("arm", "terminal"))
    terminals = sorted({r["terminal"] for r in rows}); bottoms = [0, 0, 0]
    fig, ax = plt.subplots(figsize=(10, 5))
    for terminal in terminals:
        counts = [sum(r["terminal"] == terminal for r in rows if r["arm"] == arm) for arm in ARMS]
        ax.bar(range(3), counts, bottom=bottoms, label=terminal); bottoms = [a + b for a, b in zip(bottoms, counts)]
    ax.set_xticks(range(3), [arm_short[a] for a in ARMS]); ax.legend(fontsize=6); ax.set_title(LABEL)
    fig.tight_layout(); fig.savefig(figdir / "combined144_terminal_composition.png", dpi=180); plt.close(fig)
    write_csv(tabledir / "combined144_terminal_composition.csv", terminal_rows)

    stratum = _groups(rows, ("root_stratum", "capability", "arm"))
    fig, axes = plt.subplots(1, 2, figsize=(12, 4), sharey=True)
    for ax, name in zip(axes, ("SOURCE_TIMED_TRANSFER", "HELDOUT_PLUS_SYNTHETIC_TIMING")):
        values = [next(r["primary_success_rate"] for r in stratum if r["root_stratum"] == name and r["capability"] == c and r["arm"] == a) for c in CAPABILITIES for a in ARMS]
        ax.bar(range(6), values); ax.set_xticks(range(6), [f"{c}\n{arm_short[a]}" for c in CAPABILITIES for a in ARMS], rotation=25); ax.set_title(name); ax.set_ylim(0, 1)
    fig.suptitle(LABEL); fig.tight_layout(); fig.savefig(figdir / "combined144_root_stratum_panels.png", dpi=180); plt.close(fig)
    write_csv(tabledir / "combined144_root_stratum_panels.csv", stratum)

    small = _groups(rows, ("root_id", "model_system", "capability", "arm"))
    fig, axes = plt.subplots(4, 3, figsize=(13, 11), sharey=True)
    for ax, root, model in zip(axes.flat, [r for r in ROOTS for _ in MODELS], list(MODELS) * len(ROOTS)):
        x = range(3); width = .36
        for offset, capability in ((-.5, "P"), (.5, "G")):
            values = [next(
                r["primary_success_rate"] for r in small
                if r["root_id"] == root and r["model_system"] == model
                and r["capability"] == capability and r["arm"] == arm
            ) for arm in ARMS]
            ax.bar([i + offset * width for i in x], values, width, label=capability)
        ax.set_ylim(0, 1); ax.set_xticks(list(x), [arm_short[a] for a in ARMS], rotation=30, fontsize=7); ax.set_title(f"{root} / {model.split('/')[-1]}", fontsize=8)
    axes.flat[0].legend(fontsize=7)
    fig.suptitle(LABEL); fig.tight_layout(); fig.savefig(figdir / "combined144_root_model_small_multiples.png", dpi=180); plt.close(fig)
    write_csv(tabledir / "combined144_root_model_small_multiples.csv", small)

    provenance = _groups(rows, ("phase", "capability", "arm"))
    fig, axes = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    for ax, phase in zip(axes, ("CORE_48", "EXPANSION_96")):
        values = [next(r["primary_success_rate"] for r in provenance if r["phase"] == phase and r["capability"] == c and r["arm"] == a) for c in CAPABILITIES for a in ARMS]
        ax.bar(range(6), values); ax.set_ylim(0, 1); ax.set_xticks(range(6), [f"{c}\n{arm_short[a]}" for c in CAPABILITIES for a in ARMS], rotation=25); ax.set_title(phase)
    fig.suptitle(LABEL); fig.tight_layout(); fig.savefig(figdir / "expansion96_vs_core48.png", dpi=180); plt.close(fig)
    write_csv(tabledir / "expansion96_vs_core48.csv", provenance)

    metrics = _groups(rows, ("phase", "arm"))
    fig, axes = plt.subplots(1, 3, figsize=(12, 4))
    for ax, metric, title in zip(axes, ("turns", "input_tokens", "cost_usd"), ("Turns", "Input tokens", "Cost USD")):
        values = [[float(r[metric]) for r in rows if r["arm"] == arm] for arm in ARMS]
        ax.boxplot(values, tick_labels=[arm_short[a] for a in ARMS]); ax.tick_params(axis="x", rotation=25); ax.set_title(title)
    fig.suptitle(LABEL); fig.tight_layout(); fig.savefig(figdir / "combined144_turn_token_cost.png", dpi=180); plt.close(fig)
    write_csv(tabledir / "combined144_turn_token_cost.csv", metrics)

    expansion_rows = [row for row in rows if row["phase"] == "EXPANSION_96"]
    latency_values = [[float(row["request_latency_seconds"]) for row in expansion_rows
                       if row["arm"] == arm and row["request_latency_seconds"] is not None]
                      for arm in ARMS]
    if any(len(values) != 32 for values in latency_values):
        raise RuntimeError("expansion_latency_inventory_incomplete")
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.boxplot(latency_values, tick_labels=[arm_short[a] for a in ARMS])
    ax.tick_params(axis="x", rotation=20); ax.set_ylabel("Cell request latency sum (s)")
    ax.set_title(f"EXPANSION 96 ONLY\n{LABEL}"); fig.tight_layout()
    fig.savefig(figdir / "expansion96_latency_distribution.png", dpi=180); plt.close(fig)
    write_csv(
        tabledir / "expansion96_latency_distribution.csv",
        _groups(expansion_rows, ("model_system", "capability", "arm")),
    )


def analyze(
    core_raw_path: Path, core_derived_path: Path, expansion_path: Path,
    expansion_journal_path: Path, out: Path,
) -> dict[str, Any]:
    if file_sha256(core_raw_path) != CORE_RAW_SHA256 or file_sha256(core_derived_path) != CORE_DERIVED_SHA256:
        raise RuntimeError("accepted_core_evidence_hash_invalid")
    core_raw = json.loads(core_raw_path.read_text())
    core = json.loads(core_derived_path.read_text())
    expansion = json.loads(expansion_path.read_text())
    if file_sha256(expansion_path) != EXPANSION_A2_RAW_SHA256:
        raise RuntimeError("accepted_expansion_a2_evidence_hash_invalid")
    if len(core_raw.get("cells", ())) != 48 or len(core.get("cells", ())) != 48:
        raise RuntimeError("accepted_core_cell_count_invalid")
    if expansion.get("status") not in {
        "PASS_WP016B_EXPANSION", "PASS_WP016B_EXPANSION_A2",
    } or len(expansion.get("cells", ())) != 96:
        raise RuntimeError("analysis_requires_complete_expansion96")
    reasoning_by_turn, reasoning_turn_rows, reasoning_receipts_sha256 = (
        load_expansion_reasoning_receipts(expansion_journal_path)
    )
    core_rows = [flatten(cell, phase="CORE_48", root_stratum="SOURCE_TIMED_TRANSFER") for cell in core["cells"]]
    expansion_rows = [flatten(
        cell, phase="EXPANSION_96", root_stratum=cell["root_stratum"],
        reasoning_by_turn=reasoning_by_turn,
    ) for cell in expansion["cells"]]
    rows = core_rows + expansion_rows
    if len(rows) != 144:
        raise RuntimeError("combined144_cell_count_invalid")
    used_reasoning_keys = {
        (str(cell["cell_id"]), int(turn["turn"]), str(turn["client_request_id"]))
        for cell in expansion["cells"] for turn in cell.get("turns", ())
    }
    if used_reasoning_keys != set(reasoning_by_turn):
        raise RuntimeError("reasoning_receipt_inventory_mismatch")
    reasoning_turn_sum = sum(row["reasoning_tokens"] for row in reasoning_turn_rows)
    reasoning_cell_sum = sum(int(row["reasoning_tokens"]) for row in expansion_rows)
    if reasoning_turn_sum != reasoning_cell_sum or reasoning_turn_sum != 133_190:
        raise RuntimeError("reasoning_token_reconciliation_failed")
    pairs = paired(rows); contrasts = contrast_summary(pairs)
    if any(r["pairs"] != 24 for r in contrasts):
        raise RuntimeError("combined144_pair_count_invalid")
    write_csv(out / "tables/expansion96_raw_cells.csv", expansion_rows)
    write_csv(out / "tables/expansion96_reasoning_turns.csv", reasoning_turn_rows)
    write_csv(
        out / "tables/expansion96_reasoning_tokens.csv",
        _groups(expansion_rows, ("model_system", "capability", "arm")),
    )
    write_csv(out / "tables/combined144_raw_cells.csv", rows)
    write_csv(out / "tables/combined144_paired_outcomes.csv", pairs)
    write_csv(out / "tables/combined144_contrast_summary.csv", contrasts)
    write_csv(out / "tables/combined144_strata.csv", _groups(rows, ("root_stratum", "root_id", "model_system", "capability", "arm", "repeat")))
    write_csv(out / "tables/combined144_repeat_consistency.csv", _groups(rows, ("repeat", "capability", "arm")))
    write_csv(out / "tables/combined144_model_interface_capability.csv", _groups(rows, ("model_system", "capability", "arm")))
    terminals = Counter(row["terminal"] for row in rows)
    write_csv(out / "tables/combined144_typed_terminals.csv", ({"terminal": k, "count": v} for k, v in sorted(terminals.items())))
    failures = []
    for cell in [*core["cells"], *expansion["cells"]]:
        if cell["proposal_named_family_valid_commit_v4"]:
            continue
        last = (cell.get("turns") or [{}])[-1]
        failures.append({
            "cell_id": cell["cell_id"], "terminal": cell["terminal"],
            "public_final_text_utf8": last.get("public_final_text_utf8"),
            "controller_public_response": last.get("controller_public_response"),
        })
    write_json(out / "public_failure_mode_taxonomy.json", {"counts": dict(terminals), "examples": failures})
    scheduling = {
        "configured_concurrency_cap": expansion["configured_concurrency_cap"],
        "measured_peak_inflight_requests": expansion["measured_peak_inflight_requests"],
        "wall_seconds": expansion["wall_seconds"],
        "provider_requests": expansion["provider_requests"],
        "concurrency_events": expansion["concurrency_events"],
        "launch_wave_balance": _groups(expansion_rows, ("root_id", "model_system", "capability", "arm")),
    }
    write_json(out / "scheduling-concurrency-diagnostics.json", scheduling)
    figures(rows, contrasts, out)
    exact_combined_cost = (
        Decimal(str(core["known_charged_cost_usd"]))
        + Decimal(str(expansion["known_charged_cost_usd"]))
    )
    summary = {
        "schema_version": "wp016b.expansion-combined-analysis-summary.v2",
        "status": "PASS_COMBINED_144_ANALYSIS", "claim_boundary": LABEL.replace("\n", " / "),
        "sequential_provenance": {"core_cells": 48, "expansion_cells": 96, "combined_cells": 144},
        "primary_successes": {"core48": sum(r["primary_success"] for r in core_rows),
                              "expansion96": sum(r["primary_success"] for r in expansion_rows),
                              "combined144": sum(r["primary_success"] for r in rows)},
        "terminal_counts": dict(terminals), "contrasts": contrasts,
        "cost_usd": {"core48_corrected": core["known_charged_cost_usd"],
                     "expansion96": expansion["known_charged_cost_usd"],
                     "combined144": format(exact_combined_cost, ".7f")},
        "latency_seconds": {
            "core48": None,
            "core48_status": "NOT_RECORDED",
            "expansion96_status": "OBSERVED",
            "combined144": None,
            "combined144_status": "NOT_POOLED_ACROSS_MISSING_CORE",
        },
        "reasoning_tokens": {
            "core48": None,
            "core48_status": "NOT_RECORDED",
            "expansion96": reasoning_turn_sum,
            "expansion96_turn_receipts": len(reasoning_turn_rows),
            "expansion96_status": "RECONCILED_TURN_CELL_AGGREGATE",
            "combined144": None,
            "combined144_status": "NOT_POOLED_ACROSS_MISSING_CORE",
        },
        "provider_requests": {"core48": core["provider_requests"],
                              "expansion96": expansion["provider_requests"]},
        "backend_calls": 0,
        "root_stratum_warning": "FAMILY_HELDOUT_AND_SYNTHETIC_TIMING_ARE_ALIASED",
    }
    write_json(out / "combined144_analysis_summary.json", summary)
    contrast_csv = out / "tables/combined144_contrast_summary.csv"
    paired_csv = out / "tables/combined144_paired_outcomes.csv"
    if file_sha256(contrast_csv) != LEGACY_CONTRAST_CSV_SHA256:
        raise RuntimeError("paired_contrast_output_changed_during_correction")
    if file_sha256(paired_csv) != LEGACY_PAIRED_OUTCOMES_CSV_SHA256:
        raise RuntimeError("paired_outcome_output_changed_during_correction")
    identity_digest = hashlib.sha256(
        "\n".join(sorted(row["cell_id"] for row in rows)).encode("utf-8")
    ).hexdigest()
    correction_receipt = {
        "schema_version": "wp016b.analysis-correction-receipt.v1",
        "status": "PASS_WP016B_ANALYSIS_CORRECTION",
        "raw_evidence_hashes_unchanged": True,
        "raw_evidence": {
            "core_raw_sha256": CORE_RAW_SHA256,
            "core_derived_sha256": CORE_DERIVED_SHA256,
            "expansion_a2_sha256": EXPANSION_A2_RAW_SHA256,
            "expansion_request_complete_receipts_sha256": reasoning_receipts_sha256,
        },
        "cell_identities": {"count": 144, "sha256": identity_digest, "unchanged": True},
        "primary_endpoint_counts": summary["primary_successes"],
        "primary_endpoint_counts_unchanged": summary["primary_successes"] == {
            "core48": 12, "expansion96": 20, "combined144": 32,
        },
        "paired_contrasts_unchanged": True,
        "paired_contrast_csv_sha256": file_sha256(contrast_csv),
        "paired_outcomes_csv_sha256": file_sha256(paired_csv),
        "corrections": {
            "root_model_small_multiples": "P_AND_G_REPORTED_SEPARATELY",
            "latency": "CORE_MISSING_EXPANSION_OBSERVED_NO_COMBINED_MEAN",
            "reasoning_tokens": "EXPANSION_REQUEST_COMPLETE_RECEIPTS_RECONCILED_CORE_MISSING",
            "cost": "EXACT_DECIMAL_8_9269410",
        },
        "reasoning_token_reconciliation": {
            "turn_sum": reasoning_turn_sum,
            "cell_sum": reasoning_cell_sum,
            "aggregate_sum": sum(
                int(row["reasoning_tokens"]) for row in expansion_rows
            ),
        },
        "superseded_outputs": [
            "tables/combined144_root_model_small_multiples.csv",
            "figures/combined144_root_model_small_multiples.png",
            "tables/combined144_turn_latency_token_cost.csv",
            "figures/combined144_turn_latency_token_cost.png",
            "combined144_analysis_summary.json",
        ],
        "provider_calls": 0,
        "backend_calls": 0,
    }
    if not correction_receipt["primary_endpoint_counts_unchanged"]:
        raise RuntimeError("primary_endpoint_counts_changed_during_correction")
    write_json(out / "WP016B_ANALYSIS_CORRECTION_RECEIPT.json", correction_receipt)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-raw", type=Path, required=True)
    parser.add_argument("--core-derived", type=Path, required=True)
    parser.add_argument("--expansion", type=Path, required=True)
    parser.add_argument("--expansion-journal", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    summary = analyze(*(path.resolve(strict=True) for path in (
        args.core_raw, args.core_derived, args.expansion, args.expansion_journal,
    )), args.out.resolve())
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
