"""WP-013A model-visible contract and accounting overlay.

The v1 compiler remains immutable negative-interface evidence.  This module
provides one public `action_key` representation, translates it into the
accepted v1 runtime semantics, and rebuilds accounting from the actual Macro
host-call contract.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from typing import Any, Mapping

from .language import (
    CompileContext,
    CompiledProgram,
    ConstructionError,
    HostStep,
    compile_g,
    compile_macro,
    compile_p,
)
from .macro_authority import M_ACTION_KEYS, M_HEAVY_CALLS
from .specs import canonical_json_bytes, build_protocol_freeze


CONTRACT_VERSION = "dof.single-submission.model-contract.v2"
M_LANGUAGE_V2 = "dof.single-submission.m.v2"
P_LANGUAGE_V2 = "dof.single-submission.p.v2"
G_LANGUAGE_V2 = "dof.single-submission.g-ast.v2"
PLANNER_MESSAGE_V2 = "dof.single-submission.planner-message.v2"
PLANNING_CONTEXT_V2 = "dof.single-submission.planning-context.v2"
MAX_INPUT_BYTES = 32_768
MAX_TURNS = 4
MAX_QUERIES = 8
MAX_FEEDBACK = 3

QUERY_NAMES = (
    "critical_path_details",
    "critical_object_locations",
    "high_fanout_net_details",
    "critical_hardblock_candidates",
    "placement_summary",
    "route_summary",
)


def _closed_object(
    properties: Mapping[str, Any], required: tuple[str, ...] | list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


STRING = {"type": "string"}
HANDLE_OR_REF = {
    "oneOf": [
        {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,23}_h[0-9a-f]{24}$"},
        _closed_object({"ref": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,31}$"}}, ["ref"]),
    ]
}
EMPTY_ARGS = _closed_object({}, [])
ACTION_KEY = {"type": "string", "enum": list(M_ACTION_KEYS)}

P_ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "CALL_MACRO": _closed_object({"action_key": ACTION_KEY}, ["action_key"]),
    "UNROUTE": EMPTY_ARGS,
    "UNPLACE": EMPTY_ARGS,
    "PLACE": _closed_object(
        {"directive": {"enum": ["Default", "Explore", "ExtraNetDelay_high"]}},
        ["directive"],
    ),
    "ROUTE": _closed_object(
        {"directive": {"enum": ["Default", "Explore", "AggressiveExplore"]}},
        ["directive"],
    ),
    "PHYS_OPT": _closed_object(
        {
            "directive": {
                "enum": ["Default", "Explore", "AggressiveExplore", "AlternateFlowWithRetiming"]
            },
            "options": {
                "type": "array",
                "items": {
                    "enum": [
                        "AddRetime", "critical_cell_opt", "critical_pin_opt",
                        "placement_opt", "routing_opt", "dsp_register_opt",
                        "bram_register_opt", "uram_register_opt",
                    ]
                },
                "uniqueItems": True,
                "maxItems": 8,
            },
        },
        ["directive"],
    ),
    "SELECT_CRITICAL_CELLS": _closed_object(
        {"top_k": {"type": "integer", "minimum": 1, "maximum": 64}}, ["top_k"]
    ),
    "SELECT_CRITICAL_NETS": _closed_object(
        {"top_k": {"type": "integer", "minimum": 1, "maximum": 64}}, ["top_k"]
    ),
    "SELECT_HIGH_FANOUT_NETS": _closed_object(
        {"top_k": {"type": "integer", "minimum": 1, "maximum": 64}}, ["top_k"]
    ),
    "SELECT_CRITICAL_HARDBLOCKS": _closed_object(
        {
            "type": {"enum": ["DSP", "BRAM", "URAM", "ANY"]},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 32},
        },
        ["type", "top_k"],
    ),
    "SELECT_CONGESTED_REGION": _closed_object({"region_id": HANDLE_OR_REF}, ["region_id"]),
    "CREATE_PBLOCK": _closed_object(
        {"predefined_region_id": HANDLE_OR_REF}, ["predefined_region_id"]
    ),
    "ASSIGN": _closed_object(
        {"target_handle": HANDLE_OR_REF, "pblock_handle": HANDLE_OR_REF},
        ["target_handle", "pblock_handle"],
    ),
    "REMOVE_PBLOCK": _closed_object({"pblock_handle": HANDLE_OR_REF}, ["pblock_handle"]),
    "RELOCATE_HARDBLOCK": _closed_object(
        {"typed_target": HANDLE_OR_REF, "destination": HANDLE_OR_REF},
        ["typed_target", "destination"],
    ),
}

P_BINDING_OPS = {
    "SELECT_CRITICAL_CELLS",
    "SELECT_CRITICAL_NETS",
    "SELECT_HIGH_FANOUT_NETS",
    "SELECT_CRITICAL_HARDBLOCKS",
    "SELECT_CONGESTED_REGION",
    "CREATE_PBLOCK",
}

PREDICATE_SCHEMA = {
    "description": "recursive closed predicate; depth <=4 and total nodes <=16",
    "oneOf": [
        _closed_object(
            {
                "op": {"enum": ["AND", "OR"]},
                "children": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"$ref": "#/G/$defs/predicate"}},
            },
            ["op", "children"],
        ),
        _closed_object(
            {"op": {"const": "NOT"}, "child": {"$ref": "#/G/$defs/predicate"}},
            ["op", "child"],
        ),
        _closed_object(
            {
                "op": {"const": "COMPARE"},
                "field": {
                    "enum": [
                        "object_type", "resource_type", "path_member", "path_rank",
                        "slack_ns", "delay_ns", "fanout", "slr_id",
                        "clock_region_id", "congestion_class",
                    ]
                },
                "operator": {"enum": ["EQ", "NE", "LT", "LE", "GT", "GE", "IN"]},
                "value": {"type": ["string", "integer", "number", "boolean", "array"]},
            },
            ["op", "field", "operator", "value"],
        ),
    ],
}

G_GENERATED_ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "P_PROGRAM": _closed_object({"program": {"$ref": "#/P/program_schema"}}, ["program"]),
    "SELECT_WHERE": _closed_object(
        {
            "source_type": {"enum": ["cell", "net", "hardblock"]},
            "predicate": {"$ref": "#/G/$defs/predicate"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 64},
        },
        ["source_type", "predicate", "top_k"],
    ),
    "CREATE_REGION": _closed_object(
        {key: {"type": "integer"} for key in ("x_min", "x_max", "y_min", "y_max")},
        ["x_min", "x_max", "y_min", "y_max"],
    ),
    "ASSIGN_REGION": _closed_object(
        {"target_handle": HANDLE_OR_REF, "region_handle": HANDLE_OR_REF},
        ["target_handle", "region_handle"],
    ),
    "RELOCATE_HARDBLOCK_GENERATED": _closed_object(
        {
            "typed_target": HANDLE_OR_REF,
            "candidate_sites": {
                "type": "array", "items": HANDLE_OR_REF, "minItems": 1, "maxItems": 32
            },
            "site_index": {"type": "integer", "minimum": 0, "maximum": 31},
        },
        ["typed_target", "candidate_sites", "site_index"],
    ),
}


def _p_program_schema() -> dict[str, Any]:
    variants = []
    for op, args in P_ARGUMENT_SCHEMAS.items():
        properties: dict[str, Any] = {
            "step_id": {"type": "string", "pattern": "^s[1-8]$"},
            "op": {"const": op},
            "args": args,
        }
        required = ["step_id", "op", "args"]
        if op in P_BINDING_OPS:
            properties["bind"] = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,31}$"}
            required.append("bind")
        variants.append(_closed_object(properties, required))
    return _closed_object(
        {
            "language": {"const": "P"},
            "steps": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"oneOf": variants}},
        },
        ["language", "steps"],
    )


def _g_program_schema() -> dict[str, Any]:
    variants = []
    inherited = dict(P_ARGUMENT_SCHEMAS)
    generated = {key: value for key, value in G_GENERATED_ARGUMENT_SCHEMAS.items() if key != "RELOCATE_HARDBLOCK_GENERATED"}
    for node, args in (inherited | generated).items():
        properties: dict[str, Any] = {
            "statement_id": {"type": "string", "pattern": "^g[1-8]$"},
            "node": {"const": node},
            "args": args,
        }
        required = ["statement_id", "node", "args"]
        if node in P_BINDING_OPS or node in {"SELECT_WHERE", "CREATE_REGION"}:
            properties["bind"] = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,31}$"}
            required.append("bind")
        variants.append(_closed_object(properties, required))
    variants.append(
        _closed_object(
            {
                "statement_id": {"type": "string", "pattern": "^g[1-8]$"},
                "node": {"const": "RELOCATE_HARDBLOCK"},
                "args": G_GENERATED_ARGUMENT_SCHEMAS["RELOCATE_HARDBLOCK_GENERATED"],
            },
            ["statement_id", "node", "args"],
        )
    )
    return _closed_object(
        {
            "language": {"const": "G"},
            "statements": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"oneOf": variants}},
        },
        ["language", "statements"],
    )


M_CANDIDATE_SCHEMA = _closed_object(
    {"language": {"const": "M"}, "action_key": ACTION_KEY}, ["language", "action_key"]
)
P_PROGRAM_SCHEMA = _p_program_schema()
G_PROGRAM_SCHEMA = _g_program_schema()

QUERY_MESSAGE_SCHEMA = _closed_object(
    {
        "kind": {"const": "QUERY"},
        "queries": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": _closed_object(
                {
                    "name": {"enum": list(QUERY_NAMES)},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 64},
                },
                ["name", "top_k"],
            ),
        },
    },
    ["kind", "queries"],
)


def _m_docs() -> list[dict[str, Any]]:
    spec = next(item for item in build_protocol_freeze() if item.name == "M_LANGUAGE_V1")
    rows = deepcopy(spec.payload()["body"]["guaranteed_actions"])
    for row in rows:
        calls = M_HEAVY_CALLS[row["action_key"]]
        row["cost"]["actual_heavy_call_range"] = [calls, calls]
        row["candidate"] = {"language": "M", "action_key": row["action_key"]}
    return rows


def contract_for_level(level: str) -> dict[str, Any]:
    allowed = {"L2": ["M"], "L3": ["M", "P"], "L4": ["M", "P", "G"]}
    if level not in allowed:
        raise ValueError("contract_level_not_l2_l3_l4")
    contract: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "level": level,
        "allowed_languages": allowed[level],
        "response_forms": {
            "query": QUERY_MESSAGE_SCHEMA,
            "candidate": {
                "kinds": ["DRAFT", "VALIDATE", "COMMIT"],
                "shape": _closed_object(
                    {"kind": {"enum": ["DRAFT", "VALIDATE", "COMMIT"]}, "candidate": {}},
                    ["kind", "candidate"],
                ),
            },
            "terminal": {
                "oneOf": [
                    _closed_object({"kind": {"const": "STOP"}}, ["kind"]),
                    _closed_object({"kind": {"const": "NO_VALID_SUBMISSION"}}, ["kind"]),
                ]
            },
            "unknown_fields": "rejected at every object level",
        },
        "turn_rules": {
            "turns_1_to_3": ["QUERY", "DRAFT", "VALIDATE", "COMMIT", "STOP", "NO_VALID_SUBMISSION"],
            "turn_4": ["COMMIT", "STOP", "NO_VALID_SUBMISSION"],
            "max_queries_total": MAX_QUERIES,
            "max_feedback_rounds": MAX_FEEDBACK,
            "valid_commits": 1,
        },
        "M": {"version": M_LANGUAGE_V2, "candidate_schema": M_CANDIDATE_SCHEMA, "actions": _m_docs()},
        "syntax_templates": {
            "QUERY": {"kind": "QUERY", "queries": [{"name": "<QUERY_NAME>", "top_k": 8}]},
            "M": {"kind": "COMMIT", "candidate": {"language": "M", "action_key": "<ACTION_KEY>"}},
        },
        "template_policy": "syntax-only and non-recommendational; angle-bracket values must be replaced by values visible in this contract",
        "feedback_shape": _closed_object(
            {
                "error_code": STRING,
                "location": {"type": ["string", "null"]},
                "expected_shape_or_schema_ref": STRING,
                "remaining_turns": {"type": "integer", "minimum": 0, "maximum": 4},
                "remaining_feedback_rounds": {"type": "integer", "minimum": 0, "maximum": 3},
                "remaining_query_budget": {"type": "integer", "minimum": 0, "maximum": 8},
            },
            [
                "error_code", "location", "expected_shape_or_schema_ref",
                "remaining_turns", "remaining_feedback_rounds", "remaining_query_budget",
            ],
        ),
        "forbidden": [
            "unknown fields", "system-owned binding fields", "raw Tcl", "shell/process",
            "network", "filesystem", "repository access", "arbitrary Python", "loops",
            "pre-COMMIT mutation", "pre-COMMIT QoR", "post-COMMIT resubmission",
        ],
    }
    if level in {"L3", "L4"}:
        contract["P"] = {
            "version": P_LANGUAGE_V2,
            "program_schema": _closed_object(
                {
                    "language": {"const": "P"},
                    "steps": {
                        "type": "array", "minItems": 1, "maxItems": 8,
                        "items": _closed_object(
                            {
                                "step_id": {"type": "string", "pattern": "^s[1-8]$"},
                                "op": {"enum": list(P_ARGUMENT_SCHEMAS)},
                                "args": {"type": "object"},
                                "bind": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,31}$"},
                            },
                            ["step_id", "op", "args"],
                        ),
                    },
                },
                ["language", "steps"],
            ),
            "operation_argument_schemas": P_ARGUMENT_SCHEMAS,
            "bind_rules": {
                "required_for": sorted(P_BINDING_OPS),
                "forbidden_for": sorted(set(P_ARGUMENT_SCHEMAS) - P_BINDING_OPS),
                "references": HANDLE_OR_REF,
            },
            "limits": {"steps": 8, "heavy_calls": 4, "light_mutations": 8},
            "stage_rule": "source order is s1..sN and the final program must be ROUTED with one scoreable implementation",
        }
        contract["syntax_templates"]["P"] = {
            "kind": "COMMIT",
            "candidate": {
                "language": "P",
                "steps": [
                    {"step_id": "s1", "op": "CALL_MACRO", "args": {"action_key": "<ACTION_KEY>"}}
                ],
            },
        }
    if level == "L4":
        contract["G"] = {
            "version": G_LANGUAGE_V2,
            "program_schema": _closed_object(
                {
                    "language": {"const": "G"},
                    "statements": {
                        "type": "array", "minItems": 1, "maxItems": 8,
                        "items": _closed_object(
                            {
                                "statement_id": {"type": "string", "pattern": "^g[1-8]$"},
                                "node": {"enum": sorted(set(P_ARGUMENT_SCHEMAS) | {"P_PROGRAM", "SELECT_WHERE", "CREATE_REGION", "ASSIGN_REGION"})},
                                "args": {"type": "object"},
                                "bind": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,31}$"},
                            },
                            ["statement_id", "node", "args"],
                        ),
                    },
                },
                ["language", "statements"],
            ),
            "inherited_P_schema_ref": "#/P",
            "generated_node_argument_schemas": G_GENERATED_ARGUMENT_SCHEMAS,
            "$defs": {"predicate": PREDICATE_SCHEMA},
            "limits": {
                "statements": 8, "heavy_calls": 4, "light_mutations": 8,
                "predicate_depth": 4, "predicate_nodes": 16,
            },
        }
        contract["syntax_templates"]["G"] = {
            "kind": "COMMIT",
            "candidate": {
                "language": "G",
                "statements": [
                    {"statement_id": "g1", "node": "CALL_MACRO", "args": {"action_key": "<ACTION_KEY>"}}
                ],
            },
        }
    return contract


def contract_hash(level: str) -> str:
    return hashlib.sha256(canonical_json_bytes(contract_for_level(level))).hexdigest()


def _macro_name(action_key: Any) -> str:
    if not isinstance(action_key, str) or action_key not in M_ACTION_KEYS:
        raise ConstructionError("macro_action_key_not_admitted")
    return action_key.removeprefix("macro.")


def _translate_p(program: Mapping[str, Any], *, translate_macro: bool = True) -> dict[str, Any]:
    value = deepcopy(dict(program))
    steps = value.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or step.get("op") != "CALL_MACRO":
                continue
            args = step.get("args")
            if translate_macro and isinstance(args, dict) and set(args) == {"action_key"}:
                step["args"] = {"macro": args["action_key"]}
    return value


def _translate_g(program: Mapping[str, Any], *, translate_macro: bool = True) -> dict[str, Any]:
    value = deepcopy(dict(program))
    v3_steps = value.pop("steps", None)
    if isinstance(v3_steps, list) and "statements" not in value:
        value["statements"] = [
            {
                **({"statement_id": ("g" + str(step.get("step_id"))[1:]) if str(step.get("step_id", "")).startswith("s") else step.get("step_id"), "node": step.get("op"), "args": step.get("args")} if isinstance(step, dict) else {}),
                **({"bind": step["bind"]} if isinstance(step, dict) and "bind" in step else {}),
            }
            if isinstance(step, dict) else step
            for step in v3_steps
        ]
    statements = value.get("statements")
    if isinstance(statements, list):
        for statement in statements:
            if not isinstance(statement, dict):
                continue
            args = statement.get("args")
            if translate_macro and statement.get("node") == "CALL_MACRO" and isinstance(args, dict) and set(args) == {"action_key"}:
                statement["args"] = {"macro": args["action_key"]}
            elif statement.get("node") == "P_PROGRAM" and isinstance(args, dict) and isinstance(args.get("program"), Mapping):
                statement["args"] = {"program": _translate_p(args["program"], translate_macro=translate_macro)}
    return value


def _finish_v2(source: Mapping[str, Any], compiled: CompiledProgram) -> CompiledProgram:
    steps: list[HostStep] = []
    for step in compiled.host_steps:
        if (
            compiled.language == "G"
            and isinstance(source.get("steps"), list)
            and isinstance(step.source_step_id, str)
            and step.source_step_id.startswith("g")
        ):
            step = replace(step, source_step_id="s" + step.source_step_id[1:])
        if step.operation in {"vivado.phys_opt.memory", "vivado.phys_opt.hardblock"}:
            steps.append(HostStep("vivado.place", "IMPLEMENTATION_HEAVY", {"directive": "Default", "role": "stage_preparation"}, {"directive": "Default", "role": "stage_preparation"}))
        steps.append(step)
    heavy = sum(step.heavy_cost for step in steps)
    light = sum(step.light_cost for step in steps)
    if heavy > 4:
        raise ConstructionError("static_heavy_budget_overflow")
    if light > 8:
        raise ConstructionError("static_light_budget_overflow")
    source_hash = hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    payload = {
        "schema_version": f"dof.single-submission.compiled-{compiled.language.lower()}.v2",
        "language_version": {"M": M_LANGUAGE_V2, "P": P_LANGUAGE_V2, "G": G_LANGUAGE_V2}[compiled.language],
        "source_sha256": source_hash,
        "initial_stage": compiled.initial_stage,
        "final_stage": compiled.final_stage,
        "heavy_calls": heavy,
        "light_mutations": light,
        "selected_count": sum(step.selected_count for step in steps),
        "transformed_count": sum(step.transformed_count for step in steps),
        "host_steps": [step.public() for step in steps],
        "binding_records": [dict(row) for row in compiled.binding_records],
        "accounting_authority": "actual place_design/route_design/phys_opt_design host-call starts",
    }
    raw = canonical_json_bytes(payload)
    private_hash = hashlib.sha256(canonical_json_bytes([step.private() for step in steps])).hexdigest()
    return CompiledProgram(
        compiled.language,
        source_hash,
        compiled.initial_stage,
        compiled.final_stage,
        tuple(steps),
        heavy,
        light,
        payload["selected_count"],
        payload["transformed_count"],
        raw,
        hashlib.sha256(raw).hexdigest(),
        private_hash,
        compiled.binding_records,
    )


def compile_candidate_v2(candidate: Mapping[str, Any], context: CompileContext) -> CompiledProgram:
    if not isinstance(candidate, Mapping):
        raise ConstructionError("candidate_not_object")
    language = candidate.get("language")
    if language == "M":
        if set(candidate) != {"language", "action_key"}:
            raise ConstructionError("macro_candidate_fields_invalid")
        compiled = compile_macro(_macro_name(candidate["action_key"]), context)
    elif language == "P":
        compiled = compile_p(candidate if context.action_surface_version == "v3" else _translate_p(candidate), context)
    elif language == "G":
        compiled = compile_g(_translate_g(candidate, translate_macro=context.action_surface_version != "v3"), context)
    else:
        raise ConstructionError("unknown_language")
    return _finish_v2(candidate, compiled)


def macro_from_candidate_v2(candidate: Mapping[str, Any] | None) -> str | None:
    if not isinstance(candidate, Mapping):
        return None
    if candidate.get("language") == "M" and set(candidate) == {"language", "action_key"}:
        try:
            return _macro_name(candidate["action_key"])
        except ConstructionError:
            return None
    rows = candidate.get("steps") if candidate.get("language") == "P" else candidate.get("statements") if candidate.get("language") == "G" else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return None
    row = rows[0]
    if row.get("op") == "CALL_MACRO" or row.get("node") == "CALL_MACRO":
        args = row.get("args")
        if isinstance(args, Mapping):
            try:
                return _macro_name(args.get("action_key"))
            except ConstructionError:
                return None
    if row.get("node") == "P_PROGRAM" and isinstance(row.get("args"), Mapping):
        return macro_from_candidate_v2(row["args"].get("program"))
    return None


def schema_ref_for_error(error_code: str) -> str:
    prefixes = {
        "macro": "#/M/candidate_schema",
        "p_": "#/P/program_schema",
        "g_": "#/G/program_schema",
        "predicate": "#/G/$defs/predicate",
        "generated": "#/G/generated_node_argument_schemas",
        "query": "#/response_forms/query",
        "message": "#/response_forms",
        "language": "#/allowed_languages",
        "static_heavy": "#/P/limits/heavy_calls",
        "static_light": "#/P/limits/light_mutations",
        "invalid_stage": "#/P/stage_rule",
        "program_terminal": "#/P/stage_rule",
    }
    for prefix, ref in prefixes.items():
        if error_code.startswith(prefix):
            return ref
    return "#/forbidden"


def candidate_templates() -> dict[str, dict[str, Any]]:
    key = "macro.place_route"
    return {
        "M": {"language": "M", "action_key": key},
        "P": {"language": "P", "steps": [{"step_id": "s1", "op": "CALL_MACRO", "args": {"action_key": key}}]},
        "G": {"language": "G", "statements": [{"statement_id": "g1", "node": "CALL_MACRO", "args": {"action_key": key}}]},
    }


def audit_contract_completeness(context: CompileContext) -> dict[str, Any]:
    templates = candidate_templates()
    compiled = {language: compile_candidate_v2(candidate, context) for language, candidate in templates.items()}
    contracts = {level: contract_for_level(level) for level in ("L2", "L3", "L4")}
    action_rows = contracts["L2"]["M"]["actions"]
    checks = {
        "all_public_objects_reject_unknown_fields": all(
            schema.get("additionalProperties") is False
            for schema in (M_CANDIDATE_SCHEMA, P_PROGRAM_SCHEMA, G_PROGRAM_SCHEMA, QUERY_MESSAGE_SCHEMA)
        ),
        "all_m_actions_unique_and_bound": [row["action_key"] for row in action_rows] == list(M_ACTION_KEYS),
        "all_m_costs_match_overlay": all(
            row["cost"]["actual_heavy_call_range"] == [M_HEAVY_CALLS[row["action_key"]]] * 2
            for row in action_rows
        ),
        "templates_compile": set(compiled) == {"M", "P", "G"},
        "m_action_key_preserved_in_p_g": all(
            b"action_key" in canonical_json_bytes(templates[level]) for level in ("P", "G")
        ),
        "l2_m_bytes_embed_unchanged": contracts["L2"]["M"] == contracts["L3"]["M"] == contracts["L4"]["M"],
        "l3_p_bytes_embed_unchanged": contracts["L3"]["P"] == contracts["L4"]["P"],
        "message_forms_complete": set(contracts["L4"]["response_forms"]) == {"query", "candidate", "terminal", "unknown_fields"},
        "every_query_maps_once": len(QUERY_NAMES) == len(set(QUERY_NAMES)),
        "context_byte_ceiling_declared": MAX_INPUT_BYTES == 32768,
    }
    payload = {
        "schema_version": "dof.wp013a-contract-completeness-audit.v1",
        "contract_hashes": {level: contract_hash(level) for level in contracts},
        "template_compiled_hashes": {key: value.public_sha256 for key, value in compiled.items()},
        "checks": checks,
        "accepted": all(checks.values()),
    }
    payload["audit_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


__all__ = [
    "CONTRACT_VERSION", "G_LANGUAGE_V2", "MAX_FEEDBACK", "MAX_INPUT_BYTES",
    "MAX_QUERIES", "MAX_TURNS", "M_ACTION_KEYS", "M_HEAVY_CALLS", "M_LANGUAGE_V2",
    "PLANNER_MESSAGE_V2", "PLANNING_CONTEXT_V2", "P_LANGUAGE_V2", "QUERY_NAMES",
    "audit_contract_completeness", "candidate_templates", "compile_candidate_v2",
    "contract_for_level", "contract_hash", "macro_from_candidate_v2", "schema_ref_for_error",
]
