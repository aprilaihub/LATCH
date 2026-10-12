"""OpenAI chat-completions adapter."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional

from integrations.llm.runtime.client import LLMClient, LLMClientError
from integrations.llm.runtime.pricing import PricingProfile
from integrations.llm.runtime.provider_profiles import ProviderRequestPolicy
from integrations.llm.runtime.types import LLMRequest, LLMResponse

try:
    from openai import OpenAI as OpenAIClient
except Exception:  # pragma: no cover - depends on environment
    OpenAIClient = None


class OpenAIChatClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: Optional[str],
        client: Optional[Any] = None,
        client_factory: Optional[Any] = None,
        base_url: Optional[str] = None,
        pricing_profile: Optional[PricingProfile] = None,
        request_policy: Optional[ProviderRequestPolicy] = None,
        allow_parameter_retry: bool = True,
    ) -> None:
        self.api_key = api_key
        self.client = client
        self.client_factory = client_factory
        self.base_url = base_url
        self.pricing_profile = pricing_profile
        self.request_policy = request_policy or ProviderRequestPolicy()
        self.allow_parameter_retry = allow_parameter_retry

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        client = self._client()
        kwargs = self._completion_kwargs(request, self.request_policy)
        start = time.perf_counter()
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            if self.allow_parameter_retry and "temperature" in kwargs and _is_unsupported_temperature_error(exc):
                kwargs.pop("temperature", None)
                try:
                    response = client.chat.completions.create(**kwargs)
                except Exception as retry_exc:
                    raise LLMClientError(f"LLM provider call failed: {retry_exc}") from retry_exc
            else:
                raise LLMClientError(f"LLM provider call failed: {exc}") from exc
        latency = time.perf_counter() - start
        content = _response_content(response)
        prompt_tokens, completion_tokens, total_tokens, reported_cost = _response_usage(response)
        pricing_id_override = None
        estimated_cost = self.pricing_profile.estimate_cost(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens) if self.pricing_profile is not None else None
        if estimated_cost is None:
            # No entry in the repo's own price table (common for a newly
            # released model). litellm's table is cache-aware, so a cached
            # prompt is not billed as fresh input.
            _u = getattr(response, "usage", None)
            _d = getattr(_u, "prompt_tokens_details", None) if _u is not None else None
            if _d is not None and not isinstance(_d, dict):
                _d = _d.model_dump() if hasattr(_d, "model_dump") else {}
            _d = _d or {}
            estimated_cost = _litellm_fallback_cost(
                request.model,
                prompt_tokens,
                completion_tokens,
                cached_tokens=_d.get("cached_tokens") or 0,
                cache_write_tokens=_d.get("cache_write_tokens") or 0,
            )
            if estimated_cost is not None:
                pricing_id_override = "litellm"
        cost = reported_cost if reported_cost is not None else estimated_cost
        if reported_cost is not None:
            cost_source = "provider_reported"
        elif estimated_cost is not None:
            cost_source = "litellm_price_table" if pricing_id_override else "estimated_pricing_profile"
        else:
            cost_source = "unknown"
        return LLMResponse(
            content=content,
            parsed_json=_try_parse_json(content),
            provider=request.provider,
            model=request.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            provider_reported_cost_usd=reported_cost,
            estimated_cost_usd=estimated_cost,
            cost_source=cost_source,
            pricing_profile_id=pricing_id_override or (self.pricing_profile.name if self.pricing_profile is not None else None),
            latency_sec=latency,
            raw_response=response,
        )

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        if not self.api_key:
            raise LLMClientError("No API key configured for LLM provider")
        if OpenAIClient is None and self.client_factory is None:
            raise LLMClientError("openai package is not available")
        factory = self.client_factory or OpenAIClient
        kwargs: Dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self.client = factory(**kwargs)
        return self.client

    @staticmethod
    def _completion_kwargs(request: LLMRequest, policy: Optional[ProviderRequestPolicy] = None) -> Dict[str, Any]:
        policy = policy or ProviderRequestPolicy()
        kwargs: Dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
        }
        if request.response_format == "json_object" and policy.response_format_policy == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        if _include_temperature(request, policy):
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs[policy.max_tokens_param] = request.max_tokens
        if request.timeout_sec is not None:
            kwargs["timeout"] = request.timeout_sec
        if request.reasoning_effort is not None:
            kwargs["reasoning_effort"] = request.reasoning_effort
        return kwargs


def _response_content(response: Any) -> Optional[str]:
    try:
        return response.choices[0].message.content
    except Exception:
        return None


def _response_usage(response: Any) -> tuple[Optional[int], Optional[int], Optional[int], Optional[float]]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    prompt_tokens = _get_usage_value(usage, "prompt_tokens")
    completion_tokens = _get_usage_value(usage, "completion_tokens")
    total_tokens = _get_usage_value(usage, "total_tokens")
    cost = _get_usage_value(usage, "cost")
    if cost is None:
        cost = _get_usage_value(usage, "cost_usd")
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = int(prompt_tokens) + int(completion_tokens)
    return (
        int(prompt_tokens) if prompt_tokens is not None else None,
        int(completion_tokens) if completion_tokens is not None else None,
        int(total_tokens) if total_tokens is not None else None,
        float(cost) if cost is not None else None,
    )


def _get_usage_value(usage: Any, key: str) -> Any:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _try_parse_json(content: Optional[str]) -> Optional[Dict[str, Any]]:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


# Matches o-series reasoning model ids with an optional provider prefix:
# "o3", "o3-mini", "openai/o4-mini". Anchoring on start-or-slash keeps
# "gpt-4o" and "gpt-4o-mini" out.
_O_SERIES_RE = re.compile(r"(?:^|/)o\d+(?:[-._]|$)")


def _litellm_fallback_cost(model, prompt_tokens, completion_tokens, cached_tokens=0, cache_write_tokens=0):
    """Cost from litellm's table when the repo pricing profile lacks the model.

    litellm ships per-model cache-read/cache-write rates, so a cached prompt is
    not billed as if it were fresh input. Returns None when uncovered.
    """
    try:
        import litellm
    except ImportError:
        return None
    entry = getattr(litellm, "model_cost", {}).get(str(model or ""))
    if not entry:
        return None
    inp = int(prompt_tokens or 0)
    cr = int(cached_tokens or 0)
    cw = int(cache_write_tokens or 0)
    plain = max(0, inp - cr - cw)
    total = plain * float(entry.get("input_cost_per_token") or 0.0)
    total += int(completion_tokens or 0) * float(entry.get("output_cost_per_token") or 0.0)
    total += cr * float(entry.get("cache_read_input_token_cost") or entry.get("input_cost_per_token") or 0.0)
    total += cw * float(entry.get("cache_creation_input_token_cost") or entry.get("input_cost_per_token") or 0.0)
    return round(total, 8)


def _omit_temperature_by_default(model: str) -> bool:
    m = model.lower()
    # "in" (not startswith) for gpt-5.6 since it's also called as "openai/gpt-5.6-*" via
    # the openrouter provider prefix; gpt-5.5's existing check is untouched.
    # o-series reasoning models (o1/o3/o4-mini/...) also reject an explicit
    # temperature. Without this they cost a rejected request plus a retry on
    # every call. The prefix group keeps "gpt-4o" and friends from matching.
    # Anthropic's current models reject an explicit temperature the same way
    # the o-series does ("`temperature` is deprecated for this model"), and
    # they reach this adapter through the OpenAI-compatible layer at
    # api.anthropic.com, so the same omission applies.
    if "claude" in m or m.startswith("anthropic/"):
        return True
    return m.startswith("gpt-5.5") or "gpt-5.6" in m or bool(_O_SERIES_RE.search(m))


def _include_temperature(request: LLMRequest, policy: ProviderRequestPolicy) -> bool:
    if request.temperature is None or _omit_temperature_by_default(request.model):
        return False
    if policy.temperature_policy == "omit":
        return False
    if policy.temperature_policy == "zero_only":
        try:
            return float(request.temperature) == 0.0
        except (TypeError, ValueError):
            return False
    return True


def _is_unsupported_temperature_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "temperature" in message and ("unsupported" in message or "does not support" in message)
