"""Immutable Gate-A specifications for the WP-013 production protocol.

Every specification is plain canonical JSON with its own version and digest.
The bundle deliberately separates model-visible action documentation from the
development-panel manifest, which contains development asset identities and is
never sent to a policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


SPEC_BUNDLE_VERSION = "dof.single-submission.protocol-freeze.v1"
M_LANGUAGE_V1 = "dof.single-submission.m.v1"
P_LANGUAGE_V1 = "dof.single-submission.p.v1"
G_AST_LANGUAGE_V1 = "dof.single-submission.g-ast.v1"
COMMON_OBSERVATION_O_V1 = "dof.single-submission.observation-o.v1"
COMMON_QUERY_Q_V1 = "dof.single-submission.query-q.v1"
SINGLE_SUBMISSION_PLANNER_V1 = "dof.single-submission.planner.v1"
CONSTRUCTION_VALIDATOR_V1 = "dof.single-submission.construction-validator.v1"
DEVELOPMENT_MACRO_PANEL_V1 = "dof.single-submission.development-panel.v1"

MAX_PROGRAM_STEPS = 8
MAX_HEAVY_CALLS = 4
MAX_LIGHT_MUTATIONS = 8
MAX_PREDICATE_DEPTH = 4
MAX_PREDICATE_NODES = 16


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        canonical_json_bytes(value)
        return value
    raise TypeError(f"not canonical JSON: {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class FrozenProtocolSpec:
    name: str
    version: str
    model_visible: bool
    body: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ValueError("spec name and version are required")
        object.__setattr__(self, "body", _freeze(self.body))

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "model_visible": self.model_visible,
            "body": _thaw(self.body),
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.payload())

    def manifest_row(self) -> dict[str, Any]:
        return self.payload() | {"sha256": self.sha256}


def _macro(
    key: str,
    mechanism: str,
    semantics: str,
    stages: list[str],
    transition: str,
    heavy_range: tuple[int, int],
    light_range: tuple[int, int],
    failure: str,
) -> dict[str, Any]:
    return {
        "action_key": f"macro.{key}",
        "mechanism": mechanism,
        "semantics": semantics,
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "preconditions": {"allowed_stages": stages, "root_is_read_only": True},
        "possible_state_transition": transition,
        "cost": {
            "actual_heavy_call_range": list(heavy_range),
            "actual_light_mutation_range": list(light_range),
            "scoreable_implementation": True,
            "all_host_calls_recorded": True,
        },
        "required_follow_up": "none; wrapper publishes exactly one canonical terminal",
        "failure_no_target_semantics": failure,
        "runtime_binding": {"dispatcher": "xilinx.run_test_mode", "flow": key},
    }


def _m_spec() -> FrozenProtocolSpec:
    actions = [
        _macro(
            "phys_opt",
            "physical_optimization",
            "Run the admitted two-pass Vivado physical-optimization wrapper and route the result.",
            ["PLACED", "ROUTED"],
            "PLACED_OR_ROUTED -> ROUTED",
            (3, 3),
            (0, 0),
            "tool/validation failures are terminal; this action has no target-empty endpoint",
        ),
        _macro(
            "place_route",
            "placement_routing",
            "Root-preserving unroute/unplace preparation followed by one place and one route call.",
            ["PLACED", "ROUTED"],
            "PLACED_OR_ROUTED -> ROUTED",
            (2, 2),
            (0, 2),
            "tool/validation failures are terminal; already-unplaced/unrouted preparation is an explicit no-op",
        ),
        _macro(
            "auto_pblock",
            "report_driven_spatial_constraint",
            "Derive a device-bounded pblock from timing/fabric reports, constrain selected cells, place and route.",
            ["ROUTED"],
            "ROUTED -> ROUTED",
            (2, 2),
            (1, 1),
            "empty report-derived target is NO_TARGET; invalid geometry or tool failure is terminal",
        ),
        _macro(
            "retime",
            "register_retiming",
            "Apply admitted retiming-oriented placement/physical optimisation and route.",
            ["PLACED", "ROUTED"],
            "PLACED_OR_ROUTED -> ROUTED",
            (3, 3),
            (0, 0),
            "tool/validation failures are terminal; no target-empty endpoint",
        ),
        _macro(
            "net_swap",
            "equivalent_driver_and_pin_rewire",
            "Apply admitted critical-pin/equivalent-driver optimisation, route, then routed cleanup.",
            ["PLACED", "ROUTED"],
            "PLACED_OR_ROUTED -> ROUTED",
            (3, 3),
            (0, 0),
            "tool/validation failures are terminal; no target-empty endpoint",
        ),
        _macro(
            "memory_path_opt",
            "memory_path_physical_optimization",
            "Apply BRAM/DSP/URAM register and memory-path physical optimisation, route, then cleanup.",
            ["PLACED", "ROUTED"],
            "PLACED_OR_ROUTED -> ROUTED",
            (3, 3),
            (0, 0),
            "unsupported options, tool failures and validation failures are terminal",
        ),
        _macro(
            "hardblock_relocate",
            "hardblock_aware_physical_optimization",
            "Sample critical hardblocks and run the admitted hardblock-aware placement/physical-optimisation wrapper.",
            ["PLACED", "ROUTED"],
            "PLACED_OR_ROUTED -> ROUTED",
            (3, 3),
            (0, 0),
            "zero sampled critical hardblocks is recorded but the generic wrapper still runs; tool/validation failures are terminal",
        ),
        _macro(
            "congestion_spread",
            "congestion_aware_spreading",
            "Measure congestion and apply placement/routing physical optimisation intended to spread congested logic.",
            ["PLACED", "ROUTED"],
            "PLACED_OR_ROUTED -> ROUTED",
            (2, 2),
            (0, 0),
            "missing congestion detail is recorded and does not invent targets; tool/validation failures are terminal",
        ),
    ]
    conditional = [
        {
            "action_key": "macro.fanout",
            "admission_order": 1,
            "mechanism": "driver_replication",
            "status": "CONDITIONAL_NOT_ADMITTED",
            "admission_requires": [
                "faithful high-fanout-net target selection",
                "non-empty relevant-root effect",
                "no routed-pin/netlist corruption",
            ],
        },
        {
            "action_key": "macro.lut_cone",
            "admission_order": 2,
            "mechanism": "lut_input_cone_restructure",
            "status": "CONDITIONAL_NOT_ADMITTED",
            "admission_requires": [
                "faithful critical-LUT-cone selection",
                "non-empty relevant-root effect",
                "no routed-pin/netlist corruption",
            ],
        },
    ]
    return FrozenProtocolSpec(
        "M_LANGUAGE_V1",
        M_LANGUAGE_V1,
        True,
        {
            "contract": "choose exactly one complete root-preserving Macro",
            "guaranteed_actions": actions,
            "conditional_actions": conditional,
            "mandatory_exclusions": [
                "pblock",
                "vexriscv",
                "*_rw",
                "auto_top3",
                "rescue_place_reset_retime",
                "synthesized_recipe",
                "manual_action",
            ],
            "identity_rules": [
                "no benchmark, filename, family, historical winner, or outcome lookup",
                "the input root is immutable and each action writes a distinct child",
                "every implementation, mutation and read-only call is host-accounted",
            ],
        },
    )


def _p_spec() -> FrozenProtocolSpec:
    return FrozenProtocolSpec(
        "P_LANGUAGE_V1",
        P_LANGUAGE_V1,
        True,
        {
            "contract": "closed-world typed straight-line program with one scoreable terminal",
            "canonical_syntax": {
                "program": {"language": "P", "steps": "ordered array"},
                "step": {"step_id": "s1..s8 unique in source order", "op": "allowlisted operation", "args": "closed operation-specific object", "bind": "optional local name for selector/constructor output"},
                "reference": {"ref": "previous bind name"},
                "direct_handle": "a typed handle returned by common Q may replace a reference where the type matches",
                "unknown_fields": "rejected",
            },
            "limits": {
                "max_steps": MAX_PROGRAM_STEPS,
                "max_actual_heavy_calls": MAX_HEAVY_CALLS,
                "max_light_mutations": MAX_LIGHT_MUTATIONS,
                "loops": 0,
                "recursion": 0,
            },
            "operations": [
                {"op": "CALL_MACRO", "parameters": {"macro": "frozen M action key"}, "stages": ["ANY_SUPPORTED"], "cost": "exact inherited Macro cost", "transition": "exact inherited Macro transition", "scoreable": True, "follow_up": "none", "empty_failure": "inherited"},
                {"op": "UNROUTE", "parameters": {}, "stages": ["ROUTED"], "cost": {"heavy": 0, "light": 1}, "transition": "ROUTED -> PLACED", "scoreable": False, "follow_up": "ROUTE required before COMMIT terminal", "empty_failure": "already unrouted is explicit NO_EFFECT"},
                {"op": "UNPLACE", "parameters": {}, "stages": ["PLACED"], "cost": {"heavy": 0, "light": 1}, "transition": "PLACED -> UNPLACED", "scoreable": False, "follow_up": "PLACE then ROUTE required", "empty_failure": "already unplaced is explicit NO_EFFECT"},
                {"op": "PLACE", "parameters": {"directive": ["Default", "Explore", "ExtraNetDelay_high"]}, "stages": ["UNPLACED", "PLACED"], "cost": {"heavy": 1, "light": 0}, "transition": "UNPLACED_OR_PLACED -> PLACED", "scoreable": False, "follow_up": "ROUTE required", "empty_failure": "tool failure terminal"},
                {"op": "ROUTE", "parameters": {"directive": ["Default", "Explore", "AggressiveExplore"]}, "stages": ["PLACED"], "cost": {"heavy": 1, "light": 0}, "transition": "PLACED -> ROUTED", "scoreable": True, "follow_up": "none", "empty_failure": "tool/invalid-route failure terminal"},
                {"op": "PHYS_OPT", "parameters": {"directive": ["Default", "Explore", "AggressiveExplore", "AlternateFlowWithRetiming"], "options": ["AddRetime", "critical_cell_opt", "critical_pin_opt", "placement_opt", "routing_opt", "dsp_register_opt", "bram_register_opt", "uram_register_opt"]}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 1, "light": 0}, "transition": "stage preserved; routing may require ROUTE", "scoreable": "only when final route is valid", "follow_up": "ROUTE if routing is invalidated", "empty_failure": "unsupported option or tool failure terminal"},
                {"op": "SELECT_CRITICAL_CELLS", "parameters": {"top_k": [1, 64]}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 0, "light": 0}, "transition": "none", "scoreable": False, "follow_up": "returns typed cell handles", "empty_failure": "EMPTY_SELECTION"},
                {"op": "SELECT_CRITICAL_NETS", "parameters": {"top_k": [1, 64]}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 0, "light": 0}, "transition": "none", "scoreable": False, "follow_up": "returns typed net handles", "empty_failure": "EMPTY_SELECTION"},
                {"op": "SELECT_HIGH_FANOUT_NETS", "parameters": {"top_k": [1, 64]}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 0, "light": 0}, "transition": "none", "scoreable": False, "follow_up": "returns nets ordered by fanout then canonical identity", "empty_failure": "EMPTY_SELECTION"},
                {"op": "SELECT_CRITICAL_HARDBLOCKS", "parameters": {"type": ["DSP", "BRAM", "URAM", "ANY"], "top_k": [1, 32]}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 0, "light": 0}, "transition": "none", "scoreable": False, "follow_up": "returns compatible typed hardblock handles", "empty_failure": "EMPTY_SELECTION"},
                {"op": "SELECT_CONGESTED_REGION", "parameters": {"region_id": "precomputed region handle"}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 0, "light": 0}, "transition": "none", "scoreable": False, "follow_up": "returns one region handle", "empty_failure": "UNKNOWN_OR_EMPTY_REGION"},
                {"op": "CREATE_PBLOCK", "parameters": {"predefined_region_id": "device-derived region handle"}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 0, "light": 1}, "transition": "stage preserved", "scoreable": False, "follow_up": "returns a trial-owned pblock handle", "empty_failure": "invalid geometry fails before mutation"},
                {"op": "ASSIGN", "parameters": {"target_handle": "cell-set handle", "pblock_handle": "pblock handle"}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 0, "light": 1}, "transition": "stage preserved; placement constraint added", "scoreable": False, "follow_up": "PLACE/PHYS_OPT and ROUTE required", "empty_failure": "empty/stale/wrong-type handle fails before mutation"},
                {"op": "REMOVE_PBLOCK", "parameters": {"pblock_handle": "trial-owned pblock handle"}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 0, "light": 1}, "transition": "stage preserved", "scoreable": False, "follow_up": "ROUTE if invalidated", "empty_failure": "unknown/non-owned handle fails"},
                {"op": "RELOCATE_HARDBLOCK", "parameters": {"typed_target": "non-empty hardblock handle", "destination": "compatible bounded region or site handle"}, "stages": ["PLACED", "ROUTED"], "cost": {"heavy": 0, "light": 1}, "transition": "stage becomes PLACED", "scoreable": False, "follow_up": "ROUTE required", "empty_failure": "no compatible destination is NO_TARGET; stale/type mismatch fails"},
            ],
            "conditional_operations": [
                {"op": "REPLICATE_DRIVER", "enabled_only_if": "macro.fanout admitted", "parameters": {"typed_net": "non-empty high-fanout net handle", "copies": [1, 4], "max_loads_per_copy": [2, 128]}, "cost": {"heavy": 0, "light": 1}, "follow_up": "PLACE and ROUTE required"}
            ],
            "safety": ["no raw Tcl", "no process, filesystem, network, Python or repository capability"],
        },
    )


def _g_spec() -> FrozenProtocolSpec:
    return FrozenProtocolSpec(
        "G_AST_LANGUAGE_V1",
        G_AST_LANGUAGE_V1,
        True,
        {
            "contract": "compiled bounded AST containing every frozen M and P intervention",
            "canonical_syntax": {
                "program": {"language": "G", "statements": "ordered array"},
                "statement": {"statement_id": "g1..g8 unique in source order", "node": "inherited or generated node", "args": "closed node-specific object", "bind": "optional local name"},
                "reference": {"ref": "previous bind name"},
                "predicate": "closed recursive node object with op and allowlisted operands",
                "unknown_fields": "rejected",
            },
            "limits": {
                "max_statements": MAX_PROGRAM_STEPS,
                "max_actual_heavy_calls": MAX_HEAVY_CALLS,
                "max_light_mutations": MAX_LIGHT_MUTATIONS,
                "max_predicate_depth": MAX_PREDICATE_DEPTH,
                "max_predicate_nodes": MAX_PREDICATE_NODES,
                "loops": 0,
                "recursion": 0,
            },
            "inherited_nodes": ["CALL_MACRO", "P_PROGRAM"],
            "predicate_ast": {
                "nodes": ["AND", "OR", "NOT", "COMPARE"],
                "fields": ["object_type", "resource_type", "path_member", "path_rank", "slack_ns", "delay_ns", "fanout", "slr_id", "clock_region_id", "congestion_class"],
                "operators": ["EQ", "NE", "LT", "LE", "GT", "GE", "IN"],
                "domains": {"numeric": "finite values within current O/Q bounds", "string": "values enumerated by current O/Q schema"},
                "evaluation": "full tree, left-to-right canonical child ordering; filter frozen query rows; ties by canonical private identity before pseudonymisation",
                "empty": "EMPTY_SELECTION; never substitute another selector",
            },
            "generated_nodes": [
                {"node": "SELECT_WHERE", "semantics": "execute the entire predicate AST over an explicitly typed frozen Q result", "scoreable": False},
                {"node": "CREATE_REGION", "semantics": "create device-bounded rectangular ranges from visible SLR/clock-region coordinate bounds", "scoreable": False},
                {"node": "ASSIGN_REGION", "semantics": "assign a non-empty selected cell set to generated geometry", "scoreable": False, "follow_up": "implementation and valid route required"},
                {"node": "RELOCATE_HARDBLOCK", "semantics": "move selected compatible hardblocks to generated bounded region/sites", "scoreable": False, "follow_up": "valid route required"},
            ],
            "conditional_nodes": ["REPLICATE_DRIVER when fanout is admitted"],
            "generated_domains": ["predicate thresholds", "top-k", "device-bounded pblock coordinates", "compatible hardblock region/site parameters", "fanout parameters when admitted"],
            "forbidden": ["raw Tcl", "shell/process", "network/socket", "arbitrary filesystem", "repository", "source/package loading", "arbitrary Python", "loops", "recursion", "scorer/validator/protocol mutation", "hidden host calls"],
            "canonicalization": "UTF-8 canonical JSON; sorted object keys; source-order statements; finite numbers; SHA-256 binding",
        },
    )


def _o_spec() -> FrozenProtocolSpec:
    return FrozenProtocolSpec(
        "COMMON_OBSERVATION_O_V1",
        COMMON_OBSERVATION_O_V1,
        True,
        {
            "fields": {
                "objective": ["target_clock_handle", "clock_period_ns"],
                "timing": ["fmax_mhz", "wns_ns", "tns_ns", "failing_endpoint_count"],
                "utilization": ["lut", "ff", "dsp", "bram", "uram", "percentages_when_available"],
                "implementation": ["placement_state", "route_state", "route_validity"],
                "constraints": ["pblock_count", "constrained_cell_count", "constraint_summary"],
                "geometry": ["device_class", "slr_bounds", "clock_region_bounds"],
                "congestion": ["coarse_class", "hotspot_count", "maximum_overuse_when_available"],
                "critical_paths": ["count", "top_path_slack_delay_resource_summary"],
                "budget": ["planning_turns", "queries", "feedback_rounds", "heavy_calls", "light_mutations", "wall_time_seconds", "usd"],
            },
            "determinism": "same root, trial salt and tool versions produce identical canonical bytes for L2/L3/L4",
            "truncation": "top paths by slack ascending then canonical private identity; maximum 8 summary rows",
            "privacy": ["no path", "no benchmark/family/name", "no historical outcome or winner", "no raw object names"],
            "private_evidence": "full raw reports and identity maps are hash-bound but never inserted into the prompt",
        },
    )


def _q_spec() -> FrozenProtocolSpec:
    return FrozenProtocolSpec(
        "COMMON_QUERY_Q_V1",
        COMMON_QUERY_Q_V1,
        True,
        {
            "max_queries_per_session": 8,
            "queries": [
                {"name": "critical_path_details", "top_k": [1, 32], "returns": "path handles, bounded metrics and typed object handles"},
                {"name": "critical_object_locations", "top_k": [1, 64], "returns": "typed handles and device-coordinate pseudonyms"},
                {"name": "high_fanout_net_details", "top_k": [1, 64], "returns": "net handles, fanout and bounded driver/load summaries"},
                {"name": "congested_region_details", "top_k": [1, 16], "returns": "region handles, bounds and coarse congestion values"},
                {"name": "critical_hardblock_candidates", "top_k": [1, 32], "returns": "typed hardblock and compatible region/site handles"},
                {"name": "pblock_constraint_details", "top_k": [1, 32], "returns": "pblock handles, geometry and bounded membership counts"},
                {"name": "placement_summary", "top_k": [1, 32], "returns": "region occupancy summaries"},
                {"name": "route_summary", "top_k": [1, 32], "returns": "route status and bounded critical-net summaries"},
            ],
            "ordering": "primary query metric then canonical private identity; truncate after ordering",
            "handles": "HMAC-SHA256 per-trial pseudonyms with type prefix; stable across turns; fail closed across trials, roots and types",
            "identity_map": "Vivado canonical object is authoritative; RapidWright rows require an explicit one-to-one validated map or are unavailable",
            "evidence": ["query latency", "tool call IDs", "canonical result byte count", "raw private result hash", "truncation and missing reasons"],
        },
    )


def _planner_spec() -> FrozenProtocolSpec:
    return FrozenProtocolSpec(
        "SINGLE_SUBMISSION_PLANNER_V1",
        SINGLE_SUBMISSION_PLANNER_V1,
        True,
        {
            "objective": "preserve mandatory validity and maximise final best valid Fmax under the frozen execution budget",
            "limits": {"model_turns": 4, "read_only_queries": 8, "construction_feedback_rounds": 3, "valid_commits": 1, "post_commit_qor_resubmissions": 0, "provider_retry": 0, "provider_fallback": 0},
            "model_messages": ["QUERY", "DRAFT", "VALIDATE", "COMMIT", "STOP", "NO_VALID_SUBMISSION"],
            "system_owned": ["decision_id", "proposal_id", "request_id", "catalogue_version", "catalogue_hash", "root_binding", "context_binding", "provider_binding", "model_binding", "route_binding"],
            "pre_commit_allowed": ["read-only O/Q", "schema validation", "static compilation", "target/handle resolution", "stage/geometry/budget/containment validation"],
            "pre_commit_forbidden": ["real design mutation", "candidate Fmax/WNS/TNS", "relative QoR", "real optimisation result"],
            "terminal": "one valid COMMIT is immutable and executed once; otherwise STOP/NO_VALID_SUBMISSION",
        },
    )


def _validator_spec() -> FrozenProtocolSpec:
    return FrozenProtocolSpec(
        "CONSTRUCTION_VALIDATOR_V1",
        CONSTRUCTION_VALIDATOR_V1,
        False,
        {
            "checks_in_order": ["canonical JSON/schema", "language/catalogue binding", "root/context binding", "operation allowlist", "parameter domain", "handle existence/type/root/trial/freshness", "selection non-emptiness", "stage transition", "geometry/device bounds", "static heavy/light budget", "containment", "single scoreable terminal"],
            "feedback_allowlist": ["JSON/schema error", "unknown/stale/empty target", "invalid coordinate/region", "invalid stage/order", "forbidden operation", "static budget overflow", "compile/containment failure"],
            "feedback_denylist": ["Fmax", "WNS", "TNS", "relative QoR", "candidate ranking", "real trial outcome"],
            "mutation_before_commit": False,
            "fail_closed": True,
            "accounting": "compiler emits exact host-call plan, selected/assigned/transformed counts and canonical bytes before dispatch",
        },
    )


def _panel_spec() -> FrozenProtocolSpec:
    return FrozenProtocolSpec(
        "DEVELOPMENT_MACRO_PANEL_V1",
        DEVELOPMENT_MACRO_PANEL_V1,
        False,
        {
            "candidate_roots": ["amd_mini_isp", "corescore_500_mod", "finn_radioml", "ispd16_example2", "logicnets_jscl", "rosetta_3d-rendering", "rosetta_digit-recognition", "rosetta_optical-flow", "rosetta_spam-filter", "vexriscv_re-place"],
            "long_runtime_exclusions": ["boom_soc", "vtr_mcml"],
            "minimum_eligible_roots": 8,
            "target_eligible_roots": 10,
            "max_jobs": 100,
            "job_rule": "every frozen M exactly once from each unchanged eligible root",
            "reference": "the root is the no-op reference and is never overwritten",
            "common_path": ["same scorer", "same validator", "same Macro wrapper", "one canonical terminal"],
            "scheduling": {"weekday_max_fresh": 8, "weekend_max_fresh": 16},
            "selection_metric": "LOBO/LOFO mean and median valid Fmax regret to development Macro oracle",
            "heuristic_candidates": ["global ranking", "small additive mechanism scorecard", "optional transparent depth-2 decision list/tree"],
            "complexity_gate": {"minimum_mean_regret_improvement_fraction": 0.10, "maximum_fraction_worsened_folds": 0.40, "ties": "simpler"},
            "identity_leakage_forbidden": ["benchmark name", "filename", "family identifier", "historical winner"],
        },
    )


def build_protocol_freeze() -> tuple[FrozenProtocolSpec, ...]:
    return (_m_spec(), _p_spec(), _g_spec(), _o_spec(), _q_spec(), _planner_spec(), _validator_spec(), _panel_spec())


def protocol_freeze_payload() -> dict[str, Any]:
    specs = build_protocol_freeze()
    return {
        "schema_version": SPEC_BUNDLE_VERSION,
        "specifications": [spec.manifest_row() for spec in specs],
        "cross_contract_invariants": {
            "single_submission": True,
            "common_o_q_for": ["L2", "L3", "L4"],
            "semantic_nesting": ["L2 in L3 byte-identical", "L3 in L4 byte-identical"],
            "system_owned_binding": True,
            "pre_commit_qor": False,
            "root_preserving": True,
            "raw_tool_escape": False,
        },
    }


def protocol_freeze_bytes() -> bytes:
    return canonical_json_bytes(protocol_freeze_payload())


def protocol_freeze_hash() -> str:
    return hashlib.sha256(protocol_freeze_bytes()).hexdigest()


__all__ = [name for name in globals() if name.endswith("_V1")] + [
    "SPEC_BUNDLE_VERSION",
    "MAX_PROGRAM_STEPS",
    "MAX_HEAVY_CALLS",
    "MAX_LIGHT_MUTATIONS",
    "MAX_PREDICATE_DEPTH",
    "MAX_PREDICATE_NODES",
    "FrozenProtocolSpec",
    "build_protocol_freeze",
    "canonical_json_bytes",
    "protocol_freeze_bytes",
    "protocol_freeze_hash",
    "protocol_freeze_payload",
    "sha256_json",
]
