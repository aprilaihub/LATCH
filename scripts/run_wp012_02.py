#!/usr/bin/env python3
"""WP-012-02 bounded design-probe evidence generator.

The command is intentionally analysis/prototype-only.  It cannot dispatch a
primary K/U/H job and rejects sealed holdout pseudonyms.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from optimizer_pipeline.dof.action_language_probe import (
    ActionLanguageProbeError,
    M_CANDIDATES,
    PROBE_IDENTITY,
    g_ast_adversarial_cases,
    place_route_decomposition_program,
    validate_g_ast,
    validate_p_program,
)
from optimizer_pipeline.dof.contracts import DesignStage, DoFLevel
from optimizer_pipeline.dof.expansions import (
    expand_real_action,
    real_action_expansion_manifest_v2,
)
from optimizer_pipeline.dof.toolcalls import e1v2_tool_call_registry


HISTORY = REPOSITORY_ROOT / "optimizer_pipeline/knowledge/data/reference_manual_kb.json"
LEGACY = "optimizer_pipeline/backends/xilinx/legacy.py"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, sort_keys=True)
                    if isinstance(value, (dict, list, tuple))
                    else value
                    for key, value in row.items()
                }
            )


def macro_audit_rows() -> list[dict[str, Any]]:
    # Source-referenced semantic classification.  Poor outcomes are never an
    # exclusion reason; exclusions are about specificity, dynamism or replay.
    rows = [
        ("fanout", 1131, "run_test", "fanout_replication", "reports+RapidWright nets", "RapidWright rewrite; place/route", "ROUTED", "generic_with_risk", False, False, True, "candidate_with_validation", "generic but prior WP evidence includes possible unrouted pins"),
        ("pblock", 1443, "run_test_logicnets", "fixed_pblock", "fixed cell/region assumptions", "RapidWright move + Vivado implementation", "ROUTED", "benchmark_specific", True, False, True, "excluded_benchmark_specific", "LogicNets-specific region and hierarchy assumptions"),
        ("vexriscv", 1705, "run_test_vexriscv", "critical_lut_relocation", "known LUT/path", "RapidWright relocate + Vivado implementation", "ROUTED", "benchmark_specific", True, False, True, "excluded_benchmark_specific", "explicit VexRiscv repair"),
        ("phys_opt", 1909, "run_test_phys_opt", "physical_optimization", "timing state", "Vivado phys_opt_design", "PLACED_OR_ROUTED", "generic", False, False, True, "candidate_m", "4/4 historical equivalence; current frozen expansion exists"),
        ("auto_pblock", 3456, "run_test_auto_pblock", "report_driven_pblock", "timing paths+fabric", "RapidWright analysis + pblock + implementation", "ROUTED", "generic", False, False, True, "candidate_m", "report-derived but medium risk and variable outcomes"),
        ("lut_cone", 3843, "run_test_lut_cone", "lut_cone_restructure", "critical LUT cone", "RapidWright netlist rewrite + implementation", "ROUTED", "generic_with_risk", False, False, True, "candidate_with_validation", "mechanism generic; rewrite risk and sparse family evidence"),
        ("retime", 2601, "run_test_retime", "retiming", "timing/resource state", "Vivado phys_opt AddRetime", "PLACED_OR_ROUTED", "generic", False, False, True, "candidate_m", "4/4 historical equivalence; current frozen expansion exists"),
        ("net_swap", 2806, "run_test_net_swap", "equivalent_net_rewire", "critical nets", "RapidWright logical-equivalent swap + implementation", "ROUTED", "generic", False, False, True, "candidate_m", "4/4 historical equivalence"),
        ("memory_path_opt", 2974, "run_test_memory_path_opt", "memory_path_placement", "BRAM/URAM critical paths", "RapidWright relocation + implementation", "ROUTED", "generic", False, False, True, "candidate_m", "4/4 historical equivalence"),
        ("hardblock_relocate", 3127, "run_test_hardblock_relocate", "hardblock_relocation", "DSP/BRAM/URAM timing", "RapidWright relocation + implementation", "ROUTED", "generic", False, False, True, "candidate_m", "4/4 historical equivalence"),
        ("congestion_spread", 3300, "run_test_congestion_spread", "congestion_spreading", "congestion+placement", "RapidWright relocation + implementation", "ROUTED", "generic", False, False, True, "candidate_m", "4/4 historical equivalence"),
        ("memory_path_opt_rw", 3115, "run_test_memory_path_opt_rw", "memory_path_placement_rw", "same as base+detour", "RW detour prepass + base flow", "ROUTED", "generic_variant", False, False, False, "excluded_unstable_redundant", "3/4 equivalence and semantically thin wrapper"),
        ("hardblock_relocate_rw", 3288, "run_test_hardblock_relocate_rw", "hardblock_relocation_rw", "same as base+detour", "RW detour prepass + base flow", "ROUTED", "generic_variant", False, False, True, "excluded_redundant_variant", "thin wrapper; does not add a clean mechanism family"),
        ("congestion_spread_rw", 3444, "run_test_congestion_spread_rw", "congestion_spreading_rw", "same as base+detour", "RW detour prepass + base flow", "ROUTED", "generic_variant", False, False, True, "excluded_redundant_variant", "thin wrapper; high variance"),
        ("auto_top3", 4319, "run_test_auto_top3", "meta_policy", "reports+filename+family priors", "selects/runs other flows", "ROUTED", "meta_policy", True, True, False, "excluded_meta_policy", "contains filename/family rules and chooses actions rather than being one"),
        ("rescue_place_reset_retime", 2108, "run_test_rescue_place_reset_retime", "rescue_sequence", "failure recovery state", "unroute/unplace/place/retime/route", "ROUTED", "dynamic_rescue", False, False, True, "excluded_dynamic_surface", "recovery sequence, not a single generic mechanism"),
        ("synthesized_recipe", 2308, "run_test_synthesized_recipe", "dynamic_recipe", "external generated recipe", "allowlisted primitive dispatch", "VARIES", "dynamic_synthesis", False, False, False, "excluded_dynamic_surface", "container language, not a macro"),
        ("manual_action", 2485, "run_test_manual_action", "manual_primitive", "external typed action", "allowlisted Vivado/manual operation", "VARIES", "dynamic_synthesis", False, False, False, "excluded_dynamic_surface", "primitive surface belongs in P"),
    ]
    return [
        {
            "flow": flow,
            "source": f"{LEGACY}:{line}",
            "entry": entry,
            "call_chain": f"analyze_design -> {entry} -> Vivado/RapidWright -> validate/output DCP",
            "mechanism": mechanism,
            "report_query_inputs": inputs,
            "operations": operations,
            "stage_precondition": stage,
            "assumption_class": assumption,
            "filename_or_name_prior": filename_prior,
            "history_winner_prior": history_prior,
            "deterministic_replayable": replay,
            "expected_cost": "implementation-heavy (>=1)" if classification not in {"excluded_meta_policy", "excluded_dynamic_surface"} else "dynamic",
            "classification": classification,
            "evidence_uncertainty": note,
        }
        for flow, line, entry, mechanism, inputs, operations, stage, assumption, filename_prior, history_prior, replay, classification, note in rows
    ]


def _history() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    value = json.loads(HISTORY.read_text())
    return list(value["benchmark_history"]), dict(value["flow_cards"])


def _candidate_history() -> list[dict[str, Any]]:
    history, _ = _history()
    return [row for row in history if row["flow_applied"] in M_CANDIDATES]


def _rank(items: Iterable[tuple[str, float]]) -> list[str]:
    return [name for name, _ in sorted(items, key=lambda item: (-item[1], item[0]))]


def _report_rule_score(flow: str, signals: Mapping[str, Any]) -> float:
    base = {
        "phys_opt": 3.0,
        "auto_pblock": 2.5,
        "congestion_spread": 2.0,
        "memory_path_opt": 1.8,
        "hardblock_relocate": 1.6,
        "retime": 1.2,
        "net_swap": 1.0,
        "lut_cone": 0.8,
        "fanout": 0.5,
    }[flow]
    wns = signals.get("initial_wns_ns")
    failing = signals.get("failing_endpoints")
    hard = signals.get("total_hardblocks") or 0
    if isinstance(wns, (int, float)) and wns <= -5:
        base += 0.8 if flow in {"auto_pblock", "congestion_spread"} else -0.2
    if isinstance(failing, (int, float)) and failing >= 1000 and flow in {"congestion_spread", "fanout"}:
        base += 0.7
    if hard >= 100 and flow in {"memory_path_opt", "hardblock_relocate", "phys_opt"}:
        base += 0.6
    if signals.get("ff_gt_lut") and flow == "retime":
        base += 0.5
    return base


def _distance(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    fields = ("lut_count", "ff_count", "dsp_count", "bram_count", "uram_count", "initial_wns_ns")
    terms = []
    for field in fields:
        x, y = a.get(field), b.get(field)
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            scale = max(abs(float(x)), abs(float(y)), 1.0)
            terms.append(((float(x) - float(y)) / scale) ** 2)
    return math.sqrt(sum(terms)) if terms else math.inf


def heuristic_analysis() -> dict[str, Any]:
    rows = _candidate_history()
    panels: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        panels.setdefault(row["benchmark_id"], []).append(row)
    evaluations: list[dict[str, Any]] = []
    for mode in ("LOBO", "LOFO"):
        for benchmark, test in sorted(panels.items()):
            family = test[0]["benchmark_family"]
            train = [
                row
                for other, panel in panels.items()
                if (other != benchmark if mode == "LOBO" else panel[0]["benchmark_family"] != family)
                for row in panel
            ]
            oracle_order = _rank((row["flow_applied"], row["outcome"]["final_fmax_mhz"]) for row in test)
            actual = {row["flow_applied"]: row["outcome"]["final_fmax_mhz"] for row in test}
            oracle = actual[oracle_order[0]]
            by_flow: dict[str, list[float]] = {}
            for row in train:
                by_flow.setdefault(row["flow_applied"], []).append(row["outcome"]["fmax_delta_mhz"])
            global_order = _rank((flow, statistics.mean(by_flow.get(flow, [-math.inf]))) for flow in M_CANDIDATES)
            signals = test[0]["design_signals"]
            report_order = _rank((flow, _report_rule_score(flow, signals)) for flow in M_CANDIDATES)
            train_panels = {row["benchmark_id"]: row["design_signals"] for row in train}
            nearest = min(train_panels, key=lambda key: _distance(signals, train_panels[key])) if train_panels else None
            feature_order = (
                _rank((row["flow_applied"], row["outcome"]["fmax_delta_mhz"]) for row in panels[nearest])
                if nearest
                else list(M_CANDIDATES)
            )
            for policy, order in (("global", global_order), ("report_rules_no_names", report_order), ("feature_1nn", feature_order), ("oracle_reference", oracle_order)):
                chosen = order[0]
                evaluations.append(
                    {
                        "mode": mode,
                        "benchmark": benchmark,
                        "family": family,
                        "policy": policy,
                        "training_benchmarks": len({row["benchmark_id"] for row in train}),
                        "training_families": len({row["benchmark_family"] for row in train}),
                        "top1": chosen,
                        "oracle": oracle_order[0],
                        "top1_hit": chosen == oracle_order[0],
                        "top3_hit": oracle_order[0] in order[:3],
                        "regret_mhz": oracle - actual[chosen],
                        "feature_neighbor": nearest if policy == "feature_1nn" else None,
                    }
                )
    aggregate = []
    for mode in ("LOBO", "LOFO"):
        for policy in ("global", "report_rules_no_names", "feature_1nn", "oracle_reference"):
            subset = [row for row in evaluations if row["mode"] == mode and row["policy"] == policy]
            aggregate.append(
                {
                    "mode": mode,
                    "policy": policy,
                    "n": len(subset),
                    "top1_hit_rate": sum(row["top1_hit"] for row in subset) / len(subset),
                    "top3_hit_rate": sum(row["top3_hit"] for row in subset) / len(subset),
                    "mean_regret_mhz": statistics.mean(row["regret_mhz"] for row in subset),
                    "median_regret_mhz": statistics.median(row["regret_mhz"] for row in subset),
                }
            )
    return {
        "schema_version": "dof.wp012-02-l1-heuristic-feasibility.v1",
        "data": {
            "candidate_records": len(rows),
            "benchmarks": len(panels),
            "families": len({panel[0]["benchmark_family"] for panel in panels.values()}),
            "forbidden_features_used": [],
            "missing_common_signals": sorted(
                {
                    key
                    for row in rows
                    for key, value in row["design_signals"].items()
                    if value is None
                }
            ),
        },
        "evaluations": evaluations,
        "aggregate": aggregate,
        "decision": "INSUFFICIENT_TO_FREEZE_LEARNED_L1",
        "recommendation": "Use a fixed transparent report-derived/global heuristic until materially more development panels exist; do not use benchmark/family names.",
    }


def witness_evidence() -> dict[str, Any]:
    p_embed = validate_p_program({"program_id": "embed_phys", "steps": [{"op": "CALL_MACRO", "parameters": {"name": "phys_opt"}}]})
    p_decompose = validate_p_program(place_route_decomposition_program())
    p_novel = validate_p_program(
        {
            "program_id": "typed_spatial",
            "steps": [
                {"op": "SELECT_TARGET", "parameters": {"selector": "high_fanout_cells", "limit": 16}},
                {"op": "CREATE_STANDARD_PBLOCK", "parameters": {"pblock_id": "typed_region", "region": "critical_path_window"}},
            ],
        }
    )
    g_program = {
        "program_id": "generated_region_then_opt",
        "steps": [
            {"op": "SELECT", "parameters": {"object_type": "cell", "predicates": [{"property": "FANOUT", "operator": "GE", "value": 64}, {"property": "SLACK_NS", "operator": "LE", "value": 0.0}], "limit": 24}},
            {"op": "CREATE_PBLOCK", "parameters": {"pblock_id": "novel", "x0": 11, "y0": 7, "x1": 37, "y1": 83}},
            {"op": "PHYS_OPT", "parameters": {"directive": "Explore"}},
        ],
    }
    g_valid = validate_g_ast(g_program)
    rejected = []
    for case in g_ast_adversarial_cases():
        try:
            validate_g_ast(case)
            endpoint = "UNEXPECTED_ACCEPT"
        except ActionLanguageProbeError as exc:
            endpoint = str(exc)
        rejected.append({"program_id": case["program_id"], "endpoint": endpoint})
    registry = e1v2_tool_call_registry()
    macro = expand_real_action(DoFLevel.L2, "macro.place_route", {}, current_stage=DesignStage.ROUTED, registry=registry, manifest=real_action_expansion_manifest_v2(registry))
    return {
        "schema_version": "dof.wp012-02-action-language-witnesses.v1",
        "probe_identity": PROBE_IDENTITY,
        "primary_aggregate_eligible": False,
        "p_embedding": p_embed,
        "p_decomposition": p_decompose,
        "p_decomposition_reference": macro.to_dict(),
        "p_novel": p_novel,
        "g_ast_legitimate": g_valid,
        "g_ast_adversarial": rejected,
        "g_ast_replay_equal": validate_g_ast(json.loads(json.dumps(g_program))) == g_valid,
        "g_tcl": {
            "status": "NOT_RECOMMENDED",
            "reason": "No existing production safe-interpreter alias bridge to Vivado; proving filesystem/process containment and wrapper accounting would require broader infrastructure.",
        },
        "recommended_g": "G_AST",
    }


def run_static(output: Path) -> None:
    if not os.environ.get("LATCH_WP012_HOLDOUT_AUDIT"):
        raise RuntimeError("LATCH_WP012_HOLDOUT_AUDIT must identify the external frozen audit")
    holdout_path = Path(os.environ["LATCH_WP012_HOLDOUT_AUDIT"]).resolve(strict=True)
    output.mkdir(parents=True, exist_ok=False)
    audit = macro_audit_rows()
    _write_csv(output / "macro-capability-audit.csv", audit)
    _write_json(output / "macro-capability-audit.json", {"schema_version": "dof.wp012-02-macro-audit.v1", "rows": audit, "audit_sha256": _hash(audit)})
    candidates = [row for row in audit if row["classification"] in {"candidate_m", "candidate_with_validation"}]
    counts = []
    n = len(candidates)
    historical_programs = len({row["flow_applied"] for row in _candidate_history()})
    for length in (1, 2, 3):
        counts.append(
            {
                "max_sequence_length": length,
                "heavy_ceiling": length,
                "with_replacement": sum(n**item for item in range(1, length + 1)),
                "no_immediate_repeat": sum(n * (n - 1) ** (item - 1) for item in range(1, length + 1)),
                "historically_observed_distinct_programs": historical_programs,
                "historically_observed_multi_step_programs": 0,
            }
        )
    mechanism_map = [
        {"macro": row["flow"], "mechanism": row["mechanism"], "possible_p_decomposition": "typed selector/query + bounded primitive + implementation wrapper", "classification": row["classification"]}
        for row in candidates
    ]
    _write_json(output / "m-candidates-and-program-space.json", {"candidate_m": candidates, "mechanism_map": mechanism_map, "program_space": counts, "historical_candidate_records": len(_candidate_history())})
    heuristic = heuristic_analysis()
    _write_json(output / "l1-heuristic-feasibility.json", heuristic)
    _write_csv(output / "l1-heuristic-folds.csv", heuristic["evaluations"])
    _write_csv(output / "l1-heuristic-summary.csv", heuristic["aggregate"])
    _write_json(output / "action-language-witnesses.json", witness_evidence())
    holdout_audit = json.loads(holdout_path.read_text())
    repo_truth = {
        "schema_version": "dof.wp012-02-repository-truth.v1",
        "accepted_source": {"commit": "84ac0e738d13ed1a9053a1e94796aa6eadbababd", "tree": "96d64b0a1d07f5fbbf5c0a5ffeadc0f63d250b4e"},
        "development_roots": {"vexriscv_re-place": "8baf6451f9b20b8a82f035d14187c7350529a6ea0330e02d93c7100b19753149", "external_compatibility_a": "7fe49fa4a73f43cb20b57fff9c92ecfb2d6a771372b0229b7fa5199fa86c01e1"},
        "sealed_holdouts": {row["benchmark"]: row["artifact"]["sha256"] for row in holdout_audit["rows"]},
        "sealed_holdouts_accessed": False,
        "current_surfaces": {
            "dof_catalogues": "optimizer_pipeline/dof/catalogues.py:228",
            "historical_flows": f"{LEGACY}:121",
            "recipe_surface": "optimizer_pipeline/capabilities/intervention/recipe.py:81",
            "real_expansions": "optimizer_pipeline/dof/expansions.py:331",
            "real_host": "optimizer_pipeline/dof/real_mutation.py:461",
            "read_only_host": "optimizer_pipeline/dof/vivado_readonly_worker.py:308",
        },
        "facts": [
            "Historical real surface contains exactly 18 Xilinx flow names.",
            "Reference history has 60 records: four 15-flow sibling panels across three families.",
            "Only 36 records cover the nine provisional generic M candidates.",
            "Current primary M/P/G surfaces are narrower and are being audited, not modified.",
        ],
        "recommendations": [
            "Freeze final M only after resolving fanout/lut_cone validation risk and macro wrapper implementation.",
            "Prefer typed AST over raw/safe Tcl for G.",
            "Keep all probe outputs non-primary and development-root-only.",
        ],
    }
    _write_json(output / "repository-truth.json", repo_truth | {"truth_sha256": _hash(repo_truth)})
    manifest_rows = []
    for path in sorted(output.iterdir()):
        manifest_rows.append({"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size})
    _write_json(output / "manifest.json", {"schema_version": "dof.wp012-02-static-manifest.v1", "probe_identity": PROBE_IDENTITY, "files": manifest_rows})


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    static = sub.add_parser("static")
    static.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "static":
        run_static(args.output.resolve())
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
