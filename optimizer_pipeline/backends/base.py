"""Backend-neutral action, execution, probe, and lifecycle contracts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import AsyncIterator, Mapping, Protocol, runtime_checkable

from optimizer_pipeline.core.domain import (
    ActionSpecification,
    ArtifactReference,
    Attempt,
    FrozenMap,
    OutcomeStatus,
    frozen_map,
    utc_now,
)
from optimizer_pipeline.core.domain.value import freeze_value


class BackendContractError(ValueError):
    pass


class UnsupportedActionError(BackendContractError):
    pass


class InvalidActionParameters(BackendContractError):
    pass


class UnsafeManagedActionError(BackendContractError):
    pass


class ParameterType(str, Enum):
    ANY = "ANY"
    STRING = "STRING"
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"


class AnalysisProbeKind(str, Enum):
    TIMING_SUMMARY = "TIMING_SUMMARY"
    ROUTE_STATUS = "ROUTE_STATUS"
    UTILIZATION = "UTILIZATION"


def _required(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BackendContractError(f"{name} must be a non-empty string")


def _metadata(instance: object, name: str) -> None:
    value = getattr(instance, name)
    if isinstance(value, FrozenMap):
        return
    if isinstance(value, Mapping):
        object.__setattr__(instance, name, frozen_map(value))
        return
    raise TypeError(f"{name} must be FrozenMap or mapping")


def _matches(value: object, expected: ParameterType) -> bool:
    if expected is ParameterType.ANY:
        return True
    if expected is ParameterType.STRING:
        return isinstance(value, str)
    if expected is ParameterType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is ParameterType.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class ActionParameter:
    name: str
    value_type: ParameterType = ParameterType.ANY
    required: bool = False
    allowed_values: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        _required("parameter name", self.name)
        object.__setattr__(
            self,
            "allowed_values",
            tuple(freeze_value(value) for value in self.allowed_values),
        )

    def validate(self, value: object) -> None:
        if not _matches(value, self.value_type):
            raise InvalidActionParameters(
                f"parameter {self.name!r} must be {self.value_type.value.lower()}"
            )
        frozen = freeze_value(value)
        if self.allowed_values and frozen not in self.allowed_values:
            raise InvalidActionParameters(
                f"parameter {self.name!r} must be one of {self.allowed_values!r}"
            )


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    name: str
    summary: str
    parameters: tuple[ActionParameter, ...] = ()
    allow_extra_parameters: bool = False
    managed_safe: bool = True

    def __post_init__(self) -> None:
        _required("action name", self.name)
        _required("action summary", self.summary)
        object.__setattr__(self, "parameters", tuple(self.parameters))
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise BackendContractError(f"action {self.name!r} has duplicate parameter names")

    def validate(self, values: Mapping[str, object] | FrozenMap) -> FrozenMap:
        supplied = values.to_dict() if isinstance(values, FrozenMap) else dict(values)
        definitions = {parameter.name: parameter for parameter in self.parameters}
        missing = [
            parameter.name
            for parameter in self.parameters
            if parameter.required and parameter.name not in supplied
        ]
        if missing:
            raise InvalidActionParameters(
                f"action {self.name!r} is missing required parameters: {', '.join(missing)}"
            )
        extras = sorted(set(supplied) - set(definitions))
        if extras and not self.allow_extra_parameters:
            raise InvalidActionParameters(
                f"action {self.name!r} received unknown parameters: {', '.join(extras)}"
            )
        for name, value in supplied.items():
            if name in definitions:
                definitions[name].validate(value)
        return frozen_map(supplied)


@dataclass(frozen=True, slots=True)
class ActionCatalog:
    actions: tuple[ActionDefinition, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))
        names = [action.name for action in self.actions]
        if len(names) != len(set(names)):
            raise BackendContractError("action catalog contains duplicate action names")

    def definition(self, name: str) -> ActionDefinition:
        for action in self.actions:
            if action.name == name:
                return action
        raise UnsupportedActionError(f"backend does not support action {name!r}")

    def validate(
        self,
        action: ActionSpecification,
        *,
        require_managed_safe: bool = False,
    ) -> ActionDefinition:
        definition = self.definition(action.action_type)
        definition.validate(action.parameters)
        if require_managed_safe and not definition.managed_safe:
            raise UnsafeManagedActionError(
                f"action {action.action_type!r} is not allowed in a managed path"
            )
        return definition


@dataclass(frozen=True, slots=True)
class BackendExecutionRequest:
    attempt: Attempt
    action: ActionSpecification
    input_artifact: ArtifactReference
    output_uri: str
    workspace_uri: str

    def __post_init__(self) -> None:
        _required("output_uri", self.output_uri)
        _required("workspace_uri", self.workspace_uri)
        if self.attempt.action_id != self.action.action_id:
            raise BackendContractError("attempt and action IDs do not match")
        if self.attempt.run_id != self.action.run_id or self.attempt.run_id != self.input_artifact.run_id:
            raise BackendContractError("execution request facts must belong to one run")
        if self.attempt.scope_id != self.action.scope_id:
            raise BackendContractError("attempt and action must belong to one scope")
        if self.attempt.scope_id != self.input_artifact.scope_id:
            raise BackendContractError("input artifact must belong to the execution scope")


@dataclass(frozen=True, slots=True)
class BackendExecutionResult:
    status: OutcomeStatus
    started_at: datetime
    finished_at: datetime
    output_uri: str | None = None
    metrics: FrozenMap = field(default_factory=FrozenMap)
    log_uris: tuple[str, ...] = ()
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None:
            raise BackendContractError("backend result timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise BackendContractError("backend result cannot finish before it starts")
        if self.status is OutcomeStatus.SUCCEEDED and not self.output_uri:
            raise BackendContractError("successful backend result requires output_uri")
        _metadata(self, "metrics")
        object.__setattr__(self, "log_uris", tuple(str(uri) for uri in self.log_uris))


@dataclass(frozen=True, slots=True)
class AnalysisProbeRequest:
    artifact: ArtifactReference
    probes: tuple[AnalysisProbeKind, ...]
    workspace_uri: str

    def __post_init__(self) -> None:
        _required("workspace_uri", self.workspace_uri)
        object.__setattr__(self, "probes", tuple(self.probes))
        if not self.probes:
            raise BackendContractError("analysis probe request requires at least one probe")
        if len(self.probes) != len(set(self.probes)):
            raise BackendContractError("analysis probe request contains duplicate probes")


@dataclass(frozen=True, slots=True)
class AnalysisProbeResult:
    artifact_id: str
    evidence: FrozenMap
    observed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _required("artifact_id", self.artifact_id)
        if self.observed_at.utcoffset() is None:
            raise BackendContractError("observed_at must be timezone-aware")
        _metadata(self, "evidence")


@runtime_checkable
class Backend(Protocol):
    name: str
    catalog: ActionCatalog

    async def start(self) -> None: ...
    async def execute(self, request: BackendExecutionRequest) -> BackendExecutionResult: ...
    async def probe(self, request: AnalysisProbeRequest) -> AnalysisProbeResult: ...
    async def close(self) -> None: ...


@asynccontextmanager
async def backend_session(backend: Backend) -> AsyncIterator[Backend]:
    """Start once and guarantee close after success or failure."""

    await backend.start()
    try:
        yield backend
    finally:
        await backend.close()
