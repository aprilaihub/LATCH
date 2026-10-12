"""Observed host-call contracts for controlled DoF execution.

This module is deliberately independent of the legacy contest runner and of
the mock clock in :mod:`optimizer_pipeline.dof.runtime`.  It provides the
small, content-addressed boundary needed by a later real preflight:

* a closed registry classifies operations before invocation;
* a declared plan constrains order and cardinality;
* an actual-event budget is consumed immediately before a real call starts;
* an async observer records monotonic start and terminal events; and
* reconciliation compares the declared plan with the calls that really began.

No function in this module starts Vivado, RapidWright, MCP, or any subprocess.
The caller injects the host callable.  Raw arguments are used only to compute a
canonical digest; public trace rows contain a redacted structural summary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableSequence
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import re
import time
from types import MappingProxyType
from typing import Any, TypeVar

from .contracts import DesignStage, EventClass


TOOL_CALL_REGISTRY_SCHEMA_VERSION = "dof.tool-call-registry.v1"
TOOL_CALL_REGISTRY_VERSION = "dof.xilinx-tool-calls.v2"
E1V2_TOOL_CALL_REGISTRY_VERSION = "dof.xilinx-tool-calls.e1v2.v1"
DECLARED_CALL_PLAN_SCHEMA_VERSION = "dof.declared-call-plan.v1"
L0_READ_ONLY_PLAN_VERSION = "dof.l0-read-only-call-plan.v1"
OBSERVED_CALL_TRACE_SCHEMA_VERSION = "dof.observed-call-trace.v1"
OBSERVED_CALL_OBSERVER_VERSION = "dof.observed-call-observer.v1"
OBSERVED_CALL_BUDGET_VERSION = "dof.actual-event-budget.v1"

_T = TypeVar("_T")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|credential|license)",
    re.IGNORECASE,
)
_DIAGNOSTIC_PATH_START = r"(?:file://|/(?!/)|~[/\\]|[A-Za-z]:[/\\])"
_QUOTED_ABSOLUTE_PATH_RE = re.compile(
    rf"(?ix)(?P<quote>[\"'])"
    rf"(?P<path>{_DIAGNOSTIC_PATH_START}"
    rf"(?:(?!(?P=quote))[\s\S])*)"
    rf"(?P=quote)"
)
_ABSOLUTE_PATH_RE = re.compile(
    # Consume a complete unquoted path, including spaces, until a structured
    # next-field boundary or ordinary diagnostic punctuation.  This is
    # intentionally fail-closed and may redact trailing prose.  The start
    # alternatives plus negative look-behind prevent either slash in an
    # ``https://`` URL from being mistaken for a local path.
    rf"(?ix)(?<![A-Za-z0-9_~/]){_DIAGNOSTIC_PATH_START}.*?"
    rf"(?=(?:\s+[A-Za-z_][A-Za-z0-9_.-]{{0,63}}\s*"
    rf"(?::|=|\bis\b))|[,;)\]}}]|$)"
)
_FILE_URI_RE = re.compile(r"(?i)\bfile://")
_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?ix)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|passwd|credential|authorization|"
    r"license(?:[_-]?(?:key|server))?)\b"
    r"\s*(?:[:=]|\bis\b)\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_PATHISH_SUFFIX_RE = re.compile(
    r"\.(?:dcp|tcl|jou|log|rpt|json|yaml|yml|csv|sh|bash|edf|edif)(?:$|\s)",
    re.IGNORECASE,
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _content_hash(value: Any) -> str:
    return hashlib.sha256(
        value if isinstance(value, bytes) else _canonical_bytes(value)
    ).hexdigest()


class ToolBackend(str, Enum):
    VIVADO = "vivado"
    RAPIDWRIGHT = "rapidwright"
    VALIDATION_HOST = "validation_host"
    MCP_INFRASTRUCTURE = "mcp_infrastructure"


class InfrastructureEventClass(str, Enum):
    """Lifecycle work kept visible without pretending it is a design call."""

    INFRASTRUCTURE = "INFRASTRUCTURE"


class CallRequirement(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL_BOUNDED = "OPTIONAL_BOUNDED"


class CallStatus(str, Enum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    REJECTED_BEFORE_CALL = "REJECTED_BEFORE_CALL"


class ToolCallContractError(RuntimeError):
    """A call was rejected before the injected host callable was entered."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


class ActualCallBudgetExceeded(ToolCallContractError):
    pass


class HostCallTimedOut(TimeoutError):
    def __init__(self, operation_id: str, timeout_seconds: float) -> None:
        self.operation_id = operation_id
        self.timeout_seconds = float(timeout_seconds)
        super().__init__(
            f"host call {operation_id!r} timed out after "
            f"{self.timeout_seconds:.6f}s"
        )


class BackendContractMismatch(ToolCallContractError):
    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(str(item) for item in errors)
        super().__init__("backend_contract_mismatch:" + ";".join(self.errors))


class TraceSinkFailure(ToolCallContractError):
    pass


def _event_class_label(
    value: EventClass | InfrastructureEventClass,
) -> str:
    if isinstance(value, (EventClass, InfrastructureEventClass)):
        return value.value
    raise TypeError("event_class must be EventClass or InfrastructureEventClass")


@dataclass(frozen=True, slots=True)
class HostOperation:
    operation_id: str
    backend: ToolBackend
    event_class: EventClass | InfrastructureEventClass
    mutates_design: bool
    timeout_class: str
    default_timeout_seconds: float
    allowed_stages: tuple[DesignStage, ...] = ()
    may_produce_artifact: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("operation_id must be a non-empty string")
        if not isinstance(self.backend, ToolBackend):
            raise TypeError("backend must be ToolBackend")
        _event_class_label(self.event_class)
        if not isinstance(self.mutates_design, bool):
            raise TypeError("mutates_design must be bool")
        if not isinstance(self.timeout_class, str) or not self.timeout_class.strip():
            raise ValueError("timeout_class must be a non-empty string")
        timeout = float(self.default_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("default_timeout_seconds must be finite and positive")
        object.__setattr__(self, "default_timeout_seconds", timeout)
        stages = tuple(self.allowed_stages)
        if any(not isinstance(stage, DesignStage) for stage in stages):
            raise TypeError("allowed_stages must contain DesignStage values")
        if len(stages) != len(set(stages)):
            raise ValueError("allowed_stages contains duplicates")
        object.__setattr__(self, "allowed_stages", stages)

        if self.event_class is EventClass.READ_ONLY and self.mutates_design:
            raise ValueError("READ_ONLY operation cannot mutate the design")
        if (
            self.event_class is EventClass.IMPLEMENTATION_HEAVY
            and not self.mutates_design
        ):
            raise ValueError("IMPLEMENTATION_HEAVY operation must mutate the design")
        if (
            self.event_class is InfrastructureEventClass.INFRASTRUCTURE
            and self.mutates_design
        ):
            raise ValueError("infrastructure lifecycle cannot mutate the design")

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "backend": self.backend.value,
            "event_class": _event_class_label(self.event_class),
            "mutates_design": self.mutates_design,
            "timeout_class": self.timeout_class,
            "default_timeout_seconds": self.default_timeout_seconds,
            "allowed_stages": [stage.value for stage in self.allowed_stages],
            "may_produce_artifact": self.may_produce_artifact,
        }


@dataclass(frozen=True, slots=True)
class ToolCallRegistry:
    version: str
    operations: tuple[HostOperation, ...]
    schema_version: str = TOOL_CALL_REGISTRY_SCHEMA_VERSION
    _by_id: Mapping[str, HostOperation] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("registry version must be non-empty")
        if self.schema_version != TOOL_CALL_REGISTRY_SCHEMA_VERSION:
            raise ValueError("unknown tool-call registry schema version")
        operations = tuple(self.operations)
        if not operations or any(not isinstance(item, HostOperation) for item in operations):
            raise ValueError("registry requires typed operations")
        ids = [item.operation_id for item in operations]
        if len(ids) != len(set(ids)):
            raise ValueError("registry operation IDs must be unique")
        ordered = tuple(sorted(operations, key=lambda item: item.operation_id))
        object.__setattr__(self, "operations", ordered)
        object.__setattr__(
            self,
            "_by_id",
            MappingProxyType({item.operation_id: item for item in ordered}),
        )

    def operation(self, operation_id: str) -> HostOperation:
        try:
            return self._by_id[operation_id]
        except KeyError as exc:
            raise ToolCallContractError(f"unknown_operation:{operation_id}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "operations": [item.to_dict() for item in self.operations],
        }

    @property
    def sha256(self) -> str:
        return _content_hash(self.to_dict())


def default_tool_call_registry() -> ToolCallRegistry:
    """Return the closed WP-003 Xilinx host-operation registry.

    The L0 surface deliberately omits arbitrary Tcl.  Mutation operations are
    registered so fake Gate-1 bindings can be classified and budget-tested,
    but the declared L0 plan cannot admit them.
    """

    routed = (DesignStage.ROUTED,)
    placed = (DesignStage.PLACED,)
    return ToolCallRegistry(
        TOOL_CALL_REGISTRY_VERSION,
        (
            HostOperation(
                "host.session.start",
                ToolBackend.MCP_INFRASTRUCTURE,
                InfrastructureEventClass.INFRASTRUCTURE,
                False,
                "initialization",
                60.0,
            ),
            HostOperation(
                "host.session.close",
                ToolBackend.MCP_INFRASTRUCTURE,
                InfrastructureEventClass.INFRASTRUCTURE,
                False,
                "cleanup",
                15.0,
            ),
            HostOperation(
                "vivado.open_checkpoint",
                ToolBackend.VIVADO,
                EventClass.READ_ONLY,
                False,
                "checkpoint_open",
                600.0,
            ),
            HostOperation(
                "vivado.report_timing_summary",
                ToolBackend.VIVADO,
                EventClass.READ_ONLY,
                False,
                "report",
                300.0,
                routed,
            ),
            HostOperation(
                "vivado.report_route_status",
                ToolBackend.VIVADO,
                EventClass.READ_ONLY,
                False,
                "report",
                300.0,
                routed,
            ),
            HostOperation(
                "vivado.report_utilization",
                ToolBackend.VIVADO,
                EventClass.READ_ONLY,
                False,
                "report",
                300.0,
                routed,
            ),
            HostOperation(
                "vivado.close_design",
                ToolBackend.VIVADO,
                EventClass.READ_ONLY,
                False,
                "cleanup",
                60.0,
            ),
            HostOperation(
                "validation.hash_root",
                ToolBackend.VALIDATION_HOST,
                EventClass.VALIDATION,
                False,
                "validation",
                120.0,
            ),
            HostOperation(
                "validation.validate_root",
                ToolBackend.VALIDATION_HOST,
                EventClass.VALIDATION,
                False,
                "validation",
                300.0,
            ),
            HostOperation(
                "validation.hash_child",
                ToolBackend.VALIDATION_HOST,
                EventClass.VALIDATION,
                False,
                "validation",
                120.0,
            ),
            HostOperation(
                "validation.validate_child",
                ToolBackend.VALIDATION_HOST,
                EventClass.VALIDATION,
                False,
                "validation",
                300.0,
            ),
            HostOperation(
                "validation.register_child",
                ToolBackend.VALIDATION_HOST,
                EventClass.VALIDATION,
                False,
                "artifact_registration",
                120.0,
            ),
            HostOperation(
                "evidence.finalize",
                ToolBackend.VALIDATION_HOST,
                EventClass.VALIDATION,
                False,
                "evidence_finalization",
                120.0,
            ),
            HostOperation(
                "vivado.unroute_all",
                ToolBackend.VIVADO,
                EventClass.MUTATION_LIGHT,
                True,
                "light_mutation",
                300.0,
                routed,
            ),
            HostOperation(
                "vivado.unplace_all",
                ToolBackend.VIVADO,
                EventClass.MUTATION_LIGHT,
                True,
                "light_mutation",
                300.0,
                placed,
            ),
            HostOperation(
                "vivado.place_design",
                ToolBackend.VIVADO,
                EventClass.IMPLEMENTATION_HEAVY,
                True,
                "implementation",
                3600.0,
                (DesignStage.UNPLACED,),
            ),
            HostOperation(
                "vivado.route_design",
                ToolBackend.VIVADO,
                EventClass.IMPLEMENTATION_HEAVY,
                True,
                "implementation",
                3600.0,
                placed,
            ),
            HostOperation(
                "vivado.phys_opt_design",
                ToolBackend.VIVADO,
                EventClass.IMPLEMENTATION_HEAVY,
                True,
                "implementation",
                3600.0,
                (DesignStage.PLACED, DesignStage.ROUTED),
            ),
            HostOperation(
                "vivado.write_checkpoint",
                ToolBackend.VIVADO,
                EventClass.VALIDATION,
                False,
                "artifact_write",
                600.0,
                may_produce_artifact=True,
            ),
            HostOperation(
                "rapidwright.read_checkpoint",
                ToolBackend.RAPIDWRIGHT,
                EventClass.READ_ONLY,
                False,
                "checkpoint_open",
                600.0,
            ),
            HostOperation(
                "rapidwright.write_checkpoint",
                ToolBackend.RAPIDWRIGHT,
                EventClass.VALIDATION,
                False,
                "artifact_write",
                600.0,
                may_produce_artifact=True,
            ),
        ),
    )


def e1v2_tool_call_registry() -> ToolCallRegistry:
    """Extend the frozen v1 registry by one bounded, light G operation."""

    base = default_tool_call_registry()
    return ToolCallRegistry(
        E1V2_TOOL_CALL_REGISTRY_VERSION,
        base.operations
        + (
            HostOperation(
                "vivado.generated_pblock_region",
                ToolBackend.VIVADO,
                EventClass.MUTATION_LIGHT,
                True,
                "generated_light_mutation",
                120.0,
                (DesignStage.PLACED, DesignStage.ROUTED),
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class CallRule:
    operation_id: str
    requirement: CallRequirement = CallRequirement.REQUIRED
    min_calls: int = 1
    max_calls: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("call-rule operation_id must be non-empty")
        if not isinstance(self.requirement, CallRequirement):
            raise TypeError("requirement must be CallRequirement")
        if (
            not isinstance(self.min_calls, int)
            or isinstance(self.min_calls, bool)
            or self.min_calls < 0
            or not isinstance(self.max_calls, int)
            or isinstance(self.max_calls, bool)
            or self.max_calls <= 0
            or self.min_calls > self.max_calls
        ):
            raise ValueError("call-rule cardinality is invalid")
        if self.requirement is CallRequirement.REQUIRED and self.min_calls <= 0:
            raise ValueError("required call rule needs min_calls > 0")
        if self.requirement is CallRequirement.OPTIONAL_BOUNDED and self.min_calls != 0:
            raise ValueError("optional bounded call rule needs min_calls == 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "requirement": self.requirement.value,
            "min_calls": self.min_calls,
            "max_calls": self.max_calls,
        }


@dataclass(frozen=True, slots=True)
class DeclaredCallPlan:
    version: str
    plan_id: str
    registry_hash: str
    rules: tuple[CallRule, ...]
    l0_read_only: bool = False
    schema_version: str = DECLARED_CALL_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("call-plan version must be non-empty")
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise ValueError("call-plan ID must be non-empty")
        if self.schema_version != DECLARED_CALL_PLAN_SCHEMA_VERSION:
            raise ValueError("unknown declared call-plan schema version")
        if not _SHA256_RE.fullmatch(self.registry_hash):
            raise ValueError("registry_hash must be SHA-256")
        rules = tuple(self.rules)
        if not rules or any(not isinstance(item, CallRule) for item in rules):
            raise ValueError("call plan requires typed rules")
        object.__setattr__(self, "rules", rules)

    def validate_against(self, registry: ToolCallRegistry) -> None:
        errors: list[str] = []
        if self.registry_hash != registry.sha256:
            errors.append("registry_hash_mismatch")
        for index, rule in enumerate(self.rules):
            try:
                operation = registry.operation(rule.operation_id)
            except ToolCallContractError:
                errors.append(f"rule_{index}:unknown_operation:{rule.operation_id}")
                continue
            if self.l0_read_only and (
                operation.mutates_design
                or operation.event_class
                in {EventClass.MUTATION_LIGHT, EventClass.IMPLEMENTATION_HEAVY}
                or operation.may_produce_artifact
            ):
                errors.append(f"rule_{index}:l0_forbidden_operation:{rule.operation_id}")
        if errors:
            raise BackendContractMismatch(errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "plan_id": self.plan_id,
            "registry_hash": self.registry_hash,
            "l0_read_only": self.l0_read_only,
            "rules": [item.to_dict() for item in self.rules],
        }

    @property
    def sha256(self) -> str:
        return _content_hash(self.to_dict())


def declared_l0_read_only_plan(
    registry: ToolCallRegistry | None = None,
) -> DeclaredCallPlan:
    """Return the exact read-only Vivado plan authorised for Gate 5."""

    active = registry or default_tool_call_registry()
    plan = DeclaredCallPlan(
        L0_READ_ONLY_PLAN_VERSION,
        "l0.vivado.read-only-preflight",
        active.sha256,
        (
            CallRule("validation.hash_root"),
            CallRule("host.session.start"),
            CallRule("vivado.open_checkpoint"),
            CallRule("vivado.report_timing_summary"),
            CallRule("vivado.report_route_status"),
            CallRule(
                "vivado.report_utilization",
                CallRequirement.OPTIONAL_BOUNDED,
                0,
                1,
            ),
            CallRule("vivado.close_design"),
            CallRule("host.session.close"),
            CallRule("validation.hash_root"),
            CallRule("validation.validate_root"),
        ),
        l0_read_only=True,
    )
    plan.validate_against(active)
    return plan


@dataclass(frozen=True, slots=True)
class CallAuthorization:
    event_class: EventClass | InfrastructureEventClass
    count_after_start: int
    remaining_count: int | None
    whole_run_remaining_seconds: float


class ActualCallBudget:
    """Count actual call starts and bound them by a real monotonic deadline."""

    def __init__(
        self,
        limits: Mapping[EventClass | str, int | None],
        *,
        whole_run_timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized: dict[EventClass, int | None] = {}
        for event_class in EventClass:
            raw = limits.get(event_class, limits.get(event_class.value))
            if event_class not in limits and event_class.value not in limits:
                raise ValueError(f"missing actual-call budget:{event_class.value}")
            if raw is not None and (
                not isinstance(raw, int) or isinstance(raw, bool) or raw < 0
            ):
                raise ValueError(f"invalid actual-call budget:{event_class.value}")
            normalized[event_class] = raw
        timeout = float(whole_run_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("whole_run_timeout_seconds must be finite and positive")
        self.limits = MappingProxyType(normalized)
        self.used: dict[EventClass, int] = {item: 0 for item in EventClass}
        self.infrastructure_calls = 0
        self.runtime_seconds: dict[str, float] = {
            item.value: 0.0 for item in EventClass
        } | {InfrastructureEventClass.INFRASTRUCTURE.value: 0.0}
        self.whole_run_timeout_seconds = timeout
        self.clock = clock
        self.started_monotonic_seconds = float(clock())

    def elapsed_seconds(self) -> float:
        return max(0.0, float(self.clock()) - self.started_monotonic_seconds)

    def whole_run_remaining_seconds(self) -> float:
        return max(0.0, self.whole_run_timeout_seconds - self.elapsed_seconds())

    def authorize_start(
        self,
        event_class: EventClass | InfrastructureEventClass,
        *,
        allow_after_whole_run_deadline: bool = False,
    ) -> CallAuthorization:
        remaining_wall = self.whole_run_remaining_seconds()
        if remaining_wall <= 0 and not (
            allow_after_whole_run_deadline
            and event_class is InfrastructureEventClass.INFRASTRUCTURE
        ):
            raise ActualCallBudgetExceeded("whole_run_timeout_exhausted")
        if event_class is InfrastructureEventClass.INFRASTRUCTURE:
            self.infrastructure_calls += 1
            return CallAuthorization(
                event_class,
                self.infrastructure_calls,
                None,
                remaining_wall,
            )
        if not isinstance(event_class, EventClass):
            raise TypeError("event_class must be EventClass or infrastructure")
        next_count = self.used[event_class] + 1
        limit = self.limits[event_class]
        if limit is not None and next_count > limit:
            raise ActualCallBudgetExceeded(
                f"event_cap:{event_class.value}:{next_count}>{limit}"
            )
        # Consumption happens immediately before the injected host callable.
        # Failed, timed-out, and cancelled calls therefore remain counted.
        self.used[event_class] = next_count
        return CallAuthorization(
            event_class,
            next_count,
            None if limit is None else limit - next_count,
            remaining_wall,
        )

    def record_runtime(
        self,
        event_class: EventClass | InfrastructureEventClass,
        duration_seconds: float,
    ) -> None:
        duration = float(duration_seconds)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("actual duration must be finite and non-negative")
        self.runtime_seconds[_event_class_label(event_class)] += duration

    def summary(self) -> dict[str, Any]:
        return {
            "version": OBSERVED_CALL_BUDGET_VERSION,
            "clock_source": "monotonic",
            "limits": {item.value: self.limits[item] for item in EventClass},
            "used": {item.value: self.used[item] for item in EventClass},
            "infrastructure_calls": self.infrastructure_calls,
            "runtime_seconds_by_class": dict(self.runtime_seconds),
            "whole_run_elapsed_seconds": self.elapsed_seconds(),
            "whole_run_remaining_seconds": self.whole_run_remaining_seconds(),
        }


class _PlanCursor:
    def __init__(self, plan: DeclaredCallPlan) -> None:
        self.plan = plan
        self.counts = [0 for _ in plan.rules]
        self.cursor = 0
        self.started_operations: list[str] = []

    def authorize(self, operation_id: str) -> int:
        index = self.cursor
        while index < len(self.plan.rules):
            rule = self.plan.rules[index]
            count = self.counts[index]
            if rule.operation_id == operation_id and count < rule.max_calls:
                return index
            if count >= rule.min_calls:
                index += 1
                continue
            raise ToolCallContractError(
                f"undeclared_or_out_of_order:{operation_id}:"
                f"expected:{rule.operation_id}"
            )
        raise ToolCallContractError(f"undeclared_or_excess_call:{operation_id}")

    def record_start(self, rule_index: int, operation_id: str) -> None:
        rule = self.plan.rules[rule_index]
        if rule.operation_id != operation_id:
            raise RuntimeError("authorized plan rule changed before call start")
        self.counts[rule_index] += 1
        self.started_operations.append(operation_id)
        self.cursor = (
            rule_index + 1
            if self.counts[rule_index] >= rule.max_calls
            else rule_index
        )

    def errors(self) -> tuple[str, ...]:
        return tuple(
            f"rule_{index}:missing:{rule.operation_id}:"
            f"{self.counts[index]}<{rule.min_calls}"
            for index, rule in enumerate(self.plan.rules)
            if self.counts[index] < rule.min_calls
        )

    def declared_rows(self) -> list[dict[str, Any]]:
        return [
            rule.to_dict() | {"observed_started_calls": self.counts[index]}
            for index, rule in enumerate(self.plan.rules)
        ]


@dataclass(frozen=True, slots=True)
class CallReconciliation:
    ok: bool
    errors: tuple[str, ...]
    plan_hash: str
    registry_hash: str
    declared_rules: tuple[Mapping[str, Any], ...]
    observed_started_operations: tuple[str, ...]
    observed_terminal_status_counts: Mapping[str, int]
    budget_summary: Mapping[str, Any]

    def raise_for_errors(self) -> None:
        if self.errors:
            raise BackendContractMismatch(self.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "plan_hash": self.plan_hash,
            "registry_hash": self.registry_hash,
            "declared_rules": [dict(item) for item in self.declared_rules],
            "observed_started_operations": list(self.observed_started_operations),
            "observed_terminal_status_counts": dict(
                self.observed_terminal_status_counts
            ),
            "budget_summary": dict(self.budget_summary),
        }


HostCallable = Callable[[HostOperation, Mapping[str, Any]], Awaitable[_T]]
TraceSink = Callable[[Mapping[str, Any]], Any]
OperationProcessBindingResolver = Callable[
    [HostOperation, Any, BaseException | None], Mapping[str, Any] | None
]


class ObservedCallExecutor:
    """Authorize, invoke, time, and reconcile one declared host-call stream."""

    def __init__(
        self,
        *,
        registry: ToolCallRegistry,
        plan: DeclaredCallPlan,
        budget: ActualCallBudget,
        invoke: HostCallable[Any],
        trace_sink: TraceSink | MutableSequence[Mapping[str, Any]] | None = None,
        execution_binding: Mapping[str, Any] | None = None,
        operation_process_binding_resolver: (
            OperationProcessBindingResolver | None
        ) = None,
    ) -> None:
        plan.validate_against(registry)
        self.registry = registry
        self.plan = plan
        self.budget = budget
        self.invoke = invoke
        self._cursor = _PlanCursor(plan)
        self._trace_rows: list[Mapping[str, Any]] = []
        self._trace_sink = trace_sink
        self._execution_binding = _normalize_execution_binding(execution_binding)
        if operation_process_binding_resolver is not None and not callable(
            operation_process_binding_resolver
        ):
            raise TypeError("operation_process_binding_resolver must be callable")
        self._operation_process_binding_resolver = (
            operation_process_binding_resolver
        )
        self._next_call = 0
        self._next_sequence = 0
        self._lock = asyncio.Lock()
        self._contract_errors: list[str] = []
        self._terminal_status_counts: dict[str, int] = {
            status.value: 0
            for status in CallStatus
            if status is not CallStatus.STARTED
        }

    @property
    def trace_rows(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._trace_rows)

    async def call(
        self,
        operation_id: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        current_stage: DesignStage | None = None,
        recovery_after_failure: bool = False,
    ) -> Any:
        args = _canonical_clone(arguments or {})
        if not isinstance(args, dict):
            raise TypeError("host-call arguments must be a JSON object")
        argument_hash = _content_hash(args)
        public_arguments = _public_argument_summary(args)
        known_operation_id: str | None = None
        async with self._lock:
            try:
                operation = self.registry.operation(operation_id)
                known_operation_id = operation.operation_id
                if (
                    current_stage is not None
                    and operation.allowed_stages
                    and current_stage not in operation.allowed_stages
                ):
                    raise ToolCallContractError(
                        f"stage_precondition:{operation_id}:"
                        f"{current_stage.value}"
                    )
                if recovery_after_failure:
                    if operation.event_class not in {
                        EventClass.VALIDATION,
                        InfrastructureEventClass.INFRASTRUCTURE,
                    }:
                        raise ToolCallContractError(
                            "recovery_call_must_be_validation_or_infrastructure"
                        )
                    if (
                        operation.event_class
                        is InfrastructureEventClass.INFRASTRUCTURE
                        and operation_id != "host.session.close"
                    ):
                        raise ToolCallContractError(
                            "recovery_infrastructure_must_be_session_close"
                        )
                    if operation_id not in {
                        rule.operation_id for rule in self.plan.rules
                    }:
                        raise ToolCallContractError(
                            f"undeclared_recovery_call:{operation_id}"
                        )
                    rule_index = None
                else:
                    rule_index = self._cursor.authorize(operation_id)
                effective_timeout = _effective_timeout(
                    operation,
                    timeout_seconds,
                    self.budget.whole_run_remaining_seconds(),
                    allow_after_whole_run_deadline=(
                        recovery_after_failure
                        and operation.event_class
                        is InfrastructureEventClass.INFRASTRUCTURE
                    ),
                )
                authorization = self.budget.authorize_start(
                    operation.event_class,
                    allow_after_whole_run_deadline=(
                        recovery_after_failure
                        and operation.event_class
                        is InfrastructureEventClass.INFRASTRUCTURE
                    ),
                )
                if recovery_after_failure:
                    self._cursor.started_operations.append(operation_id)
                    self._contract_errors.append(
                        f"recovery_after_failure:{operation_id}"
                    )
                else:
                    assert rule_index is not None
                    self._cursor.record_start(rule_index, operation_id)
            except (ToolCallContractError, TypeError, ValueError) as exc:
                reason = getattr(exc, "reason", str(exc))
                self._contract_errors.append(str(reason))
                public_operation_id = (
                    known_operation_id
                    if known_operation_id is not None
                    else "unclassified:" + _content_hash(str(operation_id))[:16]
                )
                self._append_trace(
                    {
                        "record_type": "HOST_CALL_TERMINAL",
                        "call_id": None,
                        "operation_id": public_operation_id,
                        "status": CallStatus.REJECTED_BEFORE_CALL.value,
                        "argument_hash": argument_hash,
                        "public_argument_summary": public_arguments,
                        "reason": _redact_text(str(reason))[:256],
                        "host_callable_entered": False,
                        "observer_version": OBSERVED_CALL_OBSERVER_VERSION,
                        "schema_version": OBSERVED_CALL_TRACE_SCHEMA_VERSION,
                    }
                )
                self._terminal_status_counts[
                    CallStatus.REJECTED_BEFORE_CALL.value
                ] += 1
                if isinstance(exc, ToolCallContractError):
                    raise
                raise ToolCallContractError(str(reason)) from exc

            call_id = f"host-call-{self._next_call:06d}"
            self._next_call += 1
            started = float(self.budget.clock())
            try:
                self._append_trace({
                    "record_type": "HOST_CALL_START",
                    "call_id": call_id,
                    "operation_id": operation.operation_id,
                    "backend": operation.backend.value,
                    "event_class": _event_class_label(operation.event_class),
                    "registry_hash": self.registry.sha256,
                    "plan_hash": self.plan.sha256,
                    "plan_rule_index": rule_index,
                    "recovery_after_failure": recovery_after_failure,
                    "argument_hash": argument_hash,
                    "public_argument_summary": public_arguments,
                    "started_monotonic_seconds": started,
                    "timeout_seconds": effective_timeout,
                    "budget_count_after_start": authorization.count_after_start,
                    "budget_remaining_count": authorization.remaining_count,
                    "whole_run_remaining_seconds": (
                        authorization.whole_run_remaining_seconds
                    ),
                    "started_after_whole_run_deadline": (
                        authorization.whole_run_remaining_seconds <= 0
                    ),
                    "host_callable_entered": False,
                    "observer_version": OBSERVED_CALL_OBSERVER_VERSION,
                    "schema_version": OBSERVED_CALL_TRACE_SCHEMA_VERSION,
                })
            except TraceSinkFailure:
                self._append_trace(
                    {
                        "record_type": "HOST_CALL_TERMINAL",
                        "call_id": call_id,
                        "operation_id": operation.operation_id,
                        "backend": operation.backend.value,
                        "event_class": _event_class_label(operation.event_class),
                        "status": CallStatus.REJECTED_BEFORE_CALL.value,
                        "argument_hash": argument_hash,
                        "reason": "trace_sink_failure",
                        "host_callable_entered": False,
                        "observer_version": OBSERVED_CALL_OBSERVER_VERSION,
                        "schema_version": OBSERVED_CALL_TRACE_SCHEMA_VERSION,
                    },
                    notify_sink=False,
                )
                self._terminal_status_counts[
                    CallStatus.REJECTED_BEFORE_CALL.value
                ] += 1
                raise

        status = CallStatus.SUCCEEDED
        error_class: str | None = None
        diagnostic: str | None = None
        result: Any = None
        call_error: BaseException | None = None
        try:
            async with asyncio.timeout(effective_timeout):
                result = await self.invoke(operation, _deep_freeze(args))
            return result
        except TimeoutError as exc:
            call_error = exc
            status = CallStatus.TIMED_OUT
            error_class = "TimeoutError"
            diagnostic = "host call exceeded its effective timeout"
            raise HostCallTimedOut(operation.operation_id, effective_timeout) from exc
        except asyncio.CancelledError as exc:
            call_error = exc
            status = CallStatus.CANCELLED
            error_class = "CancelledError"
            diagnostic = "host call task was cancelled"
            raise
        except Exception as exc:
            call_error = exc
            status = CallStatus.FAILED
            error_class = type(exc).__name__
            diagnostic = _redact_text(str(exc))[:512]
            raise
        finally:
            operation_process_binding: dict[str, Any] | None = None
            resolver = self._operation_process_binding_resolver
            if resolver is not None:
                try:
                    operation_process_binding = _normalize_execution_binding(
                        resolver(operation, result, call_error)
                    )
                except Exception as binding_exc:
                    self._contract_errors.append(
                        "operation_process_binding_failure:"
                        + type(binding_exc).__name__
                    )
            finished = float(self.budget.clock())
            duration = max(0.0, finished - started)
            self.budget.record_runtime(operation.event_class, duration)
            async with self._lock:
                terminal_row = {
                        "record_type": "HOST_CALL_TERMINAL",
                        "call_id": call_id,
                        "operation_id": operation.operation_id,
                        "backend": operation.backend.value,
                        "event_class": _event_class_label(operation.event_class),
                        "status": status.value,
                        "argument_hash": argument_hash,
                        "started_monotonic_seconds": started,
                        "finished_monotonic_seconds": finished,
                        "duration_seconds": duration,
                        "error_class": error_class,
                        "bounded_diagnostic": diagnostic,
                        "recovery_after_failure": recovery_after_failure,
                        "host_callable_entered": True,
                        "observer_version": OBSERVED_CALL_OBSERVER_VERSION,
                        "schema_version": OBSERVED_CALL_TRACE_SCHEMA_VERSION,
                    }
                if operation_process_binding is not None:
                    terminal_row["operation_process_binding"] = (
                        operation_process_binding
                    )
                self._append_trace(terminal_row)
                self._terminal_status_counts[status.value] += 1

    def reconciliation(self) -> CallReconciliation:
        errors = tuple(self._contract_errors) + self._cursor.errors()
        return CallReconciliation(
            not errors,
            errors,
            self.plan.sha256,
            self.registry.sha256,
            tuple(MappingProxyType(item) for item in self._cursor.declared_rows()),
            tuple(self._cursor.started_operations),
            MappingProxyType(dict(self._terminal_status_counts)),
            MappingProxyType(self.budget.summary()),
        )

    def reconcile_or_raise(self) -> CallReconciliation:
        result = self.reconciliation()
        result.raise_for_errors()
        return result

    def _append_trace(
        self,
        payload: Mapping[str, Any],
        *,
        notify_sink: bool = True,
    ) -> None:
        row = _canonical_clone(payload)
        if self._execution_binding is not None:
            # Inject this centrally so successful calls, pre-call rejections,
            # and trace-sink failures all carry the same immutable audit link.
            # Callers cannot override it through an individual row payload.
            if "execution_binding" in row:
                raise ValueError("trace row cannot override execution_binding")
            row["execution_binding"] = _canonical_clone(self._execution_binding)
        row["sequence"] = self._next_sequence
        self._next_sequence += 1
        frozen = _deep_freeze(row)
        self._trace_rows.append(frozen)
        target = self._trace_sink
        if target is None or not notify_sink:
            return
        sink_row = _canonical_clone(row)
        try:
            if callable(target):
                target(sink_row)
            elif isinstance(target, MutableSequence):
                target.append(sink_row)
            else:
                raise TypeError("trace_sink must be callable or mutable sequence")
        except Exception as exc:
            reason = "trace_sink_failure:" + type(exc).__name__
            self._contract_errors.append(reason)
            raise TraceSinkFailure(reason) from exc


def _effective_timeout(
    operation: HostOperation,
    requested: float | None,
    whole_run_remaining: float,
    *,
    allow_after_whole_run_deadline: bool = False,
) -> float:
    if whole_run_remaining <= 0 and not allow_after_whole_run_deadline:
        raise ActualCallBudgetExceeded("whole_run_timeout_exhausted")
    if requested is None:
        call_timeout = operation.default_timeout_seconds
    else:
        call_timeout = float(requested)
        if not math.isfinite(call_timeout) or call_timeout <= 0:
            raise ToolCallContractError("call_timeout_not_finite_positive")
        if call_timeout > operation.default_timeout_seconds:
            raise ToolCallContractError(
                f"call_timeout_exceeds_registry_bound:{call_timeout}>"
                f"{operation.default_timeout_seconds}"
            )
    if allow_after_whole_run_deadline:
        return call_timeout
    return min(call_timeout, whole_run_remaining)


def _normalize_execution_binding(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate and detach an optional public process/session audit link.

    Bindings are copied verbatim into every trace row, so silently redacting a
    bad value would break exact correlation.  Reject path- or secret-bearing
    input instead.  Hash-labelled fields may describe a sensitive identity
    without disclosing the underlying token.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("execution_binding must be a mapping")
    if not value:
        raise ValueError("execution_binding must not be empty")
    if any(not isinstance(key, str) or not key for key in value):
        raise ValueError("execution_binding keys must be non-empty strings")
    try:
        normalized = _canonical_clone(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution_binding must be canonical JSON") from exc
    if not isinstance(normalized, dict):
        raise TypeError("execution_binding must be a JSON object")
    if len(_canonical_bytes(normalized)) > 4096:
        raise ValueError("execution_binding exceeds public size bound")

    def validate_public(item: Any, *, key: str | None = None) -> None:
        if key is not None and _SENSITIVE_KEY_RE.search(key):
            lowered = key.lower()
            if not lowered.endswith(("_hash", "_sha256")):
                raise ValueError("execution_binding contains sensitive field")
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                if not isinstance(child_key, str) or not child_key:
                    raise ValueError(
                        "execution_binding keys must be non-empty strings"
                    )
                validate_public(child, key=child_key)
            return
        if isinstance(item, list):
            for child in item:
                validate_public(child, key=key)
            return
        if isinstance(item, str):
            if len(item) > 512:
                raise ValueError("execution_binding string exceeds public bound")
            if (
                _FILE_URI_RE.search(item)
                or _QUOTED_ABSOLUTE_PATH_RE.search(item)
                or _ABSOLUTE_PATH_RE.search(item)
                or _PATHISH_SUFFIX_RE.search(item)
                or _redact_text(item) != item
            ):
                raise ValueError("execution_binding contains path or secret")
            return
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float) and math.isfinite(item):
            return
        raise ValueError("execution_binding contains unsupported value")

    validate_public(normalized)
    return normalized


def _canonical_clone(value: Any) -> Any:
    """Detach JSON data from caller-owned mutable objects."""

    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _public_argument_summary(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED_SECRET]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _public_argument_summary(item, key=str(item_key))
            for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_public_argument_summary(item, key=key) for item in value]
    if isinstance(value, str):
        if (
            _FILE_URI_RE.search(value)
            or _QUOTED_ABSOLUTE_PATH_RE.search(value)
            or _ABSOLUTE_PATH_RE.search(value)
            or _PATHISH_SUFFIX_RE.search(value)
        ):
            return "[REDACTED_PATH]"
        return _redact_text(value)[:256]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("arguments contain non-finite float")
        return value
    # Canonical hashing above rejects most non-JSON inputs; retain a fail-closed
    # marker here rather than calling an arbitrary object's repr.
    return f"[UNSUPPORTED:{type(value).__name__}]"


def _redact_text(value: str) -> str:
    normalized = str(value).replace("\x00", " ")
    normalized = re.sub(r"[\r\n\t]+", " ", normalized)
    text = _DIAGNOSTIC_SECRET_RE.sub(
        lambda match: f"{match.group(1)}=[REDACTED_SECRET]",
        normalized,
    )
    text = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED_SECRET]",
        text,
    )
    text = _QUOTED_ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", text)
    return text.strip()


__all__ = [
    "ActualCallBudget",
    "ActualCallBudgetExceeded",
    "BackendContractMismatch",
    "CallReconciliation",
    "CallRequirement",
    "CallRule",
    "CallStatus",
    "DeclaredCallPlan",
    "HostCallTimedOut",
    "HostOperation",
    "InfrastructureEventClass",
    "ObservedCallExecutor",
    "ToolBackend",
    "ToolCallContractError",
    "ToolCallRegistry",
    "declared_l0_read_only_plan",
    "default_tool_call_registry",
    "e1v2_tool_call_registry",
]
