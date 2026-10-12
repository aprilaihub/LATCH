"""Native-SDK adapters for Anthropic and Google.

The OpenAI-compatible shim drops usage detail these providers do report:
Anthropic's cache_creation/cache_read/thinking token counts arrive as a null
prompt_tokens_details, and Google's completion_tokens reads 0 while thoughts
tokens (billed at output rate) are invisible. Both are material to cost, so
these two providers call their own SDKs. OpenAI/Luna already reports the full
breakdown through the existing path and is left alone.

Cost is computed by litellm's price table, which carries cache-read and
cache-write rates per model.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from ..types import LLMRequest, LLMResponse


def _litellm_cost(model: str, usage: Dict[str, Any]) -> Optional[float]:
    """Dollar cost from litellm's table, cache-aware. None if uncovered."""
    try:
        import litellm
    except ImportError:
        return None
    entry = getattr(litellm, "model_cost", {}).get(model)
    if not entry:
        return None
    inp = int(usage.get("prompt_tokens") or 0)
    out = int(usage.get("completion_tokens") or 0)
    cr = int(usage.get("cache_read_tokens") or 0)
    cw = int(usage.get("cache_write_tokens") or 0)
    # Cached reads/writes are billed at their own rates and are NOT additionally
    # billed as ordinary input, so they come out of the uncached input count.
    plain = max(0, inp - cr - cw)
    total = plain * float(entry.get("input_cost_per_token") or 0.0)
    total += out * float(entry.get("output_cost_per_token") or 0.0)
    total += cr * float(entry.get("cache_read_input_token_cost") or entry.get("input_cost_per_token") or 0.0)
    total += cw * float(entry.get("cache_creation_input_token_cost") or entry.get("input_cost_per_token") or 0.0)
    return round(total, 8)


class AnthropicNativeClient:
    """Anthropic Messages API, preserving cache and thinking token counts."""

    def __init__(self, *, api_key: Optional[str] = None, pricing_profile: Any = None,
                 request_policy: Any = None, client: Any = None, **_: Any) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.pricing_profile = pricing_profile
        self._client = client

    def _sdk(self) -> Any:
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        system = "\n\n".join(m["content"] for m in request.messages if m.get("role") == "system")
        msgs = [{"role": m["role"], "content": m["content"]}
                for m in request.messages if m.get("role") != "system"]
        started = time.time()
        kwargs: Dict[str, Any] = {
            "model": request.model,
            "max_tokens": int(request.max_tokens or 8192),
            "messages": msgs,
        }
        if system:
            kwargs["system"] = system
        # temperature is rejected by current Anthropic models; never sent.
        resp = self._sdk().messages.create(**kwargs)
        latency = time.time() - started

        text = "".join(getattr(b, "text", "") for b in (resp.content or []))
        u = resp.usage
        usage = {
            "prompt_tokens": int(getattr(u, "input_tokens", 0) or 0)
            + int(getattr(u, "cache_read_input_tokens", 0) or 0)
            + int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            "completion_tokens": int(getattr(u, "output_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
            "cache_write_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        cost = _litellm_cost(request.model, usage)

        parsed = None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        return LLMResponse(
            content=text, provider="anthropic", model=request.model, parsed_json=parsed,
            prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"], cost_usd=cost, estimated_cost_usd=cost,
            cost_source="litellm_native_usage" if cost is not None else None,
            pricing_profile_id="litellm", latency_sec=latency, raw_response=usage,
        )


class GoogleNativeClient:
    """Google GenAI SDK, preserving thoughts (reasoning) and cached tokens."""

    def __init__(self, *, api_key: Optional[str] = None, pricing_profile: Any = None,
                 request_policy: Any = None, client: Any = None, **_: Any) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        self.pricing_profile = pricing_profile
        self._client = client

    def _sdk(self) -> Any:
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        from google.genai import types as gtypes

        system = "\n\n".join(m["content"] for m in request.messages if m.get("role") == "system")
        user = "\n\n".join(m["content"] for m in request.messages if m.get("role") != "system")
        cfg: Dict[str, Any] = {"max_output_tokens": int(request.max_tokens or 8192)}
        if system:
            cfg["system_instruction"] = system
        if request.temperature is not None:
            cfg["temperature"] = request.temperature
        if request.response_format == "json_object":
            cfg["response_mime_type"] = "application/json"

        started = time.time()
        resp = self._sdk().models.generate_content(
            model=request.model, contents=user,
            config=gtypes.GenerateContentConfig(**cfg),
        )
        latency = time.time() - started

        text = resp.text or ""
        m = resp.usage_metadata
        # thoughts are billed as output but reported separately
        out = int(getattr(m, "candidates_token_count", 0) or 0) + int(getattr(m, "thoughts_token_count", 0) or 0)
        usage = {
            "prompt_tokens": int(getattr(m, "prompt_token_count", 0) or 0),
            "completion_tokens": out,
            "cache_read_tokens": int(getattr(m, "cached_content_token_count", 0) or 0),
            "cache_write_tokens": 0,
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        cost = _litellm_cost(request.model, usage)

        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        return LLMResponse(
            content=text, provider="google", model=request.model, parsed_json=parsed,
            prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"], cost_usd=cost, estimated_cost_usd=cost,
            cost_source="litellm_native_usage" if cost is not None else None,
            pricing_profile_id="litellm", latency_sec=latency, raw_response=usage,
        )
