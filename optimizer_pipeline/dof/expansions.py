"""Versioned action-to-host expansions for WP-004 technical execution.

The abstract L1/L2 and L3 catalogues remain policy-facing capability
contracts.  This module separately binds each admitted action to a closed,
ordered list of registered host operations.  It contains no raw Tcl, shell,
benchmark identity, filesystem argument, or site-specific command string.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any

from .catalogues import (
    clean_macro_catalogue,
    nested_action_catalogue,
    strict_l3_catalogue,
)
from .contracts import DesignStage, DoFLevel, EventClass
from .generated import (
    GENERATED_ACTION_KEY,
    GENERATED_OPERATION_ID,
    validate_generated_program,
)
from .toolcalls import (
    CallRule,
    DeclaredCallPlan,
    ToolCallContractError,
    ToolCallRegistry,
    default_tool_call_registry,
    e1v2_tool_call_registry,
)


REAL_EXPANSION_SCHEMA_VERSION = "dof.real-action-expansion.v1"
REAL_EXPANSION_VERSION = "dof.xilinx-real-action-expansion.v1"
E1V2_REAL_EXPANSION_VERSION = "dof.xilinx-real-action-expansion.e1v2.v1"
REAL_ACTION_CALL_PLAN_VERSION = "dof.real-action-call-plan.v1"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        _canonical_bytes(value)
        return value
    raise TypeError(f"value is not JSON-compatible:{type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class StageEffect(str, Enum):
    PRESERVE = "PRESERVE"
    SET_PLACED = "SET_PLACED"
    SET_UNPLACED = "SET_UNPLACED"
    SET_ROUTED = "SET_ROUTED"

    def apply(self, current: DesignStage) -> DesignStage:
        if self is StageEffect.PRESERVE:
            return current
        return {
            StageEffect.SET_PLACED: DesignStage.PLACED,
            StageEffect.SET_UNPLACED: DesignStage.UNPLACED,
            StageEffect.SET_ROUTED: DesignStage.ROUTED,
        }[self]


@dataclass(frozen=True, slots=True)
class ExpansionStep:
    operation_id: str
    required_stage: DesignStage | None
    stage_effect: StageEffect
    fixed_arguments: Mapping[str, Any] = field(default_factory=dict)
    directive_from_action: bool = False
    parameters_from_action: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("expansion operation_id must be non-empty")
        if self.required_stage is not None and not isinstance(
            self.required_stage, DesignStage
        ):
            raise TypeError("required_stage must be DesignStage or None")
        if not isinstance(self.stage_effect, StageEffect):
            raise TypeError("stage_effect must be StageEffect")
        if not isinstance(self.directive_from_action, bool):
            raise TypeError("directive_from_action must be bool")
        if not isinstance(self.parameters_from_action, bool):
            raise TypeError("parameters_from_action must be bool")
        frozen = _freeze_json(self.fixed_arguments)
        if not isinstance(frozen, Mapping):
            raise TypeError("fixed_arguments must be an object")
        if self.directive_from_action and frozen:
            raise ValueError("dynamic directive step cannot also have fixed arguments")
        if self.parameters_from_action and (self.directive_from_action or frozen):
            raise ValueError("full action parameters cannot be combined with other arguments")
        object.__setattr__(self, "fixed_arguments", frozen)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "operation_id": self.operation_id,
            "required_stage": (
                None if self.required_stage is None else self.required_stage.value
            ),
            "stage_effect": self.stage_effect.value,
            "fixed_arguments": _thaw_json(self.fixed_arguments),
            "directive_from_action": self.directive_from_action,
        }
        if self.parameters_from_action:
            payload["parameters_from_action"] = True
        return payload


@dataclass(frozen=True, slots=True)
class ActionExpansion:
    action_key: str
    level_contract: str
    allowed_initial_stages: tuple[DesignStage, ...]
    steps: tuple[ExpansionStep, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.action_key, str) or not self.action_key.strip():
            raise ValueError("action_key must be non-empty")
        if self.level_contract not in {
            "L1_L2_SHARED",
            DoFLevel.L3.value,
            "L4_GENERATED",
        }:
            raise ValueError("unknown expansion level contract")
        stages = tuple(self.allowed_initial_stages)
        if not stages or any(not isinstance(item, DesignStage) for item in stages):
            raise ValueError("allowed_initial_stages must be typed and non-empty")
        steps = tuple(self.steps)
        if not steps or any(not isinstance(item, ExpansionStep) for item in steps):
            raise ValueError("action expansion requires typed steps")
        object.__setattr__(self, "allowed_initial_stages", stages)
        object.__setattr__(self, "steps", steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_key": self.action_key,
            "level_contract": self.level_contract,
            "allowed_initial_stages": [
                item.value for item in self.allowed_initial_stages
            ],
            "steps": [item.to_dict() for item in self.steps],
        }


@dataclass(frozen=True, slots=True)
class ExpandedHostCall:
    operation_id: str
    arguments: Mapping[str, Any]
    input_stage: DesignStage
    output_stage: DesignStage
    event_class: EventClass

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "arguments": _thaw_json(self.arguments),
            "input_stage": self.input_stage.value,
            "output_stage": self.output_stage.value,
            "event_class": self.event_class.value,
        }


@dataclass(frozen=True, slots=True)
class ExpandedAction:
    action_key: str
    level: DoFLevel
    initial_stage: DesignStage
    final_stage: DesignStage
    calls: tuple[ExpandedHostCall, ...]
    expansion_manifest_hash: str

    @property
    def heavy_call_count(self) -> int:
        return sum(
            item.event_class is EventClass.IMPLEMENTATION_HEAVY
            for item in self.calls
        )

    @property
    def light_call_count(self) -> int:
        return sum(
            item.event_class is EventClass.MUTATION_LIGHT for item in self.calls
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_key": self.action_key,
            "level": self.level.value,
            "initial_stage": self.initial_stage.value,
            "final_stage": self.final_stage.value,
            "calls": [item.to_dict() for item in self.calls],
            "heavy_call_count": self.heavy_call_count,
            "light_call_count": self.light_call_count,
            "expansion_manifest_hash": self.expansion_manifest_hash,
        }


@dataclass(frozen=True, slots=True)
class RealActionExpansionManifest:
    registry_hash: str
    shared_macro_catalogue_hash: str
    strict_l3_catalogue_hash: str
    actions: tuple[ActionExpansion, ...]
    generated_catalogue_hash: str | None = None
    version: str = REAL_EXPANSION_VERSION
    schema_version: str = REAL_EXPANSION_SCHEMA_VERSION
    _by_action: Mapping[str, ActionExpansion] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        hashes = [
            ("registry_hash", self.registry_hash),
            ("shared_macro_catalogue_hash", self.shared_macro_catalogue_hash),
            ("strict_l3_catalogue_hash", self.strict_l3_catalogue_hash),
        ]
        if self.generated_catalogue_hash is not None:
            hashes.append(("generated_catalogue_hash", self.generated_catalogue_hash))
        for name, value in hashes:
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ValueError(f"{name} must be SHA-256")
        actions = tuple(sorted(self.actions, key=lambda item: item.action_key))
        names = [item.action_key for item in actions]
        if len(names) != len(set(names)):
            raise ValueError("expansion action keys must be unique")
        object.__setattr__(self, "actions", actions)
        object.__setattr__(
            self,
            "_by_action",
            MappingProxyType({item.action_key: item for item in actions}),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "version": self.version,
            "registry_hash": self.registry_hash,
            "abstract_catalogue_hashes": {
                "L1_L2_SHARED": self.shared_macro_catalogue_hash,
                "L3": self.strict_l3_catalogue_hash,
            },
            "actions": [item.to_dict() for item in self.actions],
        }
        if self.generated_catalogue_hash is not None:
            payload["abstract_catalogue_hashes"]["L4_GENERATED"] = (
                self.generated_catalogue_hash
            )
        return payload

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def action(self, action_key: str) -> ActionExpansion:
        try:
            return self._by_action[action_key]
        except KeyError as exc:
            raise ToolCallContractError(
                f"unknown_real_action_expansion:{action_key}"
            ) from exc

    def validate_against(self, registry: ToolCallRegistry) -> None:
        if self.registry_hash != registry.sha256:
            raise ToolCallContractError("expansion_registry_hash_mismatch")
        expected_actions = {
            item.name for item in clean_macro_catalogue(DoFLevel.L1).actions
        } | {item.name for item in strict_l3_catalogue().actions}
        if self.generated_catalogue_hash is not None:
            expected_actions.add(GENERATED_ACTION_KEY)
        if set(self._by_action) != expected_actions:
            raise ToolCallContractError("expansion_action_surface_mismatch")
        for action in self.actions:
            for step in action.steps:
                if any(
                    forbidden in step.operation_id
                    for forbidden in ("mock.", "flow.", "run_tcl", "shell")
                ):
                    raise ToolCallContractError(
                        f"forbidden_real_expansion:{step.operation_id}"
                    )
                operation = registry.operation(step.operation_id)
                if not operation.mutates_design:
                    raise ToolCallContractError(
                        f"nonmutating_action_step:{step.operation_id}"
                    )
                if operation.event_class not in {
                    EventClass.MUTATION_LIGHT,
                    EventClass.IMPLEMENTATION_HEAVY,
                }:
                    raise ToolCallContractError(
                        f"invalid_action_event_class:{step.operation_id}"
                    )


def real_action_expansion_manifest(
    registry: ToolCallRegistry | None = None,
) -> RealActionExpansionManifest:
    active = registry or default_tool_call_registry()
    shared = clean_macro_catalogue(DoFLevel.L1)
    if shared.canonical_bytes() != clean_macro_catalogue(DoFLevel.L2).canonical_bytes():
        raise RuntimeError("L1/L2 abstract macro catalogue bytes diverged")
    l3 = strict_l3_catalogue()
    routed_or_placed = (DesignStage.ROUTED, DesignStage.PLACED)
    actions = (
        ActionExpansion(
            "macro.phys_opt",
            "L1_L2_SHARED",
            routed_or_placed,
            (
                ExpansionStep(
                    "vivado.phys_opt_design",
                    None,
                    StageEffect.PRESERVE,
                    {"directive": "Explore"},
                ),
            ),
        ),
        ActionExpansion(
            "macro.retime",
            "L1_L2_SHARED",
            routed_or_placed,
            (
                ExpansionStep(
                    "vivado.phys_opt_design",
                    None,
                    StageEffect.PRESERVE,
                    {"directive": "AddRetime"},
                ),
            ),
        ),
        ActionExpansion(
            "macro.place_route",
            "L1_L2_SHARED",
            (DesignStage.ROUTED,),
            (
                ExpansionStep(
                    "vivado.unroute_all",
                    DesignStage.ROUTED,
                    StageEffect.SET_PLACED,
                ),
                ExpansionStep(
                    "vivado.unplace_all",
                    DesignStage.PLACED,
                    StageEffect.SET_UNPLACED,
                ),
                ExpansionStep(
                    "vivado.place_design",
                    DesignStage.UNPLACED,
                    StageEffect.SET_PLACED,
                    {"directive": "Default"},
                ),
                ExpansionStep(
                    "vivado.route_design",
                    DesignStage.PLACED,
                    StageEffect.SET_ROUTED,
                    {"directive": "Default"},
                ),
            ),
        ),
        ActionExpansion(
            "manual.unroute_all",
            DoFLevel.L3.value,
            (DesignStage.ROUTED,),
            (
                ExpansionStep(
                    "vivado.unroute_all",
                    DesignStage.ROUTED,
                    StageEffect.SET_PLACED,
                ),
            ),
        ),
        ActionExpansion(
            "manual.unplace_all",
            DoFLevel.L3.value,
            (DesignStage.PLACED,),
            (
                ExpansionStep(
                    "vivado.unplace_all",
                    DesignStage.PLACED,
                    StageEffect.SET_UNPLACED,
                ),
            ),
        ),
        ActionExpansion(
            "manual.place_design",
            DoFLevel.L3.value,
            (DesignStage.UNPLACED,),
            (
                ExpansionStep(
                    "vivado.place_design",
                    DesignStage.UNPLACED,
                    StageEffect.SET_PLACED,
                    directive_from_action=True,
                ),
            ),
        ),
        ActionExpansion(
            "manual.route_design",
            DoFLevel.L3.value,
            (DesignStage.PLACED,),
            (
                ExpansionStep(
                    "vivado.route_design",
                    DesignStage.PLACED,
                    StageEffect.SET_ROUTED,
                    directive_from_action=True,
                ),
            ),
        ),
        ActionExpansion(
            "manual.phys_opt_design",
            DoFLevel.L3.value,
            routed_or_placed,
            (
                ExpansionStep(
                    "vivado.phys_opt_design",
                    None,
                    StageEffect.PRESERVE,
                    directive_from_action=True,
                ),
            ),
        ),
    )
    manifest = RealActionExpansionManifest(
        active.sha256,
        shared.sha256,
        l3.sha256,
        actions,
    )
    manifest.validate_against(active)
    return manifest


def real_action_expansion_manifest_v2(
    registry: ToolCallRegistry | None = None,
) -> RealActionExpansionManifest:
    active = registry or e1v2_tool_call_registry()
    base = real_action_expansion_manifest(default_tool_call_registry())
    generated = nested_action_catalogue(DoFLevel.L4)
    manifest = RealActionExpansionManifest(
        active.sha256,
        base.shared_macro_catalogue_hash,
        base.strict_l3_catalogue_hash,
        base.actions
        + (
            ActionExpansion(
                GENERATED_ACTION_KEY,
                "L4_GENERATED",
                (DesignStage.PLACED, DesignStage.ROUTED),
                (
                    ExpansionStep(
                        GENERATED_OPERATION_ID,
                        None,
                        StageEffect.PRESERVE,
                        parameters_from_action=True,
                    ),
                ),
            ),
        ),
        generated_catalogue_hash=generated.sha256,
        version=E1V2_REAL_EXPANSION_VERSION,
    )
    manifest.validate_against(active)
    return manifest


def expand_real_action(
    level: DoFLevel,
    action_key: str,
    parameters: Mapping[str, Any],
    *,
    current_stage: DesignStage,
    registry: ToolCallRegistry | None = None,
    manifest: RealActionExpansionManifest | None = None,
) -> ExpandedAction:
    if not isinstance(level, DoFLevel):
        raise TypeError("level must be DoFLevel")
    if not isinstance(current_stage, DesignStage):
        raise TypeError("current_stage must be DesignStage")
    if not isinstance(parameters, Mapping):
        raise TypeError("action parameters must be an object")
    active_registry = registry or default_tool_call_registry()
    active_manifest = manifest or real_action_expansion_manifest(active_registry)
    active_manifest.validate_against(active_registry)
    action = active_manifest.action(action_key)
    if active_manifest.generated_catalogue_hash is None:
        permitted_contracts = {
            DoFLevel.L1: {"L1_L2_SHARED"},
            DoFLevel.L2: {"L1_L2_SHARED"},
            DoFLevel.L3: {DoFLevel.L3.value},
            DoFLevel.L4: set(),
        }[level]
    else:
        permitted_contracts = {
            DoFLevel.L1: {"L1_L2_SHARED"},
            DoFLevel.L2: {"L1_L2_SHARED"},
            DoFLevel.L3: {"L1_L2_SHARED", DoFLevel.L3.value},
            DoFLevel.L4: {"L1_L2_SHARED", DoFLevel.L3.value, "L4_GENERATED"},
        }[level]
    if action.level_contract not in permitted_contracts:
        raise ToolCallContractError(
            f"action_not_available_at_level:{level.value}:{action_key}"
        )
    if current_stage not in action.allowed_initial_stages:
        raise ToolCallContractError(
            f"action_stage_precondition:{action_key}:{current_stage.value}"
        )

    public_parameters = {str(key): value for key, value in parameters.items()}
    if active_manifest.generated_catalogue_hash is not None and level in {
        DoFLevel.L2,
        DoFLevel.L3,
        DoFLevel.L4,
    }:
        abstract = nested_action_catalogue(level).action(action_key)
    elif level in {DoFLevel.L1, DoFLevel.L2}:
        abstract = clean_macro_catalogue(level).action(action_key)
    else:
        abstract = strict_l3_catalogue().action(action_key)
    if abstract is None:
        raise ToolCallContractError(f"action_missing_from_abstract_catalogue:{action_key}")
    schema = abstract.to_dict()["parameters_schema"]
    allowed_keys = set(schema["properties"])
    if set(public_parameters) != set(schema["required"]):
        raise ToolCallContractError(f"action_parameter_shape_mismatch:{action_key}")
    if set(public_parameters) - allowed_keys:
        raise ToolCallContractError(f"unknown_action_parameter:{action_key}")
    directive = public_parameters.get("directive")
    if directive is not None:
        allowed = schema["properties"]["directive"]["enum"]
        if not isinstance(directive, str) or directive not in allowed:
            raise ToolCallContractError(
                f"directive_not_allowed:{action_key}:{directive}"
            )
    if action_key == GENERATED_ACTION_KEY:
        public_parameters = validate_generated_program(public_parameters)

    stage = current_stage
    calls: list[ExpandedHostCall] = []
    for step in action.steps:
        if step.required_stage is not None and stage is not step.required_stage:
            raise ToolCallContractError(
                f"expansion_stage_mismatch:{step.operation_id}:{stage.value}"
            )
        operation = active_registry.operation(step.operation_id)
        if operation.allowed_stages and stage not in operation.allowed_stages:
            raise ToolCallContractError(
                f"registry_stage_mismatch:{step.operation_id}:{stage.value}"
            )
        arguments = (
            dict(public_parameters)
            if step.parameters_from_action
            else (
                {"directive": directive}
                if step.directive_from_action
                else _thaw_json(step.fixed_arguments)
            )
        )
        next_stage = step.stage_effect.apply(stage)
        assert isinstance(operation.event_class, EventClass)
        calls.append(
            ExpandedHostCall(
                step.operation_id,
                _freeze_json(arguments),
                stage,
                next_stage,
                operation.event_class,
            )
        )
        stage = next_stage

    return ExpandedAction(
        action_key,
        level,
        current_stage,
        stage,
        tuple(calls),
        active_manifest.sha256,
    )


def declared_action_call_plan(
    expanded: ExpandedAction,
    registry: ToolCallRegistry | None = None,
) -> DeclaredCallPlan:
    active = registry or default_tool_call_registry()
    plan = DeclaredCallPlan(
        REAL_ACTION_CALL_PLAN_VERSION,
        "real-action:" + hashlib.sha256(
            _canonical_bytes(expanded.to_dict())
        ).hexdigest()[:24],
        active.sha256,
        tuple(CallRule(item.operation_id) for item in expanded.calls),
        l0_read_only=False,
    )
    plan.validate_against(active)
    return plan


__all__ = [
    "ActionExpansion",
    "ExpandedAction",
    "ExpandedHostCall",
    "ExpansionStep",
    "REAL_ACTION_CALL_PLAN_VERSION",
    "REAL_EXPANSION_SCHEMA_VERSION",
    "REAL_EXPANSION_VERSION",
    "E1V2_REAL_EXPANSION_VERSION",
    "RealActionExpansionManifest",
    "StageEffect",
    "declared_action_call_plan",
    "expand_real_action",
    "real_action_expansion_manifest",
    "real_action_expansion_manifest_v2",
]
