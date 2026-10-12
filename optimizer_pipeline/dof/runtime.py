"""Deterministic mock event accounting and dispatch for the DoF contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import DesignStage, EndpointCode, EventClass
from .gate import GatedStep


_MOCK_EVENT_SECONDS: dict[EventClass, float] = {
    EventClass.READ_ONLY: 0.010,
    EventClass.MUTATION_LIGHT: 0.020,
    EventClass.IMPLEMENTATION_HEAVY: 0.100,
    EventClass.VALIDATION: 0.010,
    EventClass.LLM_REQUEST: 0.020,
    EventClass.OUTER_ATTEMPT: 0.005,
}


class BudgetExceeded(RuntimeError):
    """A mock event was rejected before it could exceed its declared cap."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


@dataclass
class DeterministicMockClock:
    """A repeatable monotonic/UTC clock used only by mock evidence."""

    elapsed_seconds: float = 0.0
    anchor: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    def monotonic(self) -> float:
        return self.elapsed_seconds

    def now(self) -> datetime:
        return self.anchor + timedelta(seconds=self.elapsed_seconds)

    def advance(self, seconds: float) -> None:
        value = float(seconds)
        if not math.isfinite(value) or value < 0:
            raise ValueError("mock clock advance must be finite and non-negative")
        self.elapsed_seconds += value


class EventBudgetLedger:
    """Fail-closed counters for the six common event classes and wall/USD caps."""

    def __init__(
        self,
        limits: Mapping[EventClass | str, int | None],
        *,
        wall_time_seconds: float,
        max_usd: float | None,
        unknown_pricing_policy: str,
        clock: DeterministicMockClock,
    ) -> None:
        normalized: dict[EventClass, int | None] = {}
        for event_class in EventClass:
            if event_class not in limits and event_class.value not in limits:
                raise ValueError(f"missing/invalid budget for {event_class.value}")
            raw = limits.get(event_class, limits.get(event_class.value))
            if raw is not None and (
                not isinstance(raw, int) or isinstance(raw, bool) or raw < 0
            ):
                raise ValueError(f"missing/invalid budget for {event_class.value}")
            normalized[event_class] = raw
        wall = float(wall_time_seconds)
        if not math.isfinite(wall) or wall <= 0:
            raise ValueError("whole-run wall-time cap must be finite and positive")
        if max_usd is not None:
            usd = float(max_usd)
            if not math.isfinite(usd) or usd < 0:
                raise ValueError("USD cap must be finite and non-negative")
            max_usd = usd
        if unknown_pricing_policy not in {"reject", "allow_not_run"}:
            raise ValueError("unknown pricing policy must be reject or allow_not_run")
        self.limits = MappingProxyType(normalized)
        self.used: dict[EventClass, int] = {key: 0 for key in EventClass}
        self.wall_time_seconds = wall
        self.max_usd = max_usd
        self.unknown_pricing_policy = unknown_pricing_policy
        self.cost_usd = 0.0
        self.clock = clock

    def remaining(self) -> dict[str, int | float | None]:
        payload: dict[str, int | float | None] = {
            key.value: (
                None
                if self.limits[key] is None
                else self.limits[key] - self.used[key]
            )
            for key in EventClass
        }
        payload["WALL_TIME_SECONDS"] = max(
            0.0, self.wall_time_seconds - self.clock.monotonic()
        )
        payload["USD"] = (
            None if self.max_usd is None else max(0.0, self.max_usd - self.cost_usd)
        )
        return payload

    def preflight(
        self,
        event_class: EventClass,
        *,
        units: int = 1,
        duration_seconds: float | None = None,
        cost_usd: float | None = 0.0,
    ) -> None:
        if not isinstance(event_class, EventClass):
            raise TypeError("event_class must be EventClass")
        if not isinstance(units, int) or isinstance(units, bool) or units <= 0:
            raise ValueError("event units must be a positive integer")
        next_count = self.used[event_class] + units
        limit = self.limits[event_class]
        if limit is not None and next_count > limit:
            raise BudgetExceeded(
                f"event_cap:{event_class.value}:{next_count}>{limit}"
            )
        duration = (
            _MOCK_EVENT_SECONDS[event_class] * units
            if duration_seconds is None
            else float(duration_seconds)
        )
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("event duration must be finite and non-negative")
        projected_wall = self.clock.monotonic() + duration
        if projected_wall > self.wall_time_seconds:
            raise BudgetExceeded(
                f"wall_time_cap:{projected_wall:.6f}>{self.wall_time_seconds:.6f}"
            )
        if event_class is EventClass.LLM_REQUEST:
            if cost_usd is None:
                if self.max_usd is not None and self.unknown_pricing_policy == "reject":
                    raise BudgetExceeded("unknown_llm_price_rejected")
            else:
                cost = float(cost_usd)
                if not math.isfinite(cost) or cost < 0:
                    raise ValueError("LLM cost must be finite and non-negative")
                if self.max_usd is not None and self.cost_usd + cost > self.max_usd:
                    raise BudgetExceeded(
                        f"usd_cap:{self.cost_usd + cost:.6f}>{self.max_usd:.6f}"
                    )

    def consume(
        self,
        event_class: EventClass,
        *,
        units: int = 1,
        duration_seconds: float | None = None,
        cost_usd: float | None = 0.0,
    ) -> tuple[float, float, float]:
        self.preflight(
            event_class,
            units=units,
            duration_seconds=duration_seconds,
            cost_usd=cost_usd,
        )
        duration = (
            _MOCK_EVENT_SECONDS[event_class] * units
            if duration_seconds is None
            else float(duration_seconds)
        )
        started = self.clock.monotonic()
        self.clock.advance(duration)
        finished = self.clock.monotonic()
        self.used[event_class] += units
        if event_class is EventClass.LLM_REQUEST and cost_usd is not None:
            self.cost_usd += float(cost_usd)
        return started, finished, duration

    def preflight_batch(
        self,
        events: Iterable[tuple[EventClass, int, float | None]],
    ) -> None:
        """Check an ordered event batch without changing count, cost or clock.

        Unlike repeated :meth:`preflight` calls, this method accumulates the
        projected wall time and counts across heterogeneous event classes.  It
        lets the runner reject a complete gated plan before opening a canonical
        outer attempt when only the whole-run wall/USD cap would be exceeded.
        """

        projected_used = dict(self.used)
        projected_wall = self.clock.monotonic()
        projected_cost = self.cost_usd
        for event_class, units, cost_usd in events:
            if not isinstance(event_class, EventClass):
                raise TypeError("event_class must be EventClass")
            if not isinstance(units, int) or isinstance(units, bool) or units <= 0:
                raise ValueError("event units must be a positive integer")
            projected_used[event_class] += units
            limit = self.limits[event_class]
            if limit is not None and projected_used[event_class] > limit:
                raise BudgetExceeded(
                    f"event_cap:{event_class.value}:"
                    f"{projected_used[event_class]}>{limit}"
                )
            duration = _MOCK_EVENT_SECONDS[event_class] * units
            projected_wall += duration
            if projected_wall > self.wall_time_seconds:
                raise BudgetExceeded(
                    f"wall_time_cap:{projected_wall:.6f}>"
                    f"{self.wall_time_seconds:.6f}"
                )
            if event_class is EventClass.LLM_REQUEST:
                if cost_usd is None:
                    if (
                        self.max_usd is not None
                        and self.unknown_pricing_policy == "reject"
                    ):
                        raise BudgetExceeded("unknown_llm_price_rejected")
                else:
                    cost = float(cost_usd)
                    if not math.isfinite(cost) or cost < 0:
                        raise ValueError("LLM cost must be finite and non-negative")
                    projected_cost += cost
                    if self.max_usd is not None and projected_cost > self.max_usd:
                        raise BudgetExceeded(
                            f"usd_cap:{projected_cost:.6f}>{self.max_usd:.6f}"
                        )

    @property
    def heavy_runtime_seconds(self) -> float:
        return self.used[EventClass.IMPLEMENTATION_HEAVY] * _MOCK_EVENT_SECONDS[
            EventClass.IMPLEMENTATION_HEAVY
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "clock_source": "deterministic_mock.v1",
            "limits": {key.value: self.limits[key] for key in EventClass},
            "used": {key.value: self.used[key] for key in EventClass},
            "remaining": self.remaining(),
            "whole_run_monotonic_seconds": self.clock.monotonic(),
            "heavy_runtime_seconds": self.heavy_runtime_seconds,
            "max_usd": self.max_usd,
            "cost_usd": self.cost_usd,
            "unknown_pricing_policy": self.unknown_pricing_policy,
        }


class TraceEventRecorder:
    """Add deterministic event timing to an immutable JSONL trace writer."""

    def __init__(self, trace: Any, ledger: EventBudgetLedger) -> None:
        self.trace = trace
        self.ledger = ledger

    def append_decision(self, **payload: Any) -> int:
        return self.trace.append(
            {
                "record_type": "DECISION",
                "monotonic_seconds": self.ledger.clock.monotonic(),
                **payload,
            }
        )

    def consume_event(
        self,
        event_class: EventClass,
        operation: str,
        *,
        units: int = 1,
        outer_attempt_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
        cost_usd: float | None = 0.0,
    ) -> int:
        started, finished, elapsed = self.ledger.consume(
            event_class,
            units=units,
            cost_usd=cost_usd,
        )
        return self.trace.append(
            {
                "record_type": "INTERNAL_EVENT",
                "event_class": event_class.value,
                "operation": str(operation),
                "units": units,
                "outer_attempt_id": outer_attempt_id,
                "started_monotonic_seconds": started,
                "finished_monotonic_seconds": finished,
                "elapsed_seconds": elapsed,
                "status": "COMPLETED",
                "detail": dict(detail or {}),
            }
        )


@dataclass(frozen=True, slots=True)
class MockDispatchResult:
    endpoint: EndpointCode
    output_bytes: bytes | None
    stage_after: DesignStage | None
    metrics: Mapping[str, Any]
    validation_status: str
    valid: bool
    improved: bool
    error_message: str | None = None


class MockGatedDispatcher:
    """Dispatch only gate-produced steps and never call the real backend."""

    def __init__(self, faults: Mapping[str, EndpointCode] | None = None) -> None:
        self.faults = dict(faults or {})

    def dispatch(
        self,
        step: GatedStep,
        *,
        parent_bytes: bytes,
        parent_metrics: Mapping[str, Any],
    ) -> MockDispatchResult:
        if not isinstance(step, GatedStep):
            raise TypeError("mock dispatcher accepts GatedStep only")
        fault = self.faults.get(step.action_key)
        if fault is not None:
            if fault not in {
                EndpointCode.BACKEND_ERROR,
                EndpointCode.TIMEOUT,
                EndpointCode.CANCELLATION,
                EndpointCode.INVALID_ARTIFACT,
            }:
                raise ValueError("unsupported mock fault endpoint")
            return MockDispatchResult(
                endpoint=fault,
                output_bytes=None,
                stage_after=step.stage_before,
                metrics=dict(parent_metrics),
                validation_status="INVALID",
                valid=False,
                improved=False,
                error_message=f"injected_mock_fault:{fault.value}",
            )

        delta = _mock_delta(step.action_key)
        previous_fmax = float(parent_metrics.get("estimated_fmax_mhz", 0.0) or 0.0)
        previous_wns = float(parent_metrics.get("wns_ns", 0.0) or 0.0)
        metrics = {
            "estimated_fmax_mhz": previous_fmax + delta,
            "wns_ns": previous_wns + delta / 100.0,
            "delta_fmax_mhz": delta,
            "mock": True,
        }
        stage_after = step.stage_after or step.stage_before
        valid = stage_after is DesignStage.ROUTED
        validation_status = "CLEAN" if valid else "WARNING"
        payload = {
            "action": step.to_dict(),
            "parent_sha256": hashlib.sha256(parent_bytes).hexdigest(),
            "metrics": metrics,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        output = parent_bytes + b"\nDOF_WP002_MOCK\n" + encoded + b"\n"
        return MockDispatchResult(
            endpoint=EndpointCode.VALID_COMPLETION,
            output_bytes=output,
            stage_after=stage_after,
            metrics=MappingProxyType(metrics),
            validation_status=validation_status,
            valid=valid,
            improved=bool(valid and delta > 0),
        )


def _mock_delta(action_key: str) -> float:
    if action_key == "manual.route_design":
        return 0.40
    if action_key == "manual.phys_opt_design":
        return 0.20
    if action_key.startswith("macro."):
        return 0.50
    return 0.0
