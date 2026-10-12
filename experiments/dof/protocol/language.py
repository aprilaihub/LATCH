"""Production P/G compiler and construction validator for WP-013."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .action_surface_v3 import (
    COMPARE_OPERATORS,
    G_EXCLUSIVE_NODES,
    G_GENERATED_NODES,
    PHYS_OPT_DIRECTIVES,
    PHYS_OPT_OPTIONS,
    PHYS_OPT_PLACED_OPTIONS,
    PHYS_OPT_POST_ROUTE_OPTIONS,
    PLACE_DIRECTIVES,
    PLACE_NET_DELAY_WEIGHTS,
    PREDICATE_FIELDS,
    P_OPERATION_KEYS,
    ROUTE_DIRECTIVES,
)
from .handles import HandleError, PrivateObject, TrialHandleRegistry
from optimizer_pipeline.dof.hardblock_compatibility import (
    build_allocation, validate_allocation, validate_relation,
)
from .macro_authority import macro_definition
from .specs import (
    CONSTRUCTION_VALIDATOR_V1,
    G_AST_LANGUAGE_V1,
    MAX_HEAVY_CALLS,
    MAX_LIGHT_MUTATIONS,
    MAX_PREDICATE_DEPTH,
    MAX_PREDICATE_NODES,
    MAX_PROGRAM_STEPS,
    P_LANGUAGE_V1,
    canonical_json_bytes,
)


_STAGES = frozenset({"UNPLACED", "PLACED", "ROUTED"})
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_P_OPS = frozenset(P_OPERATION_KEYS)
_LEGACY_P_OPS = frozenset(
    {
        "CALL_MACRO", "UNROUTE", "UNPLACE", "PLACE", "ROUTE", "PHYS_OPT",
        "SELECT_CRITICAL_CELLS", "SELECT_CRITICAL_NETS", "SELECT_HIGH_FANOUT_NETS",
        "SELECT_CRITICAL_HARDBLOCKS", "SELECT_CONGESTED_REGION", "CREATE_PBLOCK",
        "ASSIGN", "REMOVE_PBLOCK", "RELOCATE_HARDBLOCK", "REPLICATE_DRIVER",
    }
)
_P_SELECTORS = frozenset(
    {"SELECT_CRITICAL_CELLS", "SELECT_CRITICAL_NETS", "SELECT_HIGH_FANOUT_NETS", "SELECT_CRITICAL_HARDBLOCKS", "SELECT_CONGESTED_REGION"}
)
_P_DIRECTIVES = {
    "PLACE": frozenset(PLACE_DIRECTIVES),
    "ROUTE": frozenset(ROUTE_DIRECTIVES),
    "PHYS_OPT": frozenset(PHYS_OPT_DIRECTIVES),
}
_PHYS_OPTIONS = frozenset(PHYS_OPT_OPTIONS)
_PHYS_POST_ROUTE_OPTIONS = frozenset(PHYS_OPT_POST_ROUTE_OPTIONS)
_PHYS_PLACED_OPTIONS = frozenset(PHYS_OPT_PLACED_OPTIONS)
_PREDICATE_FIELDS = frozenset(PREDICATE_FIELDS)
_COMPARE_OPS = frozenset(COMPARE_OPERATORS)
_PLACEMENT_MUTATION_OPERATIONS = frozenset({
    "vivado.unplace", "vivado.place", "vivado.wp014c_place_cell",
    "vivado.wp014c_unplace_cell", "vivado.wp014c_swap_cells",
    "vivado.relocate_hardblock", "vivado.wp014c_relocate_hardblocks",
    "vivado.wp014h_apply_allocation",
})


class ConstructionError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class HostStep:
    operation: str
    event_class: str
    public_args: Mapping[str, Any]
    private_args: Mapping[str, Any]
    selected_count: int = 0
    transformed_count: int = 0
    heavy_call_cost: int = 0
    light_mutation_cost: int = 0
    source_construct: str | None = None
    source_step_id: str | None = None
    input_bindings: tuple[tuple[str, str], ...] = ()

    def public(self) -> dict[str, Any]:
        return {"operation": self.operation, "event_class": self.event_class, "args": dict(self.public_args), "selected_count": self.selected_count, "transformed_count": self.transformed_count, "heavy_call_cost": self.heavy_cost, "light_mutation_cost": self.light_cost, "source_construct": self.source_construct, "source_step_id": self.source_step_id, "input_bindings": [{"input_slot": slot, "binding_id": binding} for slot, binding in self.input_bindings]}

    def private(self) -> dict[str, Any]:
        return self.public() | {"args": dict(self.private_args)}

    @property
    def heavy_cost(self) -> int:
        return self.heavy_call_cost or int(self.event_class == "IMPLEMENTATION_HEAVY")

    @property
    def light_cost(self) -> int:
        return self.light_mutation_cost or int(self.event_class == "MUTATION_LIGHT")


@dataclass(frozen=True, slots=True)
class CompiledProgram:
    language: str
    source_sha256: str
    initial_stage: str
    final_stage: str
    host_steps: tuple[HostStep, ...]
    heavy_calls: int
    light_mutations: int
    selected_count: int
    transformed_count: int
    public_bytes: bytes
    public_sha256: str
    private_plan_sha256: str
    binding_records: tuple[Mapping[str, Any], ...] = ()

    def public_payload(self) -> dict[str, Any]:
        return json.loads(self.public_bytes)


@dataclass(frozen=True, slots=True)
class CompileContext:
    registry: TrialHandleRegistry
    initial_stage: str
    predefined_regions: tuple[str, ...] = ()
    geometry_bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    admitted_macros: tuple[str, ...] = (
        "phys_opt", "place_route", "auto_pblock", "retime", "net_swap",
        "memory_path_opt", "hardblock_relocate", "congestion_spread",
    )
    fanout_admitted: bool = False
    action_surface_version: str = "legacy.v2"
    hardblock_compatibility_relation: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.initial_stage not in _STAGES:
            raise ValueError("initial_stage_invalid")
        if self.action_surface_version not in {"legacy.v2", "v3"}:
            raise ValueError("action_surface_version_invalid")


def _macro_steps(name: str) -> tuple[HostStep, ...]:
    try:
        definition = macro_definition(name)
    except ValueError as exc:
        raise ConstructionError("macro_not_admitted") from exc
    arguments: dict[str, Any] = {
        "action_key": definition.action_key,
        "flow_name": definition.flow_name,
    }
    if definition.flow_name in {"memory_path_opt", "hardblock_relocate"}:
        arguments["stage_preparation"] = "place_design"
    return (
        HostStep(
            "macro.execute",
            "MACRO_COMPOSITE",
            arguments,
            arguments,
            heavy_call_cost=definition.heavy_call_cost,
            light_mutation_cost=definition.light_mutation_cost,
            source_construct="CALL_MACRO",
        ),
    )


def compile_macro(name: str, context: CompileContext) -> CompiledProgram:
    if name not in context.admitted_macros:
        raise ConstructionError("macro_not_admitted")
    return _finish("M", {"language": "M", "macro": name}, context.initial_stage, "ROUTED", list(_macro_steps(name)))


class _State:
    def __init__(self, context: CompileContext) -> None:
        self.context = context
        self.stage = context.initial_stage
        self.bindings: dict[str, tuple[str, ...]] = {}
        self.binding_records: dict[str, dict[str, Any]] = {}
        self.steps: list[HostStep] = []
        self.requires_implementation = False

    def bind(self, name: Any, handles: Sequence[str]) -> None:
        if not isinstance(name, str) or not _ID_RE.fullmatch(name) or name in self.bindings:
            raise ConstructionError("bind_name_invalid_or_duplicate")
        values = tuple(handles)
        if not values:
            raise ConstructionError("empty_selection")
        objects = tuple(self.context.registry.resolve(handle) for handle in values)
        types = {item.object_type for item in objects}
        if len(types) != 1:
            raise ConstructionError("collection_type_mismatch")
        self.bindings[name] = values
        self.binding_records[name] = {
            "binding_id": name,
            "binding_type": next(iter(types)),
            "binding_cardinality": len(values),
            "binding_content_hash": hashlib.sha256(canonical_json_bytes(list(values))).hexdigest(),
            "ordered_member_hashes": [
                hashlib.sha256(canonical_json_bytes({"index": index, "handle": handle})).hexdigest()
                for index, handle in enumerate(values)
            ],
        }
        if len(objects) == 1 and objects[0].object_type == "allocation":
            attrs = objects[0].attributes
            self.binding_records[name].update({
                "matching_size": attrs.get("matching_size"),
                "compatibility_relation_sha256": attrs.get("compatibility_relation_sha256"),
                "feasibility_query_sha256": attrs.get("feasibility_query_sha256"),
                "target_site_pair_hashes": list(attrs.get("target_site_pair_hashes", ())),
                "allocation_sha256": attrs.get("allocation_sha256"),
                "root_sha256": attrs.get("root_sha256"),
                "stage": attrs.get("stage"),
            })

    def collection(self, value: Any) -> tuple[PrivateObject, ...]:
        if not isinstance(value, Mapping) or set(value) != {"ref"}:
            raise ConstructionError("collection_reference_required")
        ref = value.get("ref")
        if not isinstance(ref, str) or ref not in self.bindings:
            raise ConstructionError("unknown_reference")
        return tuple(self.context.registry.resolve(handle) for handle in self.bindings[ref])

    def reference(self, value: Any, *, expected: str | tuple[str, ...], allow_many: bool = True) -> tuple[PrivateObject, ...]:
        types = (expected,) if isinstance(expected, str) else expected
        handles: tuple[str, ...]
        if isinstance(value, Mapping) and set(value) == {"ref"}:
            ref = value["ref"]
            if not isinstance(ref, str) or ref not in self.bindings:
                raise ConstructionError("unknown_reference")
            handles = self.bindings[ref]
        elif isinstance(value, str):
            handles = (value,)
        else:
            raise ConstructionError("handle_or_reference_required")
        if not handles:
            raise ConstructionError("empty_selection")
        if not allow_many and len(handles) != 1:
            raise ConstructionError("single_target_required")
        objects: list[PrivateObject] = []
        for handle in handles:
            obj = None
            for kind in types:
                try:
                    obj = self.context.registry.resolve(handle, expected_type=kind)
                    break
                except HandleError:
                    pass
            if obj is None:
                raise ConstructionError("unknown_stale_or_wrong_type_handle")
            objects.append(obj)
        return tuple(objects)


def compile_p(program: Mapping[str, Any], context: CompileContext) -> CompiledProgram:
    _exact_fields(program, {"language", "steps"}, "program")
    if program.get("language") != "P":
        raise ConstructionError("p_language_marker_required")
    steps = program.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_PROGRAM_STEPS:
        raise ConstructionError("p_step_count_invalid")
    state = _State(context)
    seen: set[str] = set()
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, Mapping):
            raise ConstructionError("p_step_not_object")
        allowed = {"step_id", "op", "args", "bind"}
        if set(raw) - allowed or not {"step_id", "op", "args"} <= set(raw):
            raise ConstructionError("p_step_fields_invalid")
        step_id = raw["step_id"]
        if step_id != f"s{index}" or step_id in seen:
            raise ConstructionError("p_step_id_not_canonical")
        seen.add(step_id)
        op = raw["op"]
        if op not in _P_OPS:
            raise ConstructionError("forbidden_operation")
        if context.action_surface_version != "v3" and op not in _LEGACY_P_OPS:
            raise ConstructionError("forbidden_operation")
        args = raw["args"]
        if not isinstance(args, Mapping):
            raise ConstructionError("step_args_not_object")
        host_step_start = len(state.steps)
        _compile_p_step(state, step_id, str(op), args, raw.get("bind"))
        # Proposal certificates must be tied to the host plan, rather than to
        # an unused selector or another decorative source-language node.  G
        # already carries this lineage; retain the same exact evidence for P.
        if op != "CALL_MACRO":
            _annotate_g_host_steps(
                state,
                start=host_step_start,
                source_step_id=str(step_id),
                construct=str(op),
                args=args,
            )
        _check_budget(state.steps)
    if state.stage != "ROUTED":
        raise ConstructionError("program_terminal_not_routed")
    if state.requires_implementation:
        raise ConstructionError("mutation_requires_followup_implementation")
    if not any(step.heavy_cost for step in state.steps):
        raise ConstructionError("program_has_no_scoreable_implementation")
    return _finish("P", program, context.initial_stage, state.stage, state.steps)


def _compile_p_step(state: _State, step_id: str, op: str, args: Mapping[str, Any], bind: Any) -> None:
    if op not in _P_OPS or (
        state.context.action_surface_version != "v3" and op not in _LEGACY_P_OPS
    ):
        raise ConstructionError("forbidden_operation")
    if op == "CALL_MACRO":
        field = "action_key" if state.context.action_surface_version == "v3" else "macro"
        _exact_fields(args, {field}, op)
        name = str(args[field]).removeprefix("macro.")
        if name not in state.context.admitted_macros:
            raise ConstructionError("macro_not_admitted")
        state.steps.extend(_macro_steps(name))
        state.stage = "ROUTED"
        state.requires_implementation = False
        _no_bind(bind)
        return
    if op in _P_SELECTORS:
        selected = _select(state, op, args)
        if not selected:
            raise ConstructionError("empty_selection")
        state.bind(bind, [item.handle for item in selected])
        return
    if op == "UNROUTE":
        _empty_args(args, op); _no_bind(bind); _require_stage(state, "ROUTED")
        state.steps.append(HostStep("vivado.unroute", "MUTATION_LIGHT", {}, {})); state.stage = "PLACED"; state.requires_implementation = True; return
    if op == "UNPLACE":
        _empty_args(args, op); _no_bind(bind); _require_stage(state, "PLACED")
        state.steps.append(HostStep("vivado.unplace", "MUTATION_LIGHT", {}, {})); state.stage = "UNPLACED"; state.requires_implementation = True; return
    if op in {"PLACE", "ROUTE", "PHYS_OPT"}:
        _no_bind(bind)
        strict_v3 = state.context.action_surface_version == "v3"
        if op == "PLACE":
            allowed = {"directive", "net_delay_weight"} if strict_v3 else {"directive"}
            _exact_fields(args, allowed, op, required={"directive"})
            directive = args["directive"]
            legacy = {"Default", "Explore", "ExtraNetDelay_high"}
            if directive not in (_P_DIRECTIVES[op] if strict_v3 else legacy):
                raise ConstructionError("directive_not_allowed")
            weight = args.get("net_delay_weight")
            if weight is not None and weight not in PLACE_NET_DELAY_WEIGHTS:
                raise ConstructionError("net_delay_weight_not_allowed")
            if state.stage not in {"UNPLACED", "PLACED"}: raise ConstructionError("invalid_stage")
            state.stage = "PLACED"; operation = "vivado.place"
        elif op == "ROUTE":
            _exact_fields(args, {"directive"}, op)
            directive = args["directive"]
            legacy = {"Default", "Explore", "AggressiveExplore"}
            if directive not in (_P_DIRECTIVES[op] if strict_v3 else legacy):
                raise ConstructionError("directive_not_allowed")
            _require_stage(state, "PLACED"); state.stage = "ROUTED"; operation = "vivado.route"; state.requires_implementation = False
        else:
            if state.stage not in {"PLACED", "ROUTED"}: raise ConstructionError("invalid_stage")
            if strict_v3:
                mode = args.get("mode")
                if mode == "DIRECTIVE":
                    _exact_fields(args, {"mode", "directive"}, op)
                    if args["directive"] not in _P_DIRECTIVES[op]:
                        raise ConstructionError("directive_not_allowed")
                elif mode == "OPTIONS":
                    _exact_fields(args, {"mode", "options"}, op)
                    options = args["options"]
                    if (
                        not isinstance(options, list)
                        or not 1 <= len(options) <= 4
                        or len(options) != len(set(options))
                        or not set(options) <= _PHYS_OPTIONS
                    ):
                        raise ConstructionError("phys_opt_options_invalid")
                    option_set = set(options)
                    if "tns_cleanup" in option_set and "slr_crossing_opt" not in option_set:
                        raise ConstructionError("tns_cleanup_requires_slr_crossing_opt")
                    if state.stage == "ROUTED" and option_set & _PHYS_PLACED_OPTIONS:
                        raise ConstructionError("phys_opt_option_stage_invalid")
                else:
                    raise ConstructionError("phys_opt_mode_invalid")
            else:
                _exact_fields(args, {"directive", "options"}, op, required={"directive"})
                directive = args["directive"]
                if directive not in {"Default", "Explore", "AggressiveExplore", "AlternateFlowWithRetiming"}:
                    raise ConstructionError("directive_not_allowed")
                options = args.get("options", [])
                legacy_options = {"AddRetime", "critical_cell_opt", "critical_pin_opt", "placement_opt", "routing_opt", "dsp_register_opt", "bram_register_opt", "uram_register_opt"}
                if not isinstance(options, list) or len(options) != len(set(options)) or not set(options) <= legacy_options:
                    raise ConstructionError("phys_opt_options_invalid")
            operation = "vivado.phys_opt"
        state.steps.append(HostStep(operation, "IMPLEMENTATION_HEAVY", dict(args), dict(args))); return
    if op == "CREATE_PBLOCK":
        _exact_fields(args, {"predefined_region_id"}, op)
        region = state.reference(args["predefined_region_id"], expected="region", allow_many=False)[0]
        if region.handle not in state.context.predefined_regions:
            raise ConstructionError("region_not_predefined")
        handle = state.context.registry.register(object_type="pblock", canonical_identity=f"wp013:{step_id}:{region.canonical_identity}", attributes={"region_handle": region.handle})
        state.bind(bind, [handle])
        operation = "vivado.wp014c_create_pblock" if state.context.action_surface_version == "v3" else "vivado.create_pblock"
        state.steps.append(HostStep(operation, "MUTATION_LIGHT", {"region": region.handle, "pblock": handle}, {"region": region.canonical_identity, "pblock": state.context.registry.resolve(handle, expected_type="pblock").canonical_identity}, transformed_count=1)); state.requires_implementation = True; return
    if op == "RESIZE_PBLOCK":
        _exact_fields(args, {"pblock_handle", "region_handle", "mode"}, op); _no_bind(bind)
        pblock = state.reference(args["pblock_handle"], expected="pblock", allow_many=False)[0]
        region = state.reference(args["region_handle"], expected="region", allow_many=False)[0]
        mode = args["mode"]
        if mode not in {"ADD", "REMOVE"}: raise ConstructionError("pblock_resize_mode_invalid")
        state.steps.append(HostStep("vivado.wp014c_resize_pblock", "MUTATION_LIGHT", {"pblock": pblock.handle, "region": region.handle, "mode": mode}, {"pblock": pblock.canonical_identity, "region": region.canonical_identity, "mode": mode}, transformed_count=1)); state.requires_implementation = True; return
    if op == "ASSIGN":
        _exact_fields(args, {"target_handle", "pblock_handle"}, op); _no_bind(bind)
        targets = state.reference(args["target_handle"], expected=("cell", "hardblock"))
        pblock = state.reference(args["pblock_handle"], expected="pblock", allow_many=False)[0]
        operation = "vivado.wp014c_assign_pblock" if state.context.action_surface_version == "v3" else "vivado.assign_pblock"
        state.steps.append(HostStep(operation, "MUTATION_LIGHT", {"targets": [x.handle for x in targets], "pblock": pblock.handle}, {"targets": [x.canonical_identity for x in targets], "pblock": pblock.canonical_identity}, len(targets), len(targets))); state.requires_implementation = True; return
    if op == "UNASSIGN":
        _exact_fields(args, {"target_handle", "pblock_handle"}, op); _no_bind(bind)
        targets = state.reference(args["target_handle"], expected=("cell", "hardblock"))
        pblock = state.reference(args["pblock_handle"], expected="pblock", allow_many=False)[0]
        state.steps.append(HostStep("vivado.wp014c_unassign_pblock", "MUTATION_LIGHT", {"targets": [x.handle for x in targets], "pblock": pblock.handle}, {"targets": [x.canonical_identity for x in targets], "pblock": pblock.canonical_identity}, len(targets), len(targets))); state.requires_implementation = True; return
    if op == "REMOVE_PBLOCK":
        _exact_fields(args, {"pblock_handle"}, op); _no_bind(bind)
        pblock = state.reference(args["pblock_handle"], expected="pblock", allow_many=False)[0]
        operation = "vivado.wp014c_delete_pblock" if state.context.action_surface_version == "v3" else "vivado.remove_pblock"
        state.steps.append(HostStep(operation, "MUTATION_LIGHT", {"pblock": pblock.handle}, {"pblock": pblock.canonical_identity}, transformed_count=1)); state.requires_implementation = True; return
    if op == "RELOCATE_HARDBLOCK":
        _exact_fields(args, {"typed_target", "destination"}, op); _no_bind(bind)
        if state.context.action_surface_version == "v3": _require_stage(state, "PLACED")
        targets = state.reference(args["typed_target"], expected="hardblock")
        dest = state.reference(args["destination"], expected=("region", "site"), allow_many=False)[0]
        _validate_compatibility(targets, dest)
        operation = "vivado.wp014c_relocate_hardblocks" if state.context.action_surface_version == "v3" else "vivado.relocate_hardblock"
        state.steps.append(HostStep(operation, "MUTATION_LIGHT", {"targets": [x.handle for x in targets], "destination": dest.handle}, {"targets": [x.canonical_identity for x in targets], "destination": dest.canonical_identity}, len(targets), len(targets))); state.stage = "PLACED"; state.requires_implementation = True; return
    if op == "REPLICATE_DRIVER":
        if not state.context.fanout_admitted: raise ConstructionError("fanout_not_admitted")
        if state.context.action_surface_version == "v3":
            _exact_fields(args, {"typed_net", "split_factor"}, op)
        else:
            _exact_fields(args, {"typed_net", "copies", "max_loads_per_copy"}, op)
        _no_bind(bind)
        net = state.reference(args["typed_net"], expected="net", allow_many=False)[0]
        if state.context.action_surface_version == "v3":
            split = args["split_factor"]
            if not isinstance(split, int) or isinstance(split, bool) or not 2 <= split <= 8: raise ConstructionError("fanout_parameters_invalid")
            public = {"net": net.handle, "split_factor": split}
            private = {"net": net.canonical_identity, "split_factor": split}
            operation = "rapidwright.optimize_fanout"
            transformed = split
        else:
            copies, loads = args["copies"], args["max_loads_per_copy"]
            if not isinstance(copies, int) or not 1 <= copies <= 4 or not isinstance(loads, int) or not 2 <= loads <= 128: raise ConstructionError("fanout_parameters_invalid")
            public = {"net": net.handle, "copies": copies, "max_loads_per_copy": loads}
            private = {"net": net.canonical_identity, "copies": copies, "max_loads_per_copy": loads}
            operation = "rapidwright.replicate_driver"
            transformed = copies
        state.steps.append(HostStep(operation, "MUTATION_LIGHT", public, private, 1, transformed)); state.stage = "PLACED"; state.requires_implementation = True; return
    if op == "REPLICATE_NET":
        _exact_fields(args, {"typed_net"}, op); _no_bind(bind); _require_stage(state, "PLACED")
        net = state.reference(args["typed_net"], expected="net", allow_many=False)[0]
        state.steps.append(HostStep("vivado.wp014c_replicate_net", "IMPLEMENTATION_HEAVY", {"net": net.handle}, {"net": net.canonical_identity}, 1, 1)); state.requires_implementation = True; return
    if op == "ROUTE_NET":
        _exact_fields(args, {"typed_net", "delay_mode"}, op); _no_bind(bind)
        if state.stage not in {"PLACED", "ROUTED"}: raise ConstructionError("invalid_stage")
        mode = args["delay_mode"]
        if mode not in {"DELAY", "AUTO_DELAY"}: raise ConstructionError("route_net_delay_mode_invalid")
        net = state.reference(args["typed_net"], expected="net", allow_many=False)[0]
        state.steps.append(HostStep("vivado.wp014c_route_net", "IMPLEMENTATION_HEAVY", {"net": net.handle, "delay_mode": mode}, {"net": net.canonical_identity, "delay_mode": mode}, 1, 1)); state.stage = "ROUTED"; state.requires_implementation = False; return
    if op == "PLACE_CELL":
        _exact_fields(args, {"typed_cell", "destination_site"}, op); _no_bind(bind)
        if state.stage not in {"UNPLACED", "PLACED"}: raise ConstructionError("invalid_stage")
        cell = state.reference(args["typed_cell"], expected="cell", allow_many=False)[0]
        site = state.reference(args["destination_site"], expected="site", allow_many=False)[0]
        _validate_compatibility((cell,), site)
        state.steps.append(HostStep("vivado.wp014c_place_cell", "MUTATION_LIGHT", {"cell": cell.handle, "site": site.handle}, {"cell": cell.canonical_identity, "site": site.canonical_identity}, 1, 1)); state.stage = "PLACED"; state.requires_implementation = True; return
    if op == "UNPLACE_CELL":
        _exact_fields(args, {"typed_cell"}, op); _no_bind(bind)
        _require_stage(state, "PLACED")
        cell = state.reference(args["typed_cell"], expected="cell", allow_many=False)[0]
        state.steps.append(HostStep("vivado.wp014c_unplace_cell", "MUTATION_LIGHT", {"cell": cell.handle}, {"cell": cell.canonical_identity}, 1, 1)); state.stage = "PLACED"; state.requires_implementation = True; return
    if op == "SWAP_CELLS":
        _exact_fields(args, {"first_cell", "second_cell"}, op); _no_bind(bind)
        _require_stage(state, "PLACED")
        first = state.reference(args["first_cell"], expected="cell", allow_many=False)[0]
        second = state.reference(args["second_cell"], expected="cell", allow_many=False)[0]
        if first.handle == second.handle: raise ConstructionError("swap_cells_must_be_distinct")
        if first.attributes.get("resource_type") != second.attributes.get("resource_type"): raise ConstructionError("cell_sites_incompatible")
        state.steps.append(HostStep("vivado.wp014c_swap_cells", "MUTATION_LIGHT", {"first": first.handle, "second": second.handle}, {"first": first.canonical_identity, "second": second.canonical_identity}, 2, 2)); state.stage = "PLACED"; state.requires_implementation = True; return
    if op in {"SET_CELL_LOC_FIXED", "SET_NET_DONT_TOUCH", "SET_PBLOCK_EXCLUDE_PLACEMENT", "SET_SITE_PROHIBIT"}:
        _exact_fields(args, {"target_handle", "value"}, op); _no_bind(bind)
        value = args["value"]
        if not isinstance(value, bool): raise ConstructionError("property_value_not_boolean")
        kind, operation, allowed_stages = {
            "SET_CELL_LOC_FIXED": ("cell", "vivado.wp014c_set_cell_loc_fixed", {"PLACED", "ROUTED"}),
            "SET_NET_DONT_TOUCH": ("net", "vivado.wp014c_set_net_dont_touch", _STAGES),
            "SET_PBLOCK_EXCLUDE_PLACEMENT": ("pblock", "vivado.wp014c_set_pblock_exclude_placement", {"UNPLACED"}),
            "SET_SITE_PROHIBIT": ("site", "vivado.wp014c_set_site_prohibit", {"UNPLACED"}),
        }[op]
        if state.stage not in allowed_stages: raise ConstructionError("property_stage_invalid")
        target = state.reference(args["target_handle"], expected=kind, allow_many=False)[0]
        state.steps.append(HostStep(operation, "MUTATION_LIGHT", {"target": target.handle, "value": value}, {"target": target.canonical_identity, "value": value}, 1, 1)); state.requires_implementation = True; return
    raise ConstructionError("operation_not_implemented")


def _select(state: _State, op: str, args: Mapping[str, Any]) -> tuple[PrivateObject, ...]:
    if op == "SELECT_CONGESTED_REGION":
        _exact_fields(args, {"region_id"}, op)
        return state.reference(args["region_id"], expected="region", allow_many=False)
    required = {"top_k"} if op != "SELECT_CRITICAL_HARDBLOCKS" else {"type", "top_k"}
    _exact_fields(args, required, op)
    top_k = args["top_k"]
    maximum = 32 if op == "SELECT_CRITICAL_HARDBLOCKS" else 64
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= maximum: raise ConstructionError("selector_top_k_invalid")
    kind = "cell" if op == "SELECT_CRITICAL_CELLS" else "net" if op in {"SELECT_CRITICAL_NETS", "SELECT_HIGH_FANOUT_NETS"} else "hardblock"
    items = list(state.context.registry.objects(object_type=kind))
    if op in {"SELECT_CRITICAL_CELLS", "SELECT_CRITICAL_NETS", "SELECT_CRITICAL_HARDBLOCKS"}:
        items = [x for x in items if x.attributes.get("path_member") is True]
        items.sort(key=lambda x: (_num(x.attributes.get("path_rank"), math.inf), _num(x.attributes.get("slack_ns"), math.inf), x.canonical_identity))
    elif op == "SELECT_HIGH_FANOUT_NETS":
        items.sort(key=lambda x: (-_num(x.attributes.get("fanout"), -math.inf), x.canonical_identity))
    if op == "SELECT_CRITICAL_HARDBLOCKS":
        resource = args["type"]
        if resource not in {"DSP", "BRAM", "URAM", "ANY"}: raise ConstructionError("hardblock_type_invalid")
        if resource != "ANY": items = [x for x in items if x.attributes.get("resource_type") == resource]
    return tuple(items[:top_k])


def compile_g(program: Mapping[str, Any], context: CompileContext) -> CompiledProgram:
    _exact_fields(program, {"language", "statements"}, "program")
    if program.get("language") != "G": raise ConstructionError("g_language_marker_required")
    statements = program.get("statements")
    if not isinstance(statements, list) or not 1 <= len(statements) <= MAX_PROGRAM_STEPS: raise ConstructionError("g_statement_count_invalid")
    state = _State(context)
    for index, raw in enumerate(statements, start=1):
        if not isinstance(raw, Mapping) or set(raw) - {"statement_id", "node", "args", "bind"} or not {"statement_id", "node", "args"} <= set(raw): raise ConstructionError("g_statement_fields_invalid")
        if raw["statement_id"] != f"g{index}": raise ConstructionError("g_statement_id_not_canonical")
        node, args, bind = raw["node"], raw["args"], raw.get("bind")
        if not isinstance(args, Mapping): raise ConstructionError("statement_args_not_object")
        host_step_start = len(state.steps)
        if node == "CALL_MACRO":
            _compile_p_step(state, str(raw["statement_id"]), "CALL_MACRO", args, bind)
        elif node == "P_PROGRAM":
            _exact_fields(args, {"program"}, "P_PROGRAM")
            nested = compile_p(args["program"], context)
            state.steps.extend(nested.host_steps); state.stage = nested.final_stage; state.requires_implementation = False; _no_bind(bind)
        elif node == "RELOCATE_HARDBLOCK" and "candidate_sites" in args and context.action_surface_version != "v3":
            _exact_fields(args, {"typed_target", "candidate_sites", "site_index"}, node)
            sites = args["candidate_sites"]
            index_value = args["site_index"]
            if not isinstance(sites, list) or not 1 <= len(sites) <= 32:
                raise ConstructionError("generated_candidate_sites_invalid")
            if not isinstance(index_value, int) or isinstance(index_value, bool) or not 0 <= index_value < len(sites):
                raise ConstructionError("generated_site_index_invalid")
            destination = state.reference(sites[index_value], expected="site", allow_many=False)[0]
            _compile_p_step(
                state,
                str(raw["statement_id"]),
                "RELOCATE_HARDBLOCK",
                {"typed_target": args["typed_target"], "destination": destination.handle},
                bind,
            )
        elif node in _P_OPS:
            # G contains P byte-for-byte at the operation level.  This form is
            # also what allows generated selections/regions to feed inherited
            # typed mutations without inventing a second mechanism.
            _compile_p_step(state, str(raw["statement_id"]), str(node), args, bind)
        elif node == "SELECT_WHERE":
            _exact_fields(args, {"source_type", "predicate", "top_k"}, node)
            source_type, top_k = args["source_type"], args["top_k"]
            allowed_source_types = {"cell", "net", "hardblock", "site"} if context.action_surface_version == "v3" else {"cell", "net", "hardblock"}
            if source_type not in allowed_source_types or not isinstance(top_k, int) or not 1 <= top_k <= 64: raise ConstructionError("generated_selector_domain_invalid")
            predicate = args["predicate"]
            _validate_predicate(predicate)
            selected = tuple(x for x in context.registry.objects(object_type=source_type) if _eval_predicate(predicate, {"object_type": x.object_type} | dict(x.attributes)))[:top_k]
            if not selected: raise ConstructionError("empty_selection")
            state.bind(bind, [x.handle for x in selected])
        elif node == "FILTER":
            _exact_fields(args, {"input", "predicate"}, node)
            source = state.collection(args["input"])
            predicate = args["predicate"]
            _validate_predicate(predicate)
            selected = tuple(
                item for item in source
                if _eval_predicate(
                    predicate,
                    {"object_type": item.object_type} | dict(item.attributes),
                )
            )
            state.bind(bind, [item.handle for item in selected])
        elif node == "RANK":
            _exact_fields(args, {"input", "field", "direction"}, node)
            source = state.collection(args["input"])
            field = args["field"]
            direction = args["direction"]
            if field not in _PREDICATE_FIELDS or direction not in {"ASC", "DESC"}:
                raise ConstructionError("rank_arguments_invalid")
            ranked = _rank_collection(source, field=field, descending=direction == "DESC")
            state.bind(bind, [item.handle for item in ranked])
        elif node == "TOPK":
            _exact_fields(args, {"input", "k"}, node)
            source = state.collection(args["input"])
            k = args["k"]
            if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 64:
                raise ConstructionError("topk_out_of_range")
            state.bind(bind, [item.handle for item in source[:k]])
            state.binding_records[str(bind)].update({
                "derivation": "TOPK_PREFIX",
                "source_binding_id": str(args["input"]["ref"]),
                "source_content_hash": state.binding_records[str(args["input"]["ref"])]["binding_content_hash"],
                "source_cardinality": len(source),
                "selected_source_indices": list(range(min(k, len(source)))),
                "proper_nonempty_prefix": 0 < min(k, len(source)) < len(source),
            })
        elif node in {"UNION", "INTERSECT", "DIFF"}:
            _exact_fields(args, {"left", "right"}, node)
            left = state.collection(args["left"])
            right = state.collection(args["right"])
            if {item.object_type for item in left + right}.__len__() != 1:
                raise ConstructionError("collection_type_mismatch")
            right_handles = {item.handle for item in right}
            if node == "UNION":
                combined = left + tuple(item for item in right if item.handle not in {x.handle for x in left})
            elif node == "INTERSECT":
                combined = tuple(item for item in left if item.handle in right_handles)
            else:
                combined = tuple(item for item in left if item.handle not in right_handles)
            state.bind(bind, [item.handle for item in combined])
            if node == "DIFF":
                left_id, right_id = str(args["left"]["ref"]), str(args["right"]["ref"])
                left_handles = [item.handle for item in left]
                right_handles_ordered = [item.handle for item in right]
                result_handles = [item.handle for item in combined]
                right_source_indices = [
                    left_handles.index(handle) for handle in right_handles_ordered
                    if handle in left_handles
                ]
                result_source_indices = [left_handles.index(handle) for handle in result_handles]
                prefix = bool(
                    0 < len(right_handles_ordered) < len(left_handles)
                    and right_handles_ordered == left_handles[:len(right_handles_ordered)]
                )
                state.binding_records[str(bind)].update({
                    "derivation": "DIFF",
                    "left_binding_id": left_id,
                    "right_binding_id": right_id,
                    "left_content_hash": state.binding_records[left_id]["binding_content_hash"],
                    "removed_content_hash": state.binding_records[right_id]["binding_content_hash"],
                    "left_cardinality": len(left_handles),
                    "removed_cardinality": len(right_handles_ordered),
                    "right_source_indices": right_source_indices,
                    "result_source_indices": result_source_indices,
                    "selected_source_indices": result_source_indices,
                    "right_members_all_from_left": len(right_source_indices) == len(right_handles_ordered),
                    "result_members_all_from_left": len(result_source_indices) == len(result_handles),
                    "right_is_nonempty_proper_prefix_of_left": prefix,
                    "result_is_exact_left_minus_right": result_handles == [
                        handle for handle in left_handles if handle not in set(right_handles_ordered)
                    ],
                    "result_is_source_prefix": result_source_indices == list(range(len(result_handles))),
                })
        elif node == "CREATE_REGION":
            _exact_fields(args, {"x_min", "x_max", "y_min", "y_max"}, node)
            coords = tuple(args[key] for key in ("x_min", "x_max", "y_min", "y_max"))
            if any(not isinstance(v, int) or isinstance(v, bool) for v in coords): raise ConstructionError("generated_geometry_not_integer")
            bx0, bx1, by0, by1 = context.geometry_bounds; x0, x1, y0, y1 = coords
            if not (bx0 <= x0 <= x1 <= bx1 and by0 <= y0 <= y1 <= by1): raise ConstructionError("generated_geometry_out_of_bounds")
            identity = "generated-region:" + ":".join(map(str, coords))
            handle = context.registry.register(object_type="region", canonical_identity=identity, attributes={"bounds": list(coords)})
            state.bind(bind, [handle])
            predefined_bounds = []
            for region_handle in context.predefined_regions:
                region = context.registry.resolve(region_handle, expected_type="region")
                bounds = region.attributes.get("bounds")
                if (
                    isinstance(bounds, (list, tuple)) and len(bounds) == 4
                    and all(isinstance(value, int) and not isinstance(value, bool) for value in bounds)
                ):
                    predefined_bounds.append(list(bounds))
            predefined_bounds.sort()
            generated = list(coords)
            state.binding_records[str(bind)].update({
                "derivation": "CREATE_REGION",
                "generated_bounds": generated,
                "generated_bounds_hash": hashlib.sha256(canonical_json_bytes(generated)).hexdigest(),
                "device_geometry_bounds": list(context.geometry_bounds),
                "generated_bounds_in_device": True,
                "generated_bounds_nondegenerate": x1 > x0 or y1 > y0,
                "predefined_geometry_bounds": predefined_bounds,
                "predefined_geometry_set_hash": hashlib.sha256(canonical_json_bytes(predefined_bounds)).hexdigest(),
                "predefined_geometry_count": len(predefined_bounds),
                "generated_bounds_not_predefined": generated not in predefined_bounds,
            })
        elif node == "ASSIGN_REGION":
            _exact_fields(args, {"target_handle", "region_handle"}, node)
            pblock_handle = _generated_pblock(state, args.get("region_handle"), str(raw["statement_id"]))
            pblock = state.context.registry.resolve(pblock_handle, expected_type="pblock")
            region = state.reference(args["region_handle"], expected="region", allow_many=False)[0]
            operation = "vivado.wp014c_create_pblock" if context.action_surface_version == "v3" else "vivado.create_pblock"
            state.steps.append(HostStep(operation, "MUTATION_LIGHT", {"region": region.handle, "pblock": pblock.handle}, {"region": region.canonical_identity, "pblock": pblock.canonical_identity}, transformed_count=1))
            mapped = {"target_handle": args.get("target_handle"), "pblock_handle": pblock_handle}
            _compile_p_step(state, str(raw["statement_id"]), "ASSIGN", mapped, bind)
        elif node == "CHOOSE_SITE" and context.action_surface_version == "v3":
            _exact_fields(args, {"candidate_sites", "site_index"}, node)
            sites = args["candidate_sites"]
            index_value = args["site_index"]
            if not isinstance(sites, list) or not 1 <= len(sites) <= 32:
                raise ConstructionError("generated_candidate_sites_invalid")
            if not isinstance(index_value, int) or isinstance(index_value, bool) or not 0 <= index_value < len(sites):
                raise ConstructionError("generated_site_index_invalid")
            chosen = state.reference(sites[index_value], expected="site", allow_many=False)[0]
            state.bind(bind, [chosen.handle])
        elif node == "COUPLE_SITES" and context.action_surface_version == "v3":
            _exact_fields(args, {"hardblocks", "sites", "strategy"}, node)
            hardblocks = state.collection(args["hardblocks"])
            sites = state.collection(args["sites"])
            if any(item.object_type != "hardblock" for item in hardblocks) or any(item.object_type != "site" for item in sites):
                raise ConstructionError("allocation_collection_type_invalid")
            if not 2 <= len(hardblocks) <= 16:
                raise ConstructionError("allocation_requires_multiple_targets")
            if any(step.operation in _PLACEMENT_MUTATION_OPERATIONS for step in state.steps):
                raise ConstructionError("allocation_relation_stale_after_placement_mutation")
            relation = context.hardblock_compatibility_relation
            if not isinstance(relation, Mapping):
                raise ConstructionError("exact_compatibility_relation_missing")
            try:
                validate_relation(relation)
                allocation_attrs = build_allocation(
                    relation=relation,
                    target_handles=[item.handle for item in hardblocks],
                    submitted_site_handles=[item.handle for item in sites],
                    strategy=str(args["strategy"]),
                    root_sha256=context.registry.root_sha256,
                    stage=context.initial_stage,
                )
            except ValueError as exc:
                raise ConstructionError(str(exc)) from exc
            allocation_identity = "wp014h-allocation:" + hashlib.sha256(
                canonical_json_bytes(allocation_attrs)
            ).hexdigest()
            allocation = context.registry.register(
                object_type="allocation",
                canonical_identity=allocation_identity,
                attributes=allocation_attrs,
                source_tool="compiler",
            )
            state.bind(bind, [allocation])
        elif node == "BATCH_ROUTE_NETS" and context.action_surface_version == "v3":
            _exact_fields(args, {"typed_nets", "delay_mode"}, node); _no_bind(bind)
            nets = state.collection(args["typed_nets"])
            if any(item.object_type != "net" for item in nets):
                raise ConstructionError("batch_collection_type_invalid")
            if not 2 <= len(nets) <= 32:
                raise ConstructionError("batch_requires_multiple_targets")
            mode = args["delay_mode"]
            if mode not in {"DELAY", "AUTO_DELAY"}:
                raise ConstructionError("route_net_delay_mode_invalid")
            if state.stage not in {"PLACED", "ROUTED"}:
                raise ConstructionError("invalid_stage")
            state.steps.append(HostStep(
                "vivado.wp014h_batch_route_nets", "IMPLEMENTATION_HEAVY",
                {"nets": [item.handle for item in nets], "delay_mode": mode},
                {"nets": [item.canonical_identity for item in nets], "delay_mode": mode},
                len(nets), len(nets), source_construct=node,
            ))
            state.stage = "ROUTED"; state.requires_implementation = False
        elif node == "APPLY_ALLOCATION" and context.action_surface_version == "v3":
            _exact_fields(args, {"allocation"}, node); _no_bind(bind); _require_stage(state, "PLACED")
            allocation = state.collection(args["allocation"])
            if len(allocation) != 1 or allocation[0].object_type != "allocation":
                raise ConstructionError("allocation_reference_invalid")
            attrs = allocation[0].attributes
            relation = context.hardblock_compatibility_relation
            if not isinstance(relation, Mapping):
                raise ConstructionError("exact_compatibility_relation_missing")
            if any(step.operation in _PLACEMENT_MUTATION_OPERATIONS for step in state.steps):
                raise ConstructionError("allocation_relation_stale_after_placement_mutation")
            try:
                validate_allocation(
                    attrs, relation, root_sha256=context.registry.root_sha256,
                    stage=context.initial_stage,
                )
            except ValueError as exc:
                raise ConstructionError(str(exc)) from exc
            targets = tuple(context.registry.resolve(handle, expected_type="hardblock") for handle in attrs.get("target_handles", ()))
            sites = tuple(context.registry.resolve(handle, expected_type="site") for handle in attrs.get("site_handles", ()))
            if len(targets) < 2 or len(targets) != len(sites):
                raise ConstructionError("allocation_resolution_mismatch")
            state.steps.append(HostStep(
                "vivado.wp014h_apply_allocation", "MUTATION_LIGHT",
                {
                    "allocation": allocation[0].handle,
                    "targets": [item.handle for item in targets],
                    "sites": [item.handle for item in sites],
                    "target_site_pair_hashes": list(attrs["target_site_pair_hashes"]),
                    "compatibility_relation_sha256": attrs["compatibility_relation_sha256"],
                    "feasibility_query_sha256": attrs["feasibility_query_sha256"],
                    "allocation_sha256": attrs["allocation_sha256"],
                },
                {
                    "allocation": allocation[0].canonical_identity,
                    "targets": [item.canonical_identity for item in targets],
                    "sites": [item.canonical_identity for item in sites],
                    "target_site_pair_hashes": list(attrs["target_site_pair_hashes"]),
                    "compatibility_relation_sha256": attrs["compatibility_relation_sha256"],
                    "feasibility_query_sha256": attrs["feasibility_query_sha256"],
                    "allocation_sha256": attrs["allocation_sha256"],
                },
                len(targets), len(targets), source_construct=node,
            ))
            state.stage = "PLACED"; state.requires_implementation = True
        elif node == "RELOCATE_HARDBLOCK":
            _compile_p_step(state, str(raw["statement_id"]), "RELOCATE_HARDBLOCK", args, bind)
        else:
            raise ConstructionError("forbidden_generated_node")
        _annotate_g_host_steps(
            state,
            start=host_step_start,
            source_step_id=str(raw["statement_id"]),
            construct=str(node),
            args=args,
        )
        _check_budget(state.steps)
    if state.stage != "ROUTED": raise ConstructionError("program_terminal_not_routed")
    if state.requires_implementation: raise ConstructionError("mutation_requires_followup_implementation")
    if not any(x.heavy_cost for x in state.steps): raise ConstructionError("program_has_no_scoreable_implementation")
    return _finish(
        "G", program, context.initial_stage, state.stage, state.steps,
        binding_records=tuple(state.binding_records[key] for key in sorted(state.binding_records)),
    )


def _binding_ref_slots(value: Any, prefix: str = "args") -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        if set(value) == {"ref"} and isinstance(value.get("ref"), str):
            rows.append((prefix, str(value["ref"])))
        else:
            for key in sorted(value):
                rows.extend(_binding_ref_slots(value[key], f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(_binding_ref_slots(item, f"{prefix}[{index}]"))
    return tuple(rows)


def _annotate_g_host_steps(
    state: _State,
    *,
    start: int,
    source_step_id: str,
    construct: str,
    args: Mapping[str, Any],
) -> None:
    bindings = _binding_ref_slots(args)
    for index in range(start, len(state.steps)):
        step = state.steps[index]
        state.steps[index] = replace(
            step,
            source_construct=step.source_construct or construct,
            source_step_id=source_step_id,
            input_bindings=bindings,
        )


def _rank_collection(
    values: Sequence[PrivateObject], *, field: str, descending: bool
) -> tuple[PrivateObject, ...]:
    def raw(item: PrivateObject) -> Any:
        return item.object_type if field == "object_type" else item.attributes.get(field)

    if any(raw(item) is None for item in values):
        raise ConstructionError("rank_field_unavailable")
    if all(isinstance(raw(item), (int, float)) and not isinstance(raw(item), bool) for item in values):
        return tuple(sorted(values, key=lambda item: ((-1 if descending else 1) * float(raw(item)), item.canonical_identity)))
    if all(isinstance(raw(item), (str, bool)) for item in values):
        ordered = sorted(values, key=lambda item: (str(raw(item)), item.canonical_identity))
        return tuple(reversed(ordered)) if descending else tuple(ordered)
    raise ConstructionError("rank_field_not_orderable")


def _generated_pblock(state: _State, region_value: Any, statement_id: str) -> str:
    region = state.reference(region_value, expected="region", allow_many=False)[0]
    return state.context.registry.register(object_type="pblock", canonical_identity=f"wp013:{statement_id}:{region.canonical_identity}", attributes={"region_handle": region.handle})


def _validate_predicate(node: Any) -> None:
    count, depth = _predicate_shape(node)
    if count > MAX_PREDICATE_NODES: raise ConstructionError("predicate_node_limit_exceeded")
    if depth > MAX_PREDICATE_DEPTH: raise ConstructionError("predicate_depth_limit_exceeded")


def _predicate_shape(node: Any) -> tuple[int, int]:
    if not isinstance(node, Mapping) or "op" not in node: raise ConstructionError("predicate_node_invalid")
    op = node["op"]
    if op in {"AND", "OR"}:
        _exact_fields(node, {"op", "children"}, "predicate")
        children = node["children"]
        if not isinstance(children, list) or not 2 <= len(children) <= 8: raise ConstructionError("predicate_children_invalid")
        shapes = [_predicate_shape(child) for child in children]
        return 1 + sum(x[0] for x in shapes), 1 + max(x[1] for x in shapes)
    if op == "NOT":
        _exact_fields(node, {"op", "child"}, "predicate"); count, depth = _predicate_shape(node["child"]); return count + 1, depth + 1
    if op == "COMPARE":
        _exact_fields(node, {"op", "field", "operator", "value"}, "predicate")
        if node["field"] not in _PREDICATE_FIELDS or node["operator"] not in _COMPARE_OPS: raise ConstructionError("predicate_compare_not_allowed")
        value = node["value"]
        if isinstance(value, float) and not math.isfinite(value): raise ConstructionError("predicate_value_nonfinite")
        if not isinstance(value, (str, int, float, bool, list)): raise ConstructionError("predicate_value_invalid")
        return 1, 1
    raise ConstructionError("predicate_op_not_allowed")


def _eval_predicate(node: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    op = node["op"]
    if op == "AND": return all(_eval_predicate(x, row) for x in node["children"])
    if op == "OR": return any(_eval_predicate(x, row) for x in node["children"])
    if op == "NOT": return not _eval_predicate(node["child"], row)
    left, right, operator = row.get(node["field"]), node["value"], node["operator"]
    if operator == "EQ": return left == right
    if operator == "NE": return left != right
    if operator == "IN": return isinstance(right, list) and left in right
    if not isinstance(left, (int, float)) or isinstance(left, bool) or not isinstance(right, (int, float)) or isinstance(right, bool): return False
    return {"LT": left < right, "LE": left <= right, "GT": left > right, "GE": left >= right}[operator]


def validate_construction(program: Any, context: CompileContext) -> dict[str, Any]:
    try:
        if not isinstance(program, Mapping): raise ConstructionError("program_not_object")
        language = program.get("language")
        compiled = compile_p(program, context) if language == "P" else compile_g(program, context) if language == "G" else compile_macro(str(program.get("macro", "")).removeprefix("macro."), context) if language == "M" else (_raise("unknown_language"))
        return {"schema_version": CONSTRUCTION_VALIDATOR_V1, "accepted": True, "feedback": [], "compiled_sha256": compiled.public_sha256, "heavy_calls": compiled.heavy_calls, "light_mutations": compiled.light_mutations, "selected_count": compiled.selected_count, "transformed_count": compiled.transformed_count}
    except (ConstructionError, HandleError) as exc:
        reason = getattr(exc, "reason", "construction_failure")
        return {"schema_version": CONSTRUCTION_VALIDATOR_V1, "accepted": False, "feedback": [reason], "compiled_sha256": None, "heavy_calls": None, "light_mutations": None, "selected_count": None, "transformed_count": None}


def _finish(
    language: str,
    source: Mapping[str, Any],
    initial: str,
    final: str,
    steps: list[HostStep],
    *,
    binding_records: tuple[Mapping[str, Any], ...] = (),
) -> CompiledProgram:
    _check_budget(steps)
    source_hash = hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    heavy = sum(x.heavy_cost for x in steps)
    light = sum(x.light_cost for x in steps)
    payload = {"schema_version": f"dof.single-submission.compiled-{language.lower()}.v1", "language_version": P_LANGUAGE_V1 if language == "P" else G_AST_LANGUAGE_V1 if language == "G" else "dof.single-submission.m.v1", "source_sha256": source_hash, "initial_stage": initial, "final_stage": final, "heavy_calls": heavy, "light_mutations": light, "selected_count": sum(x.selected_count for x in steps), "transformed_count": sum(x.transformed_count for x in steps), "host_steps": [x.public() for x in steps], "binding_records": [dict(row) for row in binding_records]}
    raw = canonical_json_bytes(payload)
    private_hash = hashlib.sha256(canonical_json_bytes([x.private() for x in steps])).hexdigest()
    return CompiledProgram(language, source_hash, initial, final, tuple(steps), heavy, light, payload["selected_count"], payload["transformed_count"], raw, hashlib.sha256(raw).hexdigest(), private_hash, binding_records)


def _check_budget(steps: Sequence[HostStep]) -> None:
    if sum(x.heavy_cost for x in steps) > MAX_HEAVY_CALLS: raise ConstructionError("static_heavy_budget_overflow")
    if sum(x.light_cost for x in steps) > MAX_LIGHT_MUTATIONS: raise ConstructionError("static_light_budget_overflow")


def _validate_compatibility(targets: Sequence[PrivateObject], destination: PrivateObject) -> None:
    allowed = destination.attributes.get("compatible_resource_types")
    if isinstance(allowed, (list, tuple)):
        for target in targets:
            if target.attributes.get("resource_type") not in allowed: raise ConstructionError("hardblock_destination_incompatible")


def _exact_fields(value: Mapping[str, Any], allowed: set[str], label: str, *, required: set[str] | None = None) -> None:
    required = allowed if required is None else required
    if set(value) - allowed or not required <= set(value): raise ConstructionError(f"{label.lower()}_fields_invalid")


def _empty_args(args: Mapping[str, Any], label: str) -> None:
    if args: raise ConstructionError(f"{label.lower()}_args_must_be_empty")


def _no_bind(bind: Any) -> None:
    if bind is not None: raise ConstructionError("operation_must_not_bind")


def _require_stage(state: _State, required: str) -> None:
    if state.stage != required: raise ConstructionError("invalid_stage")


def _num(value: Any, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)): return float(value)
    return default


def _raise(reason: str) -> Any:
    raise ConstructionError(reason)


__all__ = ["CompileContext", "CompiledProgram", "ConstructionError", "HostStep", "compile_g", "compile_macro", "compile_p", "validate_construction"]
