"""Canonical Action Surface v3 registry for the WP-014C protocol epoch.

This module is the production authority for the model-visible P/G operation
surface.  It is deliberately data-only apart from deterministic projection
helpers: public schemas, the compiler and parity audits all consume these same
objects.  The isolated Vivado worker cannot trust package imports, so its
closed allowlist is checked mechanically against ``worker_meanings()``.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from .macro_authority import (
    M10_ACTION_KEYS as M_ACTION_KEYS,
    M10_HEAVY_CALLS as M_HEAVY_CALLS,
)


REGISTRY_SCHEMA_VERSION = "dof.action-surface-registry.v4"
ACTION_SURFACE_VERSION = "dof.action-surface.v4"
TARGET_ENVIRONMENT = "Vivado 2025.1 / xcvu3p-ffvc1517-2-e"

SOURCE_EVIDENCE = MappingProxyType(
    {
        "freeze_decision": "a99b134073c00ddfe05cf9f92823f8fa83bf4431933c056b5b45079ae78e8782",
        "observed_registry": "bfa083c49e5210f0cee9ccb7ca2cafd7f6fa74d2810d75e1bf0ef7f6c0a56674",
        "directive_compatibility": "09ad26495fa6f8e6c4e5e23d8075f5e6f7792611cd711055343c36a093757738",
        "stage_device_applicability": "5e0b183883f2f3b8e7cbfbc0e1af4da59bb75e9011b3d08f2d04148962463538",
        "mechanism_alignment": "09640da8eb29107040ab4fa5aaba645c03040335ac8cea6d915a032383b54c01",
        "property_allowlist": "6a8a25c3844d130ff80736160f5a285634522e13332419cd1944afc497ae9466",
        "exclusion_register": "998a1fafb60d394dbbea9e82150cffb97e69c7bc66bd574a0e57b67116005d15",
    }
)

PLACE_DIRECTIVES = ("Default", "Explore", "Quick", "RuntimeOptimized")
PLACE_NET_DELAY_WEIGHTS = ("low", "medium", "high")
ROUTE_DIRECTIVES = (
    "Default",
    "Explore",
    "AggressiveExplore",
    "NoTimingRelaxation",
    "MoreGlobalIterations",
    "HigherDelayCost",
    "AdvancedSkewModeling",
    "AlternateCLBRouting",
    "RuntimeOptimized",
    "Quick",
)
PHYS_OPT_DIRECTIVES = (
    "Default",
    "Explore",
    "AggressiveExplore",
    "ExploreWithHoldFix",
    "ExploreWithAggressiveHoldFix",
    "AlternateReplication",
    "AggressiveFanoutOpt",
    "AlternateFlowWithRetiming",
    "AddRetime",
    "RuntimeOptimized",
)
PHYS_OPT_POST_ROUTE_OPTIONS = (
    "aggressive_hold_fix",
    "critical_cell_opt",
    "hold_fix",
    "placement_opt",
    "restruct_opt",
    "retime",
    "routing_opt",
    "slr_crossing_opt",
)
PHYS_OPT_PLACED_OPTIONS = (
    "bram_enable_opt",
    "bram_register_opt",
    "dsp_register_opt",
    "fanout_opt",
    "shift_register_opt",
    "uram_register_opt",
)
PHYS_OPT_OPTIONS = PHYS_OPT_POST_ROUTE_OPTIONS + PHYS_OPT_PLACED_OPTIONS + (
    "tns_cleanup",
)

PREDICATE_FIELDS = (
    "object_type", "resource_type", "path_member", "path_rank", "slack_ns",
    "delay_ns", "fanout", "slr_id", "clock_region_id", "congestion_class",
)
COMPARE_OPERATORS = ("EQ", "NE", "LT", "LE", "GT", "GE", "IN")


def closed_object(
    properties: Mapping[str, Any], required: tuple[str, ...] | list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": deepcopy(dict(properties)),
        "required": list(required),
        "additionalProperties": False,
    }


HANDLE_PATTERN = "^[a-z][a-z0-9_]{0,23}_h[0-9a-f]{24}$"
REFERENCE_PATTERN = "^[A-Za-z][A-Za-z0-9_]{0,31}$"
REFERENCE_ONLY = closed_object(
    {"ref": {"type": "string", "pattern": REFERENCE_PATTERN}}, ["ref"]
)
HANDLE_OR_REF = {
    "oneOf": [
        {"type": "string", "pattern": HANDLE_PATTERN},
        closed_object({"ref": {"type": "string", "pattern": REFERENCE_PATTERN}}, ["ref"]),
    ]
}
EMPTY_ARGS = closed_object({}, [])
BOOL_VALUE = {"type": "boolean"}


def _op(
    key: str,
    schema: Mapping[str, Any],
    *,
    semantics: str,
    source_stage: str,
    result_stage: str,
    target_types: tuple[str, ...] = (),
    binding: str = "none",
    mutation_class: str,
    worker_operation: str | None,
    compiler_operation: str | None = None,
    heavy: int = 0,
    light: int = 0,
    follow_up: tuple[str, ...] = (),
    no_target: str = "not_applicable",
    failures: tuple[str, ...] = (),
    admission: str = "REQUIRES_WP014C_PRODUCTION_ADMISSION",
    boundary: str = "P-exclusive",
) -> dict[str, Any]:
    return {
        "operation": key,
        "visible_in": ["P", "G"],
        "argument_schema": deepcopy(dict(schema)),
        "short_semantics": semantics,
        "source_stage": source_stage,
        "result_stage": result_stage,
        "target_types": list(target_types),
        "outputs_or_bindings": binding,
        "mutation_class": mutation_class,
        "compiler_operation": compiler_operation if compiler_operation is not None else worker_operation,
        "worker_operation": worker_operation,
        "heavy_call_cost": heavy,
        "light_mutation_cost": light,
        "scoreability": "scoreable" if result_stage == "ROUTED" and heavy else "requires_scoreable_implementation",
        "required_follow_up": list(follow_up),
        "no_target_endpoint": no_target,
        "common_failure_codes": list(failures),
        "admission_status": admission,
        "boundary_eligibility": boundary,
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    }


_ACTION_KEY = {"type": "string", "enum": list(M_ACTION_KEYS)}
_TOP64 = {"type": "integer", "minimum": 1, "maximum": 64}

P_OPERATION_REGISTRY: dict[str, dict[str, Any]] = {}


def _add(row: dict[str, Any]) -> None:
    key = row["operation"]
    if key in P_OPERATION_REGISTRY:
        raise RuntimeError(f"duplicate_action_surface_operation:{key}")
    P_OPERATION_REGISTRY[key] = row


_call_macro = _op("CALL_MACRO", closed_object({"action_key": _ACTION_KEY}, ["action_key"]), semantics="execute one admitted frozen M8 macro through the same production flow authority as direct M", source_stage="ANY", result_stage="ROUTED", mutation_class="fixed_macro_composite", worker_operation=None, heavy=0, boundary="M")
_call_macro["heavy_call_cost"] = None
_call_macro["heavy_call_cost_by_action"] = dict(M_HEAVY_CALLS)
_call_macro["scoreability"] = "scoreable after the selected macro reaches its production validity endpoint"
_add(_call_macro)
_add(_op("UNROUTE", EMPTY_ARGS, semantics="remove all routing", source_stage="ROUTED", result_stage="PLACED", mutation_class="state_mutation", compiler_operation="vivado.unroute", worker_operation="vivado.unroute_all", light=1, follow_up=("ROUTE",), failures=("invalid_stage",)))
_add(_op("UNPLACE", EMPTY_ARGS, semantics="remove all placement", source_stage="PLACED", result_stage="UNPLACED", mutation_class="state_mutation", compiler_operation="vivado.unplace", worker_operation="vivado.unplace_all", light=1, follow_up=("PLACE", "ROUTE"), failures=("invalid_stage",)))
_add(_op("PLACE", closed_object({"directive": {"enum": list(PLACE_DIRECTIVES)}, "net_delay_weight": {"enum": list(PLACE_NET_DELAY_WEIGHTS)}}, ["directive"]), semantics="place the whole design with an admitted directive and optional net-delay weight", source_stage="UNPLACED_OR_PLACED", result_stage="PLACED", mutation_class="implementation", compiler_operation="vivado.place", worker_operation="vivado.place_design", heavy=1, follow_up=("ROUTE",), failures=("directive_not_allowed", "net_delay_weight_not_allowed", "invalid_stage")))
_add(_op("ROUTE", closed_object({"directive": {"enum": list(ROUTE_DIRECTIVES)}}, ["directive"]), semantics="route the whole design with an admitted directive", source_stage="PLACED", result_stage="ROUTED", mutation_class="implementation", compiler_operation="vivado.route", worker_operation="vivado.route_design", heavy=1, failures=("directive_not_allowed", "invalid_stage")))
_add(_op("PHYS_OPT", {"oneOf": [closed_object({"mode": {"const": "DIRECTIVE"}, "directive": {"enum": list(PHYS_OPT_DIRECTIVES)}}, ["mode", "directive"]), closed_object({"mode": {"const": "OPTIONS"}, "options": {"type": "array", "items": {"enum": list(PHYS_OPT_OPTIONS)}, "minItems": 1, "maxItems": 4, "uniqueItems": True}}, ["mode", "options"])]}, semantics="run exactly one phys_opt_design call in directive or specific-options mode", source_stage="PLACED_OR_ROUTED", result_stage="UNCHANGED", mutation_class="implementation", compiler_operation="vivado.phys_opt", worker_operation="vivado.phys_opt_design", heavy=1, follow_up=("ROUTE if source stage was PLACED",), failures=("phys_opt_mode_invalid", "phys_opt_options_invalid", "phys_opt_option_stage_invalid", "tns_cleanup_requires_slr_crossing_opt")))

for _key, _kind, _limit, _semantics in (
    ("SELECT_CRITICAL_CELLS", "cell", 64, "select path-member cells by critical path rank"),
    ("SELECT_CRITICAL_NETS", "net", 64, "select path-member nets by critical path rank"),
    ("SELECT_HIGH_FANOUT_NETS", "net", 64, "select nets by descending fanout"),
):
    _add(_op(_key, closed_object({"top_k": {"type": "integer", "minimum": 1, "maximum": _limit}}, ["top_k"]), semantics=_semantics, source_stage="UNCHANGED", result_stage="UNCHANGED", target_types=(_kind,), binding=f"required {_kind} binding", mutation_class="read_only_selection", worker_operation=None, no_target="empty_selection", failures=("selector_top_k_invalid", "empty_selection")))
_add(_op("SELECT_CRITICAL_HARDBLOCKS", closed_object({"type": {"enum": ["DSP", "BRAM", "URAM", "ANY"]}, "top_k": {"type": "integer", "minimum": 1, "maximum": 32}}, ["type", "top_k"]), semantics="select path-member hardblocks by rank and resource type", source_stage="UNCHANGED", result_stage="UNCHANGED", target_types=("hardblock",), binding="required hardblock binding", mutation_class="read_only_selection", worker_operation=None, no_target="empty_selection", failures=("hardblock_type_invalid", "empty_selection")))
_add(_op("SELECT_CONGESTED_REGION", closed_object({"region_id": HANDLE_OR_REF}, ["region_id"]), semantics="resolve one published predefined region", source_stage="UNCHANGED", result_stage="UNCHANGED", target_types=("region",), binding="required region binding", mutation_class="read_only_selection", worker_operation=None, no_target="empty_selection"))

_add(_op("CREATE_PBLOCK", closed_object({"predefined_region_id": HANDLE_OR_REF}, ["predefined_region_id"]), semantics="create a pblock from one predefined region", source_stage="ANY", result_stage="UNCHANGED", target_types=("region",), binding="required pblock binding", mutation_class="constraint_mutation", worker_operation="vivado.wp014c_create_pblock", light=1, follow_up=("ASSIGN", "implementation ending in ROUTE")))
_add(_op("RESIZE_PBLOCK", closed_object({"pblock_handle": HANDLE_OR_REF, "region_handle": HANDLE_OR_REF, "mode": {"enum": ["ADD", "REMOVE"]}}, ["pblock_handle", "region_handle", "mode"]), semantics="add or remove one bounded region from a pblock", source_stage="ANY", result_stage="UNCHANGED", target_types=("pblock", "region"), mutation_class="constraint_mutation", worker_operation="vivado.wp014c_resize_pblock", light=1, follow_up=("implementation ending in ROUTE",)))
_add(_op("ASSIGN", closed_object({"target_handle": HANDLE_OR_REF, "pblock_handle": HANDLE_OR_REF}, ["target_handle", "pblock_handle"]), semantics="add typed cells or hardblocks to a pblock", source_stage="ANY", result_stage="UNCHANGED", target_types=("cell", "hardblock", "pblock"), mutation_class="constraint_mutation", worker_operation="vivado.wp014c_assign_pblock", light=1, follow_up=("implementation ending in ROUTE",), no_target="empty_selection"))
_add(_op("UNASSIGN", closed_object({"target_handle": HANDLE_OR_REF, "pblock_handle": HANDLE_OR_REF}, ["target_handle", "pblock_handle"]), semantics="remove typed cells or hardblocks from a pblock", source_stage="ANY", result_stage="UNCHANGED", target_types=("cell", "hardblock", "pblock"), mutation_class="constraint_mutation", worker_operation="vivado.wp014c_unassign_pblock", light=1, follow_up=("implementation ending in ROUTE",), no_target="empty_selection"))
_add(_op("REMOVE_PBLOCK", closed_object({"pblock_handle": HANDLE_OR_REF}, ["pblock_handle"]), semantics="delete one pblock", source_stage="ANY", result_stage="UNCHANGED", target_types=("pblock",), mutation_class="constraint_mutation", worker_operation="vivado.wp014c_delete_pblock", light=1, follow_up=("implementation ending in ROUTE",)))
_add(_op("RELOCATE_HARDBLOCK", closed_object({"typed_target": HANDLE_OR_REF, "destination": HANDLE_OR_REF}, ["typed_target", "destination"]), semantics="relocate typed hardblocks to compatible sites", source_stage="PLACED", result_stage="PLACED", target_types=("hardblock", "site"), mutation_class="placement_mutation", worker_operation="vivado.wp014c_relocate_hardblocks", light=1, follow_up=("ROUTE",), no_target="empty_selection"))
_add(_op("REPLICATE_DRIVER", closed_object({"typed_net": HANDLE_OR_REF, "split_factor": {"type": "integer", "minimum": 2, "maximum": 8}}, ["typed_net", "split_factor"]), semantics="replicate one net driver through RapidWright optimize_fanout with a bounded split factor", source_stage="PLACED_OR_ROUTED", result_stage="PLACED", target_types=("net",), mutation_class="rapidwright_mutation", worker_operation="rapidwright.optimize_fanout", light=1, follow_up=("ROUTE",), no_target="empty_selection"))
_add(_op("REPLICATE_NET", closed_object({"typed_net": HANDLE_OR_REF}, ["typed_net"]), semantics="run phys_opt_design force_replication_on_nets for one typed net", source_stage="PLACED", result_stage="PLACED", target_types=("net",), mutation_class="targeted_implementation", worker_operation="vivado.wp014c_replicate_net", heavy=1, follow_up=("ROUTE",), no_target="empty_selection"))
_add(_op("ROUTE_NET", closed_object({"typed_net": HANDLE_OR_REF, "delay_mode": {"enum": ["DELAY", "AUTO_DELAY"]}}, ["typed_net", "delay_mode"]), semantics="route one typed net with the selected installed delay mode", source_stage="PLACED_OR_ROUTED", result_stage="ROUTED", target_types=("net",), mutation_class="targeted_implementation", worker_operation="vivado.wp014c_route_net", heavy=1, no_target="empty_selection"))
_add(_op("PLACE_CELL", closed_object({"typed_cell": HANDLE_OR_REF, "destination_site": HANDLE_OR_REF}, ["typed_cell", "destination_site"]), semantics="place one typed cell at one compatible site", source_stage="UNPLACED_OR_PLACED", result_stage="PLACED", target_types=("cell", "site"), mutation_class="placement_mutation", worker_operation="vivado.wp014c_place_cell", light=1, follow_up=("ROUTE",), no_target="empty_selection"))
_add(_op("UNPLACE_CELL", closed_object({"typed_cell": HANDLE_OR_REF}, ["typed_cell"]), semantics="unplace one typed cell", source_stage="PLACED", result_stage="PLACED", target_types=("cell",), mutation_class="placement_mutation", worker_operation="vivado.wp014c_unplace_cell", light=1, follow_up=("ROUTE",), no_target="empty_selection"))
_add(_op("SWAP_CELLS", closed_object({"first_cell": HANDLE_OR_REF, "second_cell": HANDLE_OR_REF}, ["first_cell", "second_cell"]), semantics="swap locations of two compatible placed cells", source_stage="PLACED", result_stage="PLACED", target_types=("cell",), mutation_class="placement_mutation", worker_operation="vivado.wp014c_swap_cells", light=1, follow_up=("ROUTE",), no_target="empty_selection"))

for _key, _target, _property, _stage, _worker in (
    ("SET_CELL_LOC_FIXED", "cell", "IS_LOC_FIXED", "PLACED_OR_ROUTED", "vivado.wp014c_set_cell_loc_fixed"),
    ("SET_NET_DONT_TOUCH", "net", "DONT_TOUCH", "ANY", "vivado.wp014c_set_net_dont_touch"),
    ("SET_PBLOCK_EXCLUDE_PLACEMENT", "pblock", "EXCLUDE_PLACEMENT", "UNPLACED", "vivado.wp014c_set_pblock_exclude_placement"),
    ("SET_SITE_PROHIBIT", "site", "PROHIBIT", "UNPLACED", "vivado.wp014c_set_site_prohibit"),
):
    _add(_op(_key, closed_object({"target_handle": HANDLE_OR_REF, "value": BOOL_VALUE}, ["target_handle", "value"]), semantics=f"set the dedicated allowlisted {_target} property {_property}", source_stage=_stage, result_stage="UNCHANGED", target_types=(_target,), mutation_class="allowlisted_property_mutation", worker_operation=_worker, light=1, follow_up=("implementation ending in ROUTE",), no_target="empty_selection"))


P_BINDING_OPS = frozenset(
    key for key, row in P_OPERATION_REGISTRY.items()
    if row["outputs_or_bindings"].startswith("required")
)
P_OPERATION_KEYS = tuple(P_OPERATION_REGISTRY)
P_EXCLUSIVE_OPERATIONS = frozenset(P_OPERATION_KEYS) - {"CALL_MACRO"}

G_GENERATED_NODES = (
    "SELECT_WHERE",
    "FILTER",
    "RANK",
    "TOPK",
    "UNION",
    "INTERSECT",
    "DIFF",
    "CREATE_REGION",
    "ASSIGN_REGION",
    "CHOOSE_SITE",
    "COUPLE_SITES",
    "BATCH_ROUTE_NETS",
    "APPLY_ALLOCATION",
)
G_EXCLUSIVE_NODES = frozenset(G_GENERATED_NODES)

PREDICATE_SCHEMA = {
    "description": "recursive closed predicate; depth <=4 and total nodes <=16",
    "oneOf": [
        closed_object({"op": {"enum": ["AND", "OR"]}, "children": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"$ref": "#/G/$defs/predicate"}}}, ["op", "children"]),
        closed_object({"op": {"const": "NOT"}, "child": {"$ref": "#/G/$defs/predicate"}}, ["op", "child"]),
        closed_object({"op": {"const": "COMPARE"}, "field": {"enum": list(PREDICATE_FIELDS)}, "operator": {"enum": list(COMPARE_OPERATORS)}, "value": {"type": ["string", "integer", "number", "boolean", "array"]}}, ["op", "field", "operator", "value"]),
    ],
}

G_GENERATED_ARGUMENT_SCHEMAS = {
    "SELECT_WHERE": closed_object({"source_type": {"enum": ["cell", "net", "hardblock", "site"]}, "predicate": {"$ref": "#/G/$defs/predicate"}, "top_k": _TOP64}, ["source_type", "predicate", "top_k"]),
    "FILTER": closed_object({"input": REFERENCE_ONLY, "predicate": {"$ref": "#/G/$defs/predicate"}}, ["input", "predicate"]),
    "RANK": closed_object({"input": REFERENCE_ONLY, "field": {"enum": list(PREDICATE_FIELDS)}, "direction": {"enum": ["ASC", "DESC"]}}, ["input", "field", "direction"]),
    "TOPK": closed_object({"input": REFERENCE_ONLY, "k": _TOP64}, ["input", "k"]),
    "UNION": closed_object({"left": REFERENCE_ONLY, "right": REFERENCE_ONLY}, ["left", "right"]),
    "INTERSECT": closed_object({"left": REFERENCE_ONLY, "right": REFERENCE_ONLY}, ["left", "right"]),
    "DIFF": closed_object({"left": REFERENCE_ONLY, "right": REFERENCE_ONLY}, ["left", "right"]),
    "CREATE_REGION": closed_object({key: {"type": "integer"} for key in ("x_min", "x_max", "y_min", "y_max")}, ["x_min", "x_max", "y_min", "y_max"]),
    "ASSIGN_REGION": closed_object({"target_handle": HANDLE_OR_REF, "region_handle": HANDLE_OR_REF}, ["target_handle", "region_handle"]),
    "CHOOSE_SITE": closed_object({"candidate_sites": {"type": "array", "items": HANDLE_OR_REF, "minItems": 1, "maxItems": 32}, "site_index": {"type": "integer", "minimum": 0, "maximum": 31}}, ["candidate_sites", "site_index"]),
    "COUPLE_SITES": closed_object({"hardblocks": REFERENCE_ONLY, "sites": REFERENCE_ONLY, "strategy": {"enum": ["FORWARD", "REVERSE", "ROTATE_ONE"]}}, ["hardblocks", "sites", "strategy"]),
    "BATCH_ROUTE_NETS": closed_object({"typed_nets": REFERENCE_ONLY, "delay_mode": {"enum": ["DELAY", "AUTO_DELAY"]}}, ["typed_nets", "delay_mode"]),
    "APPLY_ALLOCATION": closed_object({"allocation": REFERENCE_ONLY}, ["allocation"]),
}

G_NODE_REGISTRY = {
    "SELECT_WHERE": {
        "node": "SELECT_WHERE", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["SELECT_WHERE"]),
        "short_semantics": "select typed objects using a bounded generated predicate",
        "source_stage": "UNCHANGED", "result_stage": "UNCHANGED", "target_types": ["cell", "net", "hardblock", "site"],
        "outputs_or_bindings": "required typed-object binding", "mutation_class": "generated_read_only_selection",
        "heavy_call_cost": 0, "light_mutation_cost": 0, "scoreability": "requires_scoreable_implementation",
        "required_follow_up": [], "no_target_endpoint": "empty_selection", "common_failure_codes": ["predicate_depth_limit_exceeded", "predicate_node_limit_exceeded", "empty_selection"],
        "admission_status": "REQUIRES_WP014C_PRODUCTION_ADMISSION", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
    "FILTER": {
        "node": "FILTER", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["FILTER"]),
        "short_semantics": "filter one typed collection with a bounded generated predicate",
        "source_stage": "UNCHANGED", "result_stage": "UNCHANGED", "target_types": ["typed collection"],
        "outputs_or_bindings": "required same-type collection binding", "mutation_class": "typed_ssa_filter",
        "heavy_call_cost": 0, "light_mutation_cost": 0, "scoreability": "requires_scoreable_implementation",
        "required_follow_up": [], "no_target_endpoint": "empty_selection", "common_failure_codes": ["empty_selection", "predicate_depth_limit_exceeded", "predicate_node_limit_exceeded"],
        "admission_status": "WP014H_O1_CANDIDATE", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
    "RANK": {
        "node": "RANK", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["RANK"]),
        "short_semantics": "rank one typed collection by one audited public attribute",
        "source_stage": "UNCHANGED", "result_stage": "UNCHANGED", "target_types": ["typed collection"],
        "outputs_or_bindings": "required same-type collection binding", "mutation_class": "typed_ssa_rank",
        "heavy_call_cost": 0, "light_mutation_cost": 0, "scoreability": "requires_scoreable_implementation",
        "required_follow_up": [], "no_target_endpoint": "empty_selection", "common_failure_codes": ["rank_field_unavailable"],
        "admission_status": "WP014H_O1_CANDIDATE", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
    "TOPK": {
        "node": "TOPK", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["TOPK"]),
        "short_semantics": "take a bounded prefix of one typed collection",
        "source_stage": "UNCHANGED", "result_stage": "UNCHANGED", "target_types": ["typed collection"],
        "outputs_or_bindings": "required same-type collection binding", "mutation_class": "typed_ssa_topk",
        "heavy_call_cost": 0, "light_mutation_cost": 0, "scoreability": "requires_scoreable_implementation",
        "required_follow_up": [], "no_target_endpoint": "empty_selection", "common_failure_codes": ["topk_out_of_range"],
        "admission_status": "WP014H_O1_CANDIDATE", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
    **{
        key: {
            "node": key, "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS[key]),
            "short_semantics": f"compute the bounded typed-collection {key.lower()} operation",
            "source_stage": "UNCHANGED", "result_stage": "UNCHANGED", "target_types": ["typed collection"],
            "outputs_or_bindings": "required same-type collection binding", "mutation_class": "typed_ssa_set_operation",
            "heavy_call_cost": 0, "light_mutation_cost": 0, "scoreability": "requires_scoreable_implementation",
            "required_follow_up": [], "no_target_endpoint": "empty_selection", "common_failure_codes": ["collection_type_mismatch", "empty_selection"],
            "admission_status": "WP014H_O1_CANDIDATE", "boundary_eligibility": "G-exclusive",
            "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
        }
        for key in ("UNION", "INTERSECT", "DIFF")
    },
    "CREATE_REGION": {
        "node": "CREATE_REGION", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["CREATE_REGION"]),
        "short_semantics": "generate bounded device-valid rectangular geometry",
        "source_stage": "UNCHANGED", "result_stage": "UNCHANGED", "target_types": ["region"],
        "outputs_or_bindings": "required region binding", "mutation_class": "generated_geometry",
        "heavy_call_cost": 0, "light_mutation_cost": 0, "scoreability": "requires_scoreable_implementation",
        "required_follow_up": ["ASSIGN_REGION", "implementation ending in ROUTE"], "no_target_endpoint": "not_applicable", "common_failure_codes": ["generated_geometry_not_integer", "generated_geometry_out_of_bounds"],
        "admission_status": "REQUIRES_WP014C_PRODUCTION_ADMISSION", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
    "ASSIGN_REGION": {
        "node": "ASSIGN_REGION", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["ASSIGN_REGION"]),
        "short_semantics": "create a pblock for generated geometry and assign typed targets",
        "source_stage": "ANY", "result_stage": "UNCHANGED", "target_types": ["cell", "hardblock", "region"],
        "outputs_or_bindings": "none", "mutation_class": "generated_constraint_mutation",
        "heavy_call_cost": 0, "light_mutation_cost": 2, "scoreability": "requires_scoreable_implementation",
        "required_follow_up": ["implementation ending in ROUTE"], "no_target_endpoint": "empty_selection", "common_failure_codes": ["empty_selection"],
        "admission_status": "REQUIRES_WP014C_PRODUCTION_ADMISSION", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
    "CHOOSE_SITE": {
        "node": "CHOOSE_SITE", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["CHOOSE_SITE"]),
        "short_semantics": "choose one site from a bounded public candidate list",
        "source_stage": "UNCHANGED", "result_stage": "UNCHANGED", "target_types": ["site"],
        "outputs_or_bindings": "required site binding", "mutation_class": "generated_target_choice",
        "heavy_call_cost": 0, "light_mutation_cost": 0, "scoreability": "requires_scoreable_implementation",
        "required_follow_up": ["typed placement or hardblock operation", "ROUTE"], "no_target_endpoint": "empty_selection", "common_failure_codes": ["generated_candidate_sites_invalid", "generated_site_index_invalid"],
        "admission_status": "REQUIRES_WP014C_PRODUCTION_ADMISSION", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
    "COUPLE_SITES": {
        "node": "COUPLE_SITES", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["COUPLE_SITES"]),
        "short_semantics": "construct a deterministic maximum matching from the exact root-bound per-target compatibility relation",
        "source_stage": "ROOT_CAPTURE_STAGE", "result_stage": "UNCHANGED", "target_types": ["hardblock", "site", "allocation"],
        "outputs_or_bindings": "required allocation binding", "mutation_class": "typed_ssa_coupled_allocation",
        "heavy_call_cost": 0, "light_mutation_cost": 0, "scoreability": "requires_scoreable_implementation",
        "required_follow_up": ["UNROUTE if root is routed", "APPLY_ALLOCATION", "ROUTE"], "no_target_endpoint": "empty_selection", "common_failure_codes": ["allocation_requires_multiple_targets", "exact_compatibility_relation_missing", "allocation_exact_compatible_matching_unavailable", "allocation_relation_root_or_stage_stale"],
        "admission_status": "WP014H_O1_CANDIDATE", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
    "BATCH_ROUTE_NETS": {
        "node": "BATCH_ROUTE_NETS", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["BATCH_ROUTE_NETS"]),
        "short_semantics": "reroute a generated collection of two or more nets in one bounded worker dispatch",
        "source_stage": "PLACED_OR_ROUTED", "result_stage": "ROUTED", "target_types": ["net"],
        "outputs_or_bindings": "none", "mutation_class": "generated_batch_targeted_implementation",
        "heavy_call_cost": 1, "light_mutation_cost": 0, "scoreability": "scoreable",
        "required_follow_up": [], "no_target_endpoint": "empty_selection", "common_failure_codes": ["batch_requires_multiple_targets", "batch_route_no_effect"],
        "admission_status": "WP014H_O1_CANDIDATE", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
    "APPLY_ALLOCATION": {
        "node": "APPLY_ALLOCATION", "argument_schema": deepcopy(G_GENERATED_ARGUMENT_SCHEMAS["APPLY_ALLOCATION"]),
        "short_semantics": "revalidate and apply exact compatibility-proven target/site pairs in one bounded worker dispatch",
        "source_stage": "PLACED", "result_stage": "PLACED", "target_types": ["allocation"],
        "outputs_or_bindings": "none", "mutation_class": "generated_coupled_placement_mutation",
        "heavy_call_cost": 0, "light_mutation_cost": 1, "scoreability": "requires_scoreable_implementation",
        "required_follow_up": ["ROUTE"], "no_target_endpoint": "empty_selection", "common_failure_codes": ["allocation_resolution_mismatch", "allocation_pair_not_in_exact_relation", "allocation_relation_stale_after_placement_mutation", "relocation_no_effect"],
        "admission_status": "WP014H_O1_CANDIDATE", "boundary_eligibility": "G-exclusive",
        "source_evidence_sha256": list(SOURCE_EVIDENCE.values()),
    },
}


def argument_schemas() -> dict[str, dict[str, Any]]:
    return {key: deepcopy(row["argument_schema"]) for key, row in P_OPERATION_REGISTRY.items()}


def worker_meanings() -> dict[str, dict[str, Any]]:
    """Return exact worker meanings expected for non-M/non-selector operations."""

    return {
        key: {
            "worker_operation": row["worker_operation"],
            "compiler_operation": row["compiler_operation"],
            "argument_schema": deepcopy(row["argument_schema"]),
            "heavy_call_cost": row["heavy_call_cost"],
            "light_mutation_cost": row["light_mutation_cost"],
        }
        for key, row in P_OPERATION_REGISTRY.items()
        if row["worker_operation"] is not None
    }


def public_registry() -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "action_surface_version": ACTION_SURFACE_VERSION,
        "target_environment": TARGET_ENVIRONMENT,
        "authority_rule": "installed Vivado help is authoritative for executable-current in the frozen environment",
        "source_evidence": dict(SOURCE_EVIDENCE),
        "limits": {"max_program_statements": 8, "max_actual_heavy_calls": 4, "max_light_mutations": 8},
        "M": {"actions": list(M_ACTION_KEYS), "frozen": True},
        "P": {"operations": deepcopy(P_OPERATION_REGISTRY)},
        "G": {"inherits": ["M", "P"], "generated_nodes": deepcopy(G_NODE_REGISTRY), "new_mechanism_families": False},
        "excluded": {
            "raw_tcl": "FORBIDDEN",
            "generic_set_property": "FORBIDDEN",
            "rqs": "OUTSIDE_FORMAL_PROTOCOL",
            "place_design.AggressiveExplore": "NEEDS_FURTHER_EVIDENCE",
            "official_only_place_directives": "DEVICE_OR_FLOW_CONDITIONAL",
        },
    }


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def registry_sha256() -> str:
    return hashlib.sha256(canonical_json_bytes(public_registry())).hexdigest()


__all__ = [
    "ACTION_SURFACE_VERSION", "COMPARE_OPERATORS", "G_EXCLUSIVE_NODES",
    "G_GENERATED_ARGUMENT_SCHEMAS", "G_GENERATED_NODES", "G_NODE_REGISTRY", "HANDLE_OR_REF", "REFERENCE_ONLY",
    "M_ACTION_KEYS", "PREDICATE_FIELDS", "PREDICATE_SCHEMA", "P_BINDING_OPS",
    "PHYS_OPT_DIRECTIVES", "PHYS_OPT_OPTIONS", "PHYS_OPT_PLACED_OPTIONS",
    "PHYS_OPT_POST_ROUTE_OPTIONS", "PLACE_DIRECTIVES", "PLACE_NET_DELAY_WEIGHTS",
    "P_EXCLUSIVE_OPERATIONS", "P_OPERATION_KEYS", "P_OPERATION_REGISTRY",
    "REGISTRY_SCHEMA_VERSION", "ROUTE_DIRECTIVES", "SOURCE_EVIDENCE",
    "argument_schemas", "closed_object", "public_registry", "registry_sha256",
    "worker_meanings",
]
