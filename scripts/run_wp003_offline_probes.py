#!/usr/bin/env python3
"""Run replayable, offline adversarial probes for WP-003 Gates 0--4.

The probes exercise only local value objects, fake provider/host callables, a
short-lived Python subprocess, temporary SQLite stores, and fake artifact
bytes.  They never inspect a real DCP and never start Vivado, RapidWright,
MCP, a network client, or an external model.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.core.domain import (  # noqa: E402
    ActionSpecification,
    ArtifactKind,
    ArtifactReference,
    Attempt,
    AttemptEventKind,
    AttemptLifecycleEvent,
    DesignState,
    ExecutionScope,
    Run,
    RunLifecycleEvent,
    RunStatus,
    ScopeKind,
    ValidationStatus,
)
from optimizer_pipeline.core.store import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    SQLiteRunStore,
)
from optimizer_pipeline.dof.artifacts import (  # noqa: E402
    ArtifactFailurePoint,
    ArtifactTransaction,
    ArtifactValidation,
    InjectedArtifactCrash,
    run_evidence_finalizer,
    verify_artifact_lifecycle,
)
from optimizer_pipeline.dof.contracts import EventClass  # noqa: E402
from optimizer_pipeline.dof.processes import (  # noqa: E402
    OwnedProcessRunner,
    ProcessEndpoint,
    ProcessLimits,
)
from optimizer_pipeline.dof.provenance import (  # noqa: E402
    RepositoryConstraintMismatch,
    RepositoryObservation,
    enforce_repository_constraint,
    validate_repository_observation_payload,
)
from optimizer_pipeline.dof.provider import (  # noqa: E402
    DoFProviderObserver,
    ProviderCancelled,
    ProviderRequestBinding,
    ProviderRunLedger,
    ProviderSettings,
    RawProviderResponse,
    ScriptedFakeProvider,
    StrictProviderParseError,
    parse_exact_action_proposal,
    verify_provider_attempts,
)
from optimizer_pipeline.dof.toolcalls import (  # noqa: E402
    ActualCallBudget,
    BackendContractMismatch,
    CallRule,
    DeclaredCallPlan,
    ObservedCallExecutor,
    ToolCallContractError,
    default_tool_call_registry,
)


SCHEMA_VERSION = "research-flow.wp003-offline-adversarial-probes.v1"
RUN_ID = "run-wp003-crash-probe"
SCOPE_ID = "scope-root"
ROOT_STATE_ID = "state-root"
ATTEMPT_ID = "attempt-crash-probe"


class StepClock:
    """Deterministic monotonic clock for replayable fake-call evidence."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class ProviderClock:
    """Return deterministic successive provider timestamps."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += 0.25
        return current


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _clean_repository_observation() -> RepositoryObservation:
    empty = hashlib.sha256(b"").hexdigest()
    clean_diff = hashlib.sha256(b"cached\0\0working\0").hexdigest()
    return RepositoryObservation(
        observed_at="2026-08-06T12:00:00.000000+00:00",
        repository_root_identity_sha256="2" * 64,
        head_commit="1" * 40,
        head_tree="3" * 40,
        ref_state="branch",
        branch_name="probe-branch",
        tracked_tree_dirty=False,
        staged_entry_count=0,
        unstaged_entry_count=0,
        conflicted_entry_count=0,
        assume_unchanged_entry_count=0,
        skip_worktree_entry_count=0,
        tracked_status_sha256=empty,
        index_flag_status_sha256="5" * 64,
        binary_diff_sha256=clean_diff,
        submodule_status_sha256=empty,
        submodule_count=0,
        nonclean_submodule_count=0,
        untracked_entry_count=0,
        untracked_status_sha256=empty,
        git_version="git version 2.43.0",
    )


def _gate0_probes() -> dict[str, Any]:
    observed = _clean_repository_observation()
    mismatch_rejected = False
    mismatch_code: str | None = None
    mismatch_reasons: list[str] = []
    try:
        enforce_repository_constraint(
            {
                "repository_mode": "git",
                "required_head": "f" * 40,
                "require_tracked_clean": True,
                "untracked_policy": "record_only",
            },
            observed,
        )
    except RepositoryConstraintMismatch as exc:
        mismatch_rejected = True
        mismatch_code = exc.code
        mismatch_reasons = list(exc.reasons)

    forged_clean = observed.to_dict()
    forged_clean["binary_diff_sha256"] = "9" * 64
    forged_reasons = list(validate_repository_observation_payload(forged_clean))
    forged_rejected = "repository_provenance_clean_diff_hash_mismatch" in forged_reasons

    probes = {
        "declared_constraint_mismatch": {
            "rejected": mismatch_rejected,
            "endpoint": mismatch_code,
            "reasons": mismatch_reasons,
            "backend_or_provider_entries": 0,
        },
        "forged_clean_provenance": {
            "rejected": forged_rejected,
            "reasons": forged_reasons,
            "trusted_as_observation": False,
        },
    }
    passed = (
        mismatch_rejected
        and mismatch_code == "DECLARED_CONSTRAINT_MISMATCH"
        and mismatch_reasons == ["required_head_mismatch"]
        and forged_rejected
    )
    return {"passed": passed, "probes": probes}


def _event_limits(value: int = 4) -> dict[EventClass, int]:
    return {event_class: value for event_class in EventClass}


def _stable_trace(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "record_type",
        "operation_id",
        "event_class",
        "status",
        "host_callable_entered",
        "count_after_start",
    )
    return [{key: row[key] for key in keys if key in row} for row in rows]


def _gate1_probes() -> dict[str, Any]:
    registry = default_tool_call_registry()

    l0_rejected = False
    l0_reasons: list[str] = []
    l0_plan = DeclaredCallPlan(
        "wp003-probe-l0-bad.v1",
        "wp003-probe-l0-mutation",
        registry.sha256,
        (CallRule("vivado.place_design"),),
        l0_read_only=True,
    )
    try:
        l0_plan.validate_against(registry)
    except BackendContractMismatch as exc:
        l0_rejected = True
        l0_reasons = list(exc.errors)

    unknown_entries = 0

    async def unknown_invoke(_operation, _arguments):
        nonlocal unknown_entries
        unknown_entries += 1
        raise AssertionError("unknown operation entered fake host")

    unknown_clock = StepClock()
    unknown_rows: list[dict[str, Any]] = []
    unknown_budget = ActualCallBudget(
        _event_limits(), whole_run_timeout_seconds=10.0, clock=unknown_clock
    )
    unknown_observer = ObservedCallExecutor(
        registry=registry,
        plan=DeclaredCallPlan(
            "wp003-probe-unknown.v1",
            "wp003-probe-unknown",
            registry.sha256,
            (CallRule("vivado.report_route_status"),),
        ),
        budget=unknown_budget,
        invoke=unknown_invoke,
        trace_sink=unknown_rows,
    )
    unknown_rejected = False
    unknown_reason = ""
    try:
        asyncio.run(
            unknown_observer.call(
                "vivado.run_tcl", {"command": "route_design"}
            )
        )
    except ToolCallContractError as exc:
        unknown_rejected = True
        unknown_reason = exc.reason

    heavy_entries = 0
    heavy_clock = StepClock()
    heavy_rows: list[dict[str, Any]] = []

    async def heavy_invoke(_operation, _arguments):
        nonlocal heavy_entries
        heavy_entries += 1
        heavy_clock.advance(0.125)
        return "fake-heavy-complete"

    heavy_budget = ActualCallBudget(
        _event_limits(), whole_run_timeout_seconds=10.0, clock=heavy_clock
    )
    heavy_observer = ObservedCallExecutor(
        registry=registry,
        plan=DeclaredCallPlan(
            "wp003-probe-heavy.v1",
            "wp003-probe-one-heavy",
            registry.sha256,
            (CallRule("vivado.place_design"),),
        ),
        budget=heavy_budget,
        invoke=heavy_invoke,
        trace_sink=heavy_rows,
    )

    async def run_extra_heavy() -> tuple[bool, str]:
        await heavy_observer.call("vivado.place_design", {"directive": "Default"})
        try:
            await heavy_observer.call(
                "vivado.place_design", {"directive": "Explore"}
            )
        except ToolCallContractError as exc:
            return True, exc.reason
        return False, ""

    extra_rejected, extra_reason = asyncio.run(run_extra_heavy())
    reconciliation_rejected = False
    reconciliation_errors: list[str] = []
    try:
        heavy_observer.reconcile_or_raise()
    except BackendContractMismatch as exc:
        reconciliation_rejected = True
        reconciliation_errors = list(exc.errors)

    probes = {
        "unknown_operation": {
            "rejected": unknown_rejected,
            "reason": unknown_reason,
            "host_callable_entries": unknown_entries,
            "trace": _stable_trace(unknown_rows),
            "budget_used": {
                key.value: value for key, value in unknown_budget.used.items()
            },
        },
        "undeclared_extra_heavy": {
            "rejected": extra_rejected,
            "reason": extra_reason,
            "host_callable_entries": heavy_entries,
            "trace": _stable_trace(heavy_rows),
            "budget_used": {
                key.value: value for key, value in heavy_budget.used.items()
            },
            "runtime_seconds_by_class": dict(heavy_budget.runtime_seconds),
            "reconciliation_rejected": reconciliation_rejected,
            "reconciliation_errors": reconciliation_errors,
        },
        "l0_mutation": {
            "rejected": l0_rejected,
            "reasons": l0_reasons,
            "host_callable_entries": 0,
            "heavy_budget_used": 0,
        },
    }
    passed = (
        unknown_rejected
        and unknown_entries == 0
        and unknown_budget.used[EventClass.READ_ONLY] == 0
        and len(unknown_rows) == 1
        and unknown_rows[0].get("status") == "REJECTED_BEFORE_CALL"
        and extra_rejected
        and heavy_entries == 1
        and heavy_budget.used[EventClass.IMPLEMENTATION_HEAVY] == 1
        and len(heavy_rows) == 3
        and heavy_rows[-1].get("status") == "REJECTED_BEFORE_CALL"
        and reconciliation_rejected
        and l0_rejected
        and any("l0_forbidden_operation" in reason for reason in l0_reasons)
    )
    return {"passed": passed, "probes": probes}


def _gate2_probes() -> dict[str, Any]:
    if os.name != "posix":
        return {
            "passed": False,
            "probes": {
                "owned_short_lived_process": {
                    "endpoint": "UNSUPPORTED_OS",
                    "cleanup_ok": False,
                    "orphan_count": None,
                }
            },
        }

    limits = ProcessLimits(
        initialization_timeout_seconds=0.5,
        whole_run_timeout_seconds=1.0,
        graceful_close_seconds=0.05,
        terminate_grace_seconds=0.1,
        kill_grace_seconds=0.2,
        cleanup_timeout_seconds=0.5,
        output_limit_bytes=4096,
    )
    result = asyncio.run(
        OwnedProcessRunner(limits).run_vivado_batch_worker(
            [sys.executable, "-c", "print('READY', flush=True)"],
            ready_marker="READY",
        )
    )
    lifecycle = [row["event_type"] for row in result.lifecycle]
    probe = {
        "endpoint": result.endpoint.value,
        "session_owned": result.session_owned,
        "cleanup_ok": result.cleanup_ok,
        "orphan_count": len(result.orphan_process_tokens),
        "observed_process_count": len(result.observed_process_tokens),
        "root_process_token_present": bool(result.root_process_token),
        "stdout_sha256": result.stdout.sha256,
        "stdout_truncated": result.stdout.truncated,
        "lifecycle": lifecycle,
        "bounded_whole_run_seconds": limits.whole_run_timeout_seconds,
        "real_tool_process": False,
    }
    passed = (
        result.endpoint is ProcessEndpoint.SUCCEEDED
        and result.session_owned
        and result.cleanup_ok
        and not result.orphan_process_tokens
        and bool(result.root_process_token)
        and lifecycle[-1:] == ["PROCESS_EVIDENCE_FINALIZED"]
    )
    return {
        "passed": passed,
        "probes": {"owned_short_lived_process": probe},
    }


def _seed_artifact_store(root: Path) -> tuple[Path, SQLiteRunStore]:
    run_dir = root / "run"
    run_dir.mkdir(parents=True)
    root_path = run_dir / "root.dcp"
    root_bytes = b"fake root bytes; not a real DCP\n"
    root_path.write_bytes(root_bytes)
    store = SQLiteRunStore.create(run_dir / "run.db", Run(RUN_ID, "dof_fake"))
    root_artifact = ArtifactReference(
        "artifact-root",
        RUN_ID,
        SCOPE_ID,
        ArtifactKind.DCP,
        "root.dcp",
        sha256=hashlib.sha256(root_bytes).hexdigest(),
        size_bytes=len(root_bytes),
        metadata={"role": "fake-baseline"},
    )
    scope = ExecutionScope(SCOPE_ID, RUN_ID, ScopeKind.ROOT, "fake root")
    state = DesignState(ROOT_STATE_ID, RUN_ID, SCOPE_ID, root_artifact.artifact_id)
    action = ActionSpecification(
        "action-fake", RUN_ID, SCOPE_ID, "fake.artifact.producer"
    )
    attempt = Attempt(
        ATTEMPT_ID,
        RUN_ID,
        SCOPE_ID,
        ROOT_STATE_ID,
        action.action_id,
    )
    with store.transaction():
        store.add_run_event(
            RunLifecycleEvent("run-event-000", RUN_ID, 0, RunStatus.RUNNING)
        )
        store.add_scope(scope)
        store.add_artifact(root_artifact)
        store.add_state(state)
        store.add_action(action)
        store.add_attempt(attempt)
        store.add_attempt_event(
            AttemptLifecycleEvent(
                "attempt-event-000",
                ATTEMPT_ID,
                0,
                AttemptEventKind.SCHEDULED,
            )
        )
        store.add_attempt_event(
            AttemptLifecycleEvent(
                "attempt-event-001",
                ATTEMPT_ID,
                1,
                AttemptEventKind.STARTED,
            )
        )
    return run_dir, store


def _fake_producer(path: Path) -> None:
    path.write_bytes(b"fake child bytes; not a real DCP\n")


def _clean_validator(
    path: Path, sha256: str, size_bytes: int
) -> ArtifactValidation:
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == sha256
    assert len(raw) == size_bytes
    return ArtifactValidation(
        ValidationStatus.CLEAN,
        "wp003-offline-fake-validator.v1",
        {"fake_validation": True},
    )


def _gate3_probes(workspace: Path) -> dict[str, Any]:
    transaction_points = tuple(
        point
        for point in ArtifactFailurePoint
        if point is not ArtifactFailurePoint.DURING_EVIDENCE_FINALIZATION
    )
    matrix: list[dict[str, Any]] = []
    all_safe = True
    for failure_point in transaction_points:
        run_dir, store = _seed_artifact_store(workspace / failure_point.value)
        caught: InjectedArtifactCrash | None = None
        try:
            ArtifactTransaction(
                store=store,
                run_dir=run_dir,
                run_id=RUN_ID,
                scope_id=SCOPE_ID,
                attempt_id=ATTEMPT_ID,
                parent_state_id=ROOT_STATE_ID,
                state_id="state-child",
            ).execute(
                producer=_fake_producer,
                validator=_clean_validator,
                selection_eligible=True,
                fail_at=failure_point,
            )
        except InjectedArtifactCrash as exc:
            caught = exc

        verification = verify_artifact_lifecycle(store, run_dir=run_dir)
        selections = store.selections()
        terminal_success_count = sum(
            event.kind is AttemptEventKind.COMPLETED
            for event in store.attempt_events(attempt_id=ATTEMPT_ID)
        )
        row = {
            "failure_point": failure_point.value,
            "typed_interruption": caught is not None,
            "endpoint": caught.endpoint if caught is not None else None,
            "schema_version": store.schema_version,
            "database_integrity_ok": store.integrity_check() == (),
            "lifecycle_safe": verification.safe,
            "lifecycle_complete": verification.ok,
            "incomplete_attempt": ATTEMPT_ID in verification.incomplete_attempt_ids,
            "selection_count": len(selections),
            "selected_state_ids": [selection.state_id for selection in selections],
            "terminal_success_count": terminal_success_count,
            "contract_artifact_count": len(verification.checked_artifact_ids),
            "resume_policy": "new_run_or_repeat_only",
        }
        row_passed = (
            caught is not None
            and caught.failure_point is failure_point
            and row["database_integrity_ok"]
            and row["lifecycle_safe"]
            and not row["lifecycle_complete"]
            and row["incomplete_attempt"]
            and row["selection_count"] == 0
            and row["terminal_success_count"] == 0
            and store.schema_version == CURRENT_SCHEMA_VERSION == 2
        )
        row["passed"] = row_passed
        all_safe = all_safe and row_passed
        matrix.append(row)
        store.close()

    finalizer_calls: list[str] = []
    finalizer_caught: InjectedArtifactCrash | None = None
    try:
        run_evidence_finalizer(
            lambda: finalizer_calls.append("called"),
            fail_at=ArtifactFailurePoint.DURING_EVIDENCE_FINALIZATION,
        )
    except InjectedArtifactCrash as exc:
        finalizer_caught = exc
    finalizer_probe = {
        "failure_point": ArtifactFailurePoint.DURING_EVIDENCE_FINALIZATION.value,
        "typed_interruption": finalizer_caught is not None,
        "endpoint": (
            finalizer_caught.endpoint if finalizer_caught is not None else None
        ),
        "finalizer_called": bool(finalizer_calls),
        "selection_count": 0,
        "passed": (
            finalizer_caught is not None
            and finalizer_caught.failure_point
            is ArtifactFailurePoint.DURING_EVIDENCE_FINALIZATION
            and not finalizer_calls
        ),
    }
    all_safe = all_safe and bool(finalizer_probe["passed"])
    return {
        "passed": all_safe,
        "probes": {
            "artifact_transaction_crash_matrix": matrix,
            "evidence_finalization_crash": finalizer_probe,
        },
    }


def _provider_binding(**updates: Any) -> ProviderRequestBinding:
    values: dict[str, Any] = {
        "run_id": "run-wp003-provider-probe",
        "spec_hash": _sha("provider-spec"),
        "experiment_id": "experiment-probe",
        "condition_id": "condition-L2",
        "repeat_id": "repeat-0",
        "decision_id": "decision-001",
        "context_bytes": b'{"legal_actions":["macro.clean"]}',
        "prompt_bytes": b"Return one exact action proposal JSON object.",
        "prompt_version": "dof.prompt.probe.v1",
        "action_interface_version": "dof.action-interface-schema.v2",
        "action_interface_hash": _sha("action-interface"),
        "context_interface_version": "dof.context-interface-schema.v2",
        "context_interface_hash": _sha("context-interface"),
        "catalogue_version": "clean-macro.v1",
        "catalogue_hash": _sha("catalogue"),
        "capability_hash": _sha("capability"),
        "mask_evidence_hash": _sha("mask"),
        "provider_role": "catalogue_agent",
        "provider": "fake",
        "model_id": "fake-model-v1",
        "adapter_version": "fake-adapter.v1",
        "settings": ProviderSettings(
            max_tokens=256,
            timeout_seconds=1.0,
            max_requests=1,
            max_usd=1.0,
            max_cost_per_request_usd=0.25,
            unknown_cost_policy="reject",
        ),
    }
    values.update(updates)
    return ProviderRequestBinding(**values)


def _provider_payload(binding: ProviderRequestBinding) -> dict[str, Any]:
    return {
        "schema_version": "action_proposal.v1",
        "proposal_id": "proposal-001",
        "decision_id": binding.decision_id,
        "catalogue_version": binding.catalogue_version,
        "catalogue_hash": binding.catalogue_hash,
        "kind": "ACTION",
        "steps": [
            {
                "step_id": "step-001",
                "action_key": "macro.clean",
                "parameters": {},
            }
        ],
    }


def _provider_response(
    binding: ProviderRequestBinding, **updates: Any
) -> RawProviderResponse:
    values: dict[str, Any] = {
        "content_bytes": json.dumps(
            _provider_payload(binding), sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
        "actual_provider": binding.provider,
        "provider_request_id": "request-001",
        "returned_model_id": binding.model_id,
        "returned_model_revision": None,
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "cost_usd": 0.01,
        "cost_source": "fake_provider_reported",
    }
    values.update(updates)
    return RawProviderResponse(**values)


def _gate4_probes() -> dict[str, Any]:
    invalid_documents = {
        "fenced_json": b"```json\n{}\n```",
        "prefixed_json": b"prefix {}",
        "suffixed_json": b"{} suffix",
        "multiple_documents": b"{} {}",
        "non_object_root": b"[]",
        "duplicate_key": b'{"schema_version":"action_proposal.v1","schema_version":"x"}',
        "nonstandard_constant": b'{"x":NaN}',
        "utf8_bom": b"\xef\xbb\xbf{}",
    }
    parser_rows: list[dict[str, Any]] = []
    for label, raw in invalid_documents.items():
        reasons: list[str] = []
        rejected = False
        try:
            parse_exact_action_proposal(raw)
        except StrictProviderParseError as exc:
            rejected = True
            reasons = list(exc.reasons)
        parser_rows.append(
            {"case": label, "rejected": rejected, "reasons": reasons}
        )

    binding = _provider_binding()
    binding_transport = ScriptedFakeProvider(
        [_provider_response(binding, actual_provider="fallback-provider")]
    )
    binding_decision = DoFProviderObserver(
        monotonic=ProviderClock(),
        run_ledger=ProviderRunLedger.for_binding(binding),
    ).request(
        binding, binding_transport
    )
    binding_evidence_errors = list(
        verify_provider_attempts(binding_decision.attempts, binding)
    )

    budget_observer = DoFProviderObserver(
        monotonic=ProviderClock(),
        run_ledger=ProviderRunLedger.for_binding(binding),
    )
    first_transport = ScriptedFakeProvider([_provider_response(binding)])
    first_decision = budget_observer.request(binding, first_transport)
    second_binding = replace(
        binding,
        decision_id="decision-002",
        context_bytes=b'{"legal_actions":["macro.phys_opt"]}',
        prompt_bytes=b"A distinct prompt for the second decision.",
    )
    second_transport = ScriptedFakeProvider([_provider_response(second_binding)])
    second_decision = budget_observer.request(second_binding, second_transport)

    cancellation_transport = ScriptedFakeProvider(
        [ProviderCancelled("structured fake cancellation")]
    )
    cancellation_decision = DoFProviderObserver(
        monotonic=ProviderClock(),
        run_ledger=ProviderRunLedger.for_binding(binding),
    ).request(binding, cancellation_transport)
    cancellation_evidence_errors = list(
        verify_provider_attempts(cancellation_decision.attempts, binding)
    )

    probes = {
        "strict_parser": {
            "all_rejected": all(row["rejected"] for row in parser_rows),
            "cases": parser_rows,
            "repair_or_salvage_attempts": 0,
        },
        "response_binding": {
            "accepted": binding_decision.accepted,
            "endpoint": binding_decision.endpoint,
            "reasons": list(binding_decision.reasons),
            "attempt_count": len(binding_decision.attempts),
            "evidence_errors": binding_evidence_errors,
            "backend_dispatches": 0,
        },
        "budget_across_bindings": {
            "first_accepted": first_decision.accepted,
            "second_accepted": second_decision.accepted,
            "second_endpoint": second_decision.endpoint,
            "second_reasons": list(second_decision.reasons),
            "second_transport_calls": len(second_transport.calls),
            "request_counts_by_contract": dict(
                budget_observer.request_counts_by_contract
            ),
            "backend_dispatches_after_rejection": 0,
        },
        "structured_cancellation": {
            "accepted": cancellation_decision.accepted,
            "endpoint": cancellation_decision.endpoint,
            "attempt_statuses": [
                attempt.status.value for attempt in cancellation_decision.attempts
            ],
            "transport_calls": len(cancellation_transport.calls),
            "evidence_errors": cancellation_evidence_errors,
            "backend_dispatches": 0,
        },
    }
    passed = (
        probes["strict_parser"]["all_rejected"]
        and not binding_decision.accepted
        and binding_decision.endpoint == "PROVIDER_BINDING_MISMATCH"
        and not binding_evidence_errors
        and first_decision.accepted
        and not second_decision.accepted
        and second_decision.endpoint == "BUDGET_REJECTION"
        and second_transport.calls == []
        and not cancellation_decision.accepted
        and cancellation_decision.endpoint == "CANCELLATION"
        and len(cancellation_decision.attempts) == 1
        and cancellation_decision.attempts[0].status.value == "CANCELLED"
        and not cancellation_evidence_errors
    )
    return {"passed": passed, "probes": probes}


def run_probes() -> dict[str, Any]:
    gate0 = _gate0_probes()
    gate1 = _gate1_probes()
    gate2 = _gate2_probes()
    with tempfile.TemporaryDirectory(prefix="wp003-offline-probes-") as raw:
        gate3 = _gate3_probes(Path(raw))
    gate4 = _gate4_probes()
    gates = {
        "gate0_repository_provenance": gate0,
        "gate1_backend_calls": gate1,
        "gate2_process_ownership": gate2,
        "gate3_artifact_crash_safety": gate3,
        "gate4_provider_binding": gate4,
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "work_package": "WP-003",
        "execution_mode": "offline_adversarial_probe",
        "gates": gates,
        "all_gates_passed": all(gate["passed"] for gate in gates.values()),
        "execution_boundary": {
            "real_dcp_attempts": 0,
            "vivado_starts": 0,
            "rapidwright_starts": 0,
            "mcp_starts": 0,
            "external_model_requests": 0,
            "real_backend_calls": 0,
            "fake_owned_process_starts": 1 if os.name == "posix" else 0,
            "fake_provider_transport_calls": 3,
            "l4_execution": False,
            "schema_migration": False,
        },
    }
    if not result["all_gates_passed"]:
        raise AssertionError(json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run offline WP-003 Gate 0-4 adversarial probes."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path; parent directory must already exist.",
    )
    args = parser.parse_args()
    result = run_probes()
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
