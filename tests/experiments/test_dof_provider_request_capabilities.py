from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from optimizer_pipeline.dof.provider import (
    ProviderBindingError,
    ProviderRequestBinding,
    ProviderSettings,
)
from optimizer_pipeline.dof.provider_request_capabilities import (
    ProviderModelRequestCapability,
    ProviderRequestCapabilityError,
    build_canonical_outbound_request,
    provider_sdk_distribution_manifest,
)
from optimizer_pipeline.dof.provider_transport import (
    OPENAI_SDK_AMBIENT_CONFIGURATION_KEYS,
    OpenAIChatCompletionsTransport,
)
from optimizer_pipeline.dof import provider_transport as provider_transport_module


BASE_URL = "https://api.openai.com/v1"
SDK_DISTRIBUTION = provider_sdk_distribution_manifest()


class _FakeHttpClient:
    instances = []

    def __init__(self, **kwargs):
        self.options = kwargs
        self.closed = False
        type(self).instances.append(self)

    async def aclose(self):
        self.closed = True

    async def emit_request(self):
        for hook in self.options.get("event_hooks", {}).get("request", ()):
            await hook(object())


@pytest.fixture(autouse=True)
def _clear_openai_sdk_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in OPENAI_SDK_AMBIENT_CONFIGURATION_KEYS:
        monkeypatch.delenv(key, raising=False)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _profile_payload(**updates):
    payload = {
        "schema_version": "dof.provider-model-request-capability.v1",
        "profile_version": "test.openai.exact-model.v1",
        "provider": "openai",
        "adapter_type": "openai",
        "adapter_version": "dof.openai-chat-completions-transport.v2",
        "model_id": "exact-model",
        "model_revision_policy": "exact_model_id_provider_revision_optional",
        "sdk_operation": "chat.completions.create",
        "sdk_operation_version": "dof.openai.chat-completions-create.v1",
        "token_limit_parameter": "max_completion_tokens",
        "response_format": {
            "policy": "include",
            "value": {"type": "json_object"},
        },
        "temperature_policy": "omit",
        "seed_policy": "omit",
        "timeout": {"placement": "client", "parameter": "timeout"},
        "client_retry_policy": {"max_retries": 0},
        "http_transport_policy": {
            "implementation": "openai.DefaultAsyncHttpxClient",
            "follow_redirects": False,
            "trust_env": False,
            "http2": False,
            "request_counter": "httpx.async-request-event-hook.v1",
            "cleanup_deadline_policy": "single-nonrenewable-deadline.v1",
            "cleanup_reserve_seconds": 1.0,
        },
        "disallowed_simultaneous_fields": [
            ["max_tokens", "max_completion_tokens"]
        ],
    }
    payload.update(updates)
    return payload


def _profile(**updates) -> ProviderModelRequestCapability:
    return ProviderModelRequestCapability.from_mapping(_profile_payload(**updates))


def _binding(**updates) -> ProviderRequestBinding:
    values = {
        "run_id": "run-provider-capability-test",
        "spec_hash": _sha("spec"),
        "experiment_id": "experiment-provider-capability",
        "condition_id": "condition-exact",
        "repeat_id": "repeat-0",
        "decision_id": "decision-provider-capability",
        "context_bytes": b'{"source":"synthetic"}',
        "prompt_bytes": b"Return exactly one JSON object.",
        "prompt_version": "test.prompt.v1",
        "action_interface_version": "test.action.v1",
        "action_interface_hash": _sha("action"),
        "context_interface_version": "test.context.v1",
        "context_interface_hash": _sha("context"),
        "catalogue_version": "test.catalogue.v1",
        "catalogue_hash": _sha("catalogue"),
        "capability_hash": _sha("capability"),
        "mask_evidence_hash": _sha("mask"),
        "provider_role": "catalogue_agent",
        "provider": "openai",
        "model_id": "exact-model",
        "adapter_version": "dof.openai-chat-completions-transport.v2",
        "settings": ProviderSettings(
            temperature=None,
            max_tokens=123,
            seed=None,
            timeout_seconds=17.0,
            max_retries=0,
            retry_on=(),
            max_requests=1,
        ),
    }
    values.update(updates)
    return ProviderRequestBinding(**values)


@pytest.mark.parametrize(
    "token_parameter,token_value,expected_key",
    [
        ("max_tokens", 123, "max_tokens"),
        ("max_completion_tokens", 123, "max_completion_tokens"),
        ("declared_completion_limit", 123, "declared_completion_limit"),
        ("omit", None, None),
    ],
)
def test_canonical_builder_supports_exact_token_limit_policies(
    token_parameter: str,
    token_value: int | None,
    expected_key: str | None,
) -> None:
    profile = _profile(token_limit_parameter=token_parameter)
    binding = _binding(
        settings=replace(_binding().settings, max_tokens=token_value)
    )

    prepared = build_canonical_outbound_request(
        binding,
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    )
    kwargs = prepared.request_kwargs_dict()

    assert ("max_tokens" in kwargs) is (expected_key == "max_tokens")
    assert ("max_completion_tokens" in kwargs) is (
        expected_key == "max_completion_tokens"
    )
    if expected_key is not None:
        assert kwargs[expected_key] == 123
    assert not ({"max_tokens", "max_completion_tokens"} <= set(kwargs))
    assert prepared.binding.outbound_top_level_keys == tuple(sorted(kwargs))


@pytest.mark.parametrize(
    "temperature_policy,temperature,seed_policy,seed,expected_optional",
    [
        ("include", 0.0, "include", 7, {"temperature": 0.0, "seed": 7}),
        ("omit", None, "omit", None, {}),
    ],
)
def test_canonical_builder_includes_or_omits_temperature_and_seed_exactly(
    temperature_policy: str,
    temperature: float | None,
    seed_policy: str,
    seed: int | None,
    expected_optional: dict,
) -> None:
    profile = _profile(
        temperature_policy=temperature_policy,
        seed_policy=seed_policy,
    )
    binding = _binding(
        settings=replace(
            _binding().settings,
            temperature=temperature,
            seed=seed,
        )
    )

    kwargs = build_canonical_outbound_request(
        binding,
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    ).request_kwargs_dict()

    assert {key: kwargs[key] for key in expected_optional} == expected_optional
    assert ("temperature" in kwargs) is (temperature_policy == "include")
    assert ("seed" in kwargs) is (seed_policy == "include")


def test_response_format_and_timeout_placement_are_profile_controlled() -> None:
    profile = _profile(
        response_format={"policy": "omit", "value": None},
        timeout={"placement": "request", "parameter": "timeout"},
    )

    prepared = build_canonical_outbound_request(
        _binding(),
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    )

    assert "response_format" not in prepared.request_kwargs
    assert prepared.request_kwargs["timeout"] == 17.0
    assert prepared.client_options_dict() == {
        "base_url": BASE_URL,
        "max_retries": 0,
        "http_client": {
            "implementation": "openai.DefaultAsyncHttpxClient",
            "follow_redirects": False,
            "trust_env": False,
            "http2": False,
            "request_counter": "httpx.async-request-event-hook.v1",
            "cleanup_deadline_policy": "single-nonrenewable-deadline.v1",
            "cleanup_reserve_seconds": 1.0,
        },
    }


def test_provider_runtime_manifest_binds_python_and_http_dependency_closure() -> None:
    manifest = SDK_DISTRIBUTION
    assert manifest["schema_version"] == "dof.provider-sdk-runtime-binding.v2"
    assert len(manifest["python_runtime"]["executable_sha256"]) == 64
    names = [row["distribution"] for row in manifest["distributions"]]
    for required in ("openai", "httpx", "httpcore", "anyio", "pydantic", "certifi"):
        assert required in names


def test_shadow_http_runtime_origin_is_rejected_before_client_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    binding = _binding()
    expected = build_canonical_outbound_request(
        binding,
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    ).binding
    shadow = tmp_path / "httpx.py"
    shadow.write_text("raise RuntimeError('shadow executed')\n", encoding="utf-8")
    real_find_spec = provider_transport_module.importlib.util.find_spec

    def shadowed_find_spec(name):
        if name == "httpx":
            return type("ShadowSpec", (), {"origin": str(shadow)})()
        return real_find_spec(name)

    monkeypatch.setattr(
        provider_transport_module.importlib.util,
        "find_spec",
        shadowed_find_spec,
    )
    with pytest.raises(
        ProviderRequestCapabilityError,
        match="provider_runtime_module_origin_mismatch:httpx",
    ):
        OpenAIChatCompletionsTransport(
            api_key="test-only",
            base_url=BASE_URL,
            capability_profile=profile,
            sdk_distribution_manifest=SDK_DISTRIBUTION,
            expected_outbound_binding=expected,
            client_factory=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("client must not be constructed")
            ),
            http_client_factory=_FakeHttpClient,
        )


@pytest.mark.parametrize(
    "token_parameter,token_value,temperature_policy,temperature,seed_policy,seed",
    [
        ("max_tokens", 123, "include", 0.0, "include", 7),
        ("max_completion_tokens", 123, "omit", None, "omit", None),
        ("omit", None, "omit", None, "omit", None),
    ],
)
def test_recording_fake_client_receives_the_exact_canonical_kwargs(
    token_parameter: str,
    token_value: int | None,
    temperature_policy: str,
    temperature: float | None,
    seed_policy: str,
    seed: int | None,
) -> None:
    profile = _profile(
        token_limit_parameter=token_parameter,
        temperature_policy=temperature_policy,
        seed_policy=seed_policy,
    )
    binding = _binding(
        settings=replace(
            _binding().settings,
            max_tokens=token_value,
            temperature=temperature,
            seed=seed,
        )
    )
    prepared = build_canonical_outbound_request(
        binding,
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    )
    observed = {}

    class Message:
        content = '{"schema_version":"recording-fake"}'

    class Choice:
        message = Message()

    class Response:
        id = "recording-request"
        model = binding.model_id
        system_fingerprint = "recording-revision"
        usage = None
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
        base_url=BASE_URL,
        capability_profile=profile,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
        expected_outbound_binding=prepared.binding,
        client_factory=factory,
        http_client_factory=_FakeHttpClient,
    )

    asyncio.run(transport.send(binding, 0))

    assert observed["request"] == prepared.request_kwargs_dict()
    expected_client = prepared.client_options_dict()
    expected_http = expected_client.pop("http_client")
    actual_http = observed["client"].pop("http_client")
    assert observed["client"] == {"api_key": "test-only", **expected_client}
    actual_http_options = dict(actual_http.options)
    event_hooks = actual_http_options.pop("event_hooks")
    assert actual_http_options == {
        key: value
        for key, value in expected_http.items()
        if key
        not in {
            "implementation",
            "request_counter",
            "cleanup_deadline_policy",
            "cleanup_reserve_seconds",
        }
    }
    assert set(event_hooks) == {"request"}
    assert actual_http.closed is True
    assert observed["client"]["api_key"] == "test-only"
    assert transport.physical_requests == 1


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ({"unexpected": True}, "capability_profile_unknown_fields"),
        ({"token_limit_parameter": "dynamic-key"}, "unknown_token_limit_parameter"),
        (
            {"disallowed_simultaneous_fields": []},
            "token_alias_exclusion_must_be_declared_exactly_once",
        ),
        (
            {"client_retry_policy": {"max_retries": 1}},
            "sdk_retries_must_be_zero",
        ),
    ],
)
def test_unknown_or_contradictory_profiles_reject_before_transport(
    mutation: dict,
    reason: str,
) -> None:
    with pytest.raises(ProviderRequestCapabilityError, match=reason):
        ProviderModelRequestCapability.from_mapping(_profile_payload(**mutation))


def test_profile_optional_field_contradiction_rejects_before_client_factory() -> None:
    profile = _profile(temperature_policy="omit")
    binding = _binding(
        settings=replace(_binding().settings, temperature=0.0)
    )
    factory_calls = 0

    def factory(**kwargs):
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("client factory must not be entered")

    expected = build_canonical_outbound_request(
        _binding(),
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    ).binding
    transport = OpenAIChatCompletionsTransport(
        api_key="test-only",
        base_url=BASE_URL,
        capability_profile=profile,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
        expected_outbound_binding=expected,
        client_factory=factory,
        http_client_factory=_FakeHttpClient,
    )

    with pytest.raises(
        ProviderRequestCapabilityError,
        match="omitted_temperature_requires_unset_value",
    ):
        asyncio.run(transport.send(binding, 0))
    assert factory_calls == 0
    assert transport.physical_requests == 0


def test_frozen_outbound_binding_mismatch_rejects_before_client_factory() -> None:
    profile = _profile()
    frozen_binding = _binding()
    changed_binding = replace(
        frozen_binding,
        context_bytes=b'{"source":"different-synthetic-context"}',
    )
    expected = build_canonical_outbound_request(
        frozen_binding,
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    ).binding
    factory_calls = 0

    def factory(**kwargs):
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("client factory must not be entered")

    transport = OpenAIChatCompletionsTransport(
        api_key="test-only",
        base_url=BASE_URL,
        capability_profile=profile,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
        expected_outbound_binding=expected,
        client_factory=factory,
        http_client_factory=_FakeHttpClient,
    )

    with pytest.raises(
        ProviderRequestCapabilityError,
        match="canonical_outbound_binding_mismatch",
    ):
        asyncio.run(transport.send(changed_binding, 0))
    assert factory_calls == 0
    assert transport.physical_requests == 0


def test_strict_transport_uses_one_request_and_has_no_hidden_retry() -> None:
    profile = _profile()
    binding = _binding()
    expected = build_canonical_outbound_request(
        binding,
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    ).binding
    client_creates = 0
    create_calls = 0

    class Completions:
        async def create(self, **kwargs):
            nonlocal create_calls
            create_calls += 1
            await observed_http["client"].emit_request()
            raise RuntimeError("one injected provider error")

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    def factory(**kwargs):
        nonlocal client_creates
        client_creates += 1
        assert kwargs["max_retries"] == 0
        observed_http["client"] = kwargs["http_client"]
        return Client()

    observed_http = {}

    transport = OpenAIChatCompletionsTransport(
        api_key="test-only",
        base_url=BASE_URL,
        capability_profile=profile,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
        expected_outbound_binding=expected,
        client_factory=factory,
        http_client_factory=_FakeHttpClient,
    )

    with pytest.raises(RuntimeError, match="one injected provider error"):
        asyncio.run(transport.send(binding, 0))
    with pytest.raises(ProviderBindingError, match="transport exhausted"):
        asyncio.run(transport.send(binding, 0))

    assert client_creates == 1
    assert create_calls == 1
    assert transport.physical_requests == 1
    assert transport.prepared_outbound_binding == expected


def test_local_sdk_failure_consumes_send_but_counts_zero_physical_requests() -> None:
    profile = _profile()
    binding = _binding()
    expected = build_canonical_outbound_request(
        binding,
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    ).binding

    class Completions:
        async def create(self, **_kwargs):
            raise ValueError("local serialization failure")

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    transport = OpenAIChatCompletionsTransport(
        api_key="test-only",
        base_url=BASE_URL,
        capability_profile=profile,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
        expected_outbound_binding=expected,
        client_factory=lambda **_kwargs: Client(),
        http_client_factory=_FakeHttpClient,
    )

    with pytest.raises(ValueError, match="local serialization failure"):
        asyncio.run(transport.send(binding, 0))
    with pytest.raises(ProviderBindingError, match="transport exhausted"):
        asyncio.run(transport.send(binding, 0))
    assert transport.send_attempts == 1
    assert transport.sdk_create_calls == 1
    assert transport.physical_requests == 0
    assert transport.cleanup_status == "SUCCEEDED"


def test_hanging_cleanup_is_bounded_and_does_not_replace_primary_error() -> None:
    profile = _profile()
    base = _binding()
    binding = _binding(
        settings=replace(base.settings, timeout_seconds=0.05)
    )
    expected = build_canonical_outbound_request(
        binding,
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    ).binding

    class HangingHttpClient(_FakeHttpClient):
        async def aclose(self):
            await asyncio.Event().wait()

    class Completions:
        async def create(self, **_kwargs):
            raise ValueError("primary local failure")

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    transport = OpenAIChatCompletionsTransport(
        api_key="test-only",
        base_url=BASE_URL,
        capability_profile=profile,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
        expected_outbound_binding=expected,
        client_factory=lambda **_kwargs: Client(),
        http_client_factory=HangingHttpClient,
    )

    with pytest.raises(ValueError, match="primary local failure"):
        asyncio.run(transport.send(binding, 0))
    assert transport.physical_requests == 0
    assert transport.cleanup_status in {"TIMED_OUT", "DEADLINE_EXHAUSTED"}
    assert transport.cleanup_error in {
        "provider_cleanup_timed_out",
        "provider_cleanup_deadline_exhausted",
    }


def test_malformed_sdk_response_preserves_returned_metadata_and_empty_raw_content() -> None:
    profile = _profile()
    binding = _binding()
    expected = build_canonical_outbound_request(
        binding,
        profile,
        base_url=BASE_URL,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
    ).binding
    observed = {}

    class Response:
        id = "malformed-response-id"
        model = binding.model_id
        system_fingerprint = "revision-malformed"
        usage = None
        choices = []

    class Completions:
        async def create(self, **_kwargs):
            await observed["http_client"].emit_request()
            return Response()

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    def factory(**kwargs):
        observed["http_client"] = kwargs["http_client"]
        return Client()

    transport = OpenAIChatCompletionsTransport(
        api_key="test-only",
        base_url=BASE_URL,
        capability_profile=profile,
        sdk_distribution_manifest=SDK_DISTRIBUTION,
        expected_outbound_binding=expected,
        client_factory=factory,
        http_client_factory=_FakeHttpClient,
    )
    response = asyncio.run(transport.send(binding, 0))

    assert response.content_bytes == b""
    assert response.provider_request_id == "malformed-response-id"
    assert response.returned_model_id == binding.model_id
    assert response.returned_model_revision == "revision-malformed"
    assert transport.physical_requests == 1
