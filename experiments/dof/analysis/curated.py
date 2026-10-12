"""Explicit manuscript-subset analysis; not the full WP015 diagnostic corpus."""
from __future__ import annotations
import argparse
import csv
import json
import math
from pathlib import Path
from xml.sax.saxutils import escape

CONDITIONS = ("M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY")


def true(value):
    if value not in {"True", "False", ""}:
        raise ValueError("Unknown tristate boolean in source data")
    return value == "True"


def select(rows, selection):
    designs, models = selection["designs"], selection["models"]
    if not designs or not models or len(designs) != len(set(designs)) or len(models) != len(set(models)):
        raise ValueError("Selection must contain explicit unique designs and models")
    repeats = selection.get("repeat_ids")
    chosen = [r for r in rows if r["root_id"] in designs and r["model_key"] in models
              and r["condition"] in CONDITIONS and (repeats is None or r["repeat_id"] in repeats)]
    keys = [(r["root_id"], r["model_key"], r["condition"]) for r in chosen]
    expected = {(d, m, c) for d in designs for m in models for c in CONDITIONS}
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError("Curated scope requires one explicit trial per design/model/condition; missing or duplicate rows")
    return sorted(chosen, key=lambda r: (designs.index(r["root_id"]), models.index(r["model_key"]), CONDITIONS.index(r["condition"])))


def derive(rows, selection):
    stages, matrix = [], []
    baselines = {}
    for r in rows:
        baseline = float(r["root_scorer_baseline_fmax_mhz"])
        if not math.isfinite(baseline) or baseline <= 0:
            raise ValueError("Valid positive baseline required")
        old = baselines.setdefault(r["root_id"], baseline)
        if not math.isclose(old, baseline, abs_tol=1e-9):
            raise ValueError("Inconsistent root baseline")
        if r["condition"] in {"M_ONLY", "P_BOUNDARY"}:
            delta = r.get("functional_scientific_delta_mhz", "")
            value = float(delta) if r["functional_status_class"] == "PASS" and delta else None
            if value is not None and not math.isfinite(value):
                raise ValueError("Nonfinite measured delta")
            matrix.append({"root_id": r["root_id"], "model_key": r["model_key"], "condition": r["condition"],
                "functional_status": r["functional_status_class"], "delta_mhz": value,
                "improvement_percent": 100 * value / baseline if value is not None else None})
    for condition, flag in (("P_BOUNDARY", "P_exclusive_realisation"), ("G_STRICT_BOUNDARY", "G_CAUSAL_RUNTIME_EXECUTED")):
        for model in selection["models"]:
            group = [r for r in rows if r["condition"] == condition and r["model_key"] == model]
            masks = []
            for r in group:
                proposed = true(r["valid_executable_submission"])
                executed = proposed and true(r[flag])
                physical = executed and true(r["physical_validity"])
                functional = physical and r["functional_status_class"] == "PASS"
                delta = r.get("functional_scientific_delta_mhz")
                improved = functional and bool(delta) and float(delta) > 0
                masks.append((proposed, executed, physical, functional, improved))
            counts = [sum(mask[i] for mask in masks) for i in range(5)]
            stages.append(dict(condition=condition, model_key=model, trials=len(group),
                proposal=counts[0], execution=counts[1], physical=counts[2], functional=counts[3], improved=counts[4]))
    return stages, matrix


def write_csv(path, rows):
    with path.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def render_stages(stages):
    panel_height = 70 + 19 * max(sum(r["condition"] == c for r in stages) for c in ("P_BOUNDARY", "G_STRICT_BOUNDARY"))
    height = 15 + 2 * panel_height
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="680" height="{height}" viewBox="0 0 680 {height}">',
             f'<rect width="680" height="{height}" fill="white"/>',
             '<g font-family="sans-serif" font-size="11" fill="#172423">']
    for panel, condition in enumerate(("P_BOUNDARY", "G_STRICT_BOUNDARY")):
        group = [r for r in stages if r["condition"] == condition]
        y0 = 25 + panel * panel_height
        parts.append(f'<text x="10" y="{y0}">{escape(condition)}: % of all selected trials per model</text>')
        for j, key in enumerate(("proposal", "execution", "physical", "functional", "improved")):
            parts.append(f'<text x="{260+j*78}" y="{y0+22}">{key}</text>')
        for i, r in enumerate(group):
            y = y0 + 30 + i * 19
            parts.append(f'<text x="5" y="{y+14}">{escape(r["model_key"])}</text>')
            for j, key in enumerate(("proposal", "execution", "physical", "functional", "improved")):
                pct = 100 * r[key] / r["trials"]
                shade = int(245 - 140 * pct / 100)
                parts.append(f'<rect x="{255+j*78}" y="{y}" width="76" height="18" fill="rgb({shade},{min(255,shade+18)},{min(255,shade+12)})"/>')
                parts.append(f'<text x="{293+j*78}" y="{y+13}" font-size="10" text-anchor="middle">{pct:.0f}% ({r[key]}/{r["trials"]})</text>')
    return "\n".join(parts + ["</g></svg>"])


def render_matrix(matrix, selection):
    models, designs = selection["models"], selection["designs"]
    width = 250 + len(models) * 70
    legend_y = 30 + 2 * (40 + len(designs) * 22)
    height = legend_y + 20 + len(models) * 16
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        f'<rect width="{width}" height="{height}" fill="white"/>', '<g font-family="sans-serif" font-size="10">']
    lookup = {(r["condition"], r["root_id"], r["model_key"]): r for r in matrix}
    for panel, condition in enumerate(("M_ONLY", "P_BOUNDARY")):
        y0 = 30 + panel * (40 + len(designs) * 22)
        parts.append(f'<text x="10" y="{y0}">{condition}: signed % improvement; NR is missing/not functionally passing</text>')
        for j, model in enumerate(models):
            parts.append(f'<text x="{240+j*70}" y="{y0+18}">M{j+1}</text>')
        for i, design in enumerate(designs):
            y = y0 + 23 + i * 22
            parts.append(f'<text x="10" y="{y+15}">{escape(design)}</text>')
            for j, model in enumerate(models):
                value = lookup[condition, design, model]["improvement_percent"]
                color = "#eeeeee" if value is None else "#d5e9de" if value > 0 else "#f4d9d2" if value < 0 else "#ffffff"
                label = "NR" if value is None else f"{value:.1f}"
                parts.append(f'<rect x="{235+j*70}" y="{y}" width="68" height="20" fill="{color}"/>')
                parts.append(f'<text x="{245+j*70}" y="{y+14}">{label}</text>')
    for j, model in enumerate(models):
        parts.append(f'<text x="10" y="{legend_y+j*16}">M{j+1}: {escape(model)}</text>')
    return "\n".join(parts + ["</g></svg>"])


def run(trials, selection_path, output):
    inputs = [trials.resolve(strict=True), selection_path.resolve(strict=True)]
    output = output.resolve()
    if output.exists() or any(output == p or output in p.parents for p in inputs):
        raise ValueError("New output must be separate from inputs")
    selection = json.loads(selection_path.read_text())
    with trials.open(newline="") as stream:
        original = list(csv.DictReader(stream))
    rows = select(original, selection)
    stages, matrix = derive(rows, selection)
    output.mkdir(parents=True)
    write_csv(output / "selected_trials.csv", rows)
    write_csv(output / "stages.csv", stages)
    write_csv(output / "matrix.csv", matrix)
    (output / "stages.svg").write_text(render_stages(stages))
    (output / "matrix.svg").write_text(render_matrix(matrix, selection))
    report = {"scope": "explicit_curated_manuscript_subset", "full_raw_reproduction": False,
        "input_rows": len(original), "selected_rows": len(rows), "designs": selection["designs"], "models": selection["models"],
        "conditions": list(CONDITIONS), "denominator_per_model_condition": len(selection["designs"]),
        "stage_totals": {c: {k: sum(r[k] for r in stages if r["condition"] == c)
            for k in ("proposal", "execution", "physical", "functional", "improved")} for c in ("P_BOUNDARY", "G_STRICT_BOUNDARY")},
        "matrix_cells": len(matrix), "numeric_cells": sum(r["improvement_percent"] is not None for r in matrix),
        "missing_cells": sum(r["improvement_percent"] is None for r in matrix),
        "missing_is_not_zero": True, "negative_improvements_retained": True}
    (output / "curated_report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.trials, args.selection, args.output), indent=2))


if __name__ == "__main__":
    main()
