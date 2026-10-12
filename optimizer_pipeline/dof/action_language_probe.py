"""Isolated action-language feasibility contracts for WP-012-02.

This module is deliberately unreachable from the primary K/U/H runners.  It
exists only to test a possible M -> M+P -> M+P+G boundary under the explicit
``ACTION_LANGUAGE_DESIGN_PROBE_V1`` identity.  The validators fail closed and
produce ordinary audited action steps; they never execute Tcl or a backend.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .contracts import DesignStage, DoFLevel
from .expansions import (
    expand_real_action,
    real_action_expansion_manifest_v2,
)
from .toolcalls import e1v2_tool_call_registry


PROBE_IDENTITY = "ACTION_LANGUAGE_DESIGN_PROBE_V1"
PROBE_SCHEMA = "dof.action-language-design-probe.v1"
MAX_STEPS = 6
MAX_HEAVY_CALLS = 3

# Semantically generic, replayable candidates found by the historical-flow
# audit.  Inclusion here is provisional evidence, not a primary-protocol
# catalogue freeze.
M_CANDIDATES = (
    "phys_opt",
    "auto_pblock",
    "lut_cone",
    "retime",
    "net_swap",
    "memory_path_opt",
    "hardblock_relocate",
    "congestion_spread",
    "fanout",
)

_DIRECTIVES = {
    "UNROUTE_ALL": ("manual.unroute_all", {}, 0),
    "UNPLACE_ALL": ("manual.unplace_all", {}, 0),
    "PLACE": ("manual.place_design", {"directive": {"Default", "Explore"}}, 1),
    "ROUTE": ("manual.route_design", {"directive": {"Default", "Explore"}}, 1),
    "PHYS_OPT": (
        "manual.phys_opt_design",
        {"directive": {"Explore", "AddRetime", "AlternateFlowWithRetiming"}},
        1,
    ),
}
_P_SELECTORS = {"critical_cells", "high_fanout_cells", "sequential_cells"}
_P_REGIONS = {
    "device_quadrant_nw": (0, 0, 49, 49),
    "device_quadrant_se": (50, 50, 99, 99),
    "critical_path_window": (20, 20, 39, 59),
}
_G_OPS = {
    "SELECT",
    "CREATE_PBLOCK",
    "UNROUTE_ALL",
    "UNPLACE_ALL",
    "PLACE",
    "ROUTE",
    "PHYS_OPT",
}
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ActionLanguageProbeError(ValueError):
    """A design-probe program was rejected before dispatch."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ActionLanguageProbeError(f"{label}_fields_not_exact")


def _primitive_step(
    operation: str, parameters: Mapping[str, Any], stage: DesignStage
) -> tuple[dict[str, Any], DesignStage, int]:
    action_key, schema, heavy = _DIRECTIVES[operation]
    params = dict(parameters)
    if not schema:
        if params:
            raise ActionLanguageProbeError(f"{operation.lower()}_parameters_not_empty")
    else:
        _exact(params, set(schema), operation.lower())
        for key, allowed in schema.items():
            if params[key] not in allowed:
                raise ActionLanguageProbeError(f"{operation.lower()}_{key}_invalid")
    registry = e1v2_tool_call_registry()
    manifest = real_action_expansion_manifest_v2(registry)
    try:
        expanded = expand_real_action(
            DoFLevel.L3,
            action_key,
            params,
            current_stage=stage,
            registry=registry,
            manifest=manifest,
        )
    except Exception as exc:
        raise ActionLanguageProbeError(
            f"stage_precondition:{operation}:{stage.value}"
        ) from exc
    return (
        {
            "action_key": action_key,
            "parameters": params,
            "expanded": expanded.to_dict(),
        },
        expanded.final_stage,
        heavy,
    )


def validate_p_program(program: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a bounded typed P program and return its frozen semantics."""

    if not isinstance(program, Mapping):
        raise ActionLanguageProbeError("p_program_not_object")
    value = dict(program)
    _exact(value, {"program_id", "steps"}, "p_program")
    if not isinstance(value["program_id"], str) or _IDENT.fullmatch(value["program_id"]) is None:
        raise ActionLanguageProbeError("p_program_id_invalid")
    steps = value["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ActionLanguageProbeError("p_step_count_invalid")
    stage = DesignStage.ROUTED
    heavy = 0
    target: dict[str, Any] | None = None
    compiled: list[dict[str, Any]] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, Mapping):
            raise ActionLanguageProbeError(f"p_step_{index}_not_object")
        step = dict(raw)
        _exact(step, {"op", "parameters"}, f"p_step_{index}")
        op = step["op"]
        params = step["parameters"]
        if not isinstance(params, Mapping):
            raise ActionLanguageProbeError(f"p_step_{index}_parameters_not_object")
        if op == "CALL_MACRO":
            _exact(dict(params), {"name"}, "call_macro")
            name = params["name"]
            if name not in M_CANDIDATES:
                raise ActionLanguageProbeError("call_macro_not_in_candidate_m")
            # Two candidates already have a frozen real-action expansion.  Use
            # it verbatim for the embedding witness; preserve the others as
            # exact legacy symbolic bindings until a final protocol is frozen.
            inherited = {"phys_opt": "macro.phys_opt", "retime": "macro.retime"}
            if name in inherited:
                registry = e1v2_tool_call_registry()
                manifest = real_action_expansion_manifest_v2(registry)
                expanded = expand_real_action(
                    DoFLevel.L2,
                    inherited[name],
                    {},
                    current_stage=stage,
                    registry=registry,
                    manifest=manifest,
                )
                compiled.append(
                    {
                        "action_key": inherited[name],
                        "parameters": {},
                        "expanded": expanded.to_dict(),
                    }
                )
                stage = expanded.final_stage
            else:
                compiled.append(
                    {
                        "action_key": f"flow.{name}",
                        "parameters": {},
                        "legacy_symbolic": True,
                    }
                )
            heavy += 1
        elif op in _DIRECTIVES:
            item, stage, cost = _primitive_step(op, params, stage)
            compiled.append(item)
            heavy += cost
        elif op == "SELECT_TARGET":
            _exact(dict(params), {"selector", "limit"}, "select_target")
            selector, limit = params["selector"], params["limit"]
            if selector not in _P_SELECTORS or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 64:
                raise ActionLanguageProbeError("select_target_invalid")
            target = {"selector": selector, "limit": limit}
            compiled.append({"typed_target": target})
        elif op == "CREATE_STANDARD_PBLOCK":
            _exact(dict(params), {"pblock_id", "region"}, "create_standard_pblock")
            pblock_id, region = params["pblock_id"], params["region"]
            if target is None:
                raise ActionLanguageProbeError("pblock_target_missing")
            if not isinstance(pblock_id, str) or _IDENT.fullmatch(pblock_id) is None:
                raise ActionLanguageProbeError("pblock_id_invalid")
            if region not in _P_REGIONS:
                raise ActionLanguageProbeError("pblock_region_not_standard")
            compiled.append(
                {
                    "action_key": "generated.pblock_region",
                    "parameters": {
                        "pblock_id": "g_" + pblock_id[:22],
                        "selector_kind": (
                            target["selector"]
                            if target["selector"] in {"high_fanout_cells", "sequential_cells"}
                            else "sequential_cells"
                        ),
                        "cell_limit": target["limit"],
                        "slice_x0": _P_REGIONS[region][0],
                        "slice_y0": _P_REGIONS[region][1],
                        "slice_x1": _P_REGIONS[region][2],
                        "slice_y1": _P_REGIONS[region][3],
                    },
                    "typed_region": region,
                }
            )
        else:
            raise ActionLanguageProbeError(f"p_operation_not_allowed:{op}")
        if heavy > MAX_HEAVY_CALLS:
            raise ActionLanguageProbeError("p_heavy_budget_exceeded")
    payload = {
        "schema_version": PROBE_SCHEMA,
        "probe_identity": PROBE_IDENTITY,
        "primary_aggregate_eligible": False,
        "language": "P",
        "program_id": value["program_id"],
        "initial_stage": "ROUTED",
        "final_stage": stage.value,
        "heavy_calls": heavy,
        "compiled_steps": compiled,
    }
    return payload | {"program_sha256": content_hash(payload)}


def validate_g_ast(program: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a bounded G AST with generated predicates and coordinates."""

    if not isinstance(program, Mapping):
        raise ActionLanguageProbeError("g_program_not_object")
    value = dict(program)
    _exact(value, {"program_id", "steps"}, "g_program")
    if not isinstance(value["program_id"], str) or _IDENT.fullmatch(value["program_id"]) is None:
        raise ActionLanguageProbeError("g_program_id_invalid")
    steps = value["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ActionLanguageProbeError("g_step_count_invalid")
    stage = DesignStage.ROUTED
    heavy = 0
    selection: dict[str, Any] | None = None
    compiled: list[dict[str, Any]] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, Mapping):
            raise ActionLanguageProbeError(f"g_step_{index}_not_object")
        step = dict(raw)
        _exact(step, {"op", "parameters"}, f"g_step_{index}")
        op, params = step["op"], step["parameters"]
        if op not in _G_OPS or not isinstance(params, Mapping):
            raise ActionLanguageProbeError("g_operation_not_allowed")
        params = dict(params)
        if op == "SELECT":
            _exact(params, {"object_type", "predicates", "limit"}, "g_select")
            if params["object_type"] != "cell" or not isinstance(params["limit"], int) or isinstance(params["limit"], bool) or not 1 <= params["limit"] <= 64:
                raise ActionLanguageProbeError("g_select_header_invalid")
            predicates = params["predicates"]
            if not isinstance(predicates, list) or not 1 <= len(predicates) <= 4:
                raise ActionLanguageProbeError("g_predicate_count_invalid")
            clean = []
            for predicate in predicates:
                if not isinstance(predicate, Mapping):
                    raise ActionLanguageProbeError("g_predicate_not_object")
                pred = dict(predicate)
                _exact(pred, {"property", "operator", "value"}, "g_predicate")
                if pred["property"] not in {"IS_SEQUENTIAL", "REF_NAME", "FANOUT", "SLACK_NS"} or pred["operator"] not in {"EQ", "GE", "LE"} or not isinstance(pred["value"], (str, int, float, bool)):
                    raise ActionLanguageProbeError("g_predicate_invalid")
                clean.append(pred)
            selection = {"predicates": clean, "limit": params["limit"]}
            compiled.append({"generated_selector": selection})
        elif op == "CREATE_PBLOCK":
            _exact(params, {"pblock_id", "x0", "y0", "x1", "y1"}, "g_pblock")
            if selection is None:
                raise ActionLanguageProbeError("g_pblock_selection_missing")
            pid = params["pblock_id"]
            coords = [params[key] for key in ("x0", "y0", "x1", "y1")]
            if not isinstance(pid, str) or _IDENT.fullmatch(pid) is None or any(not isinstance(item, int) or isinstance(item, bool) for item in coords):
                raise ActionLanguageProbeError("g_pblock_parameters_invalid")
            x0, y0, x1, y1 = coords
            if not (0 <= x0 <= x1 <= 999 and 0 <= y0 <= y1 <= 9999):
                raise ActionLanguageProbeError("g_pblock_coordinates_invalid")
            selector = "high_fanout_cells" if any(p["property"] == "FANOUT" for p in selection["predicates"]) else "sequential_cells"
            compiled.append(
                {
                    "action_key": "generated.pblock_region",
                    "parameters": {
                        "pblock_id": "g_" + pid[:22],
                        "selector_kind": selector,
                        "cell_limit": selection["limit"],
                        "slice_x0": x0,
                        "slice_y0": y0,
                        "slice_x1": x1,
                        "slice_y1": y1,
                    },
                    "source_predicates": selection["predicates"],
                }
            )
        else:
            item, stage, cost = _primitive_step(op, params, stage)
            compiled.append(item)
            heavy += cost
        if heavy > MAX_HEAVY_CALLS:
            raise ActionLanguageProbeError("g_heavy_budget_exceeded")
    payload = {
        "schema_version": PROBE_SCHEMA,
        "probe_identity": PROBE_IDENTITY,
        "primary_aggregate_eligible": False,
        "language": "G_AST",
        "program_id": value["program_id"],
        "initial_stage": "ROUTED",
        "final_stage": stage.value,
        "heavy_calls": heavy,
        "compiled_steps": compiled,
        "containment": {
            "shell": False,
            "filesystem": False,
            "network": False,
            "raw_tcl": False,
            "loops": False,
            "hidden_heavy_calls": False,
        },
    }
    return payload | {"program_sha256": content_hash(payload)}


def place_route_decomposition_program() -> dict[str, Any]:
    return {
        "program_id": "place_route_decomposition",
        "steps": [
            {"op": "UNROUTE_ALL", "parameters": {}},
            {"op": "UNPLACE_ALL", "parameters": {}},
            {"op": "PLACE", "parameters": {"directive": "Default"}},
            {"op": "ROUTE", "parameters": {"directive": "Default"}},
        ],
    }


def g_ast_adversarial_cases() -> Sequence[dict[str, Any]]:
    forbidden = ("SHELL", "EXEC", "OPEN", "SOURCE", "PACKAGE", "SOCKET", "WRITE_FILE", "SPAWN", "WHILE")
    return tuple(
        {
            "program_id": f"reject_{name.lower()}",
            "steps": [{"op": name, "parameters": {}}],
        }
        for name in forbidden
    )


__all__ = [
    "ActionLanguageProbeError",
    "M_CANDIDATES",
    "PROBE_IDENTITY",
    "g_ast_adversarial_cases",
    "place_route_decomposition_program",
    "validate_g_ast",
    "validate_p_program",
]
