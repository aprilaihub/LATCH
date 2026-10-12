from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from optimizer_pipeline.dof.openrouter import (
    OPENROUTER_ADAPTER_VERSION,
    OPENROUTER_BASE_URL,
    OPENROUTER_E0_MODELS,
    OPENROUTER_E1_MODELS,
    OPENROUTER_E1_PROFILES,
    OpenRouterChatCompletionsTransport,
    build_openrouter_outbound,
    discover_openrouter_endpoints,
    discover_openrouter_models,
)
from optimizer_pipeline.dof.provider import (
    ProviderRequestBinding,
    ProviderSettings,
    ProviderTransportError,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _binding(model: str = OPENROUTER_E0_MODELS[0]) -> ProviderRequestBinding:
    return ProviderRequestBinding(
        run_id="run-openrouter-test",
        spec_hash=_sha("spec"),
        experiment_id="e0",
        condition_id="synthetic-l2",
        repeat_id="repeat-0",
        decision_id="decision-openrouter-test",
        context_bytes=b'{"source":"synthetic"}',
        prompt_bytes=(
            b"Return one JSON object with schema_version, proposal_id, decision_id, "
            b"catalogue_version, catalogue_hash, kind, steps, step_id, action_key, parameters."
        ),
        prompt_version="e0.prompt.v1",
        action_interface_version="action_proposal.v1",
        action_interface_hash=_sha("action"),
        context_interface_version="e0.context.v1",
        context_interface_hash=_sha("context"),
        catalogue_version="clean-macro-catalogue.v1",
        catalogue_hash=_sha("catalogue"),
        capability_hash=_sha("capability"),
        mask_evidence_hash=_sha("mask"),
        provider_role="e0_policy",
        provider="openrouter",
        model_id=model,
        adapter_version=OPENROUTER_ADAPTER_VERSION,
        settings=ProviderSettings(
            temperature=None,
            max_tokens=4096,
            seed=None,
            timeout_seconds=60.0,
            max_retries=0,
            retry_on=(),
            max_requests=1,
            max_usd=5.0,
            max_cost_per_request_usd=5.0,
            unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self.content = json.dumps(payload, separators=(",", ":")).encode()
        self.status_code = status_code
        self.headers = {
            "x-request-id": "http-request-001",
            "x-openrouter-provider": "Example Upstream",
            "set-cookie": "must-not-survive",
        }


class _Client:
    response: _Response
    instances: list["_Client"] = []

    def __init__(self, **kwargs):
        self.options = kwargs
        self.calls = []
        self.closed = False
        type(self).instances.append(self)

    async def _emit(self):
        for hook in self.options["event_hooks"]["request"]:
            await hook(object())

    async def post(self, path, **kwargs):
        await self._emit()
        self.calls.append(("POST", path, kwargs))
        return type(self).response

    async def get(self, path, **kwargs):
        await self._emit()
        self.calls.append(("GET", path, kwargs))
        return type(self).response

    async def aclose(self):
        self.closed = True


def _valid_response(binding: ProviderRequestBinding) -> dict:
    return {
        "id": "generation-001",
        "model": binding.model_id,
        "provider": OPENROUTER_E1_PROFILES[binding.model_id]["upstream_provider"],
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "schema_version": "action_proposal.v1",
                            "proposal_id": "proposal-001",
                            "decision_id": binding.decision_id,
                            "catalogue_version": binding.catalogue_version,
                            "catalogue_hash": binding.catalogue_hash,
                            "kind": "STOP",
                            "steps": [],
                        },
                        separators=(",", ":"),
                    )
                },
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cost": 0.0123,
            "completion_tokens_details": {"reasoning_tokens": 7},
            "cost_details": {"upstream_inference_cost": 0.01},
        },
    }


def test_outbound_is_the_exact_frozen_openrouter_surface() -> None:
    outbound = build_openrouter_outbound(_binding())
    assert set(outbound.body) == {
        "model",
        "messages",
        "max_tokens",
        "stream",
        "response_format",
        "provider",
        "plugins",
        "reasoning_effort",
    }
    assert outbound.body["provider"] == {
        "only": [OPENROUTER_E1_PROFILES[_binding().model_id]["endpoint_tag"]],
        "order": [OPENROUTER_E1_PROFILES[_binding().model_id]["endpoint_tag"]],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
    }
    assert outbound.body["plugins"] == [
        {"id": "response-healing", "enabled": False},
        {"id": "context-compression", "enabled": False},
    ]
    assert outbound.headers == {
        "Content-Type": "application/json",
        "X-OpenRouter-Metadata": "enabled",
        "X-OpenRouter-Cache": "false",
    }
    assert "tools" not in outbound.body
    assert "temperature" not in outbound.body
    assert "seed" not in outbound.body
    assert outbound.body["max_tokens"] == 4096


def test_transport_preserves_raw_route_usage_cost_and_hides_secret() -> None:
    binding = _binding()
    _Client.response = _Response(_valid_response(binding))
    transport = OpenRouterChatCompletionsTransport(
        api_key="private-test-key",
        expected_outbound_binding=build_openrouter_outbound(binding),
        client_factory=_Client,
    )
    result = asyncio.run(transport.send(binding, 0))

    assert result.actual_provider == "openrouter"
    assert result.returned_model_id == binding.model_id
    assert result.returned_model_revision == OPENROUTER_E1_PROFILES[binding.model_id]["upstream_provider"]
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.cost_source == "openrouter_usage.cost"
    assert transport.physical_requests == 1
    assert transport.cleanup_status == "SUCCEEDED"
    assert transport.last_exchange is not None
    assert transport.last_exchange["route"]["fallback_allowed"] is False
    assert transport.last_exchange["usage"]["completion_tokens_details"] == {
        "reasoning_tokens": 7
    }
    assert "set-cookie" not in transport.last_exchange["response_headers"]
    serialized = json.dumps(transport.last_exchange, sort_keys=True)
    assert "private-test-key" not in serialized
    client = _Client.instances[-1]
    assert client.options == {
        "base_url": OPENROUTER_BASE_URL,
        "timeout": 60.0,
        "follow_redirects": False,
        "trust_env": False,
        "event_hooks": {"request": [transport._observe_request]},
    }
    _, path, request = client.calls[0]
    assert path == "/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer private-test-key"
    body = json.loads(request["content"])
    assert body == dict(build_openrouter_outbound(binding).body)


def test_complete_response_larger_than_old_cap_reaches_strict_consumer() -> None:
    binding = _binding()
    payload = _valid_response(binding)
    content = json.loads(payload["choices"][0]["message"]["content"])
    content["padding"] = "x" * 1500
    payload["choices"][0]["message"]["content"] = json.dumps(content)
    _Client.response = _Response(payload)
    transport = OpenRouterChatCompletionsTransport(
        api_key="private-test-key",
        expected_outbound_binding=build_openrouter_outbound(binding),
        client_factory=_Client,
    )
    result = asyncio.run(transport.send(binding, 0))
    assert len(result.content_bytes) > 1024
    assert transport.last_exchange["finish_reason"] == "stop"


def test_length_finish_reason_is_preserved_as_cap_evidence() -> None:
    binding = _binding()
    payload = _valid_response(binding)
    payload["choices"][0]["finish_reason"] = "length"
    _Client.response = _Response(payload)
    transport = OpenRouterChatCompletionsTransport(
        api_key="private-test-key",
        expected_outbound_binding=build_openrouter_outbound(binding),
        client_factory=_Client,
    )
    asyncio.run(transport.send(binding, 0))
    assert transport.last_exchange["finish_reason"] == "length"


@pytest.mark.parametrize(
    "status,error,category",
    [
        (401, {"code": 401, "type": "authentication_error", "message": "invalid API key"}, "AUTHENTICATION_OR_KEY"),
        (403, {"code": 403, "type": "key_limit", "message": "key limit exceeded"}, "PAYMENT_OR_BUDGET"),
        (403, {"code": 403, "type": "workspace", "message": "workspace permission denied"}, "PERMISSION_OR_WORKSPACE"),
        (400, {"code": "prompt_block", "type": "guardrail", "message": "content policy block"}, "GUARDRAIL_OR_PROMPT_BLOCK"),
        (429, {"code": 429, "type": "rate_limit", "message": "too many requests"}, "RATE_LIMIT"),
        (503, {"code": 503, "type": "upstream", "message": "service unavailable"}, "UPSTREAM_OR_SERVER"),
        (418, {"code": 418, "type": "unknown", "message": "opaque failure"}, "UNCLASSIFIED_4XX"),
    ],
)
def test_non_2xx_error_is_safely_classified_and_raw_body_stays_private(
    status: int, error: dict, category: str
) -> None:
    binding = _binding()
    raw_message = error["message"]
    _Client.response = _Response({"error": error}, status_code=status)
    transport = OpenRouterChatCompletionsTransport(
        api_key="private-test-key",
        expected_outbound_binding=build_openrouter_outbound(binding),
        client_factory=_Client,
    )
    with pytest.raises(ProviderTransportError, match=category):
        asyncio.run(transport.send(binding, 0))
    exchange = transport.last_exchange
    assert exchange["provider_error"]["category"] == category
    assert exchange["provider_error"]["message_size_bytes"] == len(raw_message)
    assert "raw_response_bytes_b64" not in exchange
    assert raw_message not in json.dumps(exchange, sort_keys=True)
    assert transport.private_response_bytes == _Client.response.content


@pytest.mark.parametrize("model", OPENROUTER_E1_MODELS)
def test_all_e1_model_shapes_bind_and_complete_on_their_pinned_route(model: str) -> None:
    binding = _binding(model)
    _Client.response = _Response(_valid_response(binding))
    transport = OpenRouterChatCompletionsTransport(
        api_key="private-test-key",
        expected_outbound_binding=build_openrouter_outbound(binding),
        client_factory=_Client,
    )
    result = asyncio.run(transport.send(binding, 0))
    assert result.returned_model_id == model
    assert result.returned_model_revision == OPENROUTER_E1_PROFILES[model][
        "upstream_provider"
    ]
    assert transport.last_exchange["route"]["required_endpoint_tag"] == (
        OPENROUTER_E1_PROFILES[model]["endpoint_tag"]
    )


@pytest.mark.parametrize(
    "mutation,reason,failure_class",
    [
        (
            lambda payload: payload.pop("provider"),
            "openrouter_route_evidence_missing",
            "ACCOUNTING_UNAVAILABLE",
        ),
        (
            lambda payload: payload["usage"].pop("cost"),
            "openrouter_usage_cost_missing_or_invalid",
            "ACCOUNTING_UNAVAILABLE",
        ),
        (
            lambda payload: payload["usage"].update(total_tokens=999),
            "openrouter_usage_accounting_contradictory",
            "ACCOUNTING_UNAVAILABLE",
        ),
        (
            lambda payload: payload.update(model="replacement/model"),
            "openrouter_model_substitution_detected",
            "MODEL_OR_ROUTE_DRIFT",
        ),
    ],
)
def test_transport_fails_closed_on_missing_or_contradictory_evidence(
    mutation, reason: str, failure_class: str
) -> None:
    binding = _binding()
    payload = _valid_response(binding)
    mutation(payload)
    _Client.response = _Response(payload)
    transport = OpenRouterChatCompletionsTransport(
        api_key="private-test-key",
        expected_outbound_binding=build_openrouter_outbound(binding),
        client_factory=_Client,
    )
    with pytest.raises(ProviderTransportError, match=reason):
        asyncio.run(transport.send(binding, 0))
    assert transport.physical_requests == 1
    assert transport.last_exchange is not None
    assert transport.last_exchange["failure_class"] == failure_class


def test_models_discovery_requires_all_exact_slugs() -> None:
    _Client.response = _Response(
        {
            "data": [
                {"id": OPENROUTER_E0_MODELS[0]},
                {"id": "anthropic/claude-sonnet-5-latest"},
            ]
        }
    )
    evidence = asyncio.run(
        discover_openrouter_models("private-test-key", client_factory=_Client)
    )
    assert evidence["physical_requests"] == 1
    assert evidence["required_models"] == {
        model: model == OPENROUTER_E0_MODELS[0] for model in OPENROUTER_E1_MODELS
    }
    assert evidence["all_required_available"] is False
    assert "private-test-key" not in json.dumps(evidence)


def test_endpoint_discovery_requires_pinned_tag_provider_and_parameters() -> None:
    _Client.response = _Response(
        {
            "data": {
                "endpoints": [
                    {
                        "tag": profile["endpoint_tag"],
                        "provider_name": profile["upstream_provider"],
                        "supported_parameters": [
                            "max_tokens",
                            "response_format",
                            "reasoning_effort",
                        ],
                    }
                    for profile in OPENROUTER_E1_PROFILES.values()
                ]
            }
        }
    )
    evidence = asyncio.run(
        discover_openrouter_endpoints("private-test-key", client_factory=_Client)
    )
    assert evidence["physical_requests"] == 3
    assert evidence["all_profiles_available"] is True
    assert all(
        row["endpoint_available"] and row["required_parameters_supported"]
        for row in evidence["models"].values()
    )


def test_transport_rejects_upstream_route_drift() -> None:
    binding = _binding()
    payload = _valid_response(binding)
    payload["provider"] = "Unexpected Provider"
    _Client.response = _Response(payload)
    transport = OpenRouterChatCompletionsTransport(
        api_key="private-test-key",
        expected_outbound_binding=build_openrouter_outbound(binding),
        client_factory=_Client,
    )
    with pytest.raises(ProviderTransportError, match="openrouter_route_drift_detected"):
        asyncio.run(transport.send(binding, 0))


def test_openrouter_control_entry_requires_isolated_no_site_mode() -> None:
    script = REPO_ROOT / "scripts" / "run_wp005_openrouter.py"
    ordinary = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    isolated = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ordinary.returncode != 0
    assert "requires_python_isolated_no_site_mode" in ordinary.stderr
    assert isolated.returncode == 0, isolated.stderr
