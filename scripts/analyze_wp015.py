#!/usr/bin/env python3
"""Freeze the compact canonical WP-015 dataset, tables and preliminary figures."""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
from typing import Any, Iterable, Mapping


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmax(period: Any, wns: Any) -> float | None:
    period_value, wns_value = _finite(period), _finite(wns)
    if period_value is None or wns_value is None or period_value - wns_value <= 0:
        return None
    return 1000.0 / (period_value - wns_value)


def timing(row: Mapping[str, Any], root: Mapping[str, Any]) -> dict[str, float | None]:
    execution = row.get("execution") if isinstance(row.get("execution"), Mapping) else {}
    initial = _finite(execution.get("initial_fmax_mhz"))
    final = _finite(execution.get("final_fmax_mhz"))
    initial_wns = _finite(execution.get("initial_wns_ns"))
    final_wns = _finite(execution.get("final_wns_ns"))
    pre = execution.get("pre_timing") if isinstance(execution.get("pre_timing"), Mapping) else {}
    post = execution.get("post_timing") if isinstance(execution.get("post_timing"), Mapping) else {}
    initial_wns = initial_wns if initial_wns is not None else _finite(pre.get("wns"))
    final_wns = final_wns if final_wns is not None else _finite(post.get("wns"))
    period = root.get("period_ns")
    initial = initial if initial is not None else _fmax(period, initial_wns)
    final = final if final is not None else _fmax(period, final_wns)
    return {
        "initial_fmax_mhz": initial, "final_fmax_mhz": final,
        "delta_fmax_mhz": final - initial if initial is not None and final is not None else None,
        "initial_wns_ns": initial_wns, "final_wns_ns": final_wns,
    }


def canonical_jobs(scientific: Mapping[str, Any], bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for source in scientific["jobs"]:
        root = bundle["roots"][source["root_id"]]
        values = timing(source, root)
        functional = source.get("functional_terminal") == "MULTISEED_FUNCTIONAL_PASS"
        delta = values["delta_fmax_mhz"] if functional else None
        strict = bool(source.get("strict_boundary_certificate", {}).get("strict_boundary_exercised"))
        boundary = bool(source.get("boundary_runtime_exercised"))
        selected = source.get("selected_language")
        candidate = source.get("committed_candidate")
        row = {
            "job_id": source["job_id"], "repeat_id": source["repeat_id"],
            "root_id": source["root_id"], "root_pseudonym": source["root_pseudonym"],
            "family": source["family"], "split": source["split"],
            "timing_stratum": source["timing_stratum"], "resource_class": source["resource_class"],
            "model_key": source["model_key"], "provider": source["provider"],
            "track": source["track"], "condition": source["condition"],
            "level": source["level"], "coverage_regime": source["coverage_regime"],
            "canonical_terminal": source["canonical_terminal"],
            "physical_terminal": source.get("physical_terminal"),
            "functional_terminal": source.get("functional_terminal"),
            "functionally_valid": functional, "selected_language": selected,
            "boundary_runtime_exercised": boundary,
            "strict_g_exercised": strict,
            "strict_g_mechanism": source.get("strict_boundary_certificate", {}).get("mechanism_family"),
            **values,
            "functional_valid_delta_fmax_mhz": delta,
            "root_fallback_gain_mhz": max(0.0, delta) if delta is not None else 0.0,
            "beneficial": bool(delta is not None and delta > 0),
            "provider_requests": int(source.get("provider_requests") or 0),
            "cost_usd": float(source.get("cost_usd") or 0.0),
            "planning_wall_seconds": _finite(source.get("planning_wall_seconds")),
            "actual_heavy_calls": int(source.get("actual_heavy_calls") or 0),
            "actual_light_mutations": source.get("actual_light_mutations"),
            "candidate_sha256": digest(candidate) if isinstance(candidate, Mapping) else None,
            "committed_candidate": candidate,
            "boundary_evidence_sha256": digest(source.get("boundary_evidence", {})),
            "strict_certificate_sha256": source.get("strict_boundary_certificate", {}).get("certificate_sha256"),
            "child_sha256": source.get("resolved_child_sha256"),
        }
        rows.append(row)
    if len(rows) != 1134 or len({row["job_id"] for row in rows}) != 1134:
        raise ValueError("wp015_analysis_job_inventory_not_1134")
    return rows


def _summary(values: Iterable[float]) -> dict[str, float | int | None]:
    rows = [float(value) for value in values if math.isfinite(float(value))]
    if not rows:
        return {"n": 0, "mean": None, "median": None, "std": None, "q1": None, "q3": None}
    ordered = sorted(rows)
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive") if len(ordered) > 1 else [ordered[0]] * 3
    return {
        "n": len(rows), "mean": statistics.mean(rows), "median": statistics.median(rows),
        "std": statistics.stdev(rows) if len(rows) > 1 else 0.0,
        "q1": quartiles[0], "q3": quartiles[2],
    }


def bootstrap_mean_ci(values: Iterable[float], *, seed: str, samples: int = 2000) -> tuple[float | None, float | None]:
    rows = [float(value) for value in values]
    if not rows:
        return None, None
    generator = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
    estimates = sorted(
        statistics.mean(generator.choice(rows) for _ in rows) for _ in range(samples)
    )
    return estimates[int(0.025 * (samples - 1))], estimates[int(0.975 * (samples - 1))]


def grouped_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[(row["track"], row["condition"], row["model_key"])].append(row)
    result = []
    for (track, condition, model), members in sorted(groups.items()):
        valid_deltas = [row["functional_valid_delta_fmax_mhz"] for row in members if row["functional_valid_delta_fmax_mhz"] is not None]
        result.append({
            "track": track, "condition": condition, "model_key": model,
            "jobs": len(members), "roots": len({row["root_id"] for row in members}),
            "provider_failures": sum(row["canonical_terminal"] in {"PROVIDER_FAILURE", "MODEL_OR_ROUTE_DRIFT"} for row in members),
            "physical_valid": sum(row["physical_terminal"] == "VALID_COMPLETION" for row in members),
            "functional_valid": sum(row["functionally_valid"] for row in members),
            "strict_g_exercised": sum(row["strict_g_exercised"] for row in members),
            "beneficial": sum(row["beneficial"] for row in members),
            **{"delta_" + key: value for key, value in _summary(valid_deltas).items()},
            "mean_root_fallback_gain_mhz": statistics.mean(row["root_fallback_gain_mhz"] for row in members),
            "cost_usd": sum(row["cost_usd"] for row in members),
            "provider_requests": sum(row["provider_requests"] for row in members),
            "heavy_calls": sum(row["actual_heavy_calls"] for row in members),
        })
    return result


def freedom_funnel(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[(row["track"], row["condition"])].append(row)
    result = []
    for (track, condition), members in sorted(groups.items()):
        for stage, predicate in (
            ("COMMITTED", lambda row: row["selected_language"] is not None),
            ("BOUNDARY_RUNTIME", lambda row: row["boundary_runtime_exercised"]),
            ("STRICT_G", lambda row: row["strict_g_exercised"]),
            ("PHYSICAL_VALID", lambda row: row["physical_terminal"] == "VALID_COMPLETION"),
            ("FUNCTIONAL_VALID", lambda row: row["functionally_valid"]),
            ("BENEFICIAL", lambda row: row["beneficial"]),
        ):
            count = sum(bool(predicate(row)) for row in members)
            result.append({
                "track": track, "condition": condition, "stage": stage,
                "count": count, "jobs": len(members), "rate": count / len(members),
            })
    return result


def selection_gap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for model in sorted({row["model_key"] for row in rows}):
        neutral = [row for row in rows if row["model_key"] == model and row["track"] == "N_NEUTRAL" and row["condition"] == "L4"]
        conditioned = [row for row in rows if row["model_key"] == model and row["track"] == "C_CONDITIONED" and row["condition"] == "G_STRICT_BOUNDARY"]
        n_rate = sum(row["strict_g_exercised"] for row in neutral) / len(neutral)
        c_rate = sum(row["strict_g_exercised"] for row in conditioned) / len(conditioned)
        result.append({
            "model_key": model, "neutral_l4_strict_rate": n_rate,
            "conditioned_g_strict_rate": c_rate, "selection_gap": c_rate - n_rate,
            "neutral_n": len(neutral), "conditioned_n": len(conditioned),
        })
    return result


def reference_table(
    development: Mapping[str, Any], references: Mapping[str, Any], bundle: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = [{
        "root_id": row["root_id"], "action_key": row["action_key"],
        "functionally_valid": bool(row["functionally_valid"]),
        "delta_fmax_mhz": row.get("raw_child_delta_fmax_mhz"),
        "root_fallback_gain_mhz": float(row["root_fallback_gain_mhz"]),
        "physical_terminal": row["physical_endpoint"],
        "functional_terminal": row["functional_terminal"],
        "source": "DEVELOPMENT_PRE_AGENT",
    } for row in development["panel"]]
    for row in references["jobs"]:
        values = timing(row, bundle["roots"][row["root_id"]])
        valid = row.get("functional_terminal") == "MULTISEED_FUNCTIONAL_PASS"
        delta = values["delta_fmax_mhz"] if valid else None
        rows.append({
            "root_id": row["root_id"], "action_key": row["action_key"],
            "functionally_valid": valid, "delta_fmax_mhz": delta,
            "root_fallback_gain_mhz": max(0.0, delta) if delta is not None else 0.0,
            "physical_terminal": row["physical_terminal"],
            "functional_terminal": row.get("functional_terminal"),
            "source": "NONDEVELOPMENT_POST_AGENT",
        })
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[row["root_id"]].append(row)
    if len(rows) != 250 or any(len(value) != 10 for value in groups.values()):
        raise ValueError("wp015_reference_table_not_25_by_10")
    l1 = development["l1_fixed_global_macro_prior"][0]
    summary = {}
    for root_id, members in groups.items():
        winner = min(members, key=lambda row: (-row["root_fallback_gain_mhz"], row["action_key"]))
        l1_row = next(row for row in members if row["action_key"] == l1)
        summary[root_id] = {
            "root_id": root_id, "oracle_action_key": winner["action_key"],
            "oracle_gain_mhz": winner["root_fallback_gain_mhz"],
            "l1_action_key": l1, "l1_gain_mhz": l1_row["root_fallback_gain_mhz"],
            "expert_coverage_deficit_mhz": winner["root_fallback_gain_mhz"] - l1_row["root_fallback_gain_mhz"],
        }
    return rows, summary


def coverage_advantage(
    rows: list[dict[str, Any]], oracle: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    for row in rows:
        if row["track"] == "C_CONDITIONED":
            groups[(row["root_id"], row["model_key"])][row["condition"]] = row
    result = []
    for (root_id, model), values in sorted(groups.items()):
        if set(values) != {"M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY"}:
            raise ValueError("wp015_conditioned_triplet_incomplete")
        m, p, g = (values[key] for key in ("M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY"))
        result.append({
            "root_id": root_id, "family": m["family"], "split": m["split"],
            "model_key": model,
            "expert_coverage_deficit_mhz": oracle[root_id]["expert_coverage_deficit_mhz"],
            "m_gain_mhz": m["root_fallback_gain_mhz"],
            "p_gain_mhz": p["root_fallback_gain_mhz"],
            "g_gain_mhz": g["root_fallback_gain_mhz"],
            "p_minus_m_mhz": p["root_fallback_gain_mhz"] - m["root_fallback_gain_mhz"],
            "g_minus_m_mhz": g["root_fallback_gain_mhz"] - m["root_fallback_gain_mhz"],
            "best_extra_minus_m_mhz": max(p["root_fallback_gain_mhz"], g["root_fallback_gain_mhz"]) - m["root_fallback_gain_mhz"],
        })
    return result


def simple_group(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[tuple(row[field] for field in fields)].append(row)
    result = []
    for key, members in sorted(groups.items()):
        deltas = [row["functional_valid_delta_fmax_mhz"] for row in members if row["functional_valid_delta_fmax_mhz"] is not None]
        by_root: dict[str, list[float]] = collections.defaultdict(list)
        for row in members:
            by_root[row["root_id"]].append(row["root_fallback_gain_mhz"])
        root_means = [statistics.mean(value) for value in by_root.values()]
        ci_low, ci_high = bootstrap_mean_ci(root_means, seed=digest({"fields": fields, "key": key}))
        result.append({
            **dict(zip(fields, key)), "jobs": len(members),
            "roots": len({row["root_id"] for row in members}),
            "physical_valid_rate": sum(row["physical_terminal"] == "VALID_COMPLETION" for row in members) / len(members),
            "functional_valid_rate": sum(row["functionally_valid"] for row in members) / len(members),
            "strict_g_rate": sum(row["strict_g_exercised"] for row in members) / len(members),
            "beneficial_rate": sum(row["beneficial"] for row in members) / len(members),
            "mean_root_fallback_gain_mhz": statistics.mean(row["root_fallback_gain_mhz"] for row in members),
            "root_level_mean_gain_mhz": statistics.mean(root_means),
            "root_bootstrap_mean_ci95_low": ci_low,
            "root_bootstrap_mean_ci95_high": ci_high,
            "median_valid_delta_fmax_mhz": statistics.median(deltas) if deltas else None,
            "cost_usd": sum(row["cost_usd"] for row in members),
        })
    return result


def failure_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = collections.Counter((row["model_key"], row["canonical_terminal"], str(row["functional_terminal"])) for row in rows)
    return [{
        "model_key": key[0], "physical_or_planning_terminal": key[1],
        "functional_terminal": key[2], "count": value,
    } for key, value in sorted(counts.items())]


def write_csv(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row if key != "committed_candidate"})
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(row.get(key), sort_keys=True) if isinstance(row.get(key), (dict, list)) else row.get(key)
                for key in fields
            })


def evidence_tables(scientific: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    provider_turns, actions, validations, costs = [], [], [], []
    for row in scientific["jobs"]:
        exchanges = list(row.get("provider_exchange_accounting", []))
        for turn_index, turn in enumerate(row.get("turns", [])):
            provider_turns.append({
                "job_id": row["job_id"], "model_key": row["model_key"],
                "turn": turn.get("turn"), "request_contract_hash": turn.get("request_contract_hash"),
                "prompt_hash": turn.get("prompt_hash"), "context_hash": turn.get("context_hash"),
                "physical_requests": turn.get("physical_requests"), "cost_usd": turn.get("cost_usd"),
                "input_tokens": turn.get("input_tokens"), "output_tokens": turn.get("output_tokens"),
                "returned_model": turn.get("returned_model"), "returned_upstream": turn.get("returned_upstream"),
                "failure_class": turn.get("failure_class"), "error": turn.get("error"),
                "route": turn.get("route"), "planner_result": turn.get("planner_result"),
                "native_exchange_accounting": exchanges[turn_index] if turn_index < len(exchanges) else None,
            })
        actions.append({
            "job_id": row["job_id"], "selected_language": row.get("selected_language"),
            "committed_sha256": row.get("committed_sha256"),
            "committed_candidate": row.get("committed_candidate"),
            "boundary_evidence": row.get("boundary_evidence"),
            "strict_boundary_certificate": row.get("strict_boundary_certificate"),
            "actual_heavy_calls": row.get("actual_heavy_calls"),
            "actual_light_mutations": row.get("actual_light_mutations"),
        })
        validations.append({
            "job_id": row["job_id"], "physical_terminal": row.get("physical_terminal"),
            "functional_terminal": row.get("functional_terminal"),
            "functional_valid": row.get("functional_valid"),
            "functional_validation_case_sha256": row.get("functional_validation_case_sha256"),
            "functional_all_seeds_present_exactly_once": row.get("functional_all_seeds_present_exactly_once"),
            "root_sha256": row.get("root_sha256"), "child_sha256": row.get("resolved_child_sha256"),
        })
        costs.append({
            "job_id": row["job_id"], "model_key": row["model_key"],
            "provider_requests": row.get("provider_requests"), "cost_usd": row.get("cost_usd"),
            "planning_wall_seconds": row.get("planning_wall_seconds"),
            "actual_heavy_calls": row.get("actual_heavy_calls"),
            "actual_light_mutations": row.get("actual_light_mutations"),
        })
    return {
        "provider-turns": provider_turns, "actions-and-boundaries": actions,
        "functional-validation": validations, "costs": costs,
    }


def figures(rows: list[dict[str, Any]], tables: Mapping[str, list[dict[str, Any]]], output: Path) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    def save(fig: Any, name: str) -> None:
        path = output / name
        fig.tight_layout(); fig.savefig(path, dpi=180); plt.close(fig); paths.append(path)

    counts = collections.Counter(row["canonical_terminal"] for row in rows)
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(list(counts), list(counts.values()))
    ax.tick_params(axis="x", rotation=45)
    ax.set_ylabel("jobs"); ax.set_title("WP-015 terminal composition")
    save(fig, "09-failure-taxonomy.png")
    gaps = tables["selection_gap"]
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar([row["model_key"].split("/")[-1] for row in gaps], [row["selection_gap"] for row in gaps])
    ax.axhline(0, color="black", linewidth=0.8); ax.tick_params(axis="x", rotation=45)
    ax.set_ylabel("conditioned minus neutral strict-use rate"); ax.set_title("Selection Gap")
    save(fig, "05-selection-gap.png")
    valid = [row for row in rows if row["functional_valid_delta_fmax_mhz"] is not None]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter([row["cost_usd"] for row in valid], [row["functional_valid_delta_fmax_mhz"] for row in valid], alpha=0.5, s=12)
    ax.axhline(0, color="black", linewidth=0.8); ax.set_xlabel("LLM cost (USD)"); ax.set_ylabel("functional-valid delta Fmax (MHz)")
    ax.set_title("QoR–cost smoke view")
    save(fig, "08-qor-reliability-cost.png")
    funnel = tables["freedom_funnel"]
    conditioned = [row for row in funnel if row["track"] == "C_CONDITIONED"]
    labels = sorted({row["condition"] for row in conditioned})
    stages = ["COMMITTED", "BOUNDARY_RUNTIME", "STRICT_G", "PHYSICAL_VALID", "FUNCTIONAL_VALID", "BENEFICIAL"]
    fig, ax = plt.subplots(figsize=(10, 5))
    for label in labels:
        values = {row["stage"]: row["rate"] for row in conditioned if row["condition"] == label}
        ax.plot(stages, [values[stage] for stage in stages], marker="o", label=label)
    ax.tick_params(axis="x", rotation=35); ax.set_ylim(0, 1.05); ax.set_ylabel("rate"); ax.legend(); ax.set_title("Freedom Realisation Funnel")
    save(fig, "02-freedom-realisation-funnel.png")
    conditioned_rows = [row for row in rows if row["track"] == "C_CONDITIONED" and row["functional_valid_delta_fmax_mhz"] is not None]
    labels = ["M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY"]
    values = [[row["functional_valid_delta_fmax_mhz"] for row in conditioned_rows if row["condition"] == label] for label in labels]
    fig, ax = plt.subplots(figsize=(8, 5)); ax.boxplot(values, labels=labels, showfliers=False)
    ax.axhline(0, color="black", linewidth=0.8); ax.set_ylabel("functional-valid delta Fmax (MHz)"); ax.set_title("Conditioned M/P/G QoR")
    save(fig, "03-conditioned-qor.png")
    advantage = tables["coverage_advantage"]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter([row["expert_coverage_deficit_mhz"] for row in advantage], [row["best_extra_minus_m_mhz"] for row in advantage], alpha=0.55, s=14)
    ax.axhline(0, color="black", linewidth=0.8); ax.set_xlabel("expert coverage deficit (MHz)"); ax.set_ylabel("best(P,G) minus M (MHz)")
    ax.set_title("Coverage deficit versus extra-freedom advantage")
    save(fig, "04-coverage-deficit-vs-extra-freedom.png")
    models = tables["model_summary"]
    neutral_l4 = [row for row in models if row["condition"] == "L4" and row["track"] == "N_NEUTRAL"]
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot([row["model_key"].split("/")[-1] for row in neutral_l4], [row["mean_root_fallback_gain_mhz"] for row in neutral_l4], marker="o")
    ax.tick_params(axis="x", rotation=45); ax.set_ylabel("mean root-fallback gain (MHz)"); ax.set_title("Frozen model capability curve: Neutral L4")
    save(fig, "06-model-capability-curve.png")
    general = tables["generalisation"]
    splits = sorted({row["split"] for row in general}); conditions = sorted({row["condition"] for row in general})
    matrix = [[next((row["mean_root_fallback_gain_mhz"] for row in general if row["split"] == split and row["condition"] == condition), 0.0) for condition in conditions] for split in splits]
    fig, ax = plt.subplots(figsize=(9, 4)); image = ax.imshow(matrix, aspect="auto", cmap="coolwarm")
    ax.set_xticks(range(len(conditions)), conditions, rotation=45, ha="right"); ax.set_yticks(range(len(splits)), splits); ax.set_title("Generalisation heatmap"); fig.colorbar(image, ax=ax, label="mean root-fallback gain (MHz)")
    save(fig, "07-generalisation-heatmap.png")
    coverage = tables["coverage_advantage"]
    roots = sorted({row["root_id"] for row in coverage}); splits_by_root = {row["root_id"]: row["split"] for row in coverage}
    split_codes = {value: index for index, value in enumerate(sorted(set(splits_by_root.values())))}
    fig, ax = plt.subplots(figsize=(11, 3)); ax.scatter(range(len(roots)), [split_codes[splits_by_root[root]] for root in roots])
    ax.set_xticks(range(len(roots)), roots, rotation=75, ha="right", fontsize=7); ax.set_yticks(list(split_codes.values()), list(split_codes)); ax.set_title("Benchmark/root/family coverage map")
    save(fig, "01-coverage-map.png")
    return paths


def run(args: argparse.Namespace) -> int:
    scientific, bundle = read(args.scientific), read(args.bundle)
    development, references = read(args.development), read(args.references)
    if scientific.get("job_count") != 1134 or development.get("cell_count") != 140 or references.get("job_count") != 110:
        raise ValueError("wp015_analysis_required_inputs_incomplete")
    if scientific.get("bundle_sha256") != bundle.get("bundle_sha256"):
        raise ValueError("wp015_analysis_bundle_binding_mismatch")
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True)
    rows = canonical_jobs(scientific, bundle)
    reference_rows, oracle = reference_table(development, references, bundle)
    evidence = evidence_tables(scientific)
    tables = {
        "group_summary": grouped_table(rows),
        "freedom_funnel": freedom_funnel(rows),
        "selection_gap": selection_gap(rows),
        "model_summary": simple_group(rows, ("model_key", "track", "condition")),
        "generalisation": simple_group(rows, ("split", "track", "condition")),
        "failure_taxonomy": failure_table(rows),
        "coverage_advantage": coverage_advantage(rows, oracle),
        "reference_oracle": list(oracle.values()),
    }
    with (args.output / "canonical-jobs.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    write_csv(args.output / "tables" / "canonical-jobs.csv", rows)
    write_csv(args.output / "tables" / "m10-reference-cells.csv", reference_rows)
    for name, values in evidence.items():
        write_csv(args.output / "tables" / f"{name}.csv", values)
        with (args.output / f"{name}.jsonl").open("w", encoding="utf-8") as stream:
            for row in values:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    for name, values in tables.items():
        write_csv(args.output / "tables" / f"{name}.csv", values)
        write_json(args.output / "tables" / f"{name}.json", values)
    write_json(args.output / "references" / "development-m10.json", development)
    write_json(args.output / "references" / "post-agent-m10.json", references)
    figure_paths = figures(rows, tables, args.output / "figures")
    files = sorted(path for path in args.output.rglob("*") if path.is_file())
    manifest_core = {
        "schema_version": "dof.wp015-canonical-dataset-manifest.v1",
        "scientific_aggregate_sha256": scientific["aggregate_sha256"],
        "bundle_sha256": bundle["bundle_sha256"],
        "development_m10_sha256": development["freeze_sha256"],
        "post_agent_reference_sha256": references["aggregate_sha256"],
        "job_count": len(rows), "figure_count": len(figure_paths),
        "files": [{"path": str(path.relative_to(args.output)), "sha256": file_sha256(path), "size_bytes": path.stat().st_size} for path in files],
        "claim_boundary": "one repeat; descriptive interaction evidence; sampled functional validation, not formal equivalence",
    }
    manifest = manifest_core | {"manifest_sha256": digest(manifest_core)}
    write_json(args.output / "dataset-manifest.json", manifest)
    print(json.dumps({
        "jobs": len(rows), "tables": list(tables), "figures": len(figure_paths),
        "manifest_sha256": manifest["manifest_sha256"],
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--scientific", type=Path, required=True)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--development", type=Path, required=True)
    value.add_argument("--references", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
