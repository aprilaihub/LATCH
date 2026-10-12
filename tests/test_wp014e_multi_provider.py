from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
from typing import Any

import pytest

import scripts.run_wp014e as wp014e

from optimizer_pipeline.dof.multi_provider import (
    MAX_CANONICAL_INPUT_BYTES,
    MAX_OUTPUT_TOKENS,
    PROFILES,
    StrictMultiProviderTransport,
    compile_request_body,
    derive_cost_usd,
    load_pricing,
    load_profiles,
    reject_ambient_provider_overrides,
)
from optimizer_pipeline.dof.provider import (
    ProviderBindingError,
    ProviderRequestBinding,
    ProviderSettings,
    ProviderTransportError,
)


def sha(value: bytes = b"x") -> str:
    return hashlib.sha256(value).hexdigest()


def binding(model: str, *, context: bytes = b'{"state":1}') -> ProviderRequestBinding:
    profile = PROFILES[model]
    return ProviderRequestBinding(
        run_id="wp014e-test", spec_hash=sha(b"spec"),
        experiment_id="wp014e", condition_id="admission",
        repeat_id="none", decision_id="wp014e-test-decision",
        context_bytes=context, prompt_bytes=b"Return one JSON object.",
        prompt_version="wp014e.prompt.v1",
        action_interface_version="wp014e.action.v1",
        action_interface_hash=sha(b"action"),
        context_interface_version="wp014e.context.v1",
        context_interface_hash=sha(b"context"),
        catalogue_version="wp014e.catalogue.v1",
        catalogue_hash=sha(b"catalogue"), capability_hash=sha(b"capability"),
        mask_evidence_hash=sha(b"mask"), provider_role="planner",
        provider=profile.provider, model_id=model,
        adapter_version=profile.adapter_version,
        settings=ProviderSettings(
            temperature=None, max_tokens=MAX_OUTPUT_TOKENS, seed=None,
            timeout_seconds=60.0, max_retries=0, retry_on=(), max_requests=1,
        ),
    )


def test_exact_seven_profiles_and_frozen_pricing() -> None:
    assert len(load_profiles()) == len(PROFILES) == 7
    assert set(load_pricing()["models"]) == set(PROFILES)
    assert {profile.provider for profile in PROFILES.values()} == {
        "openai", "anthropic", "deepseek", "openrouter",
    }


@pytest.mark.parametrize("model", tuple(PROFILES))
def test_all_profiles_compile_high_reasoning_without_sampling(model: str) -> None:
    profile = PROFILES[model]
    body = compile_request_body(
        profile, binding(model),
        pinned_endpoint_tag="google-vertex/global" if profile.provider == "openrouter" else None,
    )
    assert body["model"] == profile.api_model_id
    assert "temperature" not in body and "top_p" not in body and "top_k" not in body
    assert body.get("reasoning_effort") == "high" or body.get("output_config") == {"effort": "high"}
    assert body.get("max_tokens", body.get("max_completion_tokens")) == 4096
    if profile.provider != "anthropic":
        assert body["response_format"] == {"type": "json_object"}
    if profile.provider == "openrouter":
        assert body["provider"]["allow_fallbacks"] is False
        assert all(plugin["enabled"] is False for plugin in body["plugins"])
    if profile.provider == "openai":
        assert "prompt_cache_options" not in body
        assert "service_tier" not in body
        assert "cache_control" not in json.dumps(body, sort_keys=True)


def test_wp016_scoped_openai_explicit_no_cache_request_contract() -> None:
    model = "openai/gpt-5.6-luna"
    body = compile_request_body(
        PROFILES[model], binding(model), explicit_no_prompt_cache=True,
    )
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    assert body["service_tier"] == "default"
    assert "cache_control" not in json.dumps(body, sort_keys=True)


def test_input_ceiling_is_bytes_and_fails_before_transport() -> None:
    model = "openai/gpt-5.6-luna"
    overhead = len(binding(model).prompt_bytes)
    allowed = binding(model, context=b"x" * (MAX_CANONICAL_INPUT_BYTES - overhead))
    compile_request_body(PROFILES[model], allowed)
    rejected = binding(model, context=b"x" * (MAX_CANONICAL_INPUT_BYTES - overhead + 1))
    with pytest.raises(ProviderBindingError, match="CANONICAL_INPUT_LIMIT_EXCEEDED"):
        compile_request_body(PROFILES[model], rejected)


def test_cost_formulas_are_exact_decimal() -> None:
    pricing = load_pricing()
    assert derive_cost_usd("openai/gpt-5.6-luna", {
        "input_tokens": 1_000_000, "uncached_input_tokens": 500_000,
        "cached_input_tokens": 250_000, "cache_write_input_tokens": 250_000,
        "output_tokens": 1_000_000,
    }, pricing) == Decimal("2.135")
    assert derive_cost_usd("anthropic/claude-sonnet-5", {
        "uncached_input_tokens": 1_000_000, "cache_write_5m_tokens": 1_000_000,
        "cache_write_1h_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }, pricing) == Decimal("18.70")
    assert derive_cost_usd("deepseek/deepseek-v4-pro", {
        "cache_hit_input_tokens": 1_000_000, "cache_miss_input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }, pricing) == Decimal("1.308625")
    assert derive_cost_usd("google/gemini-3.6-flash", {
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
    }, pricing) == Decimal("4.50")


class Response:
    def __init__(self, payload: dict[str, Any], *, status: int = 200, headers: dict[str, str] | None = None):
        self.content = json.dumps(payload, sort_keys=True).encode()
        self.status_code = status
        self.headers = headers or {}


class Client:
    def __init__(self, response: Response, **kwargs: Any):
        self.response = response
        self.hooks = kwargs["event_hooks"]["request"]
        self.closed = False

    async def post(self, endpoint: str, **kwargs: Any) -> Response:
        for hook in self.hooks:
            await hook({"endpoint": endpoint})
        return self.response

    async def aclose(self) -> None:
        self.closed = True


def factory(response: Response):
    return lambda **kwargs: Client(response, **kwargs)


@pytest.mark.parametrize(
    "model,payload,headers",
    [
        ("openai/gpt-5.6-luna", {
            "id": "o1", "model": "gpt-5.6-luna",
            "choices": [{"message": {"content": '{"kind":"PASS"}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "prompt_tokens_details": {"cached_tokens": 0},
                      "completion_tokens_details": {"reasoning_tokens": 2}},
        }, {}),
        ("anthropic/claude-sonnet-5", {
            "id": "a1", "model": "claude-sonnet-5",
            "content": [{"type": "thinking", "thinking": "private"},
                        {"type": "text", "text": '{"kind":"PASS"}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }, {}),
        ("deepseek/deepseek-v4-flash", {
            "id": "d1", "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": '{"kind":"PASS"}', "reasoning_content": "private"}}],
            "usage": {"prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 8,
                      "completion_tokens": 5, "reasoning_tokens": 2},
        }, {}),
        ("google/gemini-3.6-flash", {
            "id": "g1", "model": "google/gemini-3.6-flash", "provider": "Google",
            "choices": [{"message": {"content": '{"kind":"PASS"}', "reasoning": "private"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "reasoning_tokens": 2, "cost": 0.001},
        }, {"x-openrouter-provider": "Google"}),
    ],
)
def test_one_request_native_usage_and_no_reasoning_persistence(
    model: str, payload: dict[str, Any], headers: dict[str, str],
) -> None:
    profile = PROFILES[model]
    transport = StrictMultiProviderTransport(
        profile=profile, api_key="secret-for-test",
        pinned_endpoint_tag="google-vertex/global" if profile.provider == "openrouter" else None,
        client_factory=factory(Response(payload, headers=headers)),
    )
    result = asyncio.run(transport.send(binding(model), 0))
    assert result.content_bytes == b'{"kind":"PASS"}'
    assert result.returned_model_id == model
    expected_upstream = (
        profile.required_upstream_provider
        if profile.provider == "openrouter"
        else profile.provider + "_direct"
    )
    assert result.returned_model_revision == expected_upstream
    assert transport.physical_requests == 1
    exchange = transport.last_exchange
    assert exchange is not None
    assert exchange["retry_count"] == exchange["fallback_count"] == exchange["repair_count"] == 0
    assert exchange["raw_reasoning_persisted"] is False
    assert "private" not in json.dumps(exchange)
    assert "secret-for-test" not in json.dumps(exchange)


def test_returned_model_drift_fails_without_retry_or_raw_error() -> None:
    model = "openai/gpt-5.6-luna"
    payload = {
        "id": "x", "model": "substitute",
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    }
    transport = StrictMultiProviderTransport(
        profile=PROFILES[model], api_key="secret-for-test",
        client_factory=factory(Response(payload)),
    )
    with pytest.raises(ProviderTransportError, match="returned_model_id_mismatch"):
        asyncio.run(transport.send(binding(model), 0))
    assert transport.physical_requests == 1
    with pytest.raises(ProviderBindingError, match="exhausted"):
        asyncio.run(transport.send(binding(model), 0))


def test_http_error_is_public_taxonomy_without_body_or_key() -> None:
    model = "deepseek/deepseek-v4-pro"
    response = Response({"error": {"message": "private server and prompt"}}, status=429)
    transport = StrictMultiProviderTransport(
        profile=PROFILES[model], api_key="secret-for-test",
        client_factory=factory(response),
    )
    with pytest.raises(ProviderTransportError) as captured:
        asyncio.run(transport.send(binding(model), 0))
    message = str(captured.value)
    assert message == "deepseek_http_429:RATE_LIMIT"
    assert "private" not in message and "secret-for-test" not in message


@pytest.mark.parametrize(
    "model,payload,headers",
    [
        ("anthropic/claude-sonnet-5", {
            "id": "a-empty", "model": "claude-sonnet-5",
            "content": [{"type": "thinking", "thinking": "private"}],
            "usage": {"input_tokens": 10, "output_tokens": 4096,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }, {}),
        ("google/gemini-3.6-flash", {
            "id": "g-empty", "model": "google/gemini-3.6-flash", "provider": "Google",
            "choices": [{"message": {"content": None, "reasoning": "private"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4096,
                      "reasoning_tokens": 4096, "cost": 0.01},
        }, {"x-openrouter-provider": "Google"}),
    ],
)
def test_reasoning_only_success_is_empty_model_output_not_transport_failure(
    model: str, payload: dict[str, Any], headers: dict[str, str],
) -> None:
    profile = PROFILES[model]
    transport = StrictMultiProviderTransport(
        profile=profile, api_key="secret-for-test",
        pinned_endpoint_tag="google-vertex/global" if profile.provider == "openrouter" else None,
        client_factory=factory(Response(payload, headers=headers)),
    )
    result = asyncio.run(transport.send(binding(model), 0))
    assert result.content_bytes == b""
    assert transport.last_exchange is not None
    assert transport.last_exchange["final_text_present"] is False
    assert transport.physical_requests == 1


def test_ambient_route_override_is_rejected_by_key_only() -> None:
    with pytest.raises(ProviderBindingError, match="OPENAI_BASE_URL"):
        reject_ambient_provider_overrides({"OPENAI_BASE_URL": "private"})


def test_coverage_mini_matrix_is_exact_and_not_a_wp015_repeat() -> None:
    source = {
        "jobs": [
            {
                "root_id": root,
                "phase": "S_N",
                "admitted_action_keys": ["macro.phys_opt"],
                "mask_evidence_hash": "a" * 64,
            }
            for root in wp014e.MATRIX_ROOTS
        ]
    }
    jobs = wp014e.matrix_jobs(source)
    counts = {name: sum(row["subclass"] == name for row in jobs) for name in {
        "N_L2", "N_L3", "N_L4", "C_M_ONLY", "C_P_BOUNDARY",
        "C_G_BOUNDARY", "S_L2", "S_L3", "S_L4",
    }}
    assert len(jobs) == 130
    assert counts == {name: 14 for name in counts}
    assert sum(row["model"] == "MODEL_INDEPENDENT" for row in jobs) == 4
    assert all(row["not_a_wp015_repeat"] is True for row in jobs)
    assert all(row["label"] == wp014e.MATRIX_LABEL for row in jobs)
