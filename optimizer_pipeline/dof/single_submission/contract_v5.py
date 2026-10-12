"""Registry-driven public contract and strict compiler for Action Surface v3."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
from typing import Any, Mapping

from .action_surface_v3 import (
    ACTION_SURFACE_VERSION,
    G_EXCLUSIVE_NODES,
    G_NODE_REGISTRY,
    HANDLE_OR_REF,
    M_ACTION_KEYS,
    M_HEAVY_CALLS,
    PREDICATE_SCHEMA,
    P_BINDING_OPS,
    P_EXCLUSIVE_OPERATIONS,
    P_OPERATION_REGISTRY,
    closed_object,
    registry_sha256,
)
from .contract_v2 import M_CANDIDATE_SCHEMA, QUERY_NAMES, _finish_v2, compile_candidate_v2
from .contract_v3 import (
    CARD_FIELDS,
    COMMON_OBJECTIVE,
    COMMON_RESPONSE_GRAMMAR,
    M10_CARDS,
    MAX_FEEDBACK,
    MAX_INPUT_BYTES,
    MAX_QUERIES,
    MAX_TURNS,
    PLANNING_CONTEXT_V22,
    QUERY_RESULT_MAX_BYTES,
    QUERY_SESSION_MAX_BYTES as QUERY_SESSION_MAX_BYTES_V3,
    QUERY_SESSION_MAX_ROWS,
    QUERY_TOP_K_MAX,
)
from .language import CompileContext, CompiledProgram, ConstructionError, compile_macro
from .specs import canonical_json_bytes


CONTRACT_VERSION = "dof.single-submission.model-contract.v7"
M_LANGUAGE_V5 = "dof.single-submission.m.v5"
P_LANGUAGE_V5 = "dof.single-submission.p.v5"
G_LANGUAGE_V5 = "dof.single-submission.g-ssa.v7"
PLANNER_MESSAGE_V5 = "dof.single-submission.planner-message.v5"
PROMPT_VERSION_V3 = "dof.wp014c-one-submission-prompt.v3"
QUERY_SESSION_MAX_BYTES = 16_384


def _card_from_row(key: str, row: Mapping[str, Any], *, input_schema_ref: str) -> dict[str, Any]:
    card = {
        "key": key,
        "short_semantics": row["short_semantics"],
        # The exact closed argument schema already appears once in the
        # program-schema oneOf.  Reference it from the semantic card instead
        # of repeating tens of kilobytes in every planning turn.
        "inputs": {"$ref": input_schema_ref},
        "outputs_or_bindings": row["outputs_or_bindings"],
        "preconditions": [f"source stage: {row['source_stage']}"] + [f"target type: {kind}" for kind in row["target_types"]],
        "source_stage": row["source_stage"],
        "result_stage": row["result_stage"],
        "mutation_class": row["mutation_class"],
        "heavy_call_cost_or_range": [row["heavy_call_cost"], row["heavy_call_cost"]],
        "light_mutation_cost_or_range": [row["light_mutation_cost"], row["light_mutation_cost"]],
        "scoreability": row["scoreability"],
        "required_follow_up": list(row["required_follow_up"]),
        "empty_or_no_target_behaviour": row["no_target_endpoint"],
        "common_failure_codes": list(row["common_failure_codes"]),
    }
    if set(card) != set(CARD_FIELDS):
        raise RuntimeError(f"semantic_card_shape:{key}")
    return card


P_CARDS = [
    _card_from_row(
        key, row,
        input_schema_ref=f"#/P/program_schema/properties/steps/items/oneOf/{index}/properties/args",
    )
    for index, (key, row) in enumerate(P_OPERATION_REGISTRY.items())
]
# G inherits the exact P section by reference; only genuinely generated nodes
# are repeated here.  This preserves byte identity and the common input bound.
G_CARDS = [
    _card_from_row(
        key, row,
        input_schema_ref=f"#/G/program_schema/properties/steps/items/oneOf/{index + 1}/properties/args",
    )
    for index, (key, row) in enumerate(G_NODE_REGISTRY.items())
]


def compact_cards(cards: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Losslessly encode cards once with a shared ordered field dictionary."""

    return {
        "encoding": "ordered_rows.v1",
        "field_order": list(CARD_FIELDS),
        "rows": [[deepcopy(card[field]) for field in CARD_FIELDS] for card in cards],
    }


def _statement_schema(
    *, key: str, argument_schema: Mapping[str, Any], identifier: str, binding: bool
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "step_id" if identifier == "s" else "statement_id": {
            "type": "string", "pattern": f"^{identifier}[1-8]$"
        },
        "op" if identifier == "s" else "node": {"const": key},
        "args": deepcopy(dict(argument_schema)),
    }
    required = list(properties)
    if binding:
        properties["bind"] = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,31}$"}
        required.append("bind")
    return closed_object(properties, required)


P_STEP_SCHEMA = {
    "oneOf": [
        _statement_schema(
            key=key,
            argument_schema=row["argument_schema"],
            identifier="s",
            binding=key in P_BINDING_OPS,
        )
        for key, row in P_OPERATION_REGISTRY.items()
    ]
}
P_PROGRAM_SCHEMA = closed_object(
    {
        "language": {"const": "P"},
        "steps": {"type": "array", "minItems": 1, "maxItems": 8, "items": P_STEP_SCHEMA},
    },
    ["language", "steps"],
)

G_STATEMENT_SCHEMA = {
    "oneOf": [
        {"$ref": "#/P/program_schema/properties/steps/items"}
    ] + [
        _statement_schema(
            key=key,
            argument_schema=row["argument_schema"],
            identifier="s",
            binding=row["outputs_or_bindings"].startswith("required"),
        )
        for key, row in G_NODE_REGISTRY.items()
    ]
}
G_PROGRAM_SCHEMA = closed_object(
    {
        "language": {"const": "G"},
        "steps": {"type": "array", "minItems": 1, "maxItems": 8, "items": G_STATEMENT_SCHEMA},
    },
    ["language", "steps"],
)


SYNTAX_EXAMPLES = {
    "M": {"label": "syntax only", "candidate": {"language": "M", "action_key": "<ADMITTED_ACTION_KEY>"}},
    "P_BOUNDARY": {
        "label": "syntax only; inherited M remains permitted",
        "candidate": {"language": "P", "steps": [
            {"step_id": "s1", "op": "UNROUTE", "args": {}},
            {"step_id": "s2", "op": "ROUTE", "args": {"directive": "Default"}},
        ]},
    },
    "G_BOUNDARY_SPATIAL": {
        "label": "syntax only; generated predicate and geometry",
        "candidate": {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "cell", "predicate": {"op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True}, "top_k": 1}, "bind": "targets"},
            {"step_id": "s2", "op": "CREATE_REGION", "args": {"x_min": 0, "x_max": 0, "y_min": 0, "y_max": 0}, "bind": "region"},
            {"step_id": "s3", "op": "ASSIGN_REGION", "args": {"target_handle": {"ref": "targets"}, "region_handle": {"ref": "region"}}},
            {"step_id": "s4", "op": "UNROUTE", "args": {}},
            {"step_id": "s5", "op": "ROUTE", "args": {"directive": "Default"}},
        ]},
    },
    "G_BOUNDARY_NONPREFIX_MULTI_NET": {
        "label": "syntax only; DIFF-derived non-prefix batch, not a recommendation",
        "candidate": {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "net", "predicate": {"op": "COMPARE", "field": "fanout", "operator": "GE", "value": 2}, "top_k": 8}, "bind": "eligible"},
            {"step_id": "s2", "op": "TOPK", "args": {"input": {"ref": "eligible"}, "k": 1}, "bind": "prefix"},
            {"step_id": "s3", "op": "DIFF", "args": {"left": {"ref": "eligible"}, "right": {"ref": "prefix"}}, "bind": "nonprefix"},
            {"step_id": "s4", "op": "BATCH_ROUTE_NETS", "args": {"typed_nets": {"ref": "nonprefix"}, "delay_mode": "AUTO_DELAY"}},
        ]},
    },
    "G_BOUNDARY_COUPLED_HARDBLOCK": {
        "label": "syntax only; injective coupled allocation, not a recommendation",
        "candidate": {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "hardblock", "predicate": {"op": "COMPARE", "field": "resource_type", "operator": "EQ", "value": "BRAM"}, "top_k": 2}, "bind": "hardblocks"},
            {"step_id": "s2", "op": "SELECT_WHERE", "args": {"source_type": "site", "predicate": {"op": "COMPARE", "field": "object_type", "operator": "EQ", "value": "site"}, "top_k": 4}, "bind": "sites"},
            {"step_id": "s3", "op": "COUPLE_SITES", "args": {"hardblocks": {"ref": "hardblocks"}, "sites": {"ref": "sites"}, "strategy": "REVERSE"}, "bind": "allocation"},
            {"step_id": "s4", "op": "UNROUTE", "args": {}},
            {"step_id": "s5", "op": "APPLY_ALLOCATION", "args": {"allocation": {"ref": "allocation"}}},
            {"step_id": "s6", "op": "ROUTE", "args": {"directive": "Default"}},
        ]},
    },
}


STRICT_FAMILY_CARDS = [
    {
        "mechanism_family": "SPATIAL_GENERATED_GEOMETRY",
        "legal_structure": ["SELECT_WHERE or generated target binding", "CREATE_REGION with non-predefined coordinates", "ASSIGN_REGION consuming both bindings", "implementation follow-up to ROUTED"],
        "minimum_cardinality": 1,
        "construction_failures": ["empty selector", "out-of-device geometry", "insufficient compatible region capacity", "generated region not consumed"],
        "qor_or_target_recommendation_present": False,
    },
    {
        "mechanism_family": "NONPREFIX_MULTI_NET_BATCH",
        "legal_structure": ["generated net collection", "DIFF output cardinality at least two", "BATCH_ROUTE_NETS consumes DIFF binding"],
        "minimum_cardinality": 2,
        "construction_failures": ["empty selector", "DIFF output cardinality below two", "DIFF binding not consumed by BATCH_ROUTE_NETS"],
        "qor_or_target_recommendation_present": False,
    },
    {
        "mechanism_family": "COUPLED_HARDBLOCK_SITE_ALLOCATION",
        "legal_structure": ["at least two hardblock targets", "compatible unique site collection", "COUPLE_SITES", "APPLY_ALLOCATION consuming allocation", "implementation follow-up to ROUTED"],
        "minimum_cardinality": 2,
        "construction_failures": ["fewer than two hardblocks", "compatible unique sites unavailable", "allocation not consumed", "implementation follow-up missing"],
        "qor_or_target_recommendation_present": False,
    },
]


def contract_for_level(
    level: str, *, admitted_action_keys: tuple[str, ...] = M_ACTION_KEYS
) -> dict[str, Any]:
    allowed = {"L2": ["M"], "L3": ["M", "P"], "L4": ["M", "P", "G"]}
    if level not in allowed:
        raise ValueError("contract_level_not_l2_l3_l4")
    if any(key not in M_ACTION_KEYS for key in admitted_action_keys):
        raise ValueError("unknown_admitted_action_key")
    m_schema = deepcopy(M_CANDIDATE_SCHEMA)
    m_schema["properties"]["action_key"]["enum"] = list(admitted_action_keys)
    value: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "action_surface_version": ACTION_SURFACE_VERSION,
        "action_surface_registry_sha256": registry_sha256(),
        "level": level,
        "common_objective": COMMON_OBJECTIVE,
        "allowed_languages": allowed[level],
        "common_response_grammar": deepcopy(COMMON_RESPONSE_GRAMMAR),
        "M": {
            "version": M_LANGUAGE_V5,
            "candidate_schema": m_schema,
            "cards": compact_cards([row for row in M10_CARDS if row["key"] in admitted_action_keys]),
        },
        "examples": {"M": deepcopy(SYNTAX_EXAMPLES["M"])},
        "forbidden": ["unknown fields", "raw Tcl", "generic set_property", "RQS", "shell/process", "network", "filesystem", "arbitrary Python", "new mechanism families", "pre-COMMIT mutation", "pre-COMMIT QoR", "post-COMMIT resubmission"],
    }
    if level in {"L3", "L4"}:
        value["P"] = {
            "version": P_LANGUAGE_V5,
            "program_schema": deepcopy(P_PROGRAM_SCHEMA),
            "cards": compact_cards(P_CARDS),
            "boundary_eligible_operations": sorted(P_EXCLUSIVE_OPERATIONS),
            "limits": {"statements": 8, "actual_heavy_calls": 4, "light_mutations": 8},
        }
        value["examples"]["P_BOUNDARY"] = deepcopy(SYNTAX_EXAMPLES["P_BOUNDARY"])
    if level == "L4":
        value["G"] = {
            "version": G_LANGUAGE_V5,
            "inherits": ["M", "P"],
            "inherited_P_schema_ref": "#/P",
            "program_schema": deepcopy(G_PROGRAM_SCHEMA),
            "cards": compact_cards(G_CARDS),
            "$defs": {"predicate": deepcopy(PREDICATE_SCHEMA)},
            "boundary_eligible_nodes": sorted(G_EXCLUSIVE_NODES),
            "limits": {"statements": 8, "actual_heavy_calls": 4, "light_mutations": 8, "predicate_depth": 4, "predicate_nodes": 16},
            "strict_family_cards": deepcopy(STRICT_FAMILY_CARDS),
        }
        value["examples"]["G_BOUNDARY_SPATIAL"] = deepcopy(SYNTAX_EXAMPLES["G_BOUNDARY_SPATIAL"])
        value["examples"]["G_BOUNDARY_NONPREFIX_MULTI_NET"] = deepcopy(SYNTAX_EXAMPLES["G_BOUNDARY_NONPREFIX_MULTI_NET"])
        value["examples"]["G_BOUNDARY_COUPLED_HARDBLOCK"] = deepcopy(SYNTAX_EXAMPLES["G_BOUNDARY_COUPLED_HARDBLOCK"])
    return value


def contract_hash(level: str, *, admitted_action_keys: tuple[str, ...] = M_ACTION_KEYS) -> str:
    return hashlib.sha256(canonical_json_bytes(contract_for_level(level, admitted_action_keys=admitted_action_keys))).hexdigest()


def compile_candidate_v5(candidate: Mapping[str, Any], context: CompileContext) -> CompiledProgram:
    current = replace(context, action_surface_version="v3")
    if isinstance(candidate, Mapping) and candidate.get("language") == "M":
        if set(candidate) != {"language", "action_key"}:
            raise ConstructionError("macro_candidate_fields_invalid")
        action_key = candidate.get("action_key")
        if not isinstance(action_key, str) or action_key not in M_ACTION_KEYS:
            raise ConstructionError("macro_action_key_not_admitted")
        return _finish_v2(
            candidate, compile_macro(action_key.removeprefix("macro."), current)
        )
    return compile_candidate_v2(candidate, current)


def schema_ref_for_error(error_code: str) -> str:
    if error_code.startswith(("macro", "action")):
        return "#/M"
    if error_code.startswith(("generated", "predicate", "g_", "allocation", "batch", "compatible")):
        return "#/G"
    if error_code.startswith(("p_", "program", "selector", "directive", "phys_opt", "property", "route_net", "cell", "pblock")):
        return "#/P"
    return "#/forbidden"


__all__ = [
    "CONTRACT_VERSION", "G_CARDS", "G_LANGUAGE_V5", "G_PROGRAM_SCHEMA",
    "MAX_FEEDBACK", "MAX_INPUT_BYTES", "MAX_QUERIES", "MAX_TURNS",
    "M_ACTION_KEYS", "M_HEAVY_CALLS", "M_LANGUAGE_V5", "PLANNER_MESSAGE_V5",
    "PLANNING_CONTEXT_V22", "PROMPT_VERSION_V3", "P_CARDS", "P_LANGUAGE_V5",
    "P_PROGRAM_SCHEMA", "QUERY_NAMES", "QUERY_RESULT_MAX_BYTES",
    "QUERY_SESSION_MAX_BYTES", "QUERY_SESSION_MAX_ROWS", "QUERY_TOP_K_MAX",
    "SYNTAX_EXAMPLES", "compact_cards", "compile_candidate_v5", "contract_for_level",
    "contract_hash", "schema_ref_for_error",
]
