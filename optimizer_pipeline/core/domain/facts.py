"""Immutable stable facts for managed optimization runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from .enums import (
    ArtifactKind,
    AttemptEventKind,
    BudgetEventKind,
    OutcomeStatus,
    ParentUseMode,
    RunStatus,
    ScopeKind,
    SelectionKind,
    ValidationLevel,
    ValidationStatus,
)
from .value import FrozenMap, frozen_map


class DomainInvariantError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _id(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DomainInvariantError(f"{name} must be a non-empty string")


def _time(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainInvariantError(f"{name} must be timezone-aware")


def _metadata(instance: object, name: str) -> None:
    value = getattr(instance, name)
    if isinstance(value, FrozenMap):
        return
    if isinstance(value, Mapping):
        object.__setattr__(instance, name, frozen_map(value))
        return
    raise TypeError(f"{name} must be FrozenMap or mapping")


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    mode: str
    created_at: datetime = field(default_factory=utc_now)
    metadata: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        _id("run_id", self.run_id)
        _id("mode", self.mode)
        _time("created_at", self.created_at)
        _metadata(self, "metadata")


@dataclass(frozen=True, slots=True)
class RunLifecycleEvent:
    event_id: str
    run_id: str
    sequence: int
    status: RunStatus
    occurred_at: datetime = field(default_factory=utc_now)
    detail: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        _id("event_id", self.event_id)
        _id("run_id", self.run_id)
        if self.sequence < 0:
            raise DomainInvariantError("run event sequence cannot be negative")
        _time("occurred_at", self.occurred_at)
        _metadata(self, "detail")


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    scope_id: str
    run_id: str
    kind: ScopeKind
    name: str
    parent_scope_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    metadata: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        _id("scope_id", self.scope_id)
        _id("run_id", self.run_id)
        _id("name", self.name)
        _time("created_at", self.created_at)
        _metadata(self, "metadata")
        if self.kind is ScopeKind.ROOT and self.parent_scope_id is not None:
            raise DomainInvariantError("root scope cannot have a parent")
        if self.kind is not ScopeKind.ROOT and not self.parent_scope_id:
            raise DomainInvariantError("non-root scope requires parent_scope_id")


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    run_id: str
    scope_id: str
    kind: ArtifactKind
    uri: str
    sha256: str | None = None
    size_bytes: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    metadata: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        for name, value in (("artifact_id", self.artifact_id), ("run_id", self.run_id), ("scope_id", self.scope_id), ("uri", self.uri)):
            _id(name, value)
        if self.size_bytes is not None and self.size_bytes < 0:
            raise DomainInvariantError("size_bytes cannot be negative")
        _time("created_at", self.created_at)
        _metadata(self, "metadata")


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    run_id: str
    scope_id: str
    parent_state_id: str
    action_id: str
    source: str
    proposed_at: datetime = field(default_factory=utc_now)
    policy_metadata: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        for name, value in (
            ("proposal_id", self.proposal_id),
            ("run_id", self.run_id),
            ("scope_id", self.scope_id),
            ("parent_state_id", self.parent_state_id),
            ("action_id", self.action_id),
            ("source", self.source),
        ):
            _id(name, value)
        _time("proposed_at", self.proposed_at)
        _metadata(self, "policy_metadata")


@dataclass(frozen=True, slots=True)
class ActionSpecification:
    action_id: str
    run_id: str
    scope_id: str
    action_type: str
    parameters: FrozenMap = field(default_factory=FrozenMap)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name, value in (("action_id", self.action_id), ("run_id", self.run_id), ("scope_id", self.scope_id), ("action_type", self.action_type)):
            _id(name, value)
        _metadata(self, "parameters")
        _time("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    run_id: str
    scope_id: str
    parent_state_id: str
    action_id: str
    proposal_id: str | None = None
    parent_use_mode: ParentUseMode = ParentUseMode.NORMAL
    scheduled_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name, value in (("attempt_id", self.attempt_id), ("run_id", self.run_id), ("scope_id", self.scope_id), ("parent_state_id", self.parent_state_id), ("action_id", self.action_id)):
            _id(name, value)
        _time("scheduled_at", self.scheduled_at)


@dataclass(frozen=True, slots=True)
class AttemptLifecycleEvent:
    event_id: str
    attempt_id: str
    sequence: int
    kind: AttemptEventKind
    occurred_at: datetime = field(default_factory=utc_now)
    detail: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        _id("event_id", self.event_id)
        _id("attempt_id", self.attempt_id)
        if self.sequence < 0:
            raise DomainInvariantError("attempt event sequence cannot be negative")
        _time("occurred_at", self.occurred_at)
        _metadata(self, "detail")


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    outcome_id: str
    attempt_id: str
    status: OutcomeStatus
    started_at: datetime
    finished_at: datetime
    output_artifact_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    metrics: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        _id("outcome_id", self.outcome_id)
        _id("attempt_id", self.attempt_id)
        _time("started_at", self.started_at)
        _time("finished_at", self.finished_at)
        _metadata(self, "metrics")
        if self.finished_at < self.started_at:
            raise DomainInvariantError("finished_at cannot precede started_at")
        if self.status is OutcomeStatus.SUCCEEDED and not self.output_artifact_id:
            raise DomainInvariantError("successful outcome requires output_artifact_id")


@dataclass(frozen=True, slots=True)
class DesignState:
    state_id: str
    run_id: str
    scope_id: str
    artifact_id: str
    parent_state_id: str | None = None
    produced_by_attempt_id: str | None = None
    managed: bool = True
    created_at: datetime = field(default_factory=utc_now)
    metadata: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        for name, value in (("state_id", self.state_id), ("run_id", self.run_id), ("scope_id", self.scope_id), ("artifact_id", self.artifact_id)):
            _id(name, value)
        if (self.parent_state_id is None) != (self.produced_by_attempt_id is None):
            raise DomainInvariantError("non-root state requires parent_state_id and produced_by_attempt_id")
        _time("created_at", self.created_at)
        _metadata(self, "metadata")


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    validation_id: str
    run_id: str
    scope_id: str
    subject_id: str
    level: ValidationLevel
    status: ValidationStatus
    attempt_id: str | None = None
    checked_at: datetime = field(default_factory=utc_now)
    details: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        for name, value in (("validation_id", self.validation_id), ("run_id", self.run_id), ("scope_id", self.scope_id), ("subject_id", self.subject_id)):
            _id(name, value)
        if self.level is ValidationLevel.ATTEMPT and not self.attempt_id:
            raise DomainInvariantError("Attempt Validation requires attempt_id")
        _time("checked_at", self.checked_at)
        _metadata(self, "details")


@dataclass(frozen=True, slots=True)
class StateEligibility:
    eligibility_id: str
    run_id: str
    state_id: str
    parent_eligible: bool
    selection_eligible: bool
    reason: str
    recorded_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name, value in (("eligibility_id", self.eligibility_id), ("run_id", self.run_id), ("state_id", self.state_id), ("reason", self.reason)):
            _id(name, value)
        _time("recorded_at", self.recorded_at)


@dataclass(frozen=True, slots=True)
class BudgetEvent:
    budget_event_id: str
    run_id: str
    scope_id: str
    attempt_id: str
    kind: BudgetEventKind
    units: int = 1
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name, value in (("budget_event_id", self.budget_event_id), ("run_id", self.run_id), ("scope_id", self.scope_id), ("attempt_id", self.attempt_id)):
            _id(name, value)
        if self.units <= 0:
            raise DomainInvariantError("budget units must be positive")
        _time("occurred_at", self.occurred_at)


@dataclass(frozen=True, slots=True)
class ResultSelection:
    selection_id: str
    run_id: str
    scope_id: str
    state_id: str
    artifact_id: str
    kind: SelectionKind
    reason: str
    selected_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name, value in (("selection_id", self.selection_id), ("run_id", self.run_id), ("scope_id", self.scope_id), ("state_id", self.state_id), ("artifact_id", self.artifact_id), ("reason", self.reason)):
            _id(name, value)
        _time("selected_at", self.selected_at)
