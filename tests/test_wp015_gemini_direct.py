from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

from optimizer_pipeline.dof.gemini_direct import (
    ADAPTER_VERSION,
    API_MODEL_ID,
    CANONICAL_MODEL_ID,
    MAX_OUTPUT_TOKENS,
    PROVIDER,
    StrictGeminiDirectTransport,
    compile_request_body,
)
from optimizer_pipeline.dof.provider import (
    ProviderBindingError,
    ProviderRequestBinding,
    ProviderSettings,
    ProviderTransportError,
)


def sha(value: bytes = b"x") -> str:
    return hashlib.sha256(value).hexdigest()


def binding(max_tokens: int = MAX_OUTPUT_TOKENS) -> ProviderRequestBinding:
    return ProviderRequestBinding(
        run_id="wp015-test", spec_hash=sha(b"spec"), experiment_id="wp015",
        condition_id="admission", repeat_id="none", decision_id="wp015-gemini-test",
        context_bytes=b'{"state":1}', prompt_bytes=b"Return one JSON object.",
        prompt_version="wp015.prompt.v1", action_interface_version="wp015.action.v1",
        action_interface_hash=sha(b"action"), context_interface_version="wp015.context.v1",
        context_interface_hash=sha(b"context"), catalogue_version="wp015.catalogue.v1",
        catalogue_hash=sha(b"catalogue"), capability_hash=sha(b"capability"),
        mask_evidence_hash=sha(b"mask"), provider_role="planner", provider=PROVIDER,
        model_id=CANONICAL_MODEL_ID, adapter_version=ADAPTER_VERSION,
        settings=ProviderSettings(
            temperature=None, max_tokens=max_tokens, seed=None,
            timeout_seconds=60.0, max_retries=0, retry_on=(), max_requests=1,
        ),
    )


class Response:
    def __init__(self, payload: dict[str, Any], status: int = 200):
        self.content = json.dumps(payload, sort_keys=True).encode()
        self.status_code = status
        self.headers: dict[str, str] = {}


class Client:
    def __init__(self, response: Response, **kwargs: Any):
        self.response = response
        self.hooks = kwargs["event_hooks"]["request"]

    async def post(self, endpoint: str, **kwargs: Any) -> Response:
        assert endpoint.endswith(f"/{API_MODEL_ID}:generateContent")
        assert kwargs["headers"]["x-goog-api-key"] == "secret-for-test"
        for hook in self.hooks:
            await hook({"endpoint": endpoint})
        return self.response

    async def aclose(self) -> None:
        return None


def factory(response: Response):
    return lambda **kwargs: Client(response, **kwargs)


def payload(*, model: str = API_MODEL_ID) -> dict[str, Any]:
    return {
        "responseId": "google-response-1", "modelVersion": model,
        "candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"thought": True, "text": "private reasoning"},
            {"text": '{"kind":"NO_VALID_SUBMISSION","reason":"test"}'},
        ]}}],
        "usageMetadata": {
            "promptTokenCount": 10, "cachedContentTokenCount": 2,
            "candidatesTokenCount": 5, "thoughtsTokenCount": 3,
            "totalTokenCount": 18,
        },
    }


def test_body_freezes_high_thinking_json_schema_and_no_sampling() -> None:
    body = compile_request_body(binding())
    config = body["generationConfig"]
    assert config["maxOutputTokens"] == 4096
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["required"] == ["kind"]
    candidate = config["responseJsonSchema"]["properties"]["candidate"]
    assert set(candidate["properties"]) == {"language", "action_key", "steps"}
    assert candidate["properties"]["steps"]["items"]["properties"]["args"]["additionalProperties"] is True
    assert config["thinkingConfig"] == {"thinkingLevel": "HIGH"}
    assert not ({"temperature", "topP", "topK"} & set(config))


def test_one_request_native_usage_cost_and_no_reasoning_persistence() -> None:
    transport = StrictGeminiDirectTransport(
        api_key="secret-for-test", client_factory=factory(Response(payload())),
    )
    result = asyncio.run(transport.send(binding(), 0))
    assert result.content_bytes.startswith(b'{"kind"')
    assert result.input_tokens == 10 and result.output_tokens == 8
    assert result.returned_model_id == CANONICAL_MODEL_ID
    assert transport.physical_requests == 1
    exchange = transport.last_exchange
    assert exchange is not None
    assert exchange["native_usage"]["thought_tokens"] == 3
    assert exchange["retry_count"] == exchange["fallback_count"] == exchange["repair_count"] == 0
    assert exchange["finish_reason"] == "STOP"
    assert exchange["final_text_truncated"] is False
    assert exchange["silent_truncation_detected"] is False
    assert exchange["final_answer_tokens"] == 5
    assert "private reasoning" not in json.dumps(exchange)
    assert "secret-for-test" not in json.dumps(exchange)


def test_wp015a_envelope_uses_12288_total_with_4096_final_capacity() -> None:
    transport = StrictGeminiDirectTransport(
        api_key="secret-for-test", output_token_limit=12288,
        client_factory=factory(Response(payload())),
    )
    asyncio.run(transport.send(binding(12288), 0))
    assert transport.last_exchange is not None
    assert transport.last_exchange["configured_total_output_token_ceiling"] == 12288
    assert transport.last_exchange["reserved_final_answer_capacity_tokens"] == 4096


def test_model_drift_and_second_send_fail_closed() -> None:
    transport = StrictGeminiDirectTransport(
        api_key="secret-for-test", client_factory=factory(Response(payload(model="substitute"))),
    )
    with pytest.raises(ProviderTransportError, match="returned_model_id_mismatch"):
        asyncio.run(transport.send(binding(), 0))
    assert transport.physical_requests == 1
    with pytest.raises(ProviderBindingError, match="exhausted"):
        asyncio.run(transport.send(binding(), 0))


def test_http_error_does_not_persist_body() -> None:
    transport = StrictGeminiDirectTransport(
        api_key="secret-for-test",
        client_factory=factory(Response({"error": "private payload"}, status=429)),
    )
    with pytest.raises(ProviderTransportError) as captured:
        asyncio.run(transport.send(binding(), 0))
    assert str(captured.value) == "google_gemini_http_429:RATE_LIMIT"
    assert "private" not in str(captured.value)
