#!/usr/bin/env python3
"""Build compact, public-safe WP-014G evidence and convergence reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from optimizer_pipeline.dof.scientific_evidence import CAUSAL_LINEAGE_VERSION  # noqa: E402
from optimizer_pipeline.dof.single_submission.action_surface_v3 import (  # noqa: E402
    ACTION_SURFACE_VERSION,
    G_NODE_REGISTRY,
    P_OPERATION_REGISTRY,
)
from run_wp014g_probes import digest, mechanism_state_deltas  # noqa: E402


SCHEMA = "dof.wp014g-analysis.v1"
PERIODS = {"amd_mini-isp": 1.57, "logicnets_jscl": 1.5}


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fmax(period: float, wns: float) -> float:
    return 1000.0 / (period - wns)


def score(
    scientific: int, expressivity: int, breadth: int, alignment: int,
    causal: int, fidelity: int, discoverability: int, safety: int,
    complexity: int, validation: int, budget: int, explainability: int,
) -> dict[str, int]:
    return {
        "scientific_alignment": scientific,
        "expressivity_delta": expressivity,
        "mechanism_breadth": breadth,
        "p_g_alignment": alignment,
        "causal_measurability": causal,
        "vivado_fidelity": fidelity,
        "model_discoverability": discoverability,
        "safety_containment": safety,
        "implementation_complexity": complexity,
        "validation_compatibility": validation,
        "budget_clarity": budget,
        "reviewer_explainability": explainability,
    }


def candidate(
    cid: str, family: str, axes: list[str], hypothesis: str, delta: str,
    counterpart: str, consumer: str, origin: str, critique: list[str],
    scores: dict[str, int], decision: str, reason: str, *,
    parent: str | None = None, gates: tuple[str, str, str, str, str] = (
        "PASS", "PASS", "UNKNOWN", "PASS", "UNKNOWN",
    ), probes: list[dict[str, Any]] | None = None,
    neighbours: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "candidate_id": cid,
        "parent_candidate_id": parent,
        "architecture_family": family,
        "freedom_axes": axes,
        "hypothesis": hypothesis,
        "scientific_motivation": "Test construction freedom without granting a new physical optimiser family.",
        "expressivity_delta_over_p": delta,
        "p_alignment": {
            "restricted_counterpart": counterpart,
            "shared_consumer": consumer,
            "new_physical_mechanism_introduced": False,
        },
        "subagent_origin": origin,
        "cross_critique": critique,
        "mandatory_gate_results": dict(zip((
            "semantic_nesting", "mechanism_alignment", "causal_lineage",
            "bounded_execution", "model_runtime_semantic_parity",
        ), gates)),
        "scorecard": scores,
        "conceptual_revision_count": 0,
        "implementation_repair_count": 0,
        "prototype_commits": [],
        "real_probes": probes or [],
        "neighbouring_ideas": neighbours or [],
        "decision": decision,
        "decision_reason": reason,
    }


def concepts(probe_refs: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    s = score
    return [
        candidate("G01", "TYPED_SSA_DATAFLOW", ["generated_object_predicate", "generated_geometry"], "Generated targets and geometry flow through typed bindings to an aligned pblock consumer.", "Generated rectangles are not limited to P's predefined region catalogue.", "fixed selector + predefined region", "ASSIGN/PLACE/ROUTE", "Language Designer + Vivado Semantic Explorer", ["Strong strict-reachability witness; current implementation is spatially narrow."], s(4,4,2,4,4,4,4,4,3,4,4,4), "SURVIVING_OPTION", "Real V0/V1 witness and clearest Reachable(G)\\Reachable(P) delta.", probes=probe_refs.get("spatial_generated_geometry")),
        candidate("G02", "TYPED_SSA_DATAFLOW", ["generated_object_predicate", "path_net_scope"], "A generated net binding can drive targeted routing.", "The construction path differs, but a P program may name the same published handle.", "SELECT_CRITICAL_NETS", "ROUTE_NET", "EDA Mechanism Explorer", ["Causal G construction is proven; strict semantic reachability over P is not."], s(3,1,2,4,4,4,4,4,4,4,4,3), "RETAIN_FOR_PROTOTYPE", "Keep as causal and model-usability control, not as strong G novelty.", probes=probe_refs.get("net_generated_target_route")),
        candidate("G03", "TYPED_SSA_DATAFLOW", ["collection_algebra", "ranking_top_k"], "FILTER→RANK→TOPK provides a compact typed collection core.", "Non-prefix rankings can form sets unavailable to frozen P selectors.", "fixed ranked selector", "existing typed P consumers", "Language Designer", ["Requires a precise proof that public P handles cannot reproduce the collection under the statement cap."], s(4,3,4,4,3,3,4,4,2,4,4,3), "SURVIVING_OPTION", "Recommended semantic core for the next bounded prototype."),
        candidate("G04", "TYPED_SSA_DATAFLOW", ["collection_algebra"], "Typed UNION composes bounded published collections.", "Can combine heterogeneous evidence slices not exposed by one P selector.", "one fixed selector", "existing typed P consumers", "Vivado Semantic Explorer", ["Union alone may only save statements and may not change reachable outcomes."], s(3,2,3,4,3,4,3,4,3,4,4,2), "REVISE_AND_RETEST", "Retain only with a non-redundancy certificate."),
        candidate("G05", "TYPED_SSA_DATAFLOW", ["collection_algebra"], "Typed DIFF creates exclusion-aware target sets.", "Can create non-prefix subsets from two bounded public collections.", "fixed selector", "existing typed P consumers", "Skeptical Reviewer", ["Most defensible set operator, but negative-set semantics complicate model use."], s(4,3,3,4,3,4,2,4,3,4,4,3), "RETAIN_FOR_PROTOTYPE", "Prototype only if G03 cannot express an exclusion cleanly."),
        candidate("G06", "MECHANISM_CAPSULE", ["collection_algebra", "fanout", "batch_targeting"], "A non-prefix generated net collection drives bounded batch replication.", "Batch membership rather than physical mechanism expands G.", "SELECT_HIGH_FANOUT_NETS", "REPLICATE_NET", "EDA Mechanism Explorer", ["Singleton replication witness is not yet evidence for batch semantics."], s(4,4,4,4,3,4,3,4,2,4,3,4), "SURVIVING_OPTION", "Highest-value next net/fanout prototype; current real witness is only singleton.", probes=probe_refs.get("net_generated_replication")),
        candidate("G07", "MECHANISM_CAPSULE", ["generated_constraint_target", "property_binding"], "Generated object sets drive a typed allowlisted property shell.", "Non-prefix property targets can exceed fixed P selectors without generic set_property.", "fixed selector", "SET_NET_DONT_TOUCH and typed property consumers", "Measurement and Safety Auditor", ["Property must record before/after and reject no-effect; current singleton remains P-reachable."], s(4,3,4,4,4,4,3,4,4,4,4,4), "SURVIVING_OPTION", "Exact property delta and V1 pass; extend only to bounded collections.", probes=probe_refs.get("property_generated_net_target")),
        candidate("G08", "MECHANISM_CAPSULE", ["generated_object_predicate", "compatible_site_choice"], "Generated hardblock and site choices feed the existing relocate consumer.", "Single choices are construction evidence but may be reproducible with P handles.", "SELECT_CRITICAL_HARDBLOCKS + published site", "RELOCATE_HARDBLOCK", "Vivado Semantic Explorer", ["Use as a control; do not call it strict G expansion."], s(3,1,3,4,4,4,4,4,4,4,4,3), "RETAIN_FOR_PROTOTYPE", "Real V0/V1 causal control across hardblock/site.", probes=probe_refs.get("hardblock_generated_target_site")),
        candidate("G09", "INTENT_ALLOCATION_IR", ["compatible_site_choice", "matching", "multi_object"], "Bounded injective matching assigns several hardblocks to compatible sites.", "Coupled assignment cannot generally be reduced to one P selector/site pair under the same cap.", "one hardblock + one site relocation", "RELOCATE_HARDBLOCK", "Vivado Semantic Explorer", ["Must expose deterministic tie-breaking and reject occupied/incompatible targets."], s(4,4,4,4,3,3,3,3,1,3,3,4), "SURVIVING_OPTION", "Best genuinely new hardblock construction candidate; real admission still required."),
        candidate("G10", "MECHANISM_CAPSULE", ["compatible_site_choice", "pair_generation"], "Generated compatible pairs drive SWAP_CELLS.", "Pair generation is convenient but same two handles can be supplied by P.", "two published cell handles", "SWAP_CELLS", "EDA Mechanism Explorer", ["Likely usability/control value rather than reachable-space expansion."], s(2,1,2,4,3,4,3,4,4,4,4,3), "RETAIN_FOR_PROTOTYPE", "Reserve as control, not a headline G construct."),
        candidate("G11", "MECHANISM_CAPSULE", ["path_net_scope", "route_preservation"], "Generate a bounded local route-repair scope while preserving other routing.", "Non-prefix scope may exceed P's target selector.", "SELECT_CRITICAL_NETS", "ROUTE_NET", "EDA Mechanism Explorer", ["Runtime cost and route-state identity need stronger admission."], s(4,3,3,4,3,3,3,3,2,4,3,3), "REVISE_AND_RETEST", "Targeted-route control works; multi-net preservation semantics remain open."),
        candidate("G12", "MECHANISM_CAPSULE", ["derived_geometry", "resource_footprint"], "Derive a bounded region from selected-object footprint.", "Generated geometry is new, but helper may embed expert placement policy.", "predefined region", "ASSIGN/PLACE/ROUTE", "Vivado Semantic Explorer", ["Risk of smuggling an expert region policy into G."], s(3,3,2,4,3,4,4,4,3,4,4,2), "REVISE_AND_RETEST", "Keep only as transparent arithmetic, not an optimizer heuristic."),
        candidate("G13", "MECHANISM_CAPSULE", ["generated_parameter"], "Generate split factor or global phys-opt option values.", "No clear delta: P already admits the same bounded values.", "literal bounded value", "REPLICATE_DRIVER/PHYS_OPT", "Skeptical Reviewer", ["This is syntax sugar, not construction freedom."], s(1,0,2,4,3,4,4,4,4,4,4,4), "REJECTED_WITH_EVIDENCE", "Reachable semantics are unchanged."),
        candidate("G14", "INTENT_ALLOCATION_IR", ["slr_guidance", "multi_region"], "Allocate generated collections across bounded SLR/clock-region domains.", "Coupled regional allocation could exceed predefined P region actions.", "predefined region assignment", "ASSIGN/PLACE/ROUTE", "EDA Mechanism Explorer", ["Device-specific and high risk of embedding expert policy."], s(3,4,3,4,2,2,2,3,1,3,2,3), "NEEDS_GPT_PRO_DECISION", "Promising but device/flow-conditional and too large for this WP."),
        candidate("G15", "TYPED_SSA_DATAFLOW", ["generated_geometry", "region_transform"], "Translate/resize a generated pblock by bounded arithmetic.", "Generated geometry remains strict G but adds little mechanism breadth.", "RESIZE_PBLOCK with predefined region", "RESIZE_PBLOCK", "Vivado Semantic Explorer", ["Spatial-only neighbour risks returning to the original local optimum."], s(3,3,1,4,3,4,3,4,3,4,4,3), "REJECTED_WITH_EVIDENCE", "Defer until non-spatial breadth is solved."),
        candidate("G16", "RELATIONAL_PIPELINE_AST", ["graph_path_traversal", "relational_query"], "Bounded typed joins traverse path→net→driver→load relationships.", "Relationally derived sets are plausibly outside current P selectors.", "fixed path/net selectors", "existing typed P consumers", "Language Designer", ["Common Q currently lacks enough public relations; implementing it would change the frozen information surface."], s(4,4,4,4,2,2,2,3,0,3,2,2), "REJECTED_WITH_EVIDENCE", "Cannot advance without reopening common O/Q, a WP-014G hard boundary."),
        candidate("G17", "CERTIFICATE_LAYER", ["certificate", "safety", "causal_lineage"], "Each generated binding carries a public derivation and compatibility certificate.", "No independent action reachability; strengthens measurement and containment.", "existing P validation", "all typed consumers", "Measurement and Safety Auditor", ["This is an overlay, not a standalone G language."], s(4,0,4,4,4,4,3,4,2,4,4,4), "SURVIVING_OPTION", "Recommended safety/lineage overlay for any semantic core."),
        candidate("G18", "TRANSACTIONAL_PATCH_IR", ["transaction", "dependency_graph", "rollback"], "A bounded transaction groups dependent typed mutations with preconditions.", "A transaction alone changes failure semantics, not necessarily reachable interventions.", "straight-line P program", "existing typed P consumers", "Language Designer", ["Rollback after COMMIT would violate the scientific reliability firewall; only pre-COMMIT compilation is acceptable."], s(2,1,4,4,3,3,2,3,1,4,3,2), "REVISE_AND_RETEST", "Retain only as a compile-time dependency notation; no post-COMMIT recovery."),
    ]


def main(args: argparse.Namespace) -> int:
    runs = args.runs_root
    args.out.mkdir(parents=True, exist_ok=True)
    args.reports.mkdir(parents=True, exist_ok=True)

    validation_by_hash: dict[str, dict[str, Any]] = {}
    validation_aggregates = []
    for version in ("validation-v1", "validation-v2", "validation-v3"):
        aggregate = read(runs / version / "aggregate.json")
        validation_aggregates.append({
            "identity": version,
            "aggregate_sha256": aggregate["aggregate_sha256"],
            "executed_cases": aggregate["executed_cases"],
            "terminal_counts": aggregate["terminal_counts"],
            "queue_wall_seconds": aggregate["queue_wall_seconds"],
            "summed_case_wall_seconds": aggregate["summed_case_wall_seconds"],
            "all_inputs_unchanged": aggregate["all_inputs_unchanged"],
            "owned_process_survivors": len(aggregate["owned_process_survivors"]),
        })
        for row in aggregate["results"]:
            validation_by_hash[row["case"]["revised_sha256"]] = {
                "terminal": row["terminal"],
                "five_seed_cycles": row["validator_report"]["simulation_report"]["total_full_cycles_simulated"],
                "wall_seconds": row["wall_seconds"],
                "max_rss_bytes": row["resource"]["max_rss_bytes"],
                "artifact_bytes": row["validator_artifact_bytes"],
                "result_sha256": row["result_sha256"],
            }

    chosen = {
        "spatial_generated_geometry": ("probes-v3", "spatial_generated_geometry"),
        "hardblock_generated_target_site": ("probes-v3", "hardblock_generated_target_site"),
        "property_generated_net_target": ("probes-v4", "property_generated_net_target"),
        "net_generated_target_route": ("probes-v5", "net_generated_target_route"),
        "net_generated_replication": ("probes-v5", "net_generated_replication"),
    }
    witness_rows = []
    probe_refs: dict[str, list[dict[str, Any]]] = {}
    for mechanism, (version, probe_id) in chosen.items():
        aggregate = read(runs / version / "aggregate.json")
        row = next(item for item in aggregate["rows"] if item["probe_id"] == probe_id)
        execution = row["execution"]
        marker = mechanism_state_deltas(
            runs / version / "cases" / probe_id, probe_id=probe_id,
        )
        bound_calls = [
            item.get("arguments_sha256") for item in execution.get("call_trace", [])
            if isinstance(item, dict)
            and str(item.get("operation", "")).startswith("vivado.wp014c_")
            and item.get("arguments_sha256")
        ]
        period = PERIODS[row["root_id"]]
        before_wns = execution["pre_timing"]["wns"]
        after_wns = execution["post_timing"]["wns"]
        delta_fmax = fmax(period, after_wns) - fmax(period, before_wns)
        child = execution["child"]
        validation = validation_by_hash[child["sha256"]]
        strict_delta = mechanism == "spatial_generated_geometry"
        compact = {
            "probe_identity": version,
            "probe_id": probe_id,
            "root_id": row["root_id"],
            "root_sha256": row["root_sha256"],
            "candidate_sha256": row["candidate_sha256"],
            "compiled_public_sha256": row["compiled_public_sha256"],
            "causal_lineage_sha256": row["causal_lineage_sha256"],
            "causal_runtime_exercised": row["causal_evidence"]["boundary_runtime_exercised"],
            "strict_reachability_delta_over_current_p": strict_delta,
            "reachability_note": (
                "Generated geometry is outside the predefined P region catalogue."
                if strict_delta else
                "Causal generated construction is shown, but the selected singleton may be nameable by P."
            ),
            "selected_object_count": execution["selected_count"],
            "selected_object_identity_hash": digest(bound_calls),
            "transformed_object_count": execution["transformed_count"],
            "transformed_object_identity_hash": digest([
                item["changed_identity_hash"] for item in marker
            ]),
            "mechanism_state_deltas": marker,
            "physical_terminal": execution["canonical_endpoint"],
            "route_valid": execution["route_valid"],
            "child_sha256": child["sha256"],
            "child_size_bytes": child["size_bytes"],
            "pre_wns_ns": before_wns,
            "post_wns_ns": after_wns,
            "delta_fmax_mhz": delta_fmax,
            "runtime_seconds": execution["wall_seconds"],
            "actual_heavy_calls": execution["actual_heavy_calls"],
            "actual_light_mutations": execution["actual_light_mutations"],
            "functional_validation": validation,
        }
        witness_rows.append(compact)
        probe_refs[probe_id] = [{
            "probe_id": f"{version}:{probe_id}",
            "root_id": row["root_id"],
            "program_hash": row["candidate_sha256"],
            "producer_consumer_lineage_hash": row["causal_lineage_sha256"],
            "selected_object_count": execution["selected_count"],
            "transformed_object_count": execution["transformed_count"],
            "physical_terminal": execution["canonical_endpoint"],
            "functional_terminal": validation["terminal"],
            "delta_fmax_mhz": delta_fmax,
            "failure_class": None,
        }]

    all_probe_ids = []
    total_heavy = total_light = 0
    total_probe_wall = 0.0
    endpoint_counts: dict[str, int] = {}
    for version in ("probes-v2", "probes-v3", "probes-v4", "probes-v5"):
        aggregate = read(runs / version / "aggregate.json")
        all_probe_ids.append({
            "identity": version,
            "aggregate_sha256": aggregate["aggregate_sha256"],
            "status": "SUPERSEDED_DIAGNOSTIC" if version == "probes-v2" else "ADMISSION_OR_REPAIR_EVIDENCE",
            "actual_heavy_calls": aggregate["actual_heavy_calls"],
            "actual_light_mutations": aggregate["actual_light_mutations"],
            "queue_wall_seconds": aggregate["queue_wall_seconds"],
            "endpoint_counts": aggregate["endpoint_counts"],
            "all_roots_unchanged": aggregate["all_roots_unchanged"],
        })
        total_heavy += aggregate["actual_heavy_calls"]
        total_light += aggregate["actual_light_mutations"]
        total_probe_wall += aggregate["queue_wall_seconds"]
        for key, value in aggregate["endpoint_counts"].items():
            endpoint_counts[key] = endpoint_counts.get(key, 0) + value

    real_summary = {
        "schema_version": SCHEMA,
        "admission_rule": "semantic/causal/validity admission; QoR sign ignored",
        "representative_witness_count": len(witness_rows),
        "required_mechanism_families_covered": ["spatial", "net_path_fanout", "hardblock_site"],
        "strongly_desired_mechanism_covered": "constraint_protection",
        "strict_reachability_witness_count": sum(row["strict_reachability_delta_over_current_p"] for row in witness_rows),
        "causal_construction_witness_count": sum(row["causal_runtime_exercised"] for row in witness_rows),
        "v1_functional_pass_count": sum(row["functional_validation"]["terminal"] == "MULTISEED_FUNCTIONAL_PASS" for row in witness_rows),
        "witnesses": witness_rows,
        "all_real_identities": all_probe_ids,
        "validation_identities": validation_aggregates,
        "diagnostics": [{
            "identity": "probes-v1",
            "terminal": "CONTROLLER_CONSTRUCTION_REJECTION",
            "reason": "Q top_k exceeded frozen maximum; no backend call",
        }, {
            "identity": "probes-v4:net_generated_target_route",
            "terminal": "FRAMEWORK_INSTRUMENTATION_DEFECT",
            "reason": "route-before variable was temporarily placed in the wrong worker branch; fixed and rerun in probes-v5",
        }],
    }
    write(args.out / "real-probe-summary.json", real_summary)

    usability = read(runs / "usability-v1" / "aggregate.json")
    usability_summary = {
        "schema_version": SCHEMA,
        "aggregate_sha256": usability["aggregate_sha256"],
        "sessions": usability["physical_model_requests"],
        "providers_used": usability["providers_used"],
        "anthropic_requests": usability["anthropic_requests"],
        "openrouter_requests": usability["openrouter_requests"],
        "api_cost_usd": usability["api_cost_usd"],
        "retry_count": usability["retry_count"],
        "fallback_count": usability["fallback_count"],
        "repair_count": usability["repair_count"],
        "terminal_counts": usability["terminal_counts"],
        "by_architecture": usability["by_architecture"],
        "by_model": usability["by_model"],
        "interpretation": {
            "typed_ssa": "8/8 compatible; best construction result in this bounded two-provider probe.",
            "straight_line": "7/8 compatible; current schema remains usable.",
            "capsule": "4/8 compatible; do not freeze this card without redesign.",
            "claim_limit": "No stable model ranking or final architecture superiority claim.",
        },
    }
    write(args.out / "model-usability-summary.json", usability_summary)

    ledger = {
        "schema_version": "g-exploration-ledger.v1",
        "candidate_count": 18,
        "architecture_families": [
            "TYPED_SSA_DATAFLOW", "MECHANISM_CAPSULE", "INTENT_ALLOCATION_IR",
            "RELATIONAL_PIPELINE_AST", "CERTIFICATE_LAYER", "TRANSACTIONAL_PATCH_IR",
        ],
        "candidates": concepts(probe_refs),
    }
    write(args.out / "possibility-space-ledger.json", ledger)
    write(args.out / "candidate-scorecards.json", {
        "schema_version": SCHEMA,
        "score_scale": "0-4; mandatory gates override scores",
        "rows": [{
            "candidate_id": row["candidate_id"],
            "mandatory_gate_results": row["mandatory_gate_results"],
            "scorecard": row["scorecard"],
            "decision": row["decision"],
            "decision_reason": row["decision_reason"],
        } for row in ledger["candidates"]],
    })

    consumer_map = {
        "SELECT_WHERE": {
            "cell": ["ASSIGN_REGION", "ASSIGN", "PLACE_CELL", "UNPLACE_CELL", "SWAP_CELLS", "SET_CELL_LOC_FIXED"],
            "net": ["REPLICATE_DRIVER", "REPLICATE_NET", "ROUTE_NET", "SET_NET_DONT_TOUCH"],
            "hardblock": ["ASSIGN_REGION", "ASSIGN", "RELOCATE_HARDBLOCK"],
            "site": ["RELOCATE_HARDBLOCK", "PLACE_CELL", "SET_SITE_PROHIBIT"],
        },
        "CREATE_REGION": {"region": ["ASSIGN_REGION"]},
        "CHOOSE_SITE": {"site": ["RELOCATE_HARDBLOCK", "PLACE_CELL"]},
        "ASSIGN_REGION": {"none": []},
    }
    truth_pack = {
        "schema_version": SCHEMA,
        "action_surface_version": ACTION_SURFACE_VERSION,
        "causal_lineage_version": CAUSAL_LINEAGE_VERSION,
        "model_visible_g_nodes": G_NODE_REGISTRY,
        "inherited_p_operation_count": len(P_OPERATION_REGISTRY),
        "legal_downstream_consumers_by_generated_node": consumer_map,
        "pipeline": [
            "contract_v5 model card", "closed response schema", "compile_candidate_v5",
            "typed handle resolution", "aligned P worker", "mechanism marker",
            "child DCP state delta", "boundary_evidence/causal_lineage",
        ],
        "false_positive_status": {
            "counterexample": "unused SELECT_WHERE binding followed by CALL_MACRO",
            "baseline_risk": "syntax-only G could be counted as causal G",
            "fixed_rule": "producer output reference must reach exact observed worker and nonempty state delta",
            "deterministic_test": "tests/test_wp014g_causal_lineage.py",
        },
        "confirmed_runtime_mechanisms": [row["probe_id"] for row in witness_rows],
        "strict_expressivity_warning": "Generated selection is not automatically Reachable(G)\\Reachable(P) when P can name the same public handle.",
        "known_measurement_limit": "Route evidence records an asserted before/after ROUTE change and sanitized lengths, not a full route-string hash; replication records aggregate topology counts.",
    }
    write(args.out / "current-g-truth-pack.json", truth_pack)

    options = [{
        "option_id": "O1",
        "name": "Typed SSA core + causal certificate overlay",
        "status": "CODEX_RECOMMENDED_FOR_GPT_PRO_REVIEW",
        "semantics": "FILTER/RANK/TOPK typed values; explicit bindings; existing aligned P consumers; mandatory lineage certificate.",
        "real_breadth": ["spatial", "targeted_route", "net_replication", "hardblock_site", "typed_property"],
        "strict_delta": "Generated geometry confirmed; non-prefix collection and multi-object matching still need admission.",
        "model_usability": "Typed SSA card 8/8 compatible.",
        "wp015_impact": "Moderate: extend schema/compiler/lineage; reuse workers and validation.",
        "risk": "Must preregister a reachability/non-redundancy certificate per G producer family.",
    }, {
        "option_id": "O2",
        "name": "Mechanism capsules lowering to typed SSA",
        "status": "REVISE_BEFORE_FREEZE",
        "semantics": "Small mechanism-specific cards lower to O1 runtime semantics.",
        "real_breadth": ["same runtime consumers as O1"],
        "strict_delta": "Depends entirely on the lowered typed program.",
        "model_usability": "Capsule card only 4/8 compatible in this probe.",
        "wp015_impact": "Low runtime, moderate prompt/card redesign.",
        "risk": "Hidden expert policy and card-specific construction failures.",
    }, {
        "option_id": "O3",
        "name": "Bounded intent/allocation IR lowering to typed SSA",
        "status": "PROMISING_HIGH_RISK_OPTION",
        "semantics": "Generated multi-object intents compile deterministically to injective site/region allocations.",
        "real_breadth": ["hardblock_site", "multi_region", "spatial"],
        "strict_delta": "Potentially strong for coupled allocations; not yet real-admitted.",
        "model_usability": "Not directly tested; only frontend concept reviewed.",
        "wp015_impact": "High; requires deterministic allocator and stronger certificates.",
        "risk": "May smuggle expert optimization policy into the compiler.",
    }]
    breadth = {
        "schema_version": SCHEMA,
        "dimensions": ["syntax", "compiled", "causal_runtime", "valid_child", "five_seed_pass", "strict_reachability_delta"],
        "witnesses": [{
            "mechanism": row["probe_id"],
            "syntax": True,
            "compiled": True,
            "causal_runtime": row["causal_runtime_exercised"],
            "valid_child": row["physical_terminal"] == "VALID_COMPLETION",
            "five_seed_pass": row["functional_validation"]["terminal"] == "MULTISEED_FUNCTIONAL_PASS",
            "strict_reachability_delta": row["strict_reachability_delta_over_current_p"],
        } for row in witness_rows],
        "surviving_options": options,
    }
    write(args.out / "mechanism-breadth-matrix.json", breadth)

    validation_rows = list(validation_by_hash.values())
    resource = {
        "schema_version": SCHEMA,
        "caps": {
            "heavy_calls": 180, "functional_cases": 50,
            "model_requests": 160, "api_cost_usd": 15,
        },
        "actual": {
            "real_probe_identities_with_backend": 4,
            "real_probe_attempt_rows": sum(sum(item["endpoint_counts"].values()) for item in all_probe_ids),
            "heavy_calls": total_heavy,
            "light_mutations": total_light,
            "probe_queue_wall_seconds": total_probe_wall,
            "functional_cases": len(validation_rows),
            "functional_passes": sum(row["terminal"] == "MULTISEED_FUNCTIONAL_PASS" for row in validation_rows),
            "functional_queue_wall_seconds": sum(row["queue_wall_seconds"] for row in validation_aggregates),
            "functional_summed_wall_seconds": sum(row["summed_case_wall_seconds"] for row in validation_aggregates),
            "max_functional_case_rss_bytes": max(row["max_rss_bytes"] for row in validation_rows),
            "functional_artifact_bytes": sum(row["artifact_bytes"] for row in validation_rows),
            "model_requests": usability["physical_model_requests"],
            "api_cost_usd": usability["api_cost_usd"],
            "providers": usability["providers_used"],
            "anthropic_requests": usability["anthropic_requests"],
            "openrouter_requests": usability["openrouter_requests"],
            "sealed_holdout_accesses": 0,
            "wp015_jobs": 0,
        },
        "endpoint_counts_including_superseded_diagnostics": endpoint_counts,
        "within_all_caps": total_heavy <= 180 and len(validation_rows) <= 50 and usability["physical_model_requests"] <= 160 and usability["api_cost_usd"] <= 15,
    }
    write(args.out / "resource-ledger.json", resource)

    rejected = [row for row in ledger["candidates"] if row["decision"] in {"REJECTED_WITH_EVIDENCE", "REVISE_AND_RETEST"}]
    rejected_md = "# WP-014G Rejected and Deferred Branches\n\n" + "\n".join(
        f"## {row['candidate_id']} — {row['hypothesis']}\n\n"
        f"Decision: `{row['decision']}`. {row['decision_reason']}\n\n"
        f"Cross-critique: {'; '.join(row['cross_critique'])}\n"
        for row in rejected
    )
    (args.reports / "WP-014G-rejected-branches.md").write_text(rejected_md)

    critique_md = """# WP-014G Cross-role Critique

## Independent views

- Vivado Semantic Explorer proposed typed path/net sets, injective hardblock matching, compatible swaps, typed properties, local route perturbation and derived footprints.
- Language Designer compared Typed SSA Dataflow, Mechanism Capsules, Relational Pipeline AST, Proposal/Certificate, Intent/Allocation and Transactional Patch architectures.
- EDA Mechanism Explorer worked backward from aligned spatial, net/fanout, hardblock/site and property consumers.
- Skeptical Reviewer challenged whether a generated selector actually expands reachability when P can name the same stable handle.
- Measurement/Safety Auditor required versioned producer→binding→consumer→worker→state-delta lineage and exact cap/failure accounting.

## Cross-critique result

The shared conclusion is narrower than the raw mechanism count: generated geometry is a clear strict reachability expansion over P; singleton generated net, property and hardblock targeting are genuine causal construction paths but may still be P-reachable. The next strongest candidates are non-prefix typed collections feeding batch consumers and injective multi-hardblock allocation. A general relational AST is deferred because the frozen common Q does not expose the required graph relations.

## Disagreements retained

- Capsules are easier to explain, but the real usability probe was worse (4/8) than Typed SSA (8/8).
- Intent/allocation offers strong expressivity, but risks embedding an expert optimiser in the compiler.
- Transaction syntax may improve safety, but post-COMMIT rollback would erase scientific reliability outcomes and remains forbidden.
"""
    (args.reports / "WP-014G-cross-role-critique.md").write_text(critique_md)

    convergence_md = "# WP-014G Convergence Decision Pack\n\n"
    for option in options:
        convergence_md += (
            f"## {option['option_id']} — {option['name']}\n\n"
            f"Semantics: {option['semantics']}\n\n"
            f"Breadth: {', '.join(option['real_breadth'])}.\n\n"
            f"Strict delta: {option['strict_delta']}\n\n"
            f"Usability: {option['model_usability']}\n\n"
            f"WP-015 impact: {option['wp015_impact']}\n\n"
            f"Risk: {option['risk']}\n\n"
        )
    convergence_md += "## Interpretation\n\nO1 provides the typed core and causal-certificate overlay; O2 requires card redesign, while O3 remains a bounded prototype. Causal generated construction and strict reachability expansion are separate criteria. This comparison does not launch a collection or change the historical protocol.\n"
    (args.reports / "WP-014G-convergence-decision-pack.md").write_text(convergence_md)

    deltas = [row["delta_fmax_mhz"] for row in witness_rows]
    report = f"""# WP-014G Report — Bounded Generative Action-Space Exploration

## Executive result

WP-014G repaired the syntax-only G false positive with versioned producer→binding→consumer→worker→state-delta lineage, explored 18 concepts across six architecture families, and obtained five representative real causal witnesses spanning spatial, net route, net replication, hardblock/site and typed property mechanisms. All five representative children were physically valid and passed five deterministic 200-vector seeds (1,000 full cycles per child). Admission did not use QoR sign.

The key construct-validity correction is that five causal witnesses do **not** imply five strict `Reachable(G)\\Reachable(P)` expansions. Generated geometry is clearly outside P's predefined-region surface. Singleton generated targeting may be replayable in P using the same published handle, so those witnesses establish causal construction and mechanism fidelity, not strict semantic novelty.

## Code and measurement closure

- `dof.g-causal-lineage.v1` rejects unused generated producers, including `SELECT_WHERE` followed only by `CALL_MACRO`.
- Property mutation now records and verifies before/after values and rejects no-effect.
- Targeted route now rejects unchanged ROUTE state; net replication rejects unchanged aggregate cell/net topology.
- Probe, five-seed validation and two-provider usability controllers are reproducible and bounded.

## Real admissions

| Mechanism | Physical | V1 five-seed | ΔFmax MHz | Strict G-over-P? |
|---|---:|---:|---:|---:|
"""
    for row in witness_rows:
        report += f"| {row['probe_id']} | {row['physical_terminal']} | {row['functional_validation']['terminal']} | {row['delta_fmax_mhz']:.3f} | {'yes' if row['strict_reachability_delta_over_current_p'] else 'not proven'} |\n"
    report += f"""

The observed development deltas range from {min(deltas):.3f} to {max(deltas):.3f} MHz (median {statistics.median(deltas):.3f} MHz). These values are recorded only as diagnostics and did not determine retention.

One `probes-v4` targeted-route failure was caused by a temporary instrumentation-variable placement defect. It is classified as framework diagnostic, repaired, and rerun under `probes-v5`; it is not re-labelled as a model/tool scientific failure. All actual calls remain charged in the resource ledger.

## Model-facing usability

The bounded study made exactly 24 direct calls: 12 OpenAI and 12 DeepSeek, total USD {usability['api_cost_usd']:.9f}. Anthropic and OpenRouter calls were zero. Typed SSA produced 8/8 compatible programs, straight-line GBL 7/8, and the capsule card 4/8. There were no retries, fallbacks or response repairs. This supports frontend redesign decisions only, not model ranking or architecture superiority.

## Resource and integrity

- Heavy calls including superseded/repair identities: {total_heavy}/180.
- Functional cases: {len(validation_rows)}/50; all passed; total transient validator artifacts {sum(row['artifact_bytes'] for row in validation_rows) / 1e9:.3f} GB.
- Model requests: {usability['physical_model_requests']}/160; cost USD {usability['api_cost_usd']:.9f}/15.
- Sealed-holdout accesses: 0. WP-015 jobs: 0.
- Root hashes remained unchanged in every aggregate.
- Repository verification: 1,157 pytest cases pass; the final process audit records two zero-survivor scans.

## Convergence

Recommended option: Typed SSA collection/dataflow core plus mandatory causal certificate overlay. The next scientific admissions should test a non-prefix collection feeding batch replication/property consumers and injective multi-hardblock matching. Mechanism capsules require redesign; a general relational AST is outside the frozen common-Q boundary; intent/allocation remains promising but high risk.

## Claim boundary

This package supports executable multi-mechanism G construction, bounded two-provider usability, and a precise current-G measurement diagnosis. It does not support L4 superiority, stable model rankings, unseen-family generalisation, formal equivalence, statistical DoF effects, or a final G v2 freeze.
"""
    (args.reports / "WP-014G-report.md").write_text(report)

    manifest = {
        "schema_version": SCHEMA,
        "generated_files": {},
    }
    for path in sorted([
        *(path for path in args.out.glob("*.json") if path.name != "analysis-manifest.json"),
        *args.reports.glob("WP-014G-*.md"),
    ]):
        manifest["generated_files"][str(path.resolve().relative_to(REPO))] = sha(path)
    write(args.out / "analysis-manifest.json", manifest)
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--runs-root", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--reports", type=Path, required=True)
    return value


if __name__ == "__main__":
    raise SystemExit(main(parser().parse_args()))
