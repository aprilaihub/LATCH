from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from optimizer_pipeline.dof.catalogues import clean_macro_catalogue
from optimizer_pipeline.dof.contracts import DesignStage, DoFLevel, EndpointCode, EventClass
from optimizer_pipeline.dof.gate import ACTION_PROPOSAL_SCHEMA_VERSION, ExperimentActionGate
from optimizer_pipeline.dof.runtime import (
    BudgetExceeded,
    DeterministicMockClock,
    EventBudgetLedger,
    MockGatedDispatcher,
    TraceEventRecorder,
)
from optimizer_pipeline.dof.spec import MaskSpec


@dataclass
class FakeTrace:
    rows: list[dict] = field(default_factory=list)

    def append(self, row: dict) -> int:
        self.rows.append(dict(row))
        return len(self.rows) - 1


def limits(**updates: int) -> dict[EventClass, int | None]:
    values = {item: 10 for item in EventClass}
    for key, value in updates.items():
        values[EventClass[key]] = value
    return values


def accepted_composite():
    catalogue = clean_macro_catalogue(DoFLevel.L1)
    payload = {
        "schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "proposal-001",
        "decision_id": "decision-001",
        "catalogue_version": catalogue.version,
        "catalogue_hash": catalogue.sha256,
        "kind": "PLAN",
        "steps": [
            {
                "step_id": "place-route",
                "action_key": "macro.place_route",
                "parameters": {},
            }
        ],
    }
    decision = ExperimentActionGate().evaluate(
        payload,
        level=DoFLevel.L1,
        expected_decision_id="decision-001",
        catalogue=catalogue,
        expected_catalogue_hash=catalogue.sha256,
        current_stage=DesignStage.ROUTED,
        budget_remaining={item: 10 for item in EventClass},
        mask=MaskSpec.create(version="mask.v1"),
        infrastructure_events_per_step={
            EventClass.OUTER_ATTEMPT: 1,
            EventClass.VALIDATION: 1,
        },
    )
    assert decision.accepted
    return decision.gated_steps[0]


def test_composite_consumes_two_heavy_units_and_one_outer_attempt() -> None:
    step = accepted_composite()
    clock = DeterministicMockClock()
    ledger = EventBudgetLedger(
        limits(),
        wall_time_seconds=10.0,
        max_usd=None,
        unknown_pricing_policy="reject",
        clock=clock,
    )
    trace = FakeTrace()
    recorder = TraceEventRecorder(trace, ledger)
    for event in step.events:
        recorder.consume_event(
            event.event_class,
            event.operation,
            units=event.units,
            outer_attempt_id="attempt-001",
        )
    assert ledger.used[EventClass.IMPLEMENTATION_HEAVY] == 2
    assert ledger.used[EventClass.OUTER_ATTEMPT] == 1
    assert ledger.used[EventClass.VALIDATION] == 1
    assert all(row["elapsed_seconds"] > 0 for row in trace.rows)


def test_budget_rejects_next_event_before_count_or_clock_changes() -> None:
    clock = DeterministicMockClock()
    ledger = EventBudgetLedger(
        limits(IMPLEMENTATION_HEAVY=1),
        wall_time_seconds=10.0,
        max_usd=None,
        unknown_pricing_policy="reject",
        clock=clock,
    )
    ledger.consume(EventClass.IMPLEMENTATION_HEAVY)
    elapsed = clock.monotonic()
    with pytest.raises(BudgetExceeded, match="event_cap"):
        ledger.consume(EventClass.IMPLEMENTATION_HEAVY)
    assert ledger.used[EventClass.IMPLEMENTATION_HEAVY] == 1
    assert clock.monotonic() == elapsed


def test_unknown_price_rejects_llm_before_consumption_when_cost_is_bounded() -> None:
    clock = DeterministicMockClock()
    ledger = EventBudgetLedger(
        limits(),
        wall_time_seconds=10.0,
        max_usd=1.0,
        unknown_pricing_policy="reject",
        clock=clock,
    )
    with pytest.raises(BudgetExceeded, match="unknown_llm_price"):
        ledger.consume(EventClass.LLM_REQUEST, cost_usd=None)
    assert ledger.used[EventClass.LLM_REQUEST] == 0


def test_ledger_records_explicit_unbounded_optional_event_caps() -> None:
    clock = DeterministicMockClock()
    configured = limits()
    configured[EventClass.READ_ONLY] = None
    configured[EventClass.VALIDATION] = None
    ledger = EventBudgetLedger(
        configured,
        wall_time_seconds=10.0,
        max_usd=None,
        unknown_pricing_policy="allow_not_run",
        clock=clock,
    )

    ledger.consume(EventClass.READ_ONLY, units=3)
    ledger.consume(EventClass.VALIDATION, units=2)

    assert ledger.remaining()[EventClass.READ_ONLY.value] is None
    assert ledger.remaining()[EventClass.VALIDATION.value] is None
    assert ledger.summary()["limits"][EventClass.READ_ONLY.value] is None


def test_batch_preflight_accumulates_heterogeneous_wall_time_without_consuming() -> None:
    clock = DeterministicMockClock()
    ledger = EventBudgetLedger(
        limits(),
        wall_time_seconds=0.105,
        max_usd=None,
        unknown_pricing_policy="allow_not_run",
        clock=clock,
    )

    with pytest.raises(BudgetExceeded, match="wall_time_cap"):
        ledger.preflight_batch(
            [
                (EventClass.READ_ONLY, 1, 0.0),
                (EventClass.IMPLEMENTATION_HEAVY, 1, 0.0),
            ]
        )

    assert set(ledger.used.values()) == {0}
    assert ledger.clock.monotonic() == 0.0


def test_dispatcher_requires_gate_output_and_is_deterministic() -> None:
    step = accepted_composite()
    dispatcher = MockGatedDispatcher()
    first = dispatcher.dispatch(
        step,
        parent_bytes=b"root",
        parent_metrics={"estimated_fmax_mhz": 100.0, "wns_ns": -0.1},
    )
    second = dispatcher.dispatch(
        step,
        parent_bytes=b"root",
        parent_metrics={"estimated_fmax_mhz": 100.0, "wns_ns": -0.1},
    )
    assert first.output_bytes == second.output_bytes
    assert first.metrics == second.metrics
    assert first.endpoint is EndpointCode.VALID_COMPLETION
    with pytest.raises(TypeError, match="GatedStep"):
        dispatcher.dispatch(  # type: ignore[arg-type]
            {"action": "macro.place_route"},
            parent_bytes=b"root",
            parent_metrics={},
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        EndpointCode.BACKEND_ERROR,
        EndpointCode.TIMEOUT,
        EndpointCode.CANCELLATION,
        EndpointCode.INVALID_ARTIFACT,
    ],
)
def test_dispatcher_preserves_typed_mock_failure_endpoints(endpoint: EndpointCode) -> None:
    step = accepted_composite()
    result = MockGatedDispatcher({step.action_key: endpoint}).dispatch(
        step,
        parent_bytes=b"root",
        parent_metrics={},
    )
    assert result.endpoint is endpoint
    assert result.output_bytes is None
    assert result.valid is False
