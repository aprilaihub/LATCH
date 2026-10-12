from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from optimizer_pipeline.dof.contracts import EventClass
from optimizer_pipeline.dof.toolcalls import (
    ActualCallBudget,
    ActualCallBudgetExceeded,
    BackendContractMismatch,
    CallRequirement,
    CallRule,
    CallStatus,
    DeclaredCallPlan,
    HostCallTimedOut,
    ObservedCallExecutor,
    ToolCallContractError,
    declared_l0_read_only_plan,
    default_tool_call_registry,
)


def _limits(**updates: int | None) -> dict[EventClass, int | None]:
    result: dict[EventClass, int | None] = {item: 20 for item in EventClass}
    for key, value in updates.items():
        result[EventClass[key]] = value
    return result


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _plan(*rules: CallRule, l0: bool = False) -> DeclaredCallPlan:
    registry = default_tool_call_registry()
    return DeclaredCallPlan(
        "test-plan.v1",
        "test-plan",
        registry.sha256,
        tuple(rules),
        l0_read_only=l0,
    )


def _observer(
    plan: DeclaredCallPlan,
    invoke,
    *,
    limits=None,
    clock=None,
    execution_binding=None,
    operation_process_binding_resolver=None,
):
    registry = default_tool_call_registry()
    active_clock = clock or FakeClock()
    budget = ActualCallBudget(
        limits or _limits(),
        whole_run_timeout_seconds=100.0,
        clock=active_clock,
    )
    rows: list[dict] = []
    return (
        ObservedCallExecutor(
            registry=registry,
            plan=plan,
            budget=budget,
            invoke=invoke,
            trace_sink=rows,
            execution_binding=execution_binding,
            operation_process_binding_resolver=(
                operation_process_binding_resolver
            ),
        ),
        budget,
        rows,
    )


def test_default_registry_and_l0_plan_are_content_addressed_and_closed() -> None:
    first = default_tool_call_registry()
    second = default_tool_call_registry()
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64
    assert first.operation("vivado.open_checkpoint").mutates_design is False
    assert first.operation("vivado.place_design").event_class is EventClass.IMPLEMENTATION_HEAVY
    assert first.operation("vivado.place_design").mutates_design is True
    with pytest.raises(ToolCallContractError, match="unknown_operation"):
        first.operation("vivado.run_tcl")

    l0 = declared_l0_read_only_plan(first)
    assert l0.l0_read_only is True
    assert [rule.operation_id for rule in l0.rules] == [
        "validation.hash_root",
        "host.session.start",
        "vivado.open_checkpoint",
        "vivado.report_timing_summary",
        "vivado.report_route_status",
        "vivado.report_utilization",
        "vivado.close_design",
        "host.session.close",
        "validation.hash_root",
        "validation.validate_root",
    ]
    assert l0.rules[5].requirement is CallRequirement.OPTIONAL_BOUNDED
    assert l0.rules[5].max_calls == 1


def test_declared_plan_rejects_unknown_schema_version() -> None:
    registry = default_tool_call_registry()
    with pytest.raises(ValueError, match="unknown declared call-plan schema"):
        DeclaredCallPlan(
            "test.v1",
            "bad-schema",
            registry.sha256,
            (CallRule("vivado.open_checkpoint"),),
            schema_version="unknown.v999",
        )


def test_l0_plan_rejects_mutating_heavy_or_artifact_operations() -> None:
    registry = default_tool_call_registry()
    for operation_id in (
        "vivado.unroute_all",
        "vivado.place_design",
        "vivado.write_checkpoint",
    ):
        bad = _plan(CallRule(operation_id), l0=True)
        with pytest.raises(BackendContractMismatch, match="l0_forbidden_operation"):
            bad.validate_against(registry)


def test_exact_multi_call_plan_records_actual_monotonic_durations_and_reconciles() -> None:
    clock = FakeClock()
    entered: list[str] = []

    async def invoke(operation, arguments):
        entered.append(operation.operation_id)
        clock.advance(float(arguments["seconds"]))
        return operation.operation_id

    plan = _plan(
        CallRule("vivado.open_checkpoint"),
        CallRule("vivado.report_timing_summary"),
        CallRule("vivado.close_design"),
    )
    observer, budget, rows = _observer(plan, invoke, clock=clock)

    async def run() -> None:
        for operation in (
            "vivado.open_checkpoint",
            "vivado.report_timing_summary",
            "vivado.close_design",
        ):
            assert await observer.call(operation, {"seconds": 0.25}) == operation

    asyncio.run(run())
    result = observer.reconcile_or_raise()
    assert result.ok
    assert entered == list(result.observed_started_operations)
    assert result.observed_terminal_status_counts[CallStatus.SUCCEEDED.value] == 3
    assert budget.used[EventClass.READ_ONLY] == 3
    assert budget.runtime_seconds[EventClass.READ_ONLY.value] == pytest.approx(0.75)
    terminals = [row for row in rows if row["record_type"] == "HOST_CALL_TERMINAL"]
    assert [row["duration_seconds"] for row in terminals] == [0.25, 0.25, 0.25]
    assert [row["sequence"] for row in rows] == list(range(len(rows)))
    assert all("execution_binding" not in row for row in rows)


def test_undeclared_second_heavy_call_is_blocked_before_host_entry() -> None:
    entered: list[str] = []

    async def invoke(operation, _arguments):
        entered.append(operation.operation_id)
        return "ok"

    observer, budget, rows = _observer(
        _plan(CallRule("vivado.place_design")), invoke
    )

    async def run() -> None:
        await observer.call("vivado.place_design", {})
        with pytest.raises(ToolCallContractError, match="excess_call"):
            await observer.call("vivado.place_design", {})

    asyncio.run(run())
    assert entered == ["vivado.place_design"]
    assert budget.used[EventClass.IMPLEMENTATION_HEAVY] == 1
    rejected = [row for row in rows if row.get("status") == "REJECTED_BEFORE_CALL"]
    assert len(rejected) == 1
    assert rejected[0]["host_callable_entered"] is False
    with pytest.raises(BackendContractMismatch, match="undeclared_or_excess_call"):
        observer.reconcile_or_raise()


def test_unknown_operation_and_budget_exhaustion_fail_before_injected_callable() -> None:
    entered = 0

    async def invoke(_operation, _arguments):
        nonlocal entered
        entered += 1

    observer, budget, _ = _observer(
        _plan(CallRule("vivado.report_route_status")),
        invoke,
        limits=_limits(READ_ONLY=0),
    )

    async def run() -> None:
        with pytest.raises(ActualCallBudgetExceeded, match="event_cap"):
            await observer.call("vivado.report_route_status", {})

    asyncio.run(run())
    assert entered == 0
    assert budget.used[EventClass.READ_ONLY] == 0

    observer2, _, rows = _observer(
        _plan(CallRule("vivado.report_route_status")), invoke
    )

    async def unknown() -> None:
        with pytest.raises(ToolCallContractError, match="unknown_operation"):
            await observer2.call("vivado.run_tcl", {"command": "route_design"})

    asyncio.run(unknown())
    assert entered == 0
    assert rows[-1]["status"] == "REJECTED_BEFORE_CALL"


def test_optional_diagnostic_is_bounded_and_may_be_skipped() -> None:
    async def invoke(operation, _arguments):
        return operation.operation_id

    plan = _plan(
        CallRule("vivado.open_checkpoint"),
        CallRule(
            "vivado.report_utilization",
            CallRequirement.OPTIONAL_BOUNDED,
            0,
            1,
        ),
        CallRule("vivado.close_design"),
    )
    skipped, _, _ = _observer(plan, invoke)

    async def run_skipped() -> None:
        await skipped.call("vivado.open_checkpoint", {})
        await skipped.call("vivado.close_design", {})

    asyncio.run(run_skipped())
    skipped_result = skipped.reconcile_or_raise()
    assert skipped_result.declared_rules[1]["observed_started_calls"] == 0
    assert skipped_result.budget_summary["used"]["READ_ONLY"] == 2

    used, _, _ = _observer(plan, invoke)

    async def run_used() -> None:
        await used.call("vivado.open_checkpoint", {})
        await used.call("vivado.report_utilization", {})
        with pytest.raises(ToolCallContractError, match="expected:vivado.close_design"):
            await used.call("vivado.report_utilization", {})
        await used.call("vivado.close_design", {})

    asyncio.run(run_used())
    with pytest.raises(BackendContractMismatch):
        used.reconcile_or_raise()


def test_optional_call_out_of_order_and_over_maximum_fail_before_host_entry() -> None:
    entered: list[str] = []

    async def invoke(operation, _arguments):
        entered.append(operation.operation_id)
        return operation.operation_id

    plan = _plan(
        CallRule("vivado.open_checkpoint"),
        CallRule(
            "vivado.report_utilization",
            CallRequirement.OPTIONAL_BOUNDED,
            0,
            2,
        ),
        CallRule("vivado.close_design"),
    )

    out_of_order, _, out_of_order_rows = _observer(plan, invoke)

    async def run_out_of_order() -> None:
        await out_of_order.call("vivado.open_checkpoint", {})
        await out_of_order.call("vivado.close_design", {})
        with pytest.raises(ToolCallContractError, match="excess_call"):
            await out_of_order.call("vivado.report_utilization", {})

    asyncio.run(run_out_of_order())
    assert entered == ["vivado.open_checkpoint", "vivado.close_design"]
    assert out_of_order_rows[-1]["host_callable_entered"] is False
    with pytest.raises(BackendContractMismatch):
        out_of_order.reconcile_or_raise()

    entered.clear()
    over_limit, _, over_limit_rows = _observer(plan, invoke)

    async def run_over_limit() -> None:
        await over_limit.call("vivado.open_checkpoint", {})
        await over_limit.call("vivado.report_utilization", {})
        await over_limit.call("vivado.report_utilization", {})
        with pytest.raises(ToolCallContractError, match="expected:vivado.close_design"):
            await over_limit.call("vivado.report_utilization", {})
        await over_limit.call("vivado.close_design", {})

    asyncio.run(run_over_limit())
    assert entered == [
        "vivado.open_checkpoint",
        "vivado.report_utilization",
        "vivado.report_utilization",
        "vivado.close_design",
    ]
    assert over_limit_rows[-3]["host_callable_entered"] is False
    with pytest.raises(BackendContractMismatch):
        over_limit.reconcile_or_raise()


def test_failed_timeout_and_cancelled_calls_have_terminal_rows_and_are_counted() -> None:
    async def failed(_operation, _arguments):
        raise RuntimeError("failure at /secret/design.dcp token=abc")

    failed_observer, failed_budget, failed_rows = _observer(
        _plan(CallRule("vivado.report_timing_summary")), failed
    )
    with pytest.raises(RuntimeError):
        asyncio.run(failed_observer.call("vivado.report_timing_summary", {}))
    assert failed_budget.used[EventClass.READ_ONLY] == 1
    terminal = failed_rows[-1]
    assert terminal["status"] == "FAILED"
    assert "/secret/design.dcp" not in terminal["bounded_diagnostic"]
    assert "abc" not in terminal["bounded_diagnostic"]

    async def hangs(_operation, _arguments):
        await asyncio.sleep(60)

    timeout_observer, timeout_budget, timeout_rows = _observer(
        _plan(CallRule("vivado.report_route_status")), hangs
    )
    with pytest.raises(HostCallTimedOut):
        asyncio.run(
            timeout_observer.call(
                "vivado.report_route_status", {}, timeout_seconds=0.01
            )
        )
    assert timeout_budget.used[EventClass.READ_ONLY] == 1
    assert timeout_rows[-1]["status"] == "TIMED_OUT"

    cancel_observer, cancel_budget, cancel_rows = _observer(
        _plan(CallRule("vivado.report_route_status")), hangs
    )

    async def cancel() -> None:
        task = asyncio.create_task(
            cancel_observer.call("vivado.report_route_status", {})
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert cancel_budget.used[EventClass.READ_ONLY] == 1
    assert cancel_rows[-1]["status"] == "CANCELLED"


@pytest.mark.parametrize(
    "diagnostic",
    (
        "unknown:/private/site/raw.dcp",
        "open(/private/site/raw.dcp)",
    ),
)
def test_public_diagnostic_redacts_paths_after_punctuation(diagnostic: str) -> None:
    async def invoke(_operation, _arguments):
        raise RuntimeError(diagnostic)

    observer, _, rows = _observer(
        _plan(CallRule("vivado.report_route_status")), invoke
    )
    with pytest.raises(RuntimeError):
        asyncio.run(observer.call("vivado.report_route_status", {}))

    public_diagnostic = rows[-1]["bounded_diagnostic"]
    assert "/private" not in public_diagnostic
    assert "raw.dcp" not in public_diagnostic
    assert "[REDACTED_PATH]" in public_diagnostic


@pytest.mark.parametrize(
    "diagnostic",
    (
        "path=/private/alpha beta/secret.dcp token=abc",
        'path="/private/alpha beta/secret.dcp" token="abc"',
        "file:///private/alpha beta/secret.dcp token=abc",
        r"path=C:\Private Folder\secret.dcp token=abc",
    ),
    ids=("unquoted-posix", "quoted-posix", "file-uri", "windows-spaces"),
)
def test_public_diagnostic_redacts_complete_space_bearing_paths_and_tokens(
    diagnostic: str,
) -> None:
    async def invoke(_operation, _arguments):
        raise RuntimeError(diagnostic)

    observer, _, rows = _observer(
        _plan(CallRule("vivado.report_route_status")), invoke
    )
    with pytest.raises(RuntimeError):
        asyncio.run(observer.call("vivado.report_route_status", {}))

    public = rows[-1]["bounded_diagnostic"]
    assert "[REDACTED_PATH]" in public
    assert "[REDACTED_SECRET]" in public
    for leaked in (
        "/private",
        "alpha",
        "beta",
        "secret.dcp",
        "file://",
        r"C:\Private",
        "Private Folder",
        "abc",
    ):
        assert leaked not in public


def test_public_diagnostic_does_not_mistake_https_for_local_path() -> None:
    diagnostic = "request failed at https://provider.invalid/public/status"

    async def invoke(_operation, _arguments):
        raise RuntimeError(diagnostic)

    observer, _, rows = _observer(
        _plan(CallRule("vivado.report_route_status")), invoke
    )
    with pytest.raises(RuntimeError):
        asyncio.run(observer.call("vivado.report_route_status", {}))

    public = rows[-1]["bounded_diagnostic"]
    assert "https://provider.invalid/public/status" in public
    assert "[REDACTED_PATH]" not in public


def test_argument_hash_changes_but_public_trace_redacts_paths_and_secrets() -> None:
    async def invoke(_operation, _arguments):
        return None

    first, _, first_rows = _observer(
        _plan(CallRule("vivado.open_checkpoint")), invoke
    )
    second, _, second_rows = _observer(
        _plan(CallRule("vivado.open_checkpoint")), invoke
    )
    asyncio.run(
        first.call(
            "vivado.open_checkpoint",
            {"dcp_path": "/private/a.dcp", "license_token": "secret-a"},
        )
    )
    asyncio.run(
        second.call(
            "vivado.open_checkpoint",
            {"dcp_path": "/private/b.dcp", "license_token": "secret-b"},
        )
    )
    first_start = first_rows[0]
    second_start = second_rows[0]
    assert first_start["argument_hash"] != second_start["argument_hash"]
    serialized = repr(first_start["public_argument_summary"])
    assert "/private" not in serialized
    assert "secret-a" not in serialized
    assert "REDACTED_PATH" in serialized
    assert "REDACTED_SECRET" in serialized


def test_nested_arguments_and_sink_rows_cannot_rewrite_observed_trace() -> None:
    host_seen: list[object] = []

    async def invoke(_operation, arguments):
        host_seen.append(arguments["nested"]["value"])
        with pytest.raises(TypeError):
            arguments["nested"]["value"] = 2

    source = {"nested": {"value": 1}}
    observer, _, rows = _observer(
        _plan(CallRule("vivado.open_checkpoint")), invoke
    )
    asyncio.run(observer.call("vivado.open_checkpoint", source))
    source["nested"]["value"] = 9
    rows[0]["operation_id"] = "vivado.place_design"
    rows[0]["public_argument_summary"]["nested"]["value"] = 7

    assert host_seen == [1]
    assert observer.trace_rows[0]["operation_id"] == "vivado.open_checkpoint"
    assert observer.trace_rows[0]["public_argument_summary"]["nested"]["value"] == 1


def test_execution_binding_is_fixed_and_exact_on_every_trace_row() -> None:
    async def invoke(operation, _arguments):
        return operation.operation_id

    source_binding = {
        "execution_binding_id": "wp003-preflight-0001",
        "session_binding_id": "owned-session-0001",
        "root_process_token_hash": "a" * 64,
    }
    observer, _, rows = _observer(
        _plan(CallRule("vivado.open_checkpoint")),
        invoke,
        execution_binding=source_binding,
    )
    asyncio.run(observer.call("vivado.open_checkpoint", {}))
    with pytest.raises(ToolCallContractError, match="excess_call"):
        asyncio.run(observer.call("vivado.open_checkpoint", {}))

    expected = dict(source_binding)
    source_binding["session_binding_id"] = "rewritten-by-caller"
    assert len(rows) == 3
    assert [row["record_type"] for row in rows] == [
        "HOST_CALL_START",
        "HOST_CALL_TERMINAL",
        "HOST_CALL_TERMINAL",
    ]
    assert rows[0]["call_id"] == rows[1]["call_id"]
    assert rows[2]["call_id"] is None
    assert all(row["execution_binding"] == expected for row in rows)
    assert all(
        row["execution_binding"] == expected for row in observer.trace_rows
    )


def test_terminal_row_binds_the_actual_operation_process_token() -> None:
    process_token = "b" * 64

    async def invoke(operation, _arguments):
        return {"actual_operation": operation.operation_id}

    def resolve(operation, result, error):
        assert operation.operation_id == "vivado.open_checkpoint"
        assert result == {"actual_operation": "vivado.open_checkpoint"}
        assert error is None
        return {
            "binding_type": "owned_vivado_session",
            "root_process_token_hash": process_token,
            "session_owned": True,
        }

    observer, _, rows = _observer(
        _plan(CallRule("vivado.open_checkpoint")),
        invoke,
        execution_binding={"runtime_binding_id": "a" * 64},
        operation_process_binding_resolver=resolve,
    )
    result = asyncio.run(observer.call("vivado.open_checkpoint", {}))

    assert result == {"actual_operation": "vivado.open_checkpoint"}
    assert "operation_process_binding" not in rows[0]
    assert rows[1]["operation_process_binding"] == {
        "binding_type": "owned_vivado_session",
        "root_process_token_hash": process_token,
        "session_owned": True,
    }


def test_recovery_validation_call_is_counted_traced_and_reconciliation_fails() -> None:
    entered = 0

    async def invoke(_operation, _arguments):
        nonlocal entered
        entered += 1
        return None

    observer, budget, rows = _observer(
        _plan(
            CallRule("vivado.open_checkpoint"),
            CallRule("validation.hash_root"),
        ),
        invoke,
    )
    asyncio.run(
        observer.call(
            "validation.hash_root",
            {"phase": "failure_cleanup"},
            recovery_after_failure=True,
        )
    )

    assert entered == 1
    assert budget.used[EventClass.VALIDATION] == 1
    assert [row["record_type"] for row in rows] == [
        "HOST_CALL_START",
        "HOST_CALL_TERMINAL",
    ]
    assert all(row["recovery_after_failure"] is True for row in rows)
    reconciliation = observer.reconciliation()
    assert reconciliation.ok is False
    assert "recovery_after_failure:validation.hash_root" in reconciliation.errors


def test_recovery_infrastructure_close_remains_bounded_and_accounted_after_deadline() -> None:
    clock = FakeClock()
    registry = default_tool_call_registry()
    budget = ActualCallBudget(
        _limits(), whole_run_timeout_seconds=0.1, clock=clock
    )
    rows: list[dict] = []

    async def invoke(operation, _arguments):
        assert operation.operation_id == "host.session.close"
        clock.advance(0.25)
        return {"cleanup_ok": True}

    observer = ObservedCallExecutor(
        registry=registry,
        plan=_plan(CallRule("host.session.close")),
        budget=budget,
        invoke=invoke,
        trace_sink=rows,
    )
    clock.advance(0.2)

    result = asyncio.run(
        observer.call(
            "host.session.close",
            {"recovery_after_failure": True},
            timeout_seconds=1.0,
            recovery_after_failure=True,
        )
    )

    assert result == {"cleanup_ok": True}
    assert budget.infrastructure_calls == 1
    assert budget.runtime_seconds["INFRASTRUCTURE"] == pytest.approx(0.25)
    assert rows[0]["started_after_whole_run_deadline"] is True
    assert rows[0]["timeout_seconds"] == 1.0
    assert rows[-1]["status"] == "SUCCEEDED"
    reconciliation = observer.reconciliation()
    assert reconciliation.ok is False
    assert "recovery_after_failure:host.session.close" in reconciliation.errors


def test_recovery_infrastructure_cannot_start_a_new_session() -> None:
    entered = False

    async def invoke(_operation, _arguments):
        nonlocal entered
        entered = True

    observer, budget, rows = _observer(
        _plan(CallRule("host.session.start")), invoke
    )

    with pytest.raises(
        ToolCallContractError,
        match="recovery_infrastructure_must_be_session_close",
    ):
        asyncio.run(
            observer.call(
                "host.session.start", {}, recovery_after_failure=True
            )
        )

    assert entered is False
    assert budget.infrastructure_calls == 0
    assert rows[-1]["status"] == "REJECTED_BEFORE_CALL"


@pytest.mark.parametrize(
    "execution_binding",
    (
        {"session_binding_id": "open(/private/site/raw.dcp)"},
        {"session_token": "raw-secret"},
        {"session_binding_id": "token=raw-secret"},
    ),
)
def test_execution_binding_rejects_paths_and_secrets(execution_binding) -> None:
    async def invoke(_operation, _arguments):
        return None

    with pytest.raises(ValueError, match="execution_binding"):
        _observer(
            _plan(CallRule("vivado.open_checkpoint")),
            invoke,
            execution_binding=execution_binding,
        )


def test_trace_sink_failure_blocks_host_and_records_internal_terminal() -> None:
    entered = False

    async def invoke(_operation, _arguments):
        nonlocal entered
        entered = True

    def failing_sink(_row):
        raise OSError("sink unavailable")

    registry = default_tool_call_registry()
    observer = ObservedCallExecutor(
        registry=registry,
        plan=_plan(CallRule("vivado.open_checkpoint")),
        budget=ActualCallBudget(
            _limits(), whole_run_timeout_seconds=10.0, clock=FakeClock()
        ),
        invoke=invoke,
        trace_sink=failing_sink,
    )

    with pytest.raises(ToolCallContractError, match="trace_sink_failure"):
        asyncio.run(observer.call("vivado.open_checkpoint", {}))
    assert entered is False
    assert observer.trace_rows[-1]["status"] == "REJECTED_BEFORE_CALL"


def test_rejected_public_trace_hashes_unknown_operation_and_redacts_assignment_path() -> None:
    async def invoke(_operation, _arguments):
        raise AssertionError("must not run")

    observer, _, rows = _observer(
        _plan(CallRule("vivado.open_checkpoint")), invoke
    )
    malicious = "file=file:///secret/site/raw.dcp token=abc"
    with pytest.raises(ToolCallContractError):
        asyncio.run(observer.call(malicious, {}))
    rendered = repr(rows[-1])
    assert "/secret" not in rendered
    assert "abc" not in rendered
    assert rows[-1]["operation_id"].startswith("unclassified:")


def test_timeout_cannot_exceed_registry_upper_bound() -> None:
    async def invoke(_operation, _arguments):
        return None

    observer, budget, rows = _observer(
        _plan(CallRule("vivado.close_design")), invoke
    )
    with pytest.raises(ToolCallContractError, match="exceeds_registry_bound"):
        asyncio.run(
            observer.call("vivado.close_design", {}, timeout_seconds=61.0)
        )
    assert budget.used[EventClass.READ_ONLY] == 0
    assert rows[-1]["host_callable_entered"] is False
