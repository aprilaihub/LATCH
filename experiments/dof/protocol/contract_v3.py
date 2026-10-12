"""WP-013B semantically symmetric model contract.

The production compiler is deliberately still the accepted v2 compiler.  This
module changes only the public representation: every reachable Macro, P
operation, and G node gets the same compact semantic-card shape.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any, Mapping

from .contract_v2 import (
    ACTION_KEY,
    EMPTY_ARGS,
    G_GENERATED_ARGUMENT_SCHEMAS,
    HANDLE_OR_REF,
    M_ACTION_KEYS,
    M_CANDIDATE_SCHEMA,
    M_HEAVY_CALLS,
    PREDICATE_SCHEMA,
    P_ARGUMENT_SCHEMAS as _P_ARGUMENT_SCHEMAS_V2,
    P_BINDING_OPS,
    QUERY_NAMES,
    STRING,
    _closed_object,
    compile_candidate_v2,
)
from .language import CompileContext, CompiledProgram
from .macro_authority import M10_ACTION_KEYS, M10_HEAVY_CALLS
from .specs import canonical_json_bytes


CONTRACT_VERSION = "dof.single-submission.model-contract.v3"
M_LANGUAGE_V3 = "dof.single-submission.m.v3"
P_LANGUAGE_V3 = "dof.single-submission.p.v3"
G_LANGUAGE_V3 = "dof.single-submission.g-ast.v3"
PLANNER_MESSAGE_V3 = "dof.single-submission.planner-message.v3"
PLANNING_CONTEXT_V22 = "dof.single-submission.planning-context.v2.2"
PROMPT_VERSION_V22 = "dof.wp013b-one-submission-prompt.v2.2"
MAX_INPUT_BYTES = 65_536
MAX_TURNS = 4
MAX_QUERIES = 8
MAX_FEEDBACK = 3
QUERY_TOP_K_MAX = 16
QUERY_RESULT_MAX_BYTES = 4_096
QUERY_SESSION_MAX_BYTES = 24_576
QUERY_SESSION_MAX_ROWS = 64

COMMON_OBJECTIVE = (
    "Construct or select one intervention expected to maximise valid Fmax from the current "
    "frozen root under the published planning and execution budgets. Preserve mandatory "
    "validity. You may use any language available at your level and may stop if no "
    "intervention is justified."
)

CARD_FIELDS = (
    "key", "short_semantics", "inputs", "outputs_or_bindings", "preconditions",
    "source_stage", "result_stage", "mutation_class", "heavy_call_cost_or_range",
    "light_mutation_cost_or_range", "scoreability", "required_follow_up",
    "empty_or_no_target_behaviour", "common_failure_codes",
)

# REPLICATE_DRIVER has always existed in the production P compiler.  v3 merely
# documents its existing, fanout-gated interface; no runtime permission changes.
P_ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = deepcopy(_P_ARGUMENT_SCHEMAS_V2)
P_ARGUMENT_SCHEMAS["REPLICATE_DRIVER"] = _closed_object(
    {
        "typed_net": HANDLE_OR_REF,
        "copies": {"type": "integer", "minimum": 1, "maximum": 4},
        "max_loads_per_copy": {"type": "integer", "minimum": 2, "maximum": 128},
    },
    ["typed_net", "copies", "max_loads_per_copy"],
)


def _card(key: str, **values: Any) -> dict[str, Any]:
    row = {"key": key, **values}
    missing = set(CARD_FIELDS) - set(row)
    extra = set(row) - set(CARD_FIELDS)
    if missing or extra:
        raise RuntimeError(f"semantic_card_shape:{key}:missing={sorted(missing)}:extra={sorted(extra)}")
    return row


def _m_cards(
    action_keys: tuple[str, ...], heavy_calls: Mapping[str, int],
) -> list[dict[str, Any]]:
    semantics = {
        "macro.phys_opt": "post-place physical optimisation, route, then post-route physical optimisation",
        "macro.place_route": "unroute and unplace, then place and route with the fixed flow directives",
        "macro.auto_pblock": "apply the fixed report-derived pblock, then place and route",
        "macro.retime": "place, run the fixed retiming physical optimisation, then route",
        "macro.net_swap": "run fixed net-swap physical optimisation, route, then fixed net cleanup",
        "macro.memory_path_opt": "prepare placement, run fixed memory-path optimisation, route, then cleanup",
        "macro.hardblock_relocate": "prepare placement, run fixed hardblock relocation, route, then cleanup",
        "macro.congestion_spread": "run fixed congestion spreading physical optimisation, then route",
        "macro.fanout": "apply the fixed high-fanout replication flow, then route",
        "macro.lut_cone": "apply the fixed LUT-cone optimisation flow, then route",
    }
    light = {"macro.place_route": 2, "macro.auto_pblock": 1}
    return [
        _card(
            key,
            short_semantics=semantics[key],
            inputs={"action_key": {"const": key}},
            outputs_or_bindings="one routed scoreable implementation; no model-visible binding",
            preconditions=["action_key is admitted for this session", "frozen root is valid"],
            source_stage="ROUTED",
            result_stage="ROUTED",
            mutation_class="fixed_macro_composite",
            heavy_call_cost_or_range=[heavy_calls[key], heavy_calls[key]],
            light_mutation_cost_or_range=[light.get(key, 0), light.get(key, 0)],
            scoreability="scoreable after successful completion and mandatory validation",
            required_follow_up=[],
            empty_or_no_target_behaviour="not applicable; fixed macro owns target discovery",
            common_failure_codes=["macro_not_admitted", "static_heavy_budget_overflow"],
        )
        for key in action_keys
    ]


_P_META: dict[str, dict[str, Any]] = {
    "CALL_MACRO": dict(short="execute one admitted fixed Macro", out="no binding", pre=["referenced Macro is admitted"], src="ANY", dst="ROUTED", mut="fixed_macro_composite", heavy=[1,4], light=[0,2], score="scoreable", follow=[], empty="not applicable", fail=["macro_not_admitted"]),
    "UNROUTE": dict(short="remove routing from a routed design", out="no binding", pre=["current stage is ROUTED"], src="ROUTED", dst="PLACED", mut="state_mutation", heavy=[0,0], light=[1,1], score="not scoreable alone", follow=["ROUTE"], empty="not applicable", fail=["invalid_stage"]),
    "UNPLACE": dict(short="remove placement from a placed design", out="no binding", pre=["current stage is PLACED"], src="PLACED", dst="UNPLACED", mut="state_mutation", heavy=[0,0], light=[1,1], score="not scoreable alone", follow=["PLACE", "ROUTE"], empty="not applicable", fail=["invalid_stage"]),
    "PLACE": dict(short="place the current design using an allowed directive", out="no binding", pre=["current stage is UNPLACED or PLACED"], src="UNPLACED_OR_PLACED", dst="PLACED", mut="implementation", heavy=[1,1], light=[0,0], score="not scoreable alone", follow=["ROUTE"], empty="not applicable", fail=["directive_not_allowed", "invalid_stage"]),
    "ROUTE": dict(short="route a placed design using an allowed directive", out="no binding", pre=["current stage is PLACED"], src="PLACED", dst="ROUTED", mut="implementation", heavy=[1,1], light=[0,0], score="scoreable when no pending mutation remains", follow=[], empty="not applicable", fail=["directive_not_allowed", "invalid_stage"]),
    "PHYS_OPT": dict(short="run physical optimisation with allowed directive and options", out="no binding", pre=["current stage is PLACED or ROUTED"], src="PLACED_OR_ROUTED", dst="UNCHANGED", mut="implementation", heavy=[1,1], light=[0,0], score="scoreable only when program terminates routed", follow=["ROUTE if source stage was PLACED"], empty="not applicable", fail=["directive_not_allowed", "phys_opt_options_invalid"]),
    "SELECT_CRITICAL_CELLS": dict(short="select path-member cells by path rank", out="required binding of cell handles", pre=[], src="UNCHANGED", dst="UNCHANGED", mut="read_only_selection", heavy=[0,0], light=[0,0], score="not scoreable", follow=[], empty="empty_selection", fail=["selector_top_k_invalid", "empty_selection"]),
    "SELECT_CRITICAL_NETS": dict(short="select path-member nets by path rank", out="required binding of net handles", pre=[], src="UNCHANGED", dst="UNCHANGED", mut="read_only_selection", heavy=[0,0], light=[0,0], score="not scoreable", follow=[], empty="empty_selection", fail=["selector_top_k_invalid", "empty_selection"]),
    "SELECT_HIGH_FANOUT_NETS": dict(short="select nets by descending fanout", out="required binding of net handles", pre=[], src="UNCHANGED", dst="UNCHANGED", mut="read_only_selection", heavy=[0,0], light=[0,0], score="not scoreable", follow=[], empty="empty_selection", fail=["selector_top_k_invalid", "empty_selection"]),
    "SELECT_CRITICAL_HARDBLOCKS": dict(short="select path-member hardblocks by rank and optional resource type", out="required binding of hardblock handles", pre=[], src="UNCHANGED", dst="UNCHANGED", mut="read_only_selection", heavy=[0,0], light=[0,0], score="not scoreable", follow=[], empty="empty_selection", fail=["selector_top_k_invalid", "hardblock_type_invalid", "empty_selection"]),
    "SELECT_CONGESTED_REGION": dict(short="resolve one published region handle", out="required binding of one region handle", pre=["region handle exists in this trial"], src="UNCHANGED", dst="UNCHANGED", mut="read_only_selection", heavy=[0,0], light=[0,0], score="not scoreable", follow=[], empty="empty_selection", fail=["unknown_stale_or_wrong_type_handle", "empty_selection"]),
    "CREATE_PBLOCK": dict(short="create a pblock from one predefined region", out="required binding of one pblock handle", pre=["region is in predefined_regions"], src="UNCHANGED", dst="UNCHANGED", mut="constraint_mutation", heavy=[0,0], light=[1,1], score="not scoreable", follow=["ASSIGN", "implementation ending in ROUTE"], empty="not applicable", fail=["region_not_predefined"]),
    "ASSIGN": dict(short="assign selected cells or hardblocks to a pblock", out="no binding", pre=["target and pblock handles resolve in this trial"], src="UNCHANGED", dst="UNCHANGED", mut="constraint_mutation", heavy=[0,0], light=[1,1], score="not scoreable", follow=["implementation ending in ROUTE"], empty="empty_selection", fail=["empty_selection", "unknown_stale_or_wrong_type_handle"]),
    "REMOVE_PBLOCK": dict(short="remove one existing pblock", out="no binding", pre=["pblock handle resolves in this trial"], src="UNCHANGED", dst="UNCHANGED", mut="constraint_mutation", heavy=[0,0], light=[1,1], score="not scoreable", follow=["implementation ending in ROUTE"], empty="not applicable", fail=["unknown_stale_or_wrong_type_handle"]),
    "RELOCATE_HARDBLOCK": dict(short="relocate typed hardblock targets to one compatible region or site", out="no binding", pre=["target and destination resolve", "resource compatibility holds"], src="ANY", dst="PLACED", mut="placement_mutation", heavy=[0,0], light=[1,1], score="not scoreable", follow=["ROUTE"], empty="empty_selection", fail=["empty_selection", "incompatible_hardblock_destination"]),
    "REPLICATE_DRIVER": dict(short="replicate one selected net driver with bounded copy/load parameters", out="no binding", pre=["fanout mechanism admitted", "typed_net resolves to exactly one net"], src="ANY", dst="PLACED", mut="rapidwright_mutation", heavy=[0,0], light=[1,1], score="not scoreable", follow=["ROUTE"], empty="empty_selection", fail=["fanout_not_admitted", "fanout_parameters_invalid"]),
}


def _p_cards() -> list[dict[str, Any]]:
    cards = []
    for key, schema in P_ARGUMENT_SCHEMAS.items():
        meta = _P_META[key]
        cards.append(_card(
            key,
            short_semantics=meta["short"], inputs=schema,
            outputs_or_bindings=meta["out"], preconditions=meta["pre"],
            source_stage=meta["src"], result_stage=meta["dst"], mutation_class=meta["mut"],
            heavy_call_cost_or_range=meta["heavy"], light_mutation_cost_or_range=meta["light"],
            scoreability=meta["score"], required_follow_up=meta["follow"],
            empty_or_no_target_behaviour=meta["empty"], common_failure_codes=meta["fail"],
        ))
    return cards


def _g_cards(p_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # G inherits P byte-for-byte through the P section.  The one runtime node
    # with a G-specific argument variant is represented once below with a
    # oneOf input shape; its public key remains the exact runtime node name.
    inherited = [deepcopy(row) for row in p_cards if row["key"] != "RELOCATE_HARDBLOCK"]
    generated = [
        _card("P_PROGRAM", short_semantics="embed one complete P program", inputs=G_GENERATED_ARGUMENT_SCHEMAS["P_PROGRAM"], outputs_or_bindings="no outer binding", preconditions=["nested P program validates independently"], source_stage="ROUTED", result_stage="ROUTED", mutation_class="nested_typed_program", heavy_call_cost_or_range=[1,4], light_mutation_cost_or_range=[0,8], scoreability="scoreable", required_follow_up=[], empty_or_no_target_behaviour="inherits nested P behaviour", common_failure_codes=["p_step_count_invalid", "program_terminal_not_routed"]),
        _card("SELECT_WHERE", short_semantics="select typed objects using a bounded generated predicate", inputs=G_GENERATED_ARGUMENT_SCHEMAS["SELECT_WHERE"], outputs_or_bindings="required binding of matching handles", preconditions=["predicate depth <= 4", "predicate nodes <= 16"], source_stage="UNCHANGED", result_stage="UNCHANGED", mutation_class="generated_read_only_selection", heavy_call_cost_or_range=[0,0], light_mutation_cost_or_range=[0,0], scoreability="not scoreable", required_follow_up=[], empty_or_no_target_behaviour="empty_selection", common_failure_codes=["predicate_depth_exceeded", "predicate_node_count_exceeded", "empty_selection"]),
        _card("CREATE_REGION", short_semantics="create a generated rectangular region inside published geometry bounds", inputs=G_GENERATED_ARGUMENT_SCHEMAS["CREATE_REGION"], outputs_or_bindings="required binding of one region handle", preconditions=["integer ordered coordinates within geometry_bounds"], source_stage="UNCHANGED", result_stage="UNCHANGED", mutation_class="generated_geometry", heavy_call_cost_or_range=[0,0], light_mutation_cost_or_range=[0,0], scoreability="not scoreable", required_follow_up=["ASSIGN_REGION", "implementation ending in ROUTE"], empty_or_no_target_behaviour="not applicable", common_failure_codes=["generated_geometry_not_integer", "generated_geometry_out_of_bounds"]),
        _card("ASSIGN_REGION", short_semantics="create a pblock for a generated region and assign selected targets", inputs=G_GENERATED_ARGUMENT_SCHEMAS["ASSIGN_REGION"], outputs_or_bindings="no binding", preconditions=["target and region references resolve"], source_stage="UNCHANGED", result_stage="UNCHANGED", mutation_class="generated_constraint_mutation", heavy_call_cost_or_range=[0,0], light_mutation_cost_or_range=[2,2], scoreability="not scoreable", required_follow_up=["implementation ending in ROUTE"], empty_or_no_target_behaviour="empty_selection", common_failure_codes=["empty_selection", "unknown_stale_or_wrong_type_handle"]),
        _card("RELOCATE_HARDBLOCK", short_semantics="relocate typed hardblocks using either one direct destination or a bounded generated candidate-site index", inputs={"oneOf": [P_ARGUMENT_SCHEMAS["RELOCATE_HARDBLOCK"], G_GENERATED_ARGUMENT_SCHEMAS["RELOCATE_HARDBLOCK_GENERATED"]]}, outputs_or_bindings="no binding", preconditions=["direct destination resolves, or candidate_sites length is 1..32 and site_index is in range", "target/site compatibility holds"], source_stage="ANY", result_stage="PLACED", mutation_class="typed_or_generated_placement_mutation", heavy_call_cost_or_range=[0,0], light_mutation_cost_or_range=[1,1], scoreability="not scoreable", required_follow_up=["ROUTE"], empty_or_no_target_behaviour="empty_selection", common_failure_codes=["generated_candidate_sites_invalid", "generated_site_index_invalid", "hardblock_destination_incompatible"]),
    ]
    return inherited + generated


M_CARDS = _m_cards(M_ACTION_KEYS, M_HEAVY_CALLS)
M10_CARDS = _m_cards(M10_ACTION_KEYS, M10_HEAVY_CALLS)
P_CARDS = _p_cards()
G_CARDS = _g_cards(P_CARDS)

COMMON_RESPONSE_GRAMMAR = {
    "response_forms": {
        "query": _closed_object({"kind": {"const": "QUERY"}, "queries": {"type": "array", "minItems": 1, "maxItems": 8, "items": _closed_object({"name": {"enum": list(QUERY_NAMES)}, "top_k": {"type": "integer", "minimum": 1, "maximum": QUERY_TOP_K_MAX}}, ["name", "top_k"])}}, ["kind", "queries"]),
        "candidate": {"kinds": ["DRAFT", "VALIDATE", "COMMIT"], "shape": _closed_object({"kind": {"enum": ["DRAFT", "VALIDATE", "COMMIT"]}, "candidate": {}}, ["kind", "candidate"])},
        "terminal": {"oneOf": [_closed_object({"kind": {"const": "STOP"}}, ["kind"]), _closed_object({"kind": {"const": "NO_VALID_SUBMISSION"}}, ["kind"])]},
        "unknown_fields": "rejected at every object level",
    },
    "turn_rules": {"turns_1_to_3": ["QUERY", "DRAFT", "VALIDATE", "COMMIT", "STOP", "NO_VALID_SUBMISSION"], "turn_4": ["COMMIT", "STOP", "NO_VALID_SUBMISSION"], "max_queries_total": 8, "max_feedback_rounds": 3, "valid_commits": 1},
    "query_limits": {"top_k_max": 16, "individual_result_max_bytes": 4096, "session_result_max_bytes": 24576, "session_result_max_rows": 64, "ordering_and_truncation": "deterministic"},
}

SYNTAX_EXAMPLES = {
    "M": {"label": "syntax/semantics example; not a suggested action", "candidate": {"language": "M", "action_key": "<ADMITTED_ACTION_KEY>"}, "required_follow_up": []},
    "P_EXCLUSIVE": {"label": "syntax/semantics example; not a suggested action", "candidate": {"language": "P", "steps": [{"step_id": "s1", "op": "SELECT_CRITICAL_CELLS", "args": {"top_k": 1}, "bind": "targets"}, {"step_id": "s2", "op": "CREATE_PBLOCK", "args": {"predefined_region_id": "<PREDEFINED_REGION_HANDLE>"}, "bind": "box"}, {"step_id": "s3", "op": "ASSIGN", "args": {"target_handle": {"ref": "targets"}, "pblock_handle": {"ref": "box"}}}, {"step_id": "s4", "op": "UNROUTE", "args": {}}, {"step_id": "s5", "op": "ROUTE", "args": {"directive": "Default"}}]}, "required_follow_up": ["UNROUTE", "ROUTE"]},
    "G_SPATIAL_EXCLUSIVE": {"label": "syntax/semantics example; not a suggested action", "candidate": {"language": "G", "statements": [{"statement_id": "g1", "node": "SELECT_WHERE", "args": {"source_type": "cell", "predicate": {"op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True}, "top_k": 1}, "bind": "targets"}, {"statement_id": "g2", "node": "CREATE_REGION", "args": {"x_min": "<X_MIN>", "x_max": "<X_MAX>", "y_min": "<Y_MIN>", "y_max": "<Y_MAX>"}, "bind": "region"}, {"statement_id": "g3", "node": "ASSIGN_REGION", "args": {"target_handle": {"ref": "targets"}, "region_handle": {"ref": "region"}}}, {"statement_id": "g4", "node": "UNROUTE", "args": {}}, {"statement_id": "g5", "node": "ROUTE", "args": {"directive": "Default"}}]}, "required_follow_up": ["UNROUTE", "ROUTE"]},
    "G_HARDBLOCK_EXCLUSIVE": {"label": "syntax/semantics example; not a suggested action", "candidate": {"language": "G", "statements": [{"statement_id": "g1", "node": "SELECT_WHERE", "args": {"source_type": "hardblock", "predicate": {"op": "COMPARE", "field": "resource_type", "operator": "EQ", "value": "DSP"}, "top_k": 1}, "bind": "targets"}, {"statement_id": "g2", "node": "RELOCATE_HARDBLOCK", "args": {"typed_target": {"ref": "targets"}, "candidate_sites": ["<COMPATIBLE_SITE_HANDLE>"], "site_index": 0}}, {"statement_id": "g3", "node": "ROUTE", "args": {"directive": "Default"}}]}, "required_follow_up": ["ROUTE"]},
}


def contract_for_level(level: str, *, admitted_action_keys: tuple[str, ...] = M_ACTION_KEYS) -> dict[str, Any]:
    allowed = {"L2": ["M"], "L3": ["M", "P"], "L4": ["M", "P", "G"]}
    if level not in allowed:
        raise ValueError("contract_level_not_l2_l3_l4")
    if any(key not in M_ACTION_KEYS for key in admitted_action_keys):
        raise ValueError("unknown_admitted_action_key")
    m = {"version": M_LANGUAGE_V3, "candidate_schema": deepcopy(M_CANDIDATE_SCHEMA), "cards": [deepcopy(row) for row in M_CARDS if row["key"] in admitted_action_keys]}
    m["candidate_schema"]["properties"]["action_key"]["enum"] = list(admitted_action_keys)
    contract: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION, "level": level, "common_objective": COMMON_OBJECTIVE,
        "allowed_languages": allowed[level], "common_response_grammar": deepcopy(COMMON_RESPONSE_GRAMMAR),
        "M": m, "examples": {"M": deepcopy(SYNTAX_EXAMPLES["M"])},
        "forbidden": ["unknown fields", "system-owned binding fields", "raw Tcl", "shell/process", "network", "filesystem", "repository access", "arbitrary Python", "loops", "pre-COMMIT mutation", "pre-COMMIT QoR", "post-COMMIT resubmission"],
    }
    if level in {"L3", "L4"}:
        p_item = _closed_object({"step_id": {"type": "string", "pattern": "^s[1-8]$"}, "op": {"enum": list(P_ARGUMENT_SCHEMAS)}, "args": {"type": "object"}, "bind": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,31}$"}}, ["step_id", "op", "args"])
        contract["P"] = {"version": P_LANGUAGE_V3, "program_schema": _closed_object({"language": {"const": "P"}, "steps": {"type": "array", "minItems": 1, "maxItems": 8, "items": p_item}}, ["language", "steps"]), "argument_schema_location": "each semantic card's inputs field", "cards": deepcopy(P_CARDS), "bind_rules": {"required_for": sorted(P_BINDING_OPS), "forbidden_for": sorted(set(P_ARGUMENT_SCHEMAS) - P_BINDING_OPS), "references": deepcopy(HANDLE_OR_REF)}, "limits": {"steps": 8, "heavy_calls": 4, "light_mutations": 8}, "stage_rule": "source order is s1..sN; final program must be ROUTED with one scoreable implementation"}
        contract["examples"]["P_EXCLUSIVE"] = deepcopy(SYNTAX_EXAMPLES["P_EXCLUSIVE"])
    if level == "L4":
        g_nodes = list(P_ARGUMENT_SCHEMAS) + ["P_PROGRAM", "SELECT_WHERE", "CREATE_REGION", "ASSIGN_REGION"]
        g_item = _closed_object({"statement_id": {"type": "string", "pattern": "^g[1-8]$"}, "node": {"enum": g_nodes}, "args": {"type": "object"}, "bind": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,31}$"}}, ["statement_id", "node", "args"])
        g_delta = [deepcopy(row) for row in G_CARDS if row["key"] not in {item["key"] for item in P_CARDS} or row["key"] == "RELOCATE_HARDBLOCK"]
        contract["G"] = {"version": G_LANGUAGE_V3, "program_schema": _closed_object({"language": {"const": "G"}, "statements": {"type": "array", "minItems": 1, "maxItems": 8, "items": g_item}}, ["language", "statements"]), "inherited_P_cards_ref": "#/P/cards", "overridden_inherited_card_keys": ["RELOCATE_HARDBLOCK"], "argument_schema_location": "each inherited or generated semantic card's inputs field", "generated_cards": g_delta, "$defs": {"predicate": deepcopy(PREDICATE_SCHEMA)}, "limits": {"statements": 8, "heavy_calls": 4, "light_mutations": 8, "predicate_depth": 4, "predicate_nodes": 16}}
        contract["examples"].update({key: deepcopy(SYNTAX_EXAMPLES[key]) for key in ("G_SPATIAL_EXCLUSIVE", "G_HARDBLOCK_EXCLUSIVE")})
    return contract


def contract_hash(level: str, *, admitted_action_keys: tuple[str, ...] = M_ACTION_KEYS) -> str:
    return hashlib.sha256(canonical_json_bytes(contract_for_level(level, admitted_action_keys=admitted_action_keys))).hexdigest()


def compile_candidate_v3(candidate: Mapping[str, Any], context: CompileContext) -> CompiledProgram:
    return compile_candidate_v2(candidate, context)


def schema_ref_for_error(error_code: str) -> str:
    if error_code.startswith("query") or error_code == "unknown_query": return "#/common_response_grammar/query_limits"
    if error_code.startswith("macro"): return "#/M"
    if error_code.startswith(("p_", "program", "selector", "directive", "region", "fanout")): return "#/P"
    if error_code.startswith(("g_", "generated", "predicate")): return "#/G"
    return "#/forbidden"


__all__ = [
    "CARD_FIELDS", "COMMON_OBJECTIVE", "CONTRACT_VERSION", "G_CARDS", "G_LANGUAGE_V3",
    "MAX_FEEDBACK", "MAX_INPUT_BYTES", "MAX_QUERIES", "MAX_TURNS", "M_ACTION_KEYS",
    "M_CARDS", "M_HEAVY_CALLS", "M_LANGUAGE_V3", "PLANNER_MESSAGE_V3",
    "PLANNING_CONTEXT_V22", "PROMPT_VERSION_V22", "P_ARGUMENT_SCHEMAS", "P_CARDS",
    "P_LANGUAGE_V3", "QUERY_NAMES", "QUERY_RESULT_MAX_BYTES", "QUERY_SESSION_MAX_BYTES",
    "QUERY_SESSION_MAX_ROWS", "QUERY_TOP_K_MAX", "SYNTAX_EXAMPLES", "compile_candidate_v3",
    "contract_for_level", "contract_hash", "schema_ref_for_error",
]
