from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
from typing import Any

import pytest

from optimizer_pipeline.dof import e1v2_job
from optimizer_pipeline.dof.artifacts import fingerprint_file
from optimizer_pipeline.dof.e1v2 import E0V2_MATRIX, content_hash
from optimizer_pipeline.dof.e1v2_job import (
    execute_real_e1v2_job,
    run_bound_provider_decision_v2,
)
from optimizer_pipeline.dof.openrouter import (
    OPENROUTER_ADAPTER_VERSION,
    OPENROUTER_BASE_URL,
    OPENROUTER_E1_MODELS,
    OPENROUTER_E1_PROFILES,
    OpenRouterChatCompletionsTransport,
    verify_openrouter_unknown_cost_reserve,
)
from optimizer_pipeline.dof.provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    ProviderTimeout,
    ProviderTransportError,
    RawProviderResponse,
)


def _spec(root: Path | None = None) -> dict[str, Any]:
    observed = None if root is None else fingerprint_file(root)
    artifact = (
        {"sha256": "b" * 64, "size_bytes": 1}
        if root is None
        else {"sha256": observed.sha256, "size_bytes": observed.size_bytes}
    )
    return {
        "protocol_version": "dof.protocol-e1.v2.4",
        "matrix": E0V2_MATRIX,
        "study_status": "E0V2_4_NON_CLAIM",
        "job": {
            "job_id": "wp009-terminal-fault",
            "benchmark": "synthetic",
            "level": "L2",
            "model": "openai/gpt-5.6-luna",
            "repeat": 0,
        },
        "root": artifact
        | {
            "score": {
                "wns_ns": 0.0,
                "tns_ns": 0.0,
                "fmax_mhz": 500.0,
            }
        },
        "budgets": {
            "heavy_calls": 4,
            "model_requests": 4,
            "trajectory_wall_seconds": 3700,
            "provider_cost_usd": 0.40,
        },
        "job_spec_sha256": "a" * 64,
    }


def _observation(spec: dict[str, Any]) -> dict[str, Any]:
    core = {
        "schema_version": "dof.e1v2-real-observation.v1",
        "origin": "real_read_only_vivado",
        "job_spec_sha256": spec["job_spec_sha256"],
        "current_artifact": {
            "sha256": spec["root"]["sha256"],
            "size_bytes": spec["root"]["size_bytes"],
        },
        "source": {
            "condition": {"pseudonym": "condition-terminal"},
            "state": {
                "pseudonym": "state-terminal",
                "lineage_pseudonym": "lineage-terminal",
                "stage": "ROUTED",
                "measurements": {
                    "timing": {
                        "wns_ns": 0.0,
                        "tns_ns": 0.0,
                        "failing_endpoints": 0,
                        "clock_period_ns": 2.0,
                        "estimated_fmax_mhz": 500.0,
                    },
                    "route": {"status": "fully routed", "unrouted_nets": 0},
                },
            },
            "history": [],
            "budget_remaining": {
                "IMPLEMENTATION_HEAVY": 4,
                "MUTATION_LIGHT": 8,
                "READ_ONLY": 8,
                "LLM_REQUEST": 4,
                "WALL_TIME_SECONDS": 3700.0,
            },
            "validation": {"status": "CLEAN", "errors": [], "warnings": []},
            "stop_conditions": ["budget exhausted"],
            "known_priors": {},
        },
    }
    return core | {"observation_sha256": content_hash(core)}


class SentThenFailedTransport:
    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True
    failure_class = "TRANSPORT_ERROR"
    exception_type = ProviderTransportError
    provider_error_category = None

    def __init__(self, *, expected_outbound_binding, **_kwargs):
        self.expected = expected_outbound_binding
        self.physical_requests = 0
        self.send_attempts = 0
        self.cleanup_status = "NOT_ATTEMPTED"
        self.last_exchange = None
        self.accounting_lookup_requests = 0
        self.reconciled_accounting = None

    async def send(self, binding, _retry_index):
        self.send_attempts = 1
        self.physical_requests = 1
        self.cleanup_status = "SUCCEEDED"
        self.last_exchange = {
            "schema_version": "dof.openrouter-transport-terminal.v1",
            "attempt": 1,
            "outbound_binding": self.expected.to_dict(),
            "send_attempted": True,
            "physical_requests": 1,
            "transport_cleanup_status": "SUCCEEDED",
            "response_headers_observed": False,
            "response_body_observed": False,
            "response_headers": {},
            "generation_id": None,
            "route": None,
            "usage": None,
            "failure_class": self.failure_class,
            "accounting_lookup_requests": 0,
            "accounting_reconciled": False,
            "requested_model": binding.model_id,
        }
        if self.provider_error_category is not None:
            self.last_exchange["failure_class"] = "HTTP_ERROR"
            self.last_exchange["provider_error"] = {
                "schema_version": "dof.openrouter-provider-error.v1",
                "classifier_version": "dof.openrouter-error-classifier.v1",
                "category": self.provider_error_category,
                "http_status": 403,
                "valid_error_envelope": True,
                "error_code": 403,
                "error_type": "synthetic",
                "message_sha256": "a" * 64,
                "message_size_bytes": 12,
                "safe_identifiers": {},
            }
        raise self.exception_type("deterministic_sent_without_response")


class SentThenTimedOutTransport(SentThenFailedTransport):
    failure_class = "TRANSPORT_TIMEOUT"
    exception_type = ProviderTimeout


class PaymentBlockedTransport(SentThenFailedTransport):
    provider_error_category = "PAYMENT_OR_BUDGET"


class GuardrailBlockedTransport(SentThenFailedTransport):
    provider_error_category = "GUARDRAIL_OR_PROMPT_BLOCK"


class InvalidPolicyOutputTransport(SentThenFailedTransport):
    async def send(self, binding, _retry_index):
        self.send_attempts = 1
        self.physical_requests = 1
        self.cleanup_status = "SUCCEEDED"
        self.last_exchange = {
            "schema_version": "dof.openrouter-exchange.v1",
            "route": {
                "attempt": 1,
                "requested_model": binding.model_id,
                "returned_model": binding.model_id,
                "selected_upstream_provider": "fake-pinned-endpoint",
                "fallback_allowed": False,
            },
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "cost": 0.01,
            },
        }
        return RawProviderResponse(
            b"```json\n{}\n```",
            "openrouter",
            provider_request_id="fake-request",
            returned_model_id=binding.model_id,
            returned_model_revision="fake-pinned-endpoint",
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
            cost_usd=0.01,
            cost_source="openrouter_usage.cost",
            observed_request_contract_hash=binding.request_contract_hash,
        )


class ProposalBindingMismatchTransport(InvalidPolicyOutputTransport):
    mismatch_field = "decision_id"

    async def send(self, binding, _retry_index):
        payload = {
            "schema_version": "action_proposal.v1",
            "proposal_id": "proposal-binding-mismatch",
            "decision_id": binding.decision_id,
            "catalogue_version": binding.catalogue_version,
            "catalogue_hash": binding.catalogue_hash,
            "kind": "ACTION",
            "steps": [
                {
                    "step_id": "step-001",
                    "action_key": "macro.place_explore",
                    "parameters": {},
                }
            ],
        }
        payload[self.mismatch_field] = {
            "decision_id": binding.decision_id + "x",
            "catalogue_version": binding.catalogue_version + ".wrong",
            "catalogue_hash": "f" * 64,
        }[self.mismatch_field]
        response = await super().send(binding, _retry_index)
        return RawProviderResponse(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            response.actual_provider,
            provider_request_id=response.provider_request_id,
            returned_model_id=response.returned_model_id,
            returned_model_revision=response.returned_model_revision,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.total_tokens,
            cost_usd=response.cost_usd,
            cost_source=response.cost_source,
            observed_request_contract_hash=response.observed_request_contract_hash,
        )


def test_invalid_policy_output_is_a_canonical_no_dispatch_terminal(
    tmp_path: Path,
) -> None:
    spec = _spec()
    bound = run_bound_provider_decision_v2(
        spec,
        _observation(spec),
        tmp_path / "provider",
        decision_index=0,
        heavy_remaining=4,
        remaining_cost_usd=0.40,
        credential="fake-key",
        transport_factory=InvalidPolicyOutputTransport,
    )

    assert bound.result["endpoint"] == "INVALID_POLICY_OUTPUT"
    assert bound.result["policy_output_subtype"] is None
    assert bound.result["provider_accepted"] is False
    assert bound.result["provider_transport_accounting_accepted"] is True
    assert bound.result["proposal_dispatchable"] is False
    assert bound.result["proposal_created"] is False
    assert bound.result["action_gate_entered"] is False
    assert bound.result["vivado_dispatches_after_decision"] == 0
    assert bound.result["actual_cost_usd"] == pytest.approx(0.01)
    assert bound.result["reserved_unknown_cost_usd"] == 0
    assert (tmp_path / "provider/evidence/provider-decision.json").is_file()


@pytest.mark.parametrize(
    "field,reason",
    [
        ("decision_id", "proposal_decision_id_mismatch"),
        ("catalogue_version", "proposal_catalogue_version_mismatch"),
        ("catalogue_hash", "proposal_catalogue_hash_mismatch"),
    ],
)
def test_proposal_binding_mismatch_is_canonical_policy_output_terminal(
    tmp_path: Path, field: str, reason: str
) -> None:
    transport = type(
        "InjectedProposalBindingMismatchTransport",
        (ProposalBindingMismatchTransport,),
        {"mismatch_field": field},
    )
    spec = _spec()
    bound = run_bound_provider_decision_v2(
        spec,
        _observation(spec),
        tmp_path / field,
        decision_index=0,
        heavy_remaining=4,
        remaining_cost_usd=0.40,
        credential="fake-key",
        transport_factory=transport,
    )

    assert bound.result["endpoint"] == "INVALID_POLICY_OUTPUT"
    assert bound.result["policy_output_subtype"] == "PROPOSAL_BINDING_MISMATCH"
    assert bound.result["reasons"] == [reason]
    assert bound.result["provider_transport_accounting_accepted"] is True
    assert bound.result["proposal_dispatchable"] is False
    assert bound.result["proposal_created"] is False
    assert bound.result["action_gate_entered"] is False
    assert bound.result["vivado_dispatches_after_decision"] == 0
    assert bound.result["physical_requests"] == 1
    assert bound.result["retries"] == 0
    assert bound.result["fallbacks"] == 0
    assert bound.result["repairs"] == 0


def test_sent_without_response_becomes_canonical_typed_terminal(tmp_path: Path) -> None:
    spec = _spec()
    bound = run_bound_provider_decision_v2(
        spec,
        _observation(spec),
        tmp_path / "provider",
        decision_index=0,
        heavy_remaining=4,
        remaining_cost_usd=0.40,
        credential="fake-key",
        transport_factory=SentThenFailedTransport,
    )

    assert bound.result["endpoint"] == "PROVIDER_TRANSPORT_FAILURE"
    assert bound.decision.attempts[0].status.value == "PROVIDER_ERROR"
    assert bound.result["actual_cost_usd"] is None
    assert bound.result["reserved_unknown_cost_usd"] == pytest.approx(0.10)
    assert bound.result["cost_lower_bound_usd"] == 0
    assert bound.result["cost_upper_bound_usd"] == pytest.approx(0.10)
    assert bound.result["proposal_created"] is False
    assert bound.result["action_gate_entered"] is False
    assert bound.result["vivado_dispatches_after_decision"] == 0
    trace = tmp_path / "provider/evidence/provider_attempts.jsonl"
    row = json.loads(trace.read_text())
    assert row["status"] == "PROVIDER_ERROR"
    assert row["actual_cost_usd"] is None
    assert row["reserved_unknown_cost_usd"] == pytest.approx(0.10)
    assert stat.S_IMODE(trace.stat().st_mode) & 0o222 == 0
    assert (tmp_path / "provider/evidence/provider-decision.json").is_file()


def test_timeout_primary_status_is_not_overwritten_by_cost_rejection(
    tmp_path: Path,
) -> None:
    spec = _spec()
    bound = run_bound_provider_decision_v2(
        spec,
        _observation(spec),
        tmp_path / "provider",
        decision_index=0,
        heavy_remaining=4,
        remaining_cost_usd=0.40,
        credential="fake-key",
        transport_factory=SentThenTimedOutTransport,
    )
    assert bound.result["endpoint"] == "PROVIDER_TRANSPORT_FAILURE"
    assert bound.decision.attempts[0].status.value == "TIMED_OUT"
    assert bound.decision.attempts[0].endpoint == "TIMEOUT"


def test_guardrail_block_is_canonical_job_scoped_provider_rejection(
    tmp_path: Path,
) -> None:
    spec = _spec()
    bound = run_bound_provider_decision_v2(
        spec,
        _observation(spec),
        tmp_path / "guardrail",
        decision_index=0,
        heavy_remaining=4,
        remaining_cost_usd=0.40,
        credential="fake-key",
        transport_factory=GuardrailBlockedTransport,
    )
    assert bound.result["endpoint"] == "PROVIDER_REJECTION"
    assert bound.result["provider_error_category"] == "GUARDRAIL_OR_PROMPT_BLOCK"
    assert bound.result["provider_global_breaker"] is False
    assert bound.result["vivado_dispatches_after_decision"] == 0


def test_payment_block_sets_immediate_global_breaker_evidence(
    tmp_path: Path,
) -> None:
    spec = _spec()
    bound = run_bound_provider_decision_v2(
        spec,
        _observation(spec),
        tmp_path / "payment",
        decision_index=0,
        heavy_remaining=4,
        remaining_cost_usd=0.40,
        credential="fake-key",
        transport_factory=PaymentBlockedTransport,
    )
    assert bound.result["endpoint"] == "PROVIDER_TRANSPORT_FAILURE"
    assert bound.result["provider_error_category"] == "PAYMENT_OR_BUDGET"
    assert bound.result["provider_global_breaker"] is True


def test_fault_injected_job_writes_hash_valid_trajectory_with_zero_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"synthetic-root")
    spec = _spec(root)
    monkeypatch.setattr(
        e1v2_job,
        "capture_real_observation",
        lambda *_args, **_kwargs: _observation(spec),
    )
    output = tmp_path / "trajectory"
    result = execute_real_e1v2_job(
        spec,
        root,
        output,
        vivado_exec=tmp_path / "unused-vivado",
        python_exec=tmp_path / "unused-python",
        repository_anchor=tmp_path,
        scorer_script=tmp_path / "unused-scorer",
        fake_startup_attestation_sha256="f" * 64,
        credential="fake-key",
        transport_factory=SentThenFailedTransport,
    )
    assert result["endpoint"] == "PROVIDER_TRANSPORT_FAILURE"
    assert result["model_requests"] == 1
    assert result["heavy_calls"] == 0
    assert result["light_mutation_calls"] == 0
    assert result["design_mutated"] is False
    assert result["cost_upper_bound_usd"] == pytest.approx(0.10)
    core = {key: value for key, value in result.items() if key != "result_sha256"}
    assert result["result_sha256"] == content_hash(core)
    assert json.loads((output / "trajectory-result.json").read_text()) == result
    assert not (output / "decision-00/dispatch").exists()


def test_proposal_binding_mismatch_writes_canonical_job_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root.dcp"
    root.write_bytes(b"synthetic-root")
    spec = _spec(root)
    monkeypatch.setattr(
        e1v2_job,
        "capture_real_observation",
        lambda *_args, **_kwargs: _observation(spec),
    )
    output = tmp_path / "trajectory"
    result = execute_real_e1v2_job(
        spec,
        root,
        output,
        vivado_exec=tmp_path / "unused-vivado",
        python_exec=tmp_path / "unused-python",
        repository_anchor=tmp_path,
        scorer_script=tmp_path / "unused-scorer",
        fake_startup_attestation_sha256="f" * 64,
        credential="fake-key",
        transport_factory=ProposalBindingMismatchTransport,
    )
    assert result["endpoint"] == "INVALID_POLICY_OUTPUT"
    assert result["policy_output_subtype"] == "PROPOSAL_BINDING_MISMATCH"
    assert result["reasons"] == ["proposal_decision_id_mismatch"]
    assert result["model_requests"] == 1
    assert result["heavy_calls"] == 0
    assert result["light_mutation_calls"] == 0
    assert result["design_mutated"] is False
    assert not (output / "decision-00/dispatch").exists()


class _Response:
    def __init__(self, content: bytes, *, headers: dict[str, str] | None = None):
        self.content = content
        self.status_code = 200
        self.headers = headers or {}


class _GenerationClient:
    instances: list["_GenerationClient"] = []
    fail_lookup = False

    def __init__(self, **kwargs):
        self.options = kwargs
        self.closed = False
        type(self).instances.append(self)

    async def post(self, _path, **_kwargs):
        for hook in self.options.get("event_hooks", {}).get("request", []):
            await hook(object())
        return _Response(
            b"{",
            headers={
                "x-openrouter-generation-id": "generation-accounting-001",
                "authorization": "must-not-survive",
            },
        )

    async def get(self, _path, **_kwargs):
        if type(self).fail_lookup:
            raise ProviderTransportError("generation_lookup_unavailable")
        payload = {
            "data": {
                "id": "generation-accounting-001",
                "model": "openai/gpt-5.6-luna",
                "provider_name": "OpenAI",
                "tokens_prompt": 100,
                "tokens_completion": 20,
                "total_cost": 0.0123,
            }
        }
        return _Response(json.dumps(payload).encode())

    async def aclose(self):
        self.closed = True


def test_generation_lookup_recovers_accounting_but_never_action(
    tmp_path: Path,
) -> None:
    _GenerationClient.instances.clear()
    _GenerationClient.fail_lookup = False

    def factory(**kwargs):
        return OpenRouterChatCompletionsTransport(
            **kwargs, client_factory=_GenerationClient
        )

    spec = _spec()
    bound = run_bound_provider_decision_v2(
        spec,
        _observation(spec),
        tmp_path / "provider",
        decision_index=0,
        heavy_remaining=4,
        remaining_cost_usd=0.40,
        credential="fake-key",
        transport_factory=factory,
    )
    assert bound.result["endpoint"] == "PROVIDER_TRANSPORT_FAILURE"
    assert bound.result["actual_cost_usd"] == pytest.approx(0.0123)
    assert bound.result["reserved_unknown_cost_usd"] == 0
    assert bound.result["known_token_total"] == 120
    assert bound.result["proposal_created"] is False
    exchange = json.loads(
        (tmp_path / "provider/evidence/openrouter-exchange.json").read_text()
    )
    assert exchange["accounting_lookup_requests"] == 1
    assert exchange["accounting_reconciled"] is True
    assert exchange["accounting_lookup"]["status"] == "SUCCEEDED"
    assert "authorization" not in json.dumps(exchange).lower()
    assert len(_GenerationClient.instances) == 2


def test_failed_generation_lookup_retains_unknown_actual_and_reserve(
    tmp_path: Path,
) -> None:
    _GenerationClient.instances.clear()
    _GenerationClient.fail_lookup = True

    def factory(**kwargs):
        return OpenRouterChatCompletionsTransport(
            **kwargs, client_factory=_GenerationClient
        )

    spec = _spec()
    try:
        bound = run_bound_provider_decision_v2(
            spec,
            _observation(spec),
            tmp_path / "provider",
            decision_index=0,
            heavy_remaining=4,
            remaining_cost_usd=0.40,
            credential="fake-key",
            transport_factory=factory,
        )
    finally:
        _GenerationClient.fail_lookup = False
    assert bound.result["endpoint"] == "PROVIDER_TRANSPORT_FAILURE"
    assert bound.result["actual_cost_usd"] is None
    assert bound.result["reserved_unknown_cost_usd"] == pytest.approx(0.10)
    exchange = json.loads(
        (tmp_path / "provider/evidence/openrouter-exchange.json").read_text()
    )
    assert exchange["accounting_lookup_requests"] == 1
    assert exchange["accounting_lookup"]["status"] == "FAILED"


class RouteDriftTransport(SentThenFailedTransport):
    failure_class = "MODEL_OR_ROUTE_DRIFT"


def test_route_drift_remains_an_integrity_blocker(tmp_path: Path) -> None:
    spec = _spec()
    with pytest.raises(RuntimeError, match="model_or_route_drift"):
        run_bound_provider_decision_v2(
            spec,
            _observation(spec),
            tmp_path / "provider",
            decision_index=0,
            heavy_remaining=4,
            remaining_cost_usd=0.40,
            credential="fake-key",
            transport_factory=RouteDriftTransport,
        )
    assert not (tmp_path / "provider/evidence/provider-decision.json").exists()


def test_frozen_reserve_verification_uses_exact_pricing_and_byte_bounds() -> None:
    discovery = {
        "retrieved_at": "2026-08-09T12:00:00+00:00",
        "models": {
            model: {
                "endpoint": OPENROUTER_BASE_URL + "/models/" + model + "/endpoints",
                "response_sha256": hashlib.sha256(model.encode()).hexdigest(),
                "pricing": {
                    "prompt": "0.000003",
                    "completion": "0.000010",
                    "request": "0",
                },
            }
            for model in OPENROUTER_E1_MODELS
        },
    }
    accepted = verify_openrouter_unknown_cost_reserve(discovery)
    assert accepted["all_models_accepted"] is True
    assert all(
        row["calculated_upper_bound_usd"] <= 0.10
        for row in accepted["models"].values()
    )
    discovery["models"][OPENROUTER_E1_MODELS[0]]["pricing"]["completion"] = "1"
    rejected = verify_openrouter_unknown_cost_reserve(discovery)
    assert rejected["all_models_accepted"] is False
