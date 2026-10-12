"""Native provider usage tests with injected SDK doubles; no network."""
import sys
from types import ModuleType, SimpleNamespace as NS
from integrations.llm.runtime.adapters.native import AnthropicNativeClient, GoogleNativeClient
from integrations.llm.runtime import LLMRequest


def test_anthropic_preserves_cache_usage(monkeypatch):
    from integrations.llm.runtime.adapters import native
    monkeypatch.setattr(native, "_litellm_cost", lambda *args: None)
    response = NS(content=[NS(text='{"ok":true}')], usage=NS(input_tokens=10, output_tokens=4, cache_read_input_tokens=20, cache_creation_input_tokens=30))
    client = AnthropicNativeClient(client=NS(messages=NS(create=lambda **kwargs: response)))
    result = client.complete_json(LLMRequest(provider="anthropic", model="test", messages=[{"role":"user","content":"x"}]))
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (60, 4, 64)
    assert result.raw_response["cache_read_tokens"] == 20
    assert result.cost_usd is None  # Unknown is not free.
    assert result.parsed_json == {"ok": True}


def test_google_preserves_thoughts(monkeypatch):
    from integrations.llm.runtime.adapters import native
    monkeypatch.setattr(native, "_litellm_cost", lambda *args: None)
    module = ModuleType("google.genai")
    module.types = NS(GenerateContentConfig=lambda **kwargs: kwargs)
    monkeypatch.setitem(sys.modules, "google.genai", module)
    response = NS(text='{"ok":true}', usage_metadata=NS(prompt_token_count=25,candidates_token_count=5,thoughts_token_count=7,cached_content_token_count=12))
    client = GoogleNativeClient(client=NS(models=NS(generate_content=lambda **kwargs: response)))
    result = client.complete_json(LLMRequest(provider="google", model="test", messages=[{"role":"user","content":"x"}]))
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (25, 12, 37)
    assert result.raw_response["cache_read_tokens"] == 12
    assert result.cost_usd is None
