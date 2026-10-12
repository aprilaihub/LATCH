from __future__ import annotations

import base64
import asyncio
from dataclasses import fields, replace
import hashlib
import json
from pathlib import Path
import stat

import pytest

from optimizer_pipeline.dof.evidence import ImmutableEvidenceWriter
from optimizer_pipeline.dof.provider import (
    NOT_PROVIDED,
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    BoundProviderProposal,
    DoFProviderObserver,
    ProviderAttemptRecord,
    ProviderAttemptStatus,
    ProviderBindingError,
    ProviderCancelled,
    ProviderRequestBinding,
    ProviderRunLedger,
    ProviderSettings,
    ProviderTaskCancelled,
    ProviderTimeout,
    RawProviderResponse,
    ScriptedFakeProvider,
    StrictProviderParseError,
    parse_exact_action_proposal,
    verify_provider_attempt_aggregate,
    verify_provider_attempts,
    write_provider_attempt_trace,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _binding(**updates) -> ProviderRequestBinding:
    values = {
        "run_id": "run-provider-test",
        "spec_hash": _sha("spec"),
        "experiment_id": "experiment-1",
        "condition_id": "condition-L2",
        "repeat_id": "repeat-0",
        "decision_id": "decision-001",
        "context_bytes": b'{"legal_actions":["macro.clean"]}',
        "prompt_bytes": b"Return one exact action proposal JSON object.",
        "prompt_version": "dof.prompt.v1",
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


def _proposal_payload(binding: ProviderRequestBinding, **updates) -> dict:
    payload = {
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
    payload.update(updates)
    return payload


def _raw(binding: ProviderRequestBinding, **updates) -> RawProviderResponse:
    values = {
        "content_bytes": json.dumps(
            _proposal_payload(binding), sort_keys=True, separators=(",", ":")
        ).encode(),
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


def _forge_attempt(
    source: ProviderAttemptRecord, **updates: object
) -> ProviderAttemptRecord:
    """Bypass frozen-dataclass construction to adversarially probe verifier trust."""

    forged = object.__new__(ProviderAttemptRecord)
    for item in fields(ProviderAttemptRecord):
        object.__setattr__(
            forged,
            item.name,
            updates.get(item.name, getattr(source, item.name)),
        )
    return forged


class TickClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += 0.25
        return current


def _observer_for(
    binding: ProviderRequestBinding,
    **kwargs: object,
) -> DoFProviderObserver:
    kwargs.setdefault("run_ledger", ProviderRunLedger.for_binding(binding))
    return DoFProviderObserver(**kwargs)


def test_exact_response_is_bound_and_reverified_before_dispatch() -> None:
    binding = _binding()
    transport = ScriptedFakeProvider([_raw(binding)])
    decision = _observer_for(binding, monotonic=TickClock()).request(binding, transport)

    assert decision.accepted
    assert decision.endpoint == "PROVIDER_RESPONSE_BOUND"
    assert len(decision.attempts) == 1
    attempt = decision.attempts[0]
    assert attempt.status is ProviderAttemptStatus.SUCCEEDED
    assert attempt.request_contract_hash == binding.request_contract_hash
    assert attempt.request["context_bytes_b64"] == base64.b64encode(
        binding.context_bytes
    ).decode()
    assert attempt.returned_model_revision == NOT_PROVIDED
    assert decision.bound_proposal is not None
    proposal = decision.bound_proposal.verify_for_dispatch(binding)
    assert proposal.decision_id == binding.decision_id
    assert proposal.catalogue_hash == binding.catalogue_hash
    assert verify_provider_attempts(decision.attempts, binding) == ()
    assert transport.calls == [(binding.request_contract_hash, 0)]


def test_observer_persists_request_budget_across_calls_for_same_binding() -> None:
    binding = _binding()
    transport = ScriptedFakeProvider([_raw(binding)])
    observer = _observer_for(binding, monotonic=TickClock())

    first = observer.request(binding, transport)
    second = observer.request(binding, transport)

    assert first.accepted
    assert not second.accepted
    assert second.endpoint == "BUDGET_REJECTION"
    assert second.attempts == ()
    assert second.reasons == ("llm_request_budget_exhausted",)
    assert transport.calls == [(binding.request_contract_hash, 0)]


def test_observer_run_cap_cannot_be_reset_by_a_different_request_binding() -> None:
    first_binding = _binding()
    second_binding = replace(
        first_binding,
        decision_id="decision-002",
        context_bytes=b'{"legal_actions":["macro.phys_opt"]}',
        prompt_bytes=b"A distinct prompt for a distinct decision.",
    )
    assert first_binding.request_contract_hash != second_binding.request_contract_hash
    observer = _observer_for(first_binding, monotonic=TickClock())
    first_transport = ScriptedFakeProvider([_raw(first_binding)])
    second_transport = ScriptedFakeProvider([_raw(second_binding)])

    first = observer.request(first_binding, first_transport)
    second = observer.request(second_binding, second_transport)

    assert first.accepted
    assert not second.accepted
    assert second.endpoint == "BUDGET_REJECTION"
    assert second.attempts == ()
    assert second.reasons == ("llm_request_budget_exhausted",)
    assert first_transport.calls == [(first_binding.request_contract_hash, 0)]
    assert second_transport.calls == []
    assert dict(observer.request_counts_by_contract) == {
        first_binding.request_contract_hash: 1,
        second_binding.request_contract_hash: 0,
    }


def test_unowned_observers_fail_closed_before_any_physical_send() -> None:
    binding = _binding()
    first_transport = ScriptedFakeProvider([_raw(binding)])
    second_transport = ScriptedFakeProvider([_raw(binding)])

    for transport in (first_transport, second_transport):
        with pytest.raises(
            ProviderBindingError,
            match="explicit_run_owner_provider_ledger_required",
        ):
            DoFProviderObserver().request(binding, transport)

    assert first_transport.calls == []
    assert second_transport.calls == []


def test_injected_run_ledger_enforces_cap_across_distinct_observers_and_traces() -> None:
    first_binding = _binding()
    second_binding = replace(
        first_binding,
        decision_id="decision-002",
        context_bytes=b'{"legal_actions":["macro.phys_opt"]}',
        prompt_bytes=b"Second decision cannot reset the run ledger.",
    )
    ledger = ProviderRunLedger.for_binding(first_binding)
    first_observer = DoFProviderObserver(
        monotonic=TickClock(), run_ledger=ledger
    )
    second_observer = DoFProviderObserver(
        monotonic=TickClock(), run_ledger=ledger
    )
    first_transport = ScriptedFakeProvider([_raw(first_binding)])
    second_transport = ScriptedFakeProvider([_raw(second_binding)])

    first = first_observer.request(first_binding, first_transport)
    second = second_observer.request(second_binding, second_transport)

    assert first.accepted
    assert not second.accepted
    assert second.endpoint == "BUDGET_REJECTION"
    assert second.attempts == ()
    assert first_transport.calls == [(first_binding.request_contract_hash, 0)]
    assert second_transport.calls == []
    assert len(first_transport.calls) + len(second_transport.calls) == 1
    attempt = first.attempts[0]
    assert attempt.run_ledger_id == ledger.run_ledger_id
    assert attempt.global_request_sequence == 0
    assert attempt.global_requests_after_attempt == 1
    assert attempt.contract_requests_after_attempt == 1
    assert attempt.global_accounted_cost_usd_after_attempt == pytest.approx(0.01)
    snapshot = ledger.snapshot()
    assert snapshot.global_requests == 1
    assert snapshot.cumulative_accounted_cost_usd == pytest.approx(0.01)
    assert dict(snapshot.requests_by_contract) == {
        first_binding.request_contract_hash: 1,
        second_binding.request_contract_hash: 0,
    }
    assert verify_provider_attempt_aggregate(
        (
            (first_binding, first.attempts),
            (second_binding, second.attempts),
        ),
        ledger_snapshot=snapshot,
    ) == ()


def test_cross_trace_verifier_detects_a_second_ledger_and_sequence_reset() -> None:
    settings = ProviderSettings(
        max_tokens=256,
        timeout_seconds=1.0,
        max_requests=2,
        max_usd=1.0,
        max_cost_per_request_usd=0.25,
        unknown_cost_policy="reject",
    )
    first_binding = _binding(settings=settings)
    second_binding = replace(
        first_binding,
        decision_id="decision-002",
        context_bytes=b'{"legal_actions":["macro.phys_opt"]}',
    )
    first = DoFProviderObserver(
        run_ledger=ProviderRunLedger.for_binding(first_binding)
    ).request(first_binding, ScriptedFakeProvider([_raw(first_binding)]))
    second = DoFProviderObserver(
        run_ledger=ProviderRunLedger.for_binding(second_binding)
    ).request(second_binding, ScriptedFakeProvider([_raw(second_binding)]))

    errors = verify_provider_attempt_aggregate(
        (
            (first_binding, first.attempts),
            (second_binding, second.attempts),
        )
    )
    assert "aggregate uses multiple run ledger IDs" in errors
    assert "aggregate global request sequence is not exact/contiguous" in errors


def test_transport_must_declare_single_physical_request_and_disabled_sdk_retries() -> None:
    class OpaqueSdkTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _binding, _retry_index):
            self.calls += 1
            return _raw(binding)

    binding = _binding()
    transport = OpaqueSdkTransport()

    with pytest.raises(ProviderBindingError, match="SDK retries disabled"):
        _observer_for(binding).request(binding, transport)
    assert transport.calls == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"```json\n{}\n```",
        b"prefix {}",
        b"{} suffix",
        b"{} {}",
        b"[]",
        b'{"schema_version":"action_proposal.v1","schema_version":"x"}',
        b'{"x":NaN}',
        b"\xef\xbb\xbf{}",
    ],
)
def test_strict_parser_rejects_fences_extra_documents_nonobjects_duplicates_and_repairs(
    raw: bytes,
) -> None:
    with pytest.raises(StrictProviderParseError):
        parse_exact_action_proposal(raw)


def test_whitespace_is_legal_json_but_no_envelope_defaults_are_inserted() -> None:
    binding = _binding()
    encoded = (" \n" + json.dumps(_proposal_payload(binding)) + "\t").encode()
    assert parse_exact_action_proposal(encoded).decision_id == binding.decision_id
    with pytest.raises(StrictProviderParseError, match="missing_fields"):
        parse_exact_action_proposal(b"{}")


def test_timeout_is_counted_and_one_predeclared_retry_can_succeed() -> None:
    settings = ProviderSettings(
        max_tokens=256,
        timeout_seconds=1.0,
        max_retries=1,
        max_requests=2,
        max_usd=1.0,
        max_cost_per_request_usd=0.25,
        unknown_cost_policy="reject",
    )
    binding = _binding(settings=settings)
    transport = ScriptedFakeProvider(
        [ProviderTimeout("injected timeout"), _raw(binding)]
    )
    decision = _observer_for(binding, monotonic=TickClock()).request(binding, transport)

    assert decision.accepted
    assert [item.status for item in decision.attempts] == [
        ProviderAttemptStatus.TIMED_OUT,
        ProviderAttemptStatus.SUCCEEDED,
    ]
    assert [item.retry_index for item in decision.attempts] == [0, 1]
    assert len(transport.calls) == 2
    assert verify_provider_attempts(decision.attempts, binding) == ()


def test_configured_timeout_is_enforced_by_async_transport_boundary() -> None:
    class SlowProvider:
        transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
        internal_retries_disabled = True

        async def send(self, _binding, _retry_index):
            await asyncio.sleep(0.05)
            return _raw(binding)

    binding = _binding(
        settings=ProviderSettings(
            timeout_seconds=0.001,
            max_requests=1,
            max_usd=None,
            unknown_cost_policy="allow_unpriced",
        )
    )
    decision = _observer_for(binding).request(binding, SlowProvider())

    assert not decision.accepted
    assert decision.endpoint == "TIMEOUT"
    assert decision.attempts[0].status is ProviderAttemptStatus.TIMED_OUT
    assert verify_provider_attempts(decision.attempts, binding) == ()


def test_sync_transport_is_rejected_before_request() -> None:
    class BlockingTransport:
        def send(self, _binding, _retry_index):
            raise AssertionError("must not be called")

    binding = _binding()
    with pytest.raises(ProviderBindingError, match="must be async"):
        _observer_for(binding).request(binding, BlockingTransport())


def test_cancellation_is_terminal_and_not_retried() -> None:
    settings = ProviderSettings(
        max_retries=1,
        max_requests=2,
        max_usd=None,
        unknown_cost_policy="allow_unpriced",
    )
    binding = _binding(settings=settings)
    transport = ScriptedFakeProvider(
        [ProviderCancelled("cancelled"), _raw(binding)]
    )
    decision = _observer_for(binding, monotonic=TickClock()).request(binding, transport)

    assert not decision.accepted
    assert decision.endpoint == "CANCELLATION"
    assert len(decision.attempts) == 1
    assert decision.attempts[0].status is ProviderAttemptStatus.CANCELLED
    assert len(transport.calls) == 1


def test_asyncio_task_cancellation_propagates_with_sealable_attempt_evidence(
    tmp_path: Path,
) -> None:
    class WaitingProvider:
        transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
        internal_retries_disabled = True

        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def send(self, _binding, _retry_index):
            self.started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    binding = _binding(
        settings=ProviderSettings(
            timeout_seconds=10.0,
            max_retries=1,
            max_requests=2,
            max_usd=None,
            unknown_cost_policy="allow_unpriced",
        )
    )
    observer = _observer_for(binding, monotonic=TickClock())
    transport = WaitingProvider()

    async def run_and_cancel() -> ProviderTaskCancelled:
        task = asyncio.create_task(observer.request_async(binding, transport))
        await transport.started.wait()
        task.cancel()
        with pytest.raises(ProviderTaskCancelled) as caught:
            await task
        return caught.value

    cancelled = asyncio.run(run_and_cancel())

    assert isinstance(cancelled, asyncio.CancelledError)
    assert cancelled.endpoint == "CANCELLATION"
    assert cancelled.reasons == ("provider_task_cancelled",)
    assert cancelled.request_contract_hash == binding.request_contract_hash
    assert cancelled.evidence_errors == ()
    assert len(cancelled.attempts) == 1
    attempt = cancelled.attempts[0]
    assert attempt.status is ProviderAttemptStatus.CANCELLED
    assert attempt.endpoint == "CANCELLATION"
    assert attempt.rejection_reasons == ("provider_task_cancelled",)
    assert verify_provider_attempts(cancelled.attempts, binding) == ()

    writer = ImmutableEvidenceWriter(tmp_path, binding.run_id, binding.spec_hash)
    sidecar = write_provider_attempt_trace(
        writer, cancelled.attempts, binding=binding
    )
    assert (tmp_path / sidecar.uri).is_file()
    assert dict(observer.request_counts_by_contract) == {
        binding.request_contract_hash: 1
    }


def test_provider_failure_diagnostics_redact_paths_uris_and_secrets() -> None:
    binding = _binding(
        settings=ProviderSettings(
            timeout_seconds=1.0,
            max_requests=1,
            max_usd=None,
            unknown_cost_policy="allow_unpriced",
        )
    )
    diagnostic = (
        "failed(/private/provider/request.json) "
        "api_key=sk-do-not-record token: bearer-secret "
        "at https://provider.invalid/public/status"
    )
    decision = _observer_for(binding, monotonic=TickClock()).request(
        binding,
        ScriptedFakeProvider([RuntimeError(diagnostic)]),
    )

    rendered = " ".join((*decision.reasons, *decision.attempts[0].rejection_reasons))
    assert "private" not in rendered
    assert "sk-do-not-record" not in rendered
    assert "bearer-secret" not in rendered
    assert "https://provider.invalid/public/status" in rendered
    assert "[REDACTED_PATH]" in rendered
    assert "[REDACTED_SECRET]" in rendered


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
def test_provider_diagnostic_redacts_complete_space_bearing_paths_and_tokens(
    diagnostic: str,
) -> None:
    binding = _binding(
        settings=ProviderSettings(
            timeout_seconds=1.0,
            max_requests=1,
            max_usd=None,
            unknown_cost_policy="allow_unpriced",
        )
    )
    decision = _observer_for(binding, monotonic=TickClock()).request(
        binding,
        ScriptedFakeProvider([RuntimeError(diagnostic)]),
    )

    rendered = " ".join(
        (*decision.reasons, *decision.attempts[0].rejection_reasons)
    )
    assert "[REDACTED_PATH]" in rendered
    assert "[REDACTED_SECRET]" in rendered
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
        assert leaked not in rendered


def test_provider_diagnostic_preserves_https_as_nonlocal_context() -> None:
    binding = _binding(
        settings=ProviderSettings(
            timeout_seconds=1.0,
            max_requests=1,
            max_usd=None,
            unknown_cost_policy="allow_unpriced",
        )
    )
    url = "https://provider.invalid/public/status"
    decision = _observer_for(binding, monotonic=TickClock()).request(
        binding,
        ScriptedFakeProvider([RuntimeError(f"request failed at {url}")]),
    )

    rendered = " ".join(
        (*decision.reasons, *decision.attempts[0].rejection_reasons)
    )
    assert url in rendered
    assert "[REDACTED_PATH]" not in rendered


@pytest.mark.parametrize(
    "response_update,expected_reason",
    [
        ({"actual_provider": "fallback-provider"}, "provider_substitution_detected"),
        ({"returned_model_id": "different-model"}, "returned_model_id_mismatch"),
        ({"returned_model_id": None}, "returned_model_id_mismatch"),
        (
            {"observed_request_contract_hash": "0" * 64},
            "stale_or_unbound_request_contract",
        ),
    ],
)
def test_provider_model_fallback_and_stale_context_binding_fail_before_dispatch(
    response_update: dict,
    expected_reason: str,
) -> None:
    binding = _binding()
    transport = ScriptedFakeProvider([_raw(binding, **response_update)])
    backend_dispatches: list[str] = []
    decision = _observer_for(binding, monotonic=TickClock()).request(binding, transport)
    if decision.accepted:
        backend_dispatches.append("dispatched")

    assert not decision.accepted
    assert decision.endpoint == "PROVIDER_BINDING_MISMATCH"
    assert expected_reason in decision.reasons
    assert backend_dispatches == []
    assert len(decision.attempts) == 1


def test_wrong_decision_or_catalogue_in_raw_proposal_is_policy_output_rejection() -> None:
    binding = _binding()
    for update, expected in (
        ({"decision_id": "decision-stale"}, "proposal_decision_id_mismatch"),
        ({"catalogue_hash": "f" * 64}, "proposal_catalogue_hash_mismatch"),
        ({"catalogue_version": "other"}, "proposal_catalogue_version_mismatch"),
    ):
        raw = json.dumps(_proposal_payload(binding, **update)).encode()
        decision = _observer_for(binding, monotonic=TickClock()).request(
            binding,
            ScriptedFakeProvider(
                [_raw(binding, content_bytes=raw)]
            ),
        )
        assert not decision.accepted
        assert decision.endpoint == "INVALID_POLICY_OUTPUT"
        assert (
            decision.attempts[0].status
            is ProviderAttemptStatus.POLICY_OUTPUT_REJECTED
        )
        assert expected in decision.reasons


def test_missing_usage_and_cost_follow_declared_policy() -> None:
    rejecting = _binding()
    missing = _raw(
        rejecting,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_usd=None,
        cost_source=None,
    )
    rejected = _observer_for(rejecting, monotonic=TickClock()).request(
        rejecting, ScriptedFakeProvider([missing])
    )
    assert not rejected.accepted
    assert rejected.endpoint == "BUDGET_REJECTION"
    assert rejected.attempts[0].status is ProviderAttemptStatus.COST_REJECTED
    assert rejected.attempts[0].cost_source == NOT_PROVIDED

    allowing = _binding(
        settings=ProviderSettings(
            max_tokens=256,
            timeout_seconds=1.0,
            max_requests=1,
            max_usd=None,
            unknown_cost_policy="allow_unpriced",
            unknown_usage_policy="allow_unknown",
        )
    )
    accepted = _observer_for(allowing, monotonic=TickClock()).request(
        allowing,
        ScriptedFakeProvider(
            [
                _raw(
                    allowing,
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    cost_usd=None,
                    cost_source=None,
                )
            ]
        ),
    )
    assert accepted.accepted
    assert accepted.attempts[0].total_tokens is None
    assert accepted.attempts[0].cost_usd is None


def test_claimed_parsed_hash_disagreement_and_response_mutation_are_detected() -> None:
    binding = _binding()
    disagreement = _observer_for(binding, monotonic=TickClock()).request(
        binding,
        ScriptedFakeProvider(
            [_raw(binding, claimed_parsed_proposal_hash="f" * 64)]
        ),
    )
    assert not disagreement.accepted
    assert "raw_parsed_hash_disagreement" in disagreement.reasons

    accepted = _observer_for(binding, monotonic=TickClock()).request(
        binding, ScriptedFakeProvider([_raw(binding)])
    )
    assert isinstance(accepted.bound_proposal, BoundProviderProposal)
    with pytest.raises(ProviderBindingError, match="raw_response_changed_after_parse"):
        accepted.bound_proposal.verify_for_dispatch(
            binding, current_raw_response=b'{"mutated":true}'
        )


def test_provider_trace_is_write_once_exact_and_tampering_is_verifier_visible(
    tmp_path: Path,
) -> None:
    binding = _binding()
    decision = _observer_for(binding, monotonic=TickClock()).request(
        binding, ScriptedFakeProvider([_raw(binding)])
    )
    writer = ImmutableEvidenceWriter(
        tmp_path,
        binding.run_id,
        binding.spec_hash,
    )
    sidecar = write_provider_attempt_trace(
        writer, decision.attempts, binding=binding
    )
    trace_path = tmp_path / sidecar.uri
    assert stat.S_IMODE(trace_path.stat().st_mode) & 0o222 == 0
    row = json.loads(trace_path.read_text().splitlines()[0])
    decoded = base64.b64decode(row["raw_response_bytes_b64"])
    assert hashlib.sha256(decoded).hexdigest() == row["raw_response_hash"]
    assert row["run_id"] == binding.run_id
    assert row["spec_hash"] == binding.spec_hash

    tampered_raw = b"{}"
    tampered = replace(
        decision.attempts[0],
        raw_response_bytes_b64=base64.b64encode(tampered_raw).decode("ascii"),
        raw_response_hash=hashlib.sha256(tampered_raw).hexdigest(),
    )
    errors = verify_provider_attempts((tampered,), binding)
    assert any("accepted raw response no longer parses" in error for error in errors)


@pytest.mark.parametrize(
    "changes",
    (
        {"endpoint": "FAKE_SUCCESS"},
        {"actual_provider": "fallback-provider"},
        {"returned_model_id": "substituted-model"},
        {"observed_request_contract_hash": "0" * 64},
        {"cost_usd": 999.0},
        {"accounted_cost_usd": 999.0},
    ),
)
def test_provider_verifier_rejects_semantic_trace_tampering(changes: dict) -> None:
    binding = _binding()
    decision = _observer_for(binding, monotonic=TickClock()).request(
        binding, ScriptedFakeProvider([_raw(binding)])
    )

    tampered = replace(decision.attempts[0], **changes)

    assert verify_provider_attempts((tampered,), binding)


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        (
            {"observed_request_contract_hash": NOT_PROVIDED},
            "success has no exact response binding",
        ),
        (
            {
                "actual_provider": NOT_PROVIDED,
                "observed_request_contract_hash": NOT_PROVIDED,
            },
            "success has no provider response",
        ),
        ({"cost_usd": -0.01}, "cost domain is invalid"),
        ({"accounted_cost_usd": -0.01}, "accounted cost domain is invalid"),
        ({"parser_version": "forged-parser.v0"}, "parser version mismatch"),
    ),
)
def test_verifier_rejects_forged_impossible_success_records(
    changes: dict, expected: str
) -> None:
    binding = _binding()
    decision = _observer_for(binding, monotonic=TickClock()).request(
        binding, ScriptedFakeProvider([_raw(binding)])
    )
    forged = _forge_attempt(decision.attempts[0], **changes)

    assert any(
        expected in error for error in verify_provider_attempts((forged,), binding)
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"observed_request_contract_hash": NOT_PROVIDED},
        {"cost_usd": -0.01},
        {"accounted_cost_usd": -0.01},
        {"parser_version": "forged-parser.v0"},
    ),
)
def test_attempt_dataclass_rejects_impossible_success_domains(changes: dict) -> None:
    binding = _binding()
    decision = _observer_for(binding, monotonic=TickClock()).request(
        binding, ScriptedFakeProvider([_raw(binding)])
    )

    with pytest.raises(ProviderBindingError):
        replace(decision.attempts[0], **changes)


def test_raw_response_rejects_partial_usage_and_negative_cost() -> None:
    binding = _binding()
    with pytest.raises(ProviderBindingError, match="complete or wholly unknown"):
        _raw(binding, output_tokens=None)
    with pytest.raises(ProviderBindingError, match="non-negative"):
        _raw(binding, cost_usd=-0.01)


def test_trace_writer_refuses_unverified_records(tmp_path: Path) -> None:
    binding = _binding()
    decision = _observer_for(binding, monotonic=TickClock()).request(
        binding, ScriptedFakeProvider([_raw(binding)])
    )
    tampered = replace(decision.attempts[0], endpoint="FAKE_SUCCESS")
    writer = ImmutableEvidenceWriter(tmp_path, binding.run_id, binding.spec_hash)

    with pytest.raises(ProviderBindingError, match="trace verification failed"):
        write_provider_attempt_trace(writer, (tampered,), binding=binding)


def test_fixed_cost_cap_requires_predeclared_request_upper_bound() -> None:
    with pytest.raises(ProviderBindingError, match="per-request upper bound"):
        ProviderSettings(max_usd=1.0, unknown_cost_policy="reject")


def test_unknown_cost_can_use_predeclared_upper_bound_without_fabricating_cost() -> None:
    binding = _binding(
        settings=ProviderSettings(
            max_tokens=256,
            timeout_seconds=1.0,
            max_requests=1,
            max_usd=0.25,
            max_cost_per_request_usd=0.25,
            unknown_cost_policy="allow_unpriced",
        )
    )
    decision = _observer_for(binding, monotonic=TickClock()).request(
        binding,
        ScriptedFakeProvider(
            [
                _raw(
                    binding,
                    cost_usd=None,
                    cost_source=None,
                )
            ]
        ),
    )

    assert decision.accepted
    assert decision.endpoint == "PROVIDER_RESPONSE_BOUND"
    assert decision.attempts[0].status is ProviderAttemptStatus.SUCCEEDED
    assert decision.attempts[0].cost_usd is None
    assert decision.attempts[0].cost_source == NOT_PROVIDED
    assert decision.attempts[0].accounted_cost_usd == pytest.approx(0.25)
    assert (
        decision.attempts[0].global_accounted_cost_usd_after_attempt
        == pytest.approx(0.25)
    )
    assert verify_provider_attempts(decision.attempts, binding) == ()


def test_request_contract_is_content_addressed_and_rejects_malformed_interfaces() -> None:
    first = _binding()
    second = _binding(context_bytes=b'{"legal_actions":[]}')
    assert first.context_hash != second.context_hash
    assert first.request_contract_hash != second.request_contract_hash
    with pytest.raises(ProviderBindingError, match="action_interface_hash"):
        _binding(action_interface_hash="not-a-hash")
