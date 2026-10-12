"""Pure request-shape tests; no SDK, credentials or network."""
from dataclasses import replace
import pytest
from integrations.llm.runtime.types import LLMRequest
from integrations.llm.runtime.adapters.router_zdr_request import build_zdr_body


def fixture():
    request = LLMRequest(provider="openrouter", model="test/exact", messages=[{"role": "user", "content": "unchanged"}],
        temperature=None, max_tokens=100, timeout_sec=300, reasoning_effort="high")
    endpoint = dict(model_id="test/exact", tag="chosen", zdr_verified=True,
        supported_parameters=["reasoning", "max_completion_tokens", "response_format"])
    return request, dict(endpoint=endpoint, max_input_bytes=1000, input_rate=.5, output_rate=2.)


def test_explicit_routing_preserves_study_parameters():
    request, kwargs = fixture()
    body = build_zdr_body(request, **kwargs)
    assert body["provider"]["zdr"] is True
    assert body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["only"] == ["chosen"]
    assert body["reasoning"] == {"effort": "high"}
    assert body["max_completion_tokens"] == 100
    assert "temperature" not in body and "max_tokens" not in body
    body["messages"][0]["content"] = "mutated copy"
    assert request.messages[0]["content"] == "unchanged"


@pytest.mark.parametrize("fault", ["zdr", "model", "parameters", "size", "reasoning", "price"])
def test_no_silent_fallback_or_input_rewrite(fault):
    request, kwargs = fixture()
    if fault == "zdr": kwargs["endpoint"]["zdr_verified"] = False
    if fault == "model": request = replace(request, model="other")
    if fault == "parameters": kwargs["endpoint"]["supported_parameters"] = []
    if fault == "size": kwargs["max_input_bytes"] = 1
    if fault == "reasoning": request = replace(request, reasoning_effort=None)
    if fault == "price": kwargs["input_rate"] = float("nan")
    with pytest.raises(ValueError): build_zdr_body(request, **kwargs)
