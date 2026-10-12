"""WP-014E strict four-route provider boundary and native cost ledger.

Every ``send`` performs exactly one HTTP request.  The module uses no provider
SDK retry stack, never falls back across routes, never repairs provider text,
and retains only final text plus public-safe hashes/usage metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    ProviderBindingError,
    ProviderRequestBinding,
    ProviderTransportError,
    RawProviderResponse,
)


PROVIDER_PROFILE_SCHEMA = "dof.wp014e-provider-profiles.v1"
PROVIDER_EXCHANGE_SCHEMA = "dof.wp014e-provider-exchange.v1"
COST_FORMULA_VERSION = "dof.provider-cost-formula.v1"
MAX_CANONICAL_INPUT_BYTES = 65_536
MAX_OUTPUT_TOKENS = 4_096
PRICING_PATH = Path(__file__).parents[2] / "configs" / "wp014e" / "pricing-snapshot-2026-08-12.json"
PROFILES_PATH = Path(__file__).parents[2] / "configs" / "wp014e" / "provider-profiles-v1.json"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _strict_object(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ProviderTransportError(f"{label}_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderTransportError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise ProviderTransportError(f"{label}_not_object")
    return value


def _required_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderTransportError(f"usage_incomplete:{label}")
    return value


def _optional_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _required_int(value, label)


def _optional_str(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProviderTransportError(label + "_invalid")
    return value


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    canonical_model_id: str
    api_model_id: str
    provider: str
    route: str
    base_url: str
    endpoint: str
    credential_env: str
    adapter_version: str
    required_upstream_provider: str | None = None


class ProviderInferenceContractError(ProviderBindingError):
    """Typed pre-transport rejection of one common inference predicate."""

    def __init__(self, contract_code: str, evidence: Mapping[str, Any]) -> None:
        self.contract_code = str(contract_code)
        self.evidence = MappingProxyType(dict(evidence))
        super().__init__(self.contract_code)


def load_profiles(path: Path = PROFILES_PATH) -> Mapping[str, ProviderProfile]:
    value = json.loads(path.read_text())
    if value.get("schema_version") != PROVIDER_PROFILE_SCHEMA:
        raise ProviderBindingError("provider_profile_schema_mismatch")
    common = value.get("common")
    if not isinstance(common, Mapping) or common != {
        "max_canonical_input_bytes": MAX_CANONICAL_INPUT_BYTES,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "reasoning_class": "high",
        "application_retries": 0,
        "sdk_retries": 0,
        "fallbacks": 0,
        "response_repairs": 0,
    }:
        raise ProviderBindingError("provider_common_contract_mismatch")
    result: dict[str, ProviderProfile] = {}
    for row in value.get("profiles", []):
        profile = ProviderProfile(**row)
        if profile.canonical_model_id in result:
            raise ProviderBindingError("duplicate_provider_profile")
        result[profile.canonical_model_id] = profile
    if len(result) != 7:
        raise ProviderBindingError("seven_exact_provider_profiles_required")
    return MappingProxyType(result)


PROFILES = load_profiles()


def load_pricing(path: Path = PRICING_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if (
        value.get("schema_version") != "provider-pricing-snapshot.v1"
        or value.get("snapshot_id") != "provider-pricing-2026-08-13-v2"
        or set(value.get("models", {})) != set(PROFILES)
    ):
        raise ProviderBindingError("pricing_snapshot_invalid")
    return value


def compile_request_body(
    profile: ProviderProfile,
    binding: ProviderRequestBinding,
    *,
    pinned_endpoint_tag: str | None = None,
    output_token_limit: int = MAX_OUTPUT_TOKENS,
    explicit_no_prompt_cache: bool = False,
) -> dict[str, Any]:
    if binding.model_id != profile.canonical_model_id:
        raise ProviderBindingError("provider_profile_model_mismatch")
    if binding.provider != profile.provider:
        raise ProviderBindingError("provider_profile_route_mismatch")
    if binding.adapter_version != profile.adapter_version:
        raise ProviderBindingError("provider_profile_adapter_mismatch")
    settings = binding.settings
    canonical_input_bytes = len(binding.prompt_bytes) + len(binding.context_bytes)
    if canonical_input_bytes > MAX_CANONICAL_INPUT_BYTES:
        raise ProviderInferenceContractError(
            "CANONICAL_INPUT_LIMIT_EXCEEDED", {
                "prompt_bytes": len(binding.prompt_bytes),
                "context_bytes": len(binding.context_bytes),
                "canonical_input_bytes": canonical_input_bytes,
                "limit_bytes": MAX_CANONICAL_INPUT_BYTES,
                "overage_bytes": canonical_input_bytes - MAX_CANONICAL_INPUT_BYTES,
                "prompt_sha256": hashlib.sha256(binding.prompt_bytes).hexdigest(),
                "context_sha256": hashlib.sha256(binding.context_bytes).hexdigest(),
            },
        )
    if (
        settings.max_tokens != output_token_limit
        or not MAX_OUTPUT_TOKENS <= output_token_limit <= 12_288
    ):
        raise ProviderInferenceContractError(
            "OUTPUT_TOKEN_CONTRACT_MISMATCH", {
                "binding_max_tokens": settings.max_tokens,
                "output_token_limit": output_token_limit,
                "minimum_output_token_limit": MAX_OUTPUT_TOKENS,
                "maximum_output_token_limit": 12_288,
            },
        )
    if settings.temperature is not None or settings.seed is not None:
        raise ProviderInferenceContractError(
            "TEMPERATURE_OR_SEED_CONTRACT_MISMATCH", {
                "temperature": settings.temperature,
                "seed": settings.seed,
            },
        )
    if settings.max_retries != 0 or settings.retry_on:
        raise ProviderInferenceContractError(
            "RETRY_POLICY_CONTRACT_MISMATCH", {
                "max_retries": settings.max_retries,
                "retry_on": list(settings.retry_on),
            },
        )
    if settings.max_requests != 1:
        raise ProviderInferenceContractError(
            "PHYSICAL_REQUEST_CONTRACT_MISMATCH", {
                "max_requests": settings.max_requests,
                "required_max_requests": 1,
            },
        )
    system = binding.prompt_bytes.decode("utf-8")
    user = binding.context_bytes.decode("utf-8")
    if profile.provider == "anthropic":
        return {
            "model": profile.api_model_id,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": output_token_limit,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "stream": False,
        }
    body: dict[str, Any] = {
        "model": profile.api_model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "response_format": {"type": "json_object"},
        "reasoning_effort": "high",
    }
    body["max_completion_tokens" if profile.provider == "openai" else "max_tokens"] = output_token_limit
    if profile.provider == "openai" and explicit_no_prompt_cache:
        # WP-016 infrastructure normalization: explicit mode with no cache
        # breakpoints disables implicit provider prompt-cache reads/writes.
        body["prompt_cache_options"] = {"mode": "explicit"}
        body["service_tier"] = "default"
    if profile.provider == "deepseek":
        body["thinking"] = {"type": "enabled"}
    if profile.provider == "openrouter":
        if not pinned_endpoint_tag:
            raise ProviderBindingError("openrouter_pinned_endpoint_required")
        body["provider"] = {
            "only": [pinned_endpoint_tag],
            "order": [pinned_endpoint_tag],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
        }
        body["plugins"] = [
            {"id": "response-healing", "enabled": False},
            {"id": "context-compression", "enabled": False},
        ]
    return body


def _native_usage(profile: ProviderProfile, payload: Mapping[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise ProviderTransportError("usage_incomplete:usage")
    if profile.provider == "openai":
        prompt = _required_int(usage.get("prompt_tokens"), "prompt_tokens")
        completion = _required_int(usage.get("completion_tokens"), "completion_tokens")
        details = usage.get("prompt_tokens_details")
        if not isinstance(details, Mapping):
            raise ProviderTransportError("usage_incomplete:prompt_tokens_details")
        cached = _required_int(details.get("cached_tokens"), "cached_tokens")
        cache_write_present = "cache_write_tokens" in details or "cache_write_tokens" in usage
        cache_write = _required_int(
            details.get("cache_write_tokens", usage.get("cache_write_tokens", 0)),
            "cache_write_tokens",
        )
        if cached > prompt:
            raise ProviderTransportError("usage_invalid:cached_gt_prompt")
        if cache_write > prompt:
            raise ProviderTransportError("usage_invalid:cache_write_gt_prompt")
        completion_details = usage.get("completion_tokens_details")
        reasoning = _optional_int(
            completion_details.get("reasoning_tokens") if isinstance(completion_details, Mapping) else None,
            "reasoning_tokens",
        )
        return {
            "input_tokens": prompt,
            "uncached_input_tokens": prompt - cached,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write,
            "cache_write_basis": (
                "provider_native_usage" if cache_write_present
                else "absent_interpreted_zero_under_explicit_no_breakpoint_policy"
            ),
            "output_tokens": completion,
            "reasoning_tokens": reasoning,
        }
    if profile.provider == "anthropic":
        direct_input = _required_int(usage.get("input_tokens"), "input_tokens")
        output = _required_int(usage.get("output_tokens"), "output_tokens")
        cache_create = _required_int(usage.get("cache_creation_input_tokens", 0), "cache_creation_input_tokens")
        cache_read = _required_int(usage.get("cache_read_input_tokens", 0), "cache_read_input_tokens")
        cache = usage.get("cache_creation")
        cache_5m = _required_int(cache.get("ephemeral_5m_input_tokens", cache_create) if isinstance(cache, Mapping) else cache_create, "cache_write_5m_tokens")
        cache_1h = _required_int(cache.get("ephemeral_1h_input_tokens", 0) if isinstance(cache, Mapping) else 0, "cache_write_1h_tokens")
        if cache_5m + cache_1h != cache_create:
            raise ProviderTransportError("usage_invalid:anthropic_cache_split")
        return {
            "input_tokens": direct_input + cache_create + cache_read,
            "uncached_input_tokens": direct_input,
            "cache_write_5m_tokens": cache_5m,
            "cache_write_1h_tokens": cache_1h,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output,
            "reasoning_tokens": None,
        }
    if profile.provider == "deepseek":
        hit = _required_int(usage.get("prompt_cache_hit_tokens"), "prompt_cache_hit_tokens")
        miss = _required_int(usage.get("prompt_cache_miss_tokens"), "prompt_cache_miss_tokens")
        output = _required_int(usage.get("completion_tokens"), "completion_tokens")
        return {
            "input_tokens": hit + miss,
            "cache_hit_input_tokens": hit,
            "cache_miss_input_tokens": miss,
            "output_tokens": output,
            "reasoning_tokens": _optional_int(usage.get("reasoning_tokens"), "reasoning_tokens"),
        }
    prompt = _required_int(usage.get("prompt_tokens"), "prompt_tokens")
    output = _required_int(usage.get("completion_tokens"), "completion_tokens")
    cost = usage.get("cost")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or float(cost) < 0:
        raise ProviderTransportError("usage_incomplete:openrouter_cost")
    return {
        "input_tokens": prompt,
        "cached_input_tokens": _optional_int(usage.get("cached_tokens"), "cached_tokens"),
        "output_tokens": output,
        "reasoning_tokens": _optional_int(usage.get("reasoning_tokens"), "reasoning_tokens"),
        "provider_reported_cost_usd": float(cost),
    }


def derive_cost_usd(
    canonical_model_id: str,
    usage: Mapping[str, Any],
    pricing: Mapping[str, Any] | None = None,
) -> Decimal:
    snapshot = load_pricing() if pricing is None else dict(pricing)
    row = snapshot["models"][canonical_model_id]
    million = Decimal(1_000_000)
    d = lambda name: Decimal(str(row[name]))  # noqa: E731
    n = lambda name: Decimal(int(usage[name]))  # noqa: E731
    provider = PROFILES[canonical_model_id].provider
    if provider == "openai":
        input_multiplier = output_multiplier = Decimal(1)
        if int(usage["input_tokens"]) > int(row["long_context_threshold_tokens"]):
            input_multiplier = Decimal(str(row["long_context_input_multiplier"]))
            output_multiplier = Decimal(str(row["long_context_output_multiplier"]))
        total = (
            n("uncached_input_tokens") * d("input")
            + n("cached_input_tokens") * d("cached_input")
            + n("cache_write_input_tokens") * d("cache_write")
        ) * input_multiplier + n("output_tokens") * d("output") * output_multiplier
    elif provider == "anthropic":
        total = (
            n("uncached_input_tokens") * d("input")
            + n("cache_write_5m_tokens") * d("cache_write_5m")
            + n("cache_write_1h_tokens") * d("cache_write_1h")
            + n("cache_read_input_tokens") * d("cache_read")
            + n("output_tokens") * d("output")
        )
    elif provider == "deepseek":
        total = (
            n("cache_hit_input_tokens") * d("cache_hit_input")
            + n("cache_miss_input_tokens") * d("cache_miss_input")
            + n("output_tokens") * d("output")
        )
    else:
        total = n("input_tokens") * d("list_input") + n("output_tokens") * d("list_output")
    return total / million


def _headers(profile: ProviderProfile, api_key: str,
             client_request_id: str | None = None) -> dict[str, str]:
    if profile.provider == "anthropic":
        return {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    return {
        "content-type": "application/json",
        "authorization": "Bearer " + api_key,
        **({"X-Client-Request-Id": client_request_id}
           if profile.provider == "openai" and client_request_id is not None else {}),
        **({"x-openrouter-metadata": "enabled", "x-openrouter-cache": "false"} if profile.provider == "openrouter" else {}),
    }


def _extract_final_text(profile: ProviderProfile, payload: Mapping[str, Any]) -> bytes:
    if profile.provider == "anthropic":
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise ProviderTransportError("anthropic_content_shape_invalid")
        texts = [
            row.get("text") for row in blocks
            if isinstance(row, Mapping) and row.get("type") == "text"
        ]
        if any(not isinstance(value, str) for value in texts):
            raise ProviderTransportError("anthropic_text_block_invalid")
        # A valid HTTP/provider response may spend its output budget entirely
        # on private reasoning and return no final text.  That is a model-output
        # terminal for the planner, not a transport failure.
        return "".join(texts).encode("utf-8")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        raise ProviderTransportError(f"{profile.provider}_choice_shape_invalid")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise ProviderTransportError(f"{profile.provider}_message_shape_invalid")
    content = message.get("content")
    if content is None:
        return b""
    if not isinstance(content, str):
        raise ProviderTransportError(f"{profile.provider}_final_text_invalid")
    return content.encode("utf-8")


def _finish_reason(profile: ProviderProfile, payload: Mapping[str, Any]) -> str | None:
    if profile.provider == "anthropic":
        value = payload.get("stop_reason")
    else:
        choices = payload.get("choices")
        value = choices[0].get("finish_reason") if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], Mapping) else None
    if value is not None and not isinstance(value, str):
        raise ProviderTransportError(f"{profile.provider}_finish_reason_invalid")
    return value


def _final_answer_tokens(profile: ProviderProfile, usage: Mapping[str, Any]) -> int | None:
    output = int(usage["output_tokens"])
    reasoning = usage.get("reasoning_tokens")
    if profile.provider in {"openai", "deepseek", "openrouter"} and isinstance(reasoning, int):
        return max(0, output - reasoning)
    # Anthropic does not expose a stable split between adaptive-thinking and
    # visible text tokens in the frozen response contract.
    return None


def parse_offline_provider_envelope(
    profile: ProviderProfile,
    raw: bytes,
    *,
    request_body: Mapping[str, Any],
    expected_service_tier: str | None = None,
    response_headers: Mapping[str, str] | None = None,
    client_request_id: str | None = None,
    require_openai_x_request_id: bool = False,
) -> tuple[RawProviderResponse, dict[str, Any]]:
    """Parse a canned response through the frozen provider response contract.

    This is deliberately transport-free.  It shares the strict JSON, model,
    usage, final-text, finish-reason and hash rules used by ``send`` so offline
    controller evidence cannot bypass the real adapter boundary.
    """

    payload = _strict_object(raw, profile.provider + "_response")
    returned_model = payload.get("model")
    if returned_model != profile.api_model_id:
        raise ProviderTransportError("returned_model_id_mismatch")
    usage = _native_usage(profile, payload)
    content = _extract_final_text(profile, payload)
    finish_reason = _finish_reason(profile, payload)
    response_object_id = _optional_str(payload.get("id"), "response_object_id")
    headers = {} if response_headers is None else dict(response_headers)
    openai_x_request_id = _optional_str(
        headers.get("x-request-id"), "openai_x_request_id",
    ) if profile.provider == "openai" else None
    if profile.provider == "openai" and require_openai_x_request_id and openai_x_request_id is None:
        raise ProviderTransportError("NO_GO_OPENAI_X_REQUEST_ID_MISSING")
    returned_service_tier = _optional_str(
        payload.get("service_tier"), "returned_service_tier",
    ) if profile.provider == "openai" else None
    system_fingerprint = _optional_str(
        payload.get("system_fingerprint"), "system_fingerprint",
    ) if profile.provider == "openai" else None
    if profile.provider == "openai" and expected_service_tier is not None:
        if returned_service_tier != expected_service_tier:
            raise ProviderTransportError("NO_GO_OPENAI_SERVICE_TIER_MISMATCH")
    final_text_truncated = finish_reason in {"length", "max_tokens", "MAX_TOKENS"}
    derived_cost = derive_cost_usd(profile.canonical_model_id, usage)
    provider_cost = usage.get("provider_reported_cost_usd")
    accounting_cost = Decimal(str(provider_cost)) if provider_cost is not None else derived_cost
    exchange = {
        "schema_version": PROVIDER_EXCHANGE_SCHEMA,
        "offline_canned": True,
        "canonical_provider": profile.provider,
        "provider_route": profile.route,
        "canonical_model_id": profile.canonical_model_id,
        "requested_api_model_id": profile.api_model_id,
        "returned_api_model_id": returned_model,
        "provider_request_id": openai_x_request_id or response_object_id,
        "client_request_id": client_request_id,
        "openai_x_request_id": openai_x_request_id,
        "response_object_id": response_object_id,
        "returned_service_tier": returned_service_tier,
        "system_fingerprint": system_fingerprint,
        "request_body_sha256": _hash(request_body),
        "response_sha256": _hash(raw),
        "final_text_sha256": _hash(content),
        "final_text_present": bool(content),
        "finish_reason": finish_reason,
        "final_text_truncated": final_text_truncated,
        "silent_truncation_detected": finish_reason is None,
        "native_usage": usage,
        "provider_reported_cost_usd": provider_cost,
        "derived_cost_usd": str(derived_cost),
        "cost_source": "offline_canned_not_billed",
        "pricing_snapshot_id": load_pricing()["snapshot_id"],
        "pricing_snapshot_sha256": _hash(PRICING_PATH.read_bytes()),
        "final_answer_tokens": _final_answer_tokens(profile, usage),
        "retry_count": 0,
        "fallback_count": 0,
        "repair_count": 0,
        "provider_calls": 0,
    }
    total_input, total_output = int(usage["input_tokens"]), int(usage["output_tokens"])
    response = RawProviderResponse(
        content_bytes=content,
        actual_provider=profile.provider,
        provider_request_id=openai_x_request_id or response_object_id,
        returned_model_id=profile.canonical_model_id,
        returned_model_revision=profile.provider + "_direct",
        input_tokens=total_input,
        output_tokens=total_output,
        total_tokens=total_input + total_output,
        cost_usd=float(accounting_cost),
        cost_source="offline_canned_not_billed",
        observed_request_contract_hash=None,
    )
    return response, exchange


def _http_error_category(status: int) -> str:
    if status in {401, 403}:
        return "AUTHENTICATION_OR_PERMISSION"
    if status == 402:
        return "PAYMENT_OR_BUDGET"
    if status == 429:
        return "RATE_LIMIT"
    if 500 <= status:
        return "UPSTREAM_OR_SERVER"
    return "REQUEST_CONTRACT_OR_OTHER"


def reject_ambient_provider_overrides(environment: Mapping[str, str] | None = None) -> None:
    source = os.environ if environment is None else environment
    forbidden = (
        "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "DEEPSEEK_BASE_URL",
        "OPENROUTER_BASE_URL", "OPENAI_MODEL", "ANTHROPIC_MODEL",
        "DEEPSEEK_MODEL", "OPENROUTER_MODEL",
    )
    present = [key for key in forbidden if key in source]
    if present:
        raise ProviderBindingError("ambient_provider_override_forbidden:" + ",".join(present))


class StrictMultiProviderTransport:
    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True

    def __init__(
        self,
        *,
        profile: ProviderProfile,
        api_key: str,
        pinned_endpoint_tag: str | None = None,
        output_token_limit: int = MAX_OUTPUT_TOKENS,
        explicit_no_prompt_cache: bool = False,
        client_request_id: str | None = None,
        expected_service_tier: str | None = None,
        require_openai_x_request_id: bool = False,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not api_key:
            raise ProviderBindingError("provider_credential_unavailable")
        reject_ambient_provider_overrides()
        self.profile = profile
        self.api_key = api_key
        self.pinned_endpoint_tag = pinned_endpoint_tag
        if not MAX_OUTPUT_TOKENS <= output_token_limit <= 12_288:
            raise ProviderBindingError("provider_output_token_limit_invalid")
        self.output_token_limit = output_token_limit
        self.explicit_no_prompt_cache = explicit_no_prompt_cache
        if client_request_id is not None:
            try:
                encoded_client_id = client_request_id.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ProviderBindingError("client_request_id_not_ascii") from exc
            if not encoded_client_id or len(encoded_client_id) > 512:
                raise ProviderBindingError("client_request_id_length_invalid")
        if require_openai_x_request_id and profile.provider != "openai":
            raise ProviderBindingError("openai_request_id_policy_on_non_openai_profile")
        self.client_request_id = client_request_id
        self.expected_service_tier = expected_service_tier
        self.require_openai_x_request_id = require_openai_x_request_id
        self.client_factory = client_factory
        self.send_attempts = 0
        self.physical_requests = 0
        self.last_exchange: dict[str, Any] | None = None

    async def _observe(self, _request: Any) -> None:
        self.physical_requests += 1
        if self.physical_requests > 1:
            raise ProviderBindingError("single_physical_request_transport_exhausted")

    async def send(self, binding: ProviderRequestBinding, retry_index: int) -> RawProviderResponse:
        if retry_index != 0 or self.send_attempts != 0:
            raise ProviderBindingError("single_physical_request_transport_exhausted")
        self.send_attempts += 1
        body = compile_request_body(
            self.profile, binding, pinned_endpoint_tag=self.pinned_endpoint_tag,
            output_token_limit=self.output_token_limit,
            explicit_no_prompt_cache=self.explicit_no_prompt_cache,
        )
        if self.client_factory is None:
            import httpx
            factory = httpx.AsyncClient
        else:
            factory = self.client_factory
        client = factory(
            base_url=self.profile.base_url,
            timeout=binding.settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            event_hooks={"request": [self._observe]},
        )
        self.last_exchange = {
            "schema_version": PROVIDER_EXCHANGE_SCHEMA,
            "canonical_provider": self.profile.provider,
            "provider_route": self.profile.route,
            "canonical_model_id": self.profile.canonical_model_id,
            "client_request_id": self.client_request_id,
            "openai_x_request_id": None,
            "response_object_id": None,
            "request_body_sha256": _hash(body),
            "physical_requests": 0,
            "parser_status": "DISPATCHING_NO_RESPONSE",
            "retry_count": 0,
            "fallback_count": 0,
            "repair_count": 0,
            "raw_reasoning_persisted": False,
        }
        try:
            response = await client.post(
                self.profile.endpoint,
                headers=_headers(self.profile, self.api_key, self.client_request_id),
                json=body,
            )
            raw = bytes(response.content)
            status = int(response.status_code)
        except Exception as exc:
            assert self.last_exchange is not None
            self.last_exchange["physical_requests"] = self.physical_requests
            self.last_exchange["parser_status"] = "TRANSPORT_FAILURE_NO_RESPONSE"
            self.last_exchange["failure_class"] = type(exc).__name__
            raise ProviderTransportError(
                f"{self.profile.provider}_transport:{type(exc).__name__}"
            ) from exc
        finally:
            await client.aclose()
        if self.physical_requests != 1:
            raise ProviderTransportError("physical_request_count_not_one")
        # Preserve safe response metadata before any HTTP/provider/parser
        # validation can fail.  This lets the caller durably journal a billed
        # response even when the strict adapter rejects it afterwards.
        raw_openai_x_request_id = (
            response.headers.get("x-request-id")
            if self.profile.provider == "openai" else None
        )
        openai_x_request_id = (
            raw_openai_x_request_id
            if isinstance(raw_openai_x_request_id, str) and raw_openai_x_request_id
            else None
        )
        self.last_exchange = {
            "schema_version": PROVIDER_EXCHANGE_SCHEMA,
            "canonical_provider": self.profile.provider,
            "provider_route": self.profile.route,
            "canonical_model_id": self.profile.canonical_model_id,
            "request_contract_hash": binding.request_contract_hash,
            "prompt_sha256": binding.prompt_hash,
            "context_sha256": binding.context_hash,
            "request_body_sha256": _hash(body),
            "client_request_id": self.client_request_id,
            "openai_x_request_id": openai_x_request_id,
            "response_object_id": None,
            "http_status": status,
            "response_sha256": _hash(raw),
            "physical_requests": self.physical_requests,
            "parser_status": "RAW_RESPONSE_RECEIVED",
            "retry_count": 0,
            "fallback_count": 0,
            "repair_count": 0,
            "raw_reasoning_persisted": False,
        }
        if (
            self.profile.provider == "openai"
            and self.require_openai_x_request_id
            and openai_x_request_id is None
        ):
            self.last_exchange["parser_status"] = "OPENAI_X_REQUEST_ID_MISSING"
            raise ProviderTransportError("NO_GO_OPENAI_X_REQUEST_ID_MISSING")
        if not 200 <= status < 300:
            self.last_exchange["parser_status"] = "HTTP_REJECTED"
            raise ProviderTransportError(
                f"{self.profile.provider}_http_{status}:" + _http_error_category(status)
            )
        try:
            payload = _strict_object(raw, self.profile.provider + "_response")
            returned_model = payload.get("model")
            if returned_model != self.profile.api_model_id:
                raise ProviderTransportError("returned_model_id_mismatch")
            if self.profile.provider == "openrouter":
                returned_upstream = (
                    response.headers.get("x-openrouter-provider")
                    or payload.get("provider")
                )
                if returned_upstream != self.profile.required_upstream_provider:
                    raise ProviderTransportError("returned_upstream_provider_mismatch")
            else:
                returned_upstream = self.profile.provider + "_direct"
            usage = _native_usage(self.profile, payload)
            derived_cost = derive_cost_usd(self.profile.canonical_model_id, usage)
            provider_cost = usage.get("provider_reported_cost_usd")
            content = _extract_final_text(self.profile, payload)
            finish_reason = _finish_reason(self.profile, payload)
            truncation_reasons = {"length", "max_tokens", "MAX_TOKENS"}
            final_text_truncated = finish_reason in truncation_reasons
            final_answer_tokens = _final_answer_tokens(self.profile, usage)
            response_object_id = _optional_str(payload.get("id"), "response_object_id")
            returned_service_tier = _optional_str(
                payload.get("service_tier"), "returned_service_tier",
            ) if self.profile.provider == "openai" else None
            system_fingerprint = _optional_str(
                payload.get("system_fingerprint"), "system_fingerprint",
            ) if self.profile.provider == "openai" else None
            if self.profile.provider == "openai" and self.expected_service_tier is not None:
                if returned_service_tier != self.expected_service_tier:
                    raise ProviderTransportError("NO_GO_OPENAI_SERVICE_TIER_MISMATCH")
        except Exception as exc:
            assert self.last_exchange is not None
            self.last_exchange["parser_status"] = "STRICT_ADAPTER_REJECTED"
            self.last_exchange["failure_class"] = type(exc).__name__
            raise
        self.last_exchange = {
            "schema_version": PROVIDER_EXCHANGE_SCHEMA,
            "canonical_provider": self.profile.provider,
            "provider_route": self.profile.route,
            "canonical_model_id": self.profile.canonical_model_id,
            "requested_api_model_id": self.profile.api_model_id,
            "returned_api_model_id": returned_model,
            "returned_upstream_provider": returned_upstream,
            "provider_request_id": openai_x_request_id or response_object_id,
            "client_request_id": self.client_request_id,
            "openai_x_request_id": openai_x_request_id,
            "response_object_id": response_object_id,
            "returned_service_tier": returned_service_tier,
            "system_fingerprint": system_fingerprint,
            "request_contract_hash": binding.request_contract_hash,
            "prompt_sha256": binding.prompt_hash,
            "context_sha256": binding.context_hash,
            "request_body_sha256": _hash(body),
            "response_sha256": _hash(raw),
            "final_text_sha256": _hash(content),
            "final_text_present": bool(content),
            "finish_reason": finish_reason,
            "final_text_truncated": final_text_truncated,
            "silent_truncation_detected": finish_reason is None,
            "configured_total_output_token_ceiling": self.output_token_limit,
            "reserved_final_answer_capacity_tokens": MAX_OUTPUT_TOKENS,
            "final_answer_tokens": final_answer_tokens,
            "physical_requests": self.physical_requests,
            "http_status": status,
            "parser_status": "PASS",
            "retry_count": 0,
            "fallback_count": 0,
            "repair_count": 0,
            "native_usage": usage,
            "provider_reported_cost_usd": provider_cost,
            "derived_cost_usd": str(derived_cost),
            "pricing_snapshot_id": load_pricing()["snapshot_id"],
            "pricing_snapshot_sha256": _hash(PRICING_PATH.read_bytes()),
            "cost_formula_version": COST_FORMULA_VERSION,
            "billing_reconciliation_status": "UNRECONCILED_NO_ADMIN_CREDENTIAL",
            "raw_reasoning_persisted": False,
        }
        total_input = int(usage["input_tokens"])
        total_output = int(usage["output_tokens"])
        accounting_cost = Decimal(str(provider_cost)) if provider_cost is not None else derived_cost
        return RawProviderResponse(
            content_bytes=content,
            actual_provider=self.profile.provider,
            provider_request_id=openai_x_request_id or response_object_id,
            returned_model_id=self.profile.canonical_model_id,
            returned_model_revision=returned_upstream,
            input_tokens=total_input,
            output_tokens=total_output,
            total_tokens=total_input + total_output,
            cost_usd=float(accounting_cost),
            cost_source="provider_reported" if provider_cost is not None else "derived_frozen_pricing",
            observed_request_contract_hash=binding.request_contract_hash,
        )


async def discover_openrouter_endpoint(
    api_key: str,
    *,
    canonical_model_id: str = "google/gemini-3.6-flash",
    required_provider: str = "Google",
    required_endpoint_tag: str = "google-vertex/global",
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Perform one non-inference metadata request and freeze one exact route."""

    profile = PROFILES[canonical_model_id]
    if profile.provider != "openrouter":
        raise ProviderBindingError("openrouter_profile_required")
    if client_factory is None:
        import httpx
        factory = httpx.AsyncClient
    else:
        factory = client_factory
    count = 0

    async def observe(_request: Any) -> None:
        nonlocal count
        count += 1
        if count > 1:
            raise ProviderBindingError("openrouter_discovery_request_cap_exceeded")

    client = factory(
        base_url=profile.base_url, timeout=60.0, follow_redirects=False,
        trust_env=False, event_hooks={"request": [observe]},
    )
    try:
        response = await client.get(
            f"/models/{profile.api_model_id}/endpoints",
            headers={"authorization": "Bearer " + api_key},
        )
        raw = bytes(response.content)
        status = int(response.status_code)
    finally:
        await client.aclose()
    if count != 1 or not 200 <= status < 300:
        raise ProviderTransportError("openrouter_endpoint_discovery_failed")
    payload = _strict_object(raw, "openrouter_endpoint_discovery")
    data = payload.get("data")
    endpoints = data.get("endpoints") if isinstance(data, Mapping) else data
    if not isinstance(endpoints, list):
        raise ProviderTransportError("openrouter_endpoint_discovery_shape_invalid")
    required = {"max_tokens", "response_format", "reasoning_effort"}
    matching = [
        row for row in endpoints
        if isinstance(row, Mapping)
        and row.get("provider_name") == required_provider
        and row.get("tag") == required_endpoint_tag
        and required <= set(row.get("supported_parameters", []))
        and isinstance(row.get("tag"), str)
    ]
    if len(matching) != 1:
        raise ProviderTransportError("openrouter_exact_endpoint_not_unique")
    row = matching[0]
    return {
        "schema_version": "dof.wp014h-openrouter-endpoint-freeze.v1",
        "canonical_model_id": profile.canonical_model_id,
        "upstream_provider": required_provider,
        "endpoint_tag": row["tag"],
        "supported_parameters": sorted(set(row.get("supported_parameters", []))),
        "context_length": row.get("context_length"),
        "max_completion_tokens": row.get("max_completion_tokens"),
        "metadata_response_sha256": _hash(raw),
        "physical_metadata_requests": count,
        "fallback_allowed": False,
    }


__all__ = [
    "COST_FORMULA_VERSION", "MAX_CANONICAL_INPUT_BYTES", "MAX_OUTPUT_TOKENS",
    "PROFILES", "PROVIDER_EXCHANGE_SCHEMA", "ProviderInferenceContractError",
    "ProviderProfile",
    "parse_offline_provider_envelope",
    "StrictMultiProviderTransport", "compile_request_body",
    "derive_cost_usd", "discover_openrouter_endpoint", "load_pricing",
    "load_profiles", "reject_ambient_provider_overrides",
]
