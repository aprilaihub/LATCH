"""Strict first-party Google Gemini transport for WP-015.

One transport instance can issue exactly one physical ``generateContent``
request.  It has no SDK, retry, fallback, response-repair, or reasoning-content
persistence path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .provider import (
    PROVIDER_TRANSPORT_CONTRACT_VERSION,
    ProviderBindingError,
    ProviderRequestBinding,
    ProviderTransportError,
    RawProviderResponse,
)


CANONICAL_MODEL_ID = "google/gemini-3.6-flash"
API_MODEL_ID = "gemini-3.6-flash"
PROVIDER = "google_gemini"
ADAPTER_VERSION = "dof.google-generate-content.v1"
BASE_URL = "https://generativelanguage.googleapis.com"
ENDPOINT = f"/v1beta/models/{API_MODEL_ID}:generateContent"
MAX_INPUT_BYTES = 65_536
MAX_OUTPUT_TOKENS = 4_096
EXCHANGE_SCHEMA = "dof.wp015-gemini-direct-exchange.v1"
PRICING_PATH = Path(__file__).parents[2] / ".research-flow/source/WP015-Pricing-Snapshot-2026-08-14.json"


# The language contract remains authoritative.  This outer schema guarantees a
# JSON object with the protocol discriminator while leaving the candidate body
# to the frozen parser/compiler, which provides the stricter closed-world gate.
ACTION_RESPONSE_JSON_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["QUERY", "COMMIT", "NO_VALID_SUBMISSION"],
        },
        "candidate": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": ["M", "P", "G"]},
                "action_key": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string"},
                            "op": {"type": "string"},
                            "bind": {"type": "string"},
                            "args": {"type": "object", "additionalProperties": True},
                        },
                        "required": ["step_id", "op", "args"],
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["language"],
            "additionalProperties": True,
        },
        "queries": {"type": "array", "items": {"type": "object"}},
        "reason": {"type": "string"},
    },
    "required": ["kind"],
    "additionalProperties": True,
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: bytes | Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _strict_object(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ProviderTransportError("google_gemini_response_duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderTransportError("google_gemini_response_invalid_json") from exc
    if not isinstance(value, dict):
        raise ProviderTransportError("google_gemini_response_not_object")
    return value


def _required_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderTransportError(f"usage_incomplete:{label}")
    return value


def compile_request_body(
    binding: ProviderRequestBinding, *, output_token_limit: int = MAX_OUTPUT_TOKENS,
) -> dict[str, Any]:
    settings = binding.settings
    if (
        binding.provider != PROVIDER
        or binding.model_id != CANONICAL_MODEL_ID
        or binding.adapter_version != ADAPTER_VERSION
    ):
        raise ProviderBindingError("gemini_direct_binding_mismatch")
    if (
        len(binding.prompt_bytes) + len(binding.context_bytes) > MAX_INPUT_BYTES
        or settings.max_tokens != output_token_limit
        or not MAX_OUTPUT_TOKENS <= output_token_limit <= 12_288
        or settings.temperature is not None
        or settings.seed is not None
        or settings.max_retries != 0
        or settings.retry_on
        or settings.max_requests != 1
    ):
        raise ProviderBindingError("gemini_direct_inference_contract_mismatch")
    return {
        "systemInstruction": {
            "parts": [{"text": binding.prompt_bytes.decode("utf-8")}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": binding.context_bytes.decode("utf-8")}],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": output_token_limit,
            "responseMimeType": "application/json",
            "responseJsonSchema": dict(ACTION_RESPONSE_JSON_SCHEMA),
            "thinkingConfig": {"thinkingLevel": "HIGH"},
        },
    }


def _usage(payload: Mapping[str, Any]) -> dict[str, int]:
    value = payload.get("usageMetadata")
    if not isinstance(value, Mapping):
        raise ProviderTransportError("usage_incomplete:usageMetadata")
    prompt = _required_int(value.get("promptTokenCount"), "promptTokenCount")
    cached = _required_int(value.get("cachedContentTokenCount", 0), "cachedContentTokenCount")
    candidates = _required_int(value.get("candidatesTokenCount", 0), "candidatesTokenCount")
    thoughts = _required_int(value.get("thoughtsTokenCount", 0), "thoughtsTokenCount")
    provider_total = _required_int(value.get("totalTokenCount"), "totalTokenCount")
    if cached > prompt:
        raise ProviderTransportError("usage_invalid:cached_gt_prompt")
    accounted_total = prompt + candidates + thoughts
    if provider_total < accounted_total:
        raise ProviderTransportError("usage_invalid:provider_total_lt_accounted")
    return {
        "input_tokens": prompt,
        "cached_input_tokens": cached,
        "uncached_input_tokens": prompt - cached,
        "candidate_output_tokens": candidates,
        "thought_tokens": thoughts,
        "output_tokens": candidates + thoughts,
        "provider_total_tokens": provider_total,
        "unclassified_tokens": provider_total - accounted_total,
    }


def _final_text(payload: Mapping[str, Any]) -> bytes:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], Mapping):
        raise ProviderTransportError("google_gemini_candidate_shape_invalid")
    content = candidates[0].get("content")
    if not isinstance(content, Mapping):
        raise ProviderTransportError("google_gemini_content_shape_invalid")
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise ProviderTransportError("google_gemini_parts_shape_invalid")
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            raise ProviderTransportError("google_gemini_part_invalid")
        if part.get("thought") is True:
            continue
        text = part.get("text")
        if text is not None:
            if not isinstance(text, str):
                raise ProviderTransportError("google_gemini_final_text_invalid")
            texts.append(text)
    return "".join(texts).encode("utf-8")


def _cost(usage: Mapping[str, int]) -> Decimal:
    snapshot = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
    if snapshot.get("snapshot_id") != "WP015_PRICING_2026-08-14":
        raise ProviderBindingError("wp015_pricing_snapshot_mismatch")
    prices = snapshot["providers"]["google_gemini"]["per_million_tokens"][API_MODEL_ID]
    return (
        Decimal(usage["input_tokens"]) * Decimal(str(prices["input"]))
        + Decimal(usage["output_tokens"]) * Decimal(str(prices["output_including_thought_tokens"]))
    ) / Decimal(1_000_000)


def _error_category(status: int) -> str:
    if status in {401, 403}:
        return "AUTHENTICATION_OR_PERMISSION"
    if status == 429:
        return "RATE_LIMIT"
    if 500 <= status:
        return "UPSTREAM_OR_SERVER"
    return "REQUEST_CONTRACT_OR_OTHER"


@dataclass(frozen=True, slots=True)
class GeminiDirectProfile:
    canonical_model_id: str = CANONICAL_MODEL_ID
    api_model_id: str = API_MODEL_ID
    provider: str = PROVIDER
    route: str = "GOOGLE_GEMINI_DIRECT"
    base_url: str = BASE_URL
    endpoint: str = ENDPOINT
    credential_env: str = "GEMINI_API_KEY"
    adapter_version: str = ADAPTER_VERSION


PROFILE = GeminiDirectProfile()


class StrictGeminiDirectTransport:
    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True

    def __init__(
        self,
        *,
        api_key: str,
        output_token_limit: int = MAX_OUTPUT_TOKENS,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not api_key:
            raise ProviderBindingError("provider_credential_unavailable")
        self.api_key = api_key
        if not MAX_OUTPUT_TOKENS <= output_token_limit <= 12_288:
            raise ProviderBindingError("google_gemini_output_token_limit_invalid")
        self.output_token_limit = output_token_limit
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
        body = compile_request_body(binding, output_token_limit=self.output_token_limit)
        if self.client_factory is None:
            import httpx
            factory = httpx.AsyncClient
        else:
            factory = self.client_factory
        client = factory(
            base_url=BASE_URL,
            timeout=binding.settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            event_hooks={"request": [self._observe]},
        )
        try:
            response = await client.post(
                ENDPOINT,
                headers={"content-type": "application/json", "x-goog-api-key": self.api_key},
                json=body,
            )
            raw = bytes(response.content)
            status = int(response.status_code)
        except Exception as exc:
            raise ProviderTransportError(f"google_gemini_transport:{type(exc).__name__}") from exc
        finally:
            await client.aclose()
        if self.physical_requests != 1:
            raise ProviderTransportError("physical_request_count_not_one")
        if not 200 <= status < 300:
            raise ProviderTransportError(f"google_gemini_http_{status}:{_error_category(status)}")
        payload = _strict_object(raw)
        returned_model = payload.get("modelVersion")
        if returned_model != API_MODEL_ID:
            raise ProviderTransportError("returned_model_id_mismatch")
        request_id = payload.get("responseId") or response.headers.get("x-request-id")
        if request_id is not None and not isinstance(request_id, str):
            raise ProviderTransportError("provider_request_id_invalid")
        usage = _usage(payload)
        cost = _cost(usage)
        content = _final_text(payload)
        candidates = payload.get("candidates")
        finish_reason = candidates[0].get("finishReason") if isinstance(candidates, list) and len(candidates) == 1 and isinstance(candidates[0], Mapping) else None
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ProviderTransportError("google_gemini_finish_reason_invalid")
        final_text_truncated = finish_reason in {"MAX_TOKENS", "length", "max_tokens"}
        self.last_exchange = {
            "schema_version": EXCHANGE_SCHEMA,
            "canonical_provider": PROVIDER,
            "provider_route": PROFILE.route,
            "canonical_model_id": CANONICAL_MODEL_ID,
            "requested_api_model_id": API_MODEL_ID,
            "returned_api_model_id": returned_model,
            "provider_request_id": request_id,
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
            "final_answer_tokens": usage["candidate_output_tokens"],
            "physical_requests": self.physical_requests,
            "retry_count": 0,
            "fallback_count": 0,
            "repair_count": 0,
            "native_usage": usage,
            "derived_cost_usd": str(cost),
            "pricing_snapshot_id": "WP015_PRICING_2026-08-14",
            "billing_reconciliation_status": "UNRECONCILED_NO_ADMIN_CREDENTIAL",
            "raw_reasoning_persisted": False,
        }
        return RawProviderResponse(
            content_bytes=content,
            actual_provider=PROVIDER,
            provider_request_id=request_id,
            returned_model_id=CANONICAL_MODEL_ID,
            returned_model_revision=returned_model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            total_tokens=usage["input_tokens"] + usage["output_tokens"],
            cost_usd=float(cost),
            cost_source="derived_frozen_pricing",
            observed_request_contract_hash=binding.request_contract_hash,
        )
