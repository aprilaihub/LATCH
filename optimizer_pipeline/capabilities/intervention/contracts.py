"""Typed, provider-neutral contracts for managed intervention episodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping

from optimizer_pipeline.backends import ActionCatalog
from optimizer_pipeline.core.domain import (
    ActionSpecification,
    FrozenMap,
    ManagedStateFacts,
    ParentAuthorization,
    ParentUseMode,
    authorize_parent,
    frozen_map,
)


class InterventionContractError(ValueError):
    pass


class InterventionKind(str, Enum):
    DETERMINISTIC_RECIPE = "DETERMINISTIC_RECIPE"
    SYNTHESIZED_RECIPE = "SYNTHESIZED_RECIPE"
    MANUAL_PLAN = "MANUAL_PLAN"


FORBIDDEN_ACTION_NAMES = frozenset({"shell", "bash", "run_tcl", "raw_tcl", "write_checkpoint"})
FORBIDDEN_PARAMETER_PARTS = ("command", "shell", "script", "raw_tcl", "tcl_source")


def _required(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InterventionContractError(f"{name} must be a non-empty string")


def _metadata(instance: object, name: str) -> None:
    value = getattr(instance, name)
    if isinstance(value, FrozenMap):
        return
    if isinstance(value, Mapping):
        object.__setattr__(instance, name, frozen_map(value))
        return
    raise TypeError(f"{name} must be FrozenMap or mapping")


def _guard_action(action_name: str, parameters: FrozenMap) -> None:
    lowered = action_name.strip().lower()
    if lowered in FORBIDDEN_ACTION_NAMES or lowered.startswith(("shell.", "tcl.")):
        raise InterventionContractError(f"raw command action is forbidden: {action_name}")
    forbidden = sorted(
        key
        for key, _ in parameters.items
        if any(part in key.lower() for part in FORBIDDEN_PARAMETER_PARTS)
    )
    if forbidden:
        raise InterventionContractError(
            "raw command parameters are forbidden: " + ", ".join(forbidden)
        )


@dataclass(frozen=True, slots=True)
class InterventionPrimitive:
    """Allowlisted intervention primitive mapped to one typed backend action."""

    name: str
    action_name: str
    fixed_parameters: FrozenMap = field(default_factory=FrozenMap)
    summary: str = "Managed intervention primitive"

    def __post_init__(self) -> None:
        _required("primitive name", self.name)
        _required("action_name", self.action_name)
        _required("summary", self.summary)
        _metadata(self, "fixed_parameters")
        _guard_action(self.action_name, self.fixed_parameters)


@dataclass(frozen=True, slots=True)
class InterventionStep:
    step_id: str
    primitive: str
    parameters: FrozenMap = field(default_factory=FrozenMap)
    rationale: str = ""
    expected_observation: str = ""

    def __post_init__(self) -> None:
        _required("step_id", self.step_id)
        _required("primitive", self.primitive)
        _metadata(self, "parameters")
        _guard_action(self.primitive, self.parameters)


@dataclass(frozen=True, slots=True)
class InterventionDiagnosis:
    diagnosis_id: str
    state_id: str
    summary: str
    evidence: FrozenMap = field(default_factory=FrozenMap)
    prefer_primitives: tuple[str, ...] = ()
    avoid_primitives: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required("diagnosis_id", self.diagnosis_id)
        _required("state_id", self.state_id)
        _required("summary", self.summary)
        _metadata(self, "evidence")
        object.__setattr__(self, "prefer_primitives", tuple(self.prefer_primitives))
        object.__setattr__(self, "avoid_primitives", tuple(self.avoid_primitives))
        if set(self.prefer_primitives) & set(self.avoid_primitives):
            raise InterventionContractError("a primitive cannot be both preferred and avoided")


@dataclass(frozen=True, slots=True)
class InterventionSpec:
    intervention_id: str
    kind: InterventionKind
    parent_state_id: str
    steps: tuple[InterventionStep, ...]
    source: str
    hypothesis: str = ""
    diagnosis_id: str | None = None
    revise_conditions: tuple[str, ...] = ()
    abort_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("intervention_id", "parent_state_id", "source"):
            _required(name, getattr(self, name))
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "revise_conditions", tuple(self.revise_conditions))
        object.__setattr__(self, "abort_conditions", tuple(self.abort_conditions))
        if not self.steps:
            raise InterventionContractError("intervention must contain at least one step")
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise InterventionContractError("intervention step IDs must be unique")


@dataclass(frozen=True, slots=True)
class InterventionParentPolicy:
    mode: ParentUseMode = ParentUseMode.NORMAL
    reason: str = "managed intervention"

    def __post_init__(self) -> None:
        _required("parent policy reason", self.reason)


@dataclass(frozen=True, slots=True)
class InterventionLoopPolicy:
    max_attempts: int
    max_episodes: int = 1
    stop_on_first_selection_eligible: bool = True
    allow_invalid_continuation: bool = False

    def __post_init__(self) -> None:
        if self.max_attempts <= 0 or self.max_episodes <= 0:
            raise InterventionContractError("loop budgets must be positive")
        if self.allow_invalid_continuation and self.max_attempts < 2:
            raise InterventionContractError("invalid continuation requires budget for another attempt")


@dataclass(frozen=True, slots=True)
class CompiledIntervention:
    spec: InterventionSpec
    actions: tuple[ActionSpecification, ...]


class InterventionCatalog:
    def __init__(self, primitives: tuple[InterventionPrimitive, ...]) -> None:
        names = [primitive.name for primitive in primitives]
        if len(names) != len(set(names)):
            raise InterventionContractError("intervention catalog has duplicate primitive names")
        self._primitives = {primitive.name: primitive for primitive in primitives}

    def compile(
        self,
        spec: InterventionSpec,
        *,
        run_id: str,
        scope_id: str,
        backend_catalog: ActionCatalog,
        action_id: Callable[[int, InterventionStep], str],
    ) -> CompiledIntervention:
        actions: list[ActionSpecification] = []
        for index, step in enumerate(spec.steps):
            primitive = self._primitives.get(step.primitive)
            if primitive is None:
                raise InterventionContractError(f"unknown intervention primitive: {step.primitive}")
            parameters = primitive.fixed_parameters.to_dict()
            parameters.update(step.parameters.to_dict())
            action = ActionSpecification(
                action_id(index, step), run_id, scope_id,
                primitive.action_name, parameters,
            )
            backend_catalog.validate(action, require_managed_safe=True)
            actions.append(action)
        return CompiledIntervention(spec, tuple(actions))


def authorize_intervention_parent(
    spec: InterventionSpec,
    facts: ManagedStateFacts,
    policy: InterventionParentPolicy,
) -> ParentAuthorization:
    if spec.parent_state_id != facts.state.state_id:
        raise InterventionContractError("intervention spec names another parent state")
    return authorize_parent(facts, mode=policy.mode, reason=policy.reason)
