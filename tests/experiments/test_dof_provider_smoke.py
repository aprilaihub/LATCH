from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys

import pytest

from optimizer_pipeline.dof import provider_smoke
from optimizer_pipeline.dof.provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    RawProviderResponse,
    ProviderTaskCancelled,
)
from optimizer_pipeline.dof.provider_smoke import (
    PROVIDER_SMOKE_DECISION_ID,
    PROVIDER_SMOKE_PLAN_SCHEMA,
    PROVIDER_SMOKE_RUN_ID,
    ProviderSmokePlanError,
    execute_provider_smoke,
    freeze_provider_smoke_plan,
    validate_provider_smoke_plan,
)
from optimizer_pipeline.dof.provider_transport import (
    OPENAI_SDK_AMBIENT_CONFIGURATION_KEYS,
    OpenAIChatCompletionsTransport,
)
from optimizer_pipeline.dof.provider_request_capabilities import (
    CanonicalOutboundRequestBinding,
    ProviderModelRequestCapability,
    ProviderRequestCapabilityError,
    build_canonical_outbound_request,
    provider_sdk_distribution_manifest,
    verify_canonical_outbound_binding,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeHttpClient:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def emit_request(self):
        for hook in self.options.get("event_hooks", {}).get("request", ()):
            await hook(object())


def test_provider_runner_requires_isolated_python_mode() -> None:
    script = REPO_ROOT / "scripts" / "run_wp004_provider_smoke.py"
    rejected = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    isolated_with_site = subprocess.run(
        [sys.executable, "-I", str(script), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    accepted = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "requires_python_isolated_no_site_mode" in rejected.stderr
    assert isolated_with_site.returncode != 0
    assert "requires_python_isolated_no_site_mode" in isolated_with_site.stderr
    assert accepted.returncode == 0, accepted.stderr


@dataclass(frozen=True)
class _Repository:
    head_commit: str = "a" * 40
    head_tree: str = "b" * 40
    binary_diff_sha256: str = "c" * 64
    tracked_tree_dirty: bool = False
    branch_name: str = "phase0-policy-evidence-replan"


class _Transport:
    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True
    requires_canonical_outbound_binding = True
    physical_requests = 0

    def __init__(self, **kwargs):
        self.physical_requests = 0
        self.profile = kwargs["capability_profile"]
        self.base_url = kwargs["base_url"]
        self.sdk_distribution = kwargs["sdk_distribution_manifest"]
        self.expected_outbound = kwargs["expected_outbound_binding"]
        self.prepared_outbound_binding = None

    async def send(self, binding, retry_index):
        assert retry_index == 0
        prepared = build_canonical_outbound_request(
            binding,
            self.profile,
            base_url=self.base_url,
            sdk_distribution_manifest=self.sdk_distribution,
        )
        verify_canonical_outbound_binding(
            prepared.binding,
            self.expected_outbound,
        )
        self.prepared_outbound_binding = prepared.binding
        self.physical_requests += 1
        payload = {
            "schema_version": "action_proposal.v1",
            "proposal_id": "wp004a-provider-proposal-001",
            "decision_id": binding.decision_id,
            "catalogue_version": binding.catalogue_version,
            "catalogue_hash": binding.catalogue_hash,
            "kind": "ACTION",
            "steps": [
                {
                    "step_id": "step-001",
                    "action_key": "macro.phys_opt",
                    "parameters": {},
                }
            ],
        }
        return RawProviderResponse(
            json.dumps(payload, separators=(",", ":")).encode(),
            binding.provider,
            provider_request_id="request-001",
            returned_model_id=binding.model_id,
            input_tokens=50,
            output_tokens=40,
            total_tokens=90,
            cost_usd=0.01,
            cost_source="fake-provider-reported",
            observed_request_contract_hash=binding.request_contract_hash,
        )


def _plan(monkeypatch):
    monkeypatch.setattr(provider_smoke, "observe_repository", lambda _path: _Repository())
    return freeze_provider_smoke_plan(REPO_ROOT)


@pytest.fixture(autouse=True)
def _clear_openai_sdk_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in OPENAI_SDK_AMBIENT_CONFIGURATION_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_provider_plan_is_synthetic_exact_and_zero_dispatch(monkeypatch) -> None:
    plan = _plan(monkeypatch)
    assert validate_provider_smoke_plan(plan) == plan
    context = json.loads(__import__("base64").b64decode(plan["context_bytes_b64"]))
    assert context["observation"]["source"] == "synthetic"
    assert context["observation"]["real_benchmark_data"] is False
    assert [item["action_key"] for item in context["legal_actions"]] == [
        "macro.phys_opt",
        "macro.place_route",
        "macro.retime",
    ]
    assert plan["limits"]["transport_requests"] == 1
    assert plan["limits"]["backend_dispatches"] == 0
    profile = ProviderModelRequestCapability.from_mapping(
        plan["request_capability_profile"]["profile"]
    )
    outbound = CanonicalOutboundRequestBinding.from_mapping(
        plan["canonical_outbound_binding"]
    )
    assert profile.model_id == "gpt-5.4-mini"
    assert profile.token_limit_parameter == "max_completion_tokens"
    assert profile.temperature_policy == "omit"
    assert profile.seed_policy == "omit"
    assert profile.sha256 == plan["request_capability_profile"]["profile_sha256"]
    assert outbound.capability_profile_hash == profile.sha256
    binding = provider_smoke._binding_from_evidence(plan["request_binding"])
    assert plan["limits"]["settings"] == binding.settings.to_dict()
    assert binding.settings.max_requests == 1
    assert binding.settings.max_retries == 0
    assert binding.settings.retry_on == ()
    assert binding.settings.max_usd == pytest.approx(0.25)
    assert binding.settings.max_cost_per_request_usd == pytest.approx(0.25)
    assert binding.settings.unknown_cost_policy == "allow_unpriced"
    assert binding.settings.unknown_usage_policy == "reject"
    assert plan["sdk_distribution"] == provider_sdk_distribution_manifest()
    rebuilt_outbound = build_canonical_outbound_request(
        binding,
        profile,
        base_url=plan["provider_config"]["base_url"],
        sdk_distribution_manifest=plan["sdk_distribution"],
    ).binding
    assert outbound.prompt_sha256 == plan["prompt_sha256"]
    assert outbound.context_sha256 == plan["context_sha256"]
    assert outbound.messages_sha256 == rebuilt_outbound.messages_sha256
    assert outbound.outbound_top_level_keys == (
        "max_completion_tokens",
        "messages",
        "model",
        "response_format",
    )
    assert plan["schema_version"] == "dof.wp004a-provider-smoke-plan.v1"
    assert PROVIDER_SMOKE_PLAN_SCHEMA == "dof.wp004a-provider-smoke-plan.v1"
    assert PROVIDER_SMOKE_RUN_ID == "wp004a-provider-technical-smoke"
    assert PROVIDER_SMOKE_DECISION_ID == "wp004a-provider-decision-001"


def test_old_wp004_provider_plan_schema_is_not_reusable(monkeypatch) -> None:
    plan = _plan(monkeypatch)
    plan["schema_version"] = "dof.wp004-provider-smoke-plan.v1"

    with pytest.raises(ProviderSmokePlanError, match="schema_or_hash_mismatch"):
        validate_provider_smoke_plan(plan)


def test_rehashed_plan_cannot_change_frozen_cost_policy(monkeypatch) -> None:
    plan = _plan(monkeypatch)
    plan["request_binding"]["settings"]["unknown_cost_policy"] = "reject"
    plan["limits"]["settings"]["unknown_cost_policy"] = "reject"
    payload = dict(plan)
    payload.pop("plan_sha256")
    plan["plan_sha256"] = provider_smoke._hash(payload)

    with pytest.raises(ProviderSmokePlanError, match="provider_plan_limits_mismatch"):
        validate_provider_smoke_plan(plan)


def test_rehashed_plan_cannot_redirect_credential_or_endpoint(monkeypatch) -> None:
    plan = _plan(monkeypatch)
    plan["provider_config"]["base_url"] = "https://attacker.invalid/v1"
    plan["provider_config"]["credential_env_name"] = "HOME"
    payload = dict(plan)
    payload.pop("plan_sha256")
    plan["plan_sha256"] = provider_smoke._hash(payload)

    with pytest.raises(
        ProviderSmokePlanError,
        match="provider_plan_provider_binding_mismatch",
    ):
        validate_provider_smoke_plan(plan)


def test_rehashed_plan_cannot_change_request_settings_or_add_ignored_fields(
    monkeypatch,
) -> None:
    plan = _plan(monkeypatch)
    plan["limits"]["settings"]["max_tokens"] = 100000
    plan["request_binding"]["settings"]["max_tokens"] = 100000
    plan["limits"]["ignored_private_path"] = "/must/not/persist"
    payload = dict(plan)
    payload.pop("plan_sha256")
    plan["plan_sha256"] = provider_smoke._hash(payload)
    with pytest.raises(
        ProviderSmokePlanError, match="provider_plan_limits_mismatch"
    ):
        validate_provider_smoke_plan(plan)

    plan = _plan(monkeypatch)
    plan["request_capability_profile"]["ignored_private_path"] = (
        "/must/not/persist"
    )
    payload = dict(plan)
    payload.pop("plan_sha256")
    plan["plan_sha256"] = provider_smoke._hash(payload)
    with pytest.raises(
        ProviderSmokePlanError,
        match="provider_request_capability_profile_missing",
    ):
        validate_provider_smoke_plan(plan)


def test_rehashed_plan_cannot_change_provider_role(monkeypatch) -> None:
    plan = _plan(monkeypatch)
    plan["request_binding"]["provider_role"] = "unbound_role"
    binding = provider_smoke._binding_from_evidence(plan["request_binding"])
    plan["request_contract_hash"] = binding.request_contract_hash
    profile = ProviderModelRequestCapability.from_mapping(
        plan["request_capability_profile"]["profile"]
    )
    plan["canonical_outbound_binding"] = build_canonical_outbound_request(
        binding,
        profile,
        base_url=plan["provider_config"]["base_url"],
        sdk_distribution_manifest=plan["sdk_distribution"],
    ).binding.to_dict()
    payload = dict(plan)
    payload.pop("plan_sha256")
    plan["plan_sha256"] = provider_smoke._hash(payload)
    with pytest.raises(
        ProviderSmokePlanError,
        match="provider_plan_spec_or_request_binding_mismatch",
    ):
        validate_provider_smoke_plan(plan)


def test_runtime_profile_file_hash_mismatch_rejects_before_transport_factory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = _plan(monkeypatch)
    invalid_profile = tmp_path / "runtime-profile.json"
    invalid_profile.write_text("{}\n", encoding="utf-8")
    original_input_path = provider_smoke._repository_input_path

    def input_path(root, uri, label):
        if label == "capability_profile":
            return invalid_profile
        return original_input_path(root, uri, label)

    monkeypatch.setattr(provider_smoke, "_repository_input_path", input_path)
    factory_calls = 0

    def transport_factory(**kwargs):
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("transport factory must not be entered")

    with pytest.raises(
        ProviderSmokePlanError,
        match="provider_runtime_capability_file_mismatch",
    ):
        execute_provider_smoke(
            plan,
            tmp_path / "must-not-exist",
            credential="test-only",
            transport_factory=transport_factory,
        )

    assert factory_calls == 0
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize(
    "ambient_key,ambient_value",
    [
        ("OPENAI_BASE_URL", "https://attacker.invalid/v1"),
        ("SSLKEYLOGFILE", "must-not-write-tls-keys"),
        ("OPENAI_LOG", "debug"),
    ],
)
def test_sdk_ambient_configuration_rejects_before_output_or_transport(
    tmp_path: Path,
    monkeypatch,
    ambient_key,
    ambient_value,
) -> None:
    plan = _plan(monkeypatch)
    monkeypatch.setenv(ambient_key, ambient_value)
    factory_calls = 0

    def transport_factory(**kwargs):
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("transport factory must not be entered")

    with pytest.raises(
        ProviderRequestCapabilityError,
        match="openai_sdk_ambient_configuration_forbidden",
    ):
        execute_provider_smoke(
            plan,
            tmp_path / "must-not-exist-ambient",
            credential="test-only",
            transport_factory=transport_factory,
        )

    assert factory_calls == 0
    assert not (tmp_path / "must-not-exist-ambient").exists()


def test_unavailable_credential_consumes_zero_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result = execute_provider_smoke(
        _plan(monkeypatch),
        tmp_path / "unavailable",
        credential="",
        transport_factory=_Transport,
    )
    assert result["endpoint"] == "PROVIDER_UNAVAILABLE"
    assert result["transport_requests"] == 0
    assert result["physical_transport_requests"] == 0
    assert result["backend_dispatches"] == 0


def test_transport_without_canonical_contract_is_rejected_before_construction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class UnboundTransport:
        construction_count = 0

        def __init__(self, **_kwargs):
            type(self).construction_count += 1

    with pytest.raises(
        ProviderSmokePlanError,
        match="provider_transport_canonical_binding_required",
    ):
        execute_provider_smoke(
            _plan(monkeypatch),
            tmp_path / "must-not-exist-unbound",
            credential="test-only",
            transport_factory=UnboundTransport,
        )
    assert UnboundTransport.construction_count == 0
    assert not (tmp_path / "must-not-exist-unbound").exists()


def test_pre_physical_client_failure_retains_one_attempt_and_zero_http_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class PrePhysicalFailureTransport(_Transport):
        async def send(self, binding, retry_index):
            assert retry_index == 0
            prepared = build_canonical_outbound_request(
                binding,
                self.profile,
                base_url=self.base_url,
                sdk_distribution_manifest=self.sdk_distribution,
            )
            verify_canonical_outbound_binding(
                prepared.binding, self.expected_outbound
            )
            self.prepared_outbound_binding = prepared.binding
            raise RuntimeError("injected client construction failure")

    result = execute_provider_smoke(
        _plan(monkeypatch),
        tmp_path / "pre-physical-failure",
        credential="test-only",
        transport_factory=PrePhysicalFailureTransport,
    )
    assert result["endpoint"] == "PROVIDER_ERROR"
    assert result["transport_requests"] == 1
    assert result["physical_transport_requests"] == 0
    assert result["attempt_status"] == "PROVIDER_ERROR"
    assert result["accounted_cost_usd"] == pytest.approx(0.25)
    assert result["backend_dispatches"] == 0


def test_one_provider_request_is_parsed_gated_and_never_dispatched(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = _plan(monkeypatch)
    result = execute_provider_smoke(
        plan,
        tmp_path / "one-request",
        credential="test-only",
        transport_factory=_Transport,
    )
    assert result["endpoint"] == "PROVIDER_RESPONSE_BOUND"
    assert result["transport_requests"] == 1
    assert result["physical_transport_requests"] == 1
    assert result["retries"] == 0
    assert result["fallbacks"] == 0
    assert result["backend_dispatches"] == 0
    assert result["parser_result"]["status"] == "PARSED"
    assert result["gate_result"]["accepted"] is True
    assert result["reported_cost_usd"] == 0.01
    assert result["secrets_persisted"] is False
    outbound = plan["canonical_outbound_binding"]
    assert result["prompt_sha256"] == outbound["prompt_sha256"]
    assert result["context_sha256"] == outbound["context_sha256"]
    assert result["messages_sha256"] == outbound["messages_sha256"]
    assert result["outbound_request_kwargs_hash"] == outbound[
        "outbound_request_kwargs_hash"
    ]


def test_task_cancellation_persists_attempt_and_terminal_result_before_propagation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class CancelledTransport(_Transport):
        async def send(self, binding, retry_index):
            assert retry_index == 0
            prepared = build_canonical_outbound_request(
                binding,
                self.profile,
                base_url=self.base_url,
                sdk_distribution_manifest=self.sdk_distribution,
            )
            verify_canonical_outbound_binding(
                prepared.binding,
                self.expected_outbound,
            )
            self.prepared_outbound_binding = prepared.binding
            self.physical_requests += 1
            raise asyncio.CancelledError()

    plan = _plan(monkeypatch)
    output = tmp_path / "cancelled-request"
    with pytest.raises(ProviderTaskCancelled):
        execute_provider_smoke(
            plan,
            output,
            credential="test-only",
            transport_factory=CancelledTransport,
        )

    trace = (output / "evidence" / "provider_attempts.jsonl").read_text()
    result = json.loads(
        (output / "evidence" / "provider-result.json").read_text()
    )
    assert '"endpoint":"CANCELLATION"' in trace
    assert result["endpoint"] == "CANCELLATION"
    assert result["physical_transport_requests"] == 1


def test_unknown_reported_cost_uses_frozen_upper_bound_and_reaches_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class UnknownCostTransport(_Transport):
        async def send(self, binding, retry_index):
            response = await super().send(binding, retry_index)
            return RawProviderResponse(
                content_bytes=response.content_bytes,
                actual_provider=response.actual_provider,
                provider_request_id=response.provider_request_id,
                returned_model_id=response.returned_model_id,
                returned_model_revision=response.returned_model_revision,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                total_tokens=response.total_tokens,
                cost_usd=None,
                cost_source=None,
                observed_request_contract_hash=(
                    response.observed_request_contract_hash
                ),
            )

    plan = _plan(monkeypatch)
    result = execute_provider_smoke(
        plan,
        tmp_path / "unknown-cost",
        credential="test-only",
        transport_factory=UnknownCostTransport,
    )

    assert result["endpoint"] == "PROVIDER_RESPONSE_BOUND"
    assert result["provider_decision_accepted"] is True
    assert result["parser_result"]["status"] == "PARSED"
    assert result["gate_result"]["accepted"] is True
    assert result["reported_cost_usd"] is None
    assert result["accounted_cost_usd"] == pytest.approx(0.25)
    assert result["physical_transport_requests"] == 1


def test_openai_transport_disables_sdk_retries_and_preserves_raw_content(
    monkeypatch,
) -> None:
    plan = _plan(monkeypatch)
    binding = provider_smoke._binding_from_evidence(plan["request_binding"])
    profile = ProviderModelRequestCapability.from_mapping(
        plan["request_capability_profile"]["profile"]
    )
    expected_outbound = CanonicalOutboundRequestBinding.from_mapping(
        plan["canonical_outbound_binding"]
    )
    observed = {}

    class Usage:
        prompt_tokens = 11
        completion_tokens = 13
        total_tokens = 24

    class Message:
        content = '{"strict":"raw"}'

    class Choice:
        message = Message()

    class Response:
        id = "request-transport-001"
        model = binding.model_id
        system_fingerprint = "revision-001"
        usage = Usage()
        choices = [Choice()]

    class Completions:
        async def create(self, **kwargs):
            observed["request"] = kwargs
            await observed["client"]["http_client"].emit_request()
            return Response()

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    def factory(**kwargs):
        observed["client"] = kwargs
        return Client()

    transport = OpenAIChatCompletionsTransport(
        api_key="test-only",
        base_url=plan["provider_config"]["base_url"],
        capability_profile=profile,
        sdk_distribution_manifest=plan["sdk_distribution"],
        expected_outbound_binding=expected_outbound,
        client_factory=factory,
        http_client_factory=_FakeHttpClient,
    )
    response = __import__("asyncio").run(transport.send(binding, 0))

    assert observed["client"]["max_retries"] == 0
    assert observed["client"]["timeout"] == 60.0
    assert observed["client"]["http_client"].options == {
        "follow_redirects": False,
        "trust_env": False,
        "http2": False,
        "event_hooks": observed["client"]["http_client"].options[
            "event_hooks"
        ],
    }
    assert observed["client"]["http_client"].closed is True
    assert observed["request"] == {
        "model": binding.model_id,
        "messages": [
            {"role": "system", "content": binding.prompt_bytes.decode()},
            {"role": "user", "content": binding.context_bytes.decode()},
        ],
        "max_completion_tokens": 300,
        "response_format": {"type": "json_object"},
    }
    assert response.content_bytes == b'{"strict":"raw"}'
    assert response.total_tokens == 24
    assert response.cost_usd is None
    assert transport.physical_requests == 1
    assert transport.prepared_outbound_binding == expected_outbound
