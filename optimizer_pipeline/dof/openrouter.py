"""Strict OpenRouter transport and model-discovery boundary for E0/E1.

The adapter is intentionally small and provider-specific.  It emits one
non-streaming chat-completions request, performs no retry or repair, and keeps
the complete provider response as immutable evidence outside the canonical
policy-output bytes consumed by :mod:`optimizer_pipeline.dof.provider`.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
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


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CHAT_ENDPOINT = "/chat/completions"
OPENROUTER_GENERATION_ENDPOINT = "/generation"
OPENROUTER_MODELS_ENDPOINT = "/models"
OPENROUTER_ENDPOINTS_TEMPLATE = "/models/{model}/endpoints"
OPENROUTER_CREDENTIAL_ENV = "OPENROUTER_API_KEY"
OPENROUTER_E0_MODELS = (
    "openai/gpt-5.6-luna",
    "anthropic/claude-sonnet-5",
)
OPENROUTER_E1_MODELS = (
    "openai/gpt-5.6-luna",
    "google/gemini-3.6-flash",
    "anthropic/claude-sonnet-5",
)
OPENROUTER_E1_PROFILES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "openai/gpt-5.6-luna": MappingProxyType(
            {"endpoint_tag": "openai", "upstream_provider": "OpenAI"}
        ),
        "google/gemini-3.6-flash": MappingProxyType(
            {
                "endpoint_tag": "google-vertex/global",
                "upstream_provider": "Google",
            }
        ),
        "anthropic/claude-sonnet-5": MappingProxyType(
            {
                "endpoint_tag": "amazon-bedrock/claude-on-aws",
                "upstream_provider": "Amazon Bedrock",
            }
        ),
    }
)
OPENROUTER_ADAPTER_VERSION = "dof.openrouter-chat-completions.v3"
OPENROUTER_OUTBOUND_SCHEMA = "dof.openrouter-outbound.v1"
OPENROUTER_EXCHANGE_SCHEMA = "dof.openrouter-exchange.v1"
OPENROUTER_DISCOVERY_SCHEMA = "dof.openrouter-model-discovery.v1"
OPENROUTER_TRANSPORT_TERMINAL_SCHEMA = "dof.openrouter-transport-terminal.v1"
OPENROUTER_PROVIDER_ERROR_SCHEMA = "dof.openrouter-provider-error.v1"
OPENROUTER_PROVIDER_ERROR_CLASSIFIER_VERSION = "dof.openrouter-error-classifier.v1"
OPENROUTER_PROVIDER_ERROR_CATEGORIES = frozenset(
    {
        "AUTHENTICATION_OR_KEY",
        "PAYMENT_OR_BUDGET",
        "PERMISSION_OR_WORKSPACE",
        "GUARDRAIL_OR_PROMPT_BLOCK",
        "RATE_LIMIT",
        "UPSTREAM_OR_SERVER",
        "UNCLASSIFIED_4XX",
        "OTHER_TRANSPORT",
    }
)

_TRANSPORT_FAILURE_CLASSES = frozenset(
    {
        "RESPONSE_NOT_OBSERVED",
        "HTTP_ERROR",
        "RESPONSE_EVIDENCE_INVALID",
        "ACCOUNTING_UNAVAILABLE",
        "TRANSPORT_TIMEOUT",
        "TRANSPORT_ERROR",
        "CLEANUP_FAILURE",
        "MODEL_OR_ROUTE_DRIFT",
    }
)
OPENROUTER_MAX_BILLABLE_INPUT_BYTES = 16_384
# WP-013A contract-v2 expands the bounded nested L2/L3/L4 catalogue while
# preserving the legacy E1 reserve proof above.  The larger ceiling is enabled
# only by the exact v2 prompt/action/context triple and remains a pre-dispatch
# capacity guard, not an input-size target.
OPENROUTER_WP013A_MAX_BILLABLE_INPUT_BYTES = 32_768
# WP-013B is the only protocol epoch authorised to use the larger context.
# The exact three-part identity below prevents this from silently widening any
# legacy request path.
OPENROUTER_WP013B_MAX_BILLABLE_INPUT_BYTES = 65_536
OPENROUTER_MAX_OUTPUT_TOKENS = 4_096
UNKNOWN_PROVIDER_REQUEST_RESERVE_USD = Decimal("0.10")

_PUBLIC_RESPONSE_HEADERS = frozenset(
    {
        "cf-ray",
        "content-type",
        "openrouter-processing-time",
        "x-openrouter-cache",
        "x-openrouter-generation-id",
        "x-openrouter-model",
        "x-openrouter-provider",
        "x-request-id",
    }
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _strict_json_object(raw: bytes, label: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label}_duplicate_key:{key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"{label}_nonfinite_number:{value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderTransportError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise ProviderTransportError(f"{label}_not_object")
    return value


@dataclass(frozen=True, slots=True)
class OpenRouterOutboundBinding:
    request_contract_hash: str
    requested_model: str
    body: Mapping[str, Any]
    headers: Mapping[str, str]
    body_sha256: str
    headers_sha256: str
    binding_sha256: str
    schema_version: str = OPENROUTER_OUTBOUND_SCHEMA

    def __post_init__(self) -> None:
        body = json.loads(_canonical_bytes(dict(self.body)))
        headers = {str(key): str(value) for key, value in self.headers.items()}
        if "authorization" in {key.lower() for key in headers}:
            raise ProviderBindingError("openrouter_authorization_must_not_be_bound")
        if _hash(body) != self.body_sha256 or _hash(headers) != self.headers_sha256:
            raise ProviderBindingError("openrouter_outbound_hash_mismatch")
        payload = {
            "schema_version": self.schema_version,
            "request_contract_hash": self.request_contract_hash,
            "requested_model": self.requested_model,
            "body_sha256": self.body_sha256,
            "headers_sha256": self.headers_sha256,
        }
        if _hash(payload) != self.binding_sha256:
            raise ProviderBindingError("openrouter_binding_hash_mismatch")
        object.__setattr__(self, "body", MappingProxyType(body))
        object.__setattr__(self, "headers", MappingProxyType(headers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_contract_hash": self.request_contract_hash,
            "requested_model": self.requested_model,
            "body": json.loads(_canonical_bytes(dict(self.body))),
            "headers": dict(self.headers),
            "body_sha256": self.body_sha256,
            "headers_sha256": self.headers_sha256,
            "binding_sha256": self.binding_sha256,
        }


def build_openrouter_outbound(
    binding: ProviderRequestBinding,
) -> OpenRouterOutboundBinding:
    """Build the exact secret-free E0 request body and headers."""

    settings = binding.settings
    if binding.provider != "openrouter":
        raise ProviderBindingError("openrouter_provider_binding_required")
    if binding.model_id not in OPENROUTER_E1_MODELS:
        raise ProviderBindingError("openrouter_exact_e1_model_required")
    if binding.adapter_version != OPENROUTER_ADAPTER_VERSION:
        raise ProviderBindingError("openrouter_adapter_binding_mismatch")
    if (
        settings.max_tokens != OPENROUTER_MAX_OUTPUT_TOKENS
        or settings.temperature is not None
        or settings.seed is not None
        or settings.response_format != "json_object"
        or settings.max_retries != 0
        or settings.retry_on
        or settings.max_requests != 1
        or settings.unknown_cost_policy != "reject"
        or settings.unknown_usage_policy != "reject"
    ):
        raise ProviderBindingError("openrouter_frozen_settings_mismatch")
    billable_input_bytes = len(binding.prompt_bytes) + len(binding.context_bytes)
    wp013a_v2 = (
        binding.prompt_version == "dof.wp013a-one-submission-prompt.v2"
        and binding.action_interface_version == "dof.single-submission.planner-message.v2"
        and binding.context_interface_version == "dof.single-submission.planning-context.v2"
    )
    wp013b_v22 = (
        binding.prompt_version == "dof.wp013b-one-submission-prompt.v2.2"
        and binding.action_interface_version == "dof.single-submission.planner-message.v3"
        and binding.context_interface_version == "dof.single-submission.planning-context.v2.2"
    )
    wp014_dual_track_v1 = (
        binding.prompt_version == "dof.wp013b-one-submission-prompt.v2.2"
        and binding.action_interface_version == "dof.single-submission.planner-message.v4"
        and binding.context_interface_version == "dof.single-submission.planning-context.v2.2"
    )
    wp014c_action_surface_v3 = (
        binding.prompt_version == "dof.wp014c-one-submission-prompt.v3"
        and binding.action_interface_version == "dof.single-submission.planner-message.v6"
        and binding.context_interface_version == "dof.single-submission.planning-context.v2.2"
    )
    input_ceiling = (
        OPENROUTER_WP013B_MAX_BILLABLE_INPUT_BYTES
        if wp013b_v22 or wp014_dual_track_v1 or wp014c_action_surface_v3
        else OPENROUTER_WP013A_MAX_BILLABLE_INPUT_BYTES if wp013a_v2
        else OPENROUTER_MAX_BILLABLE_INPUT_BYTES
    )
    if billable_input_bytes > input_ceiling:
        raise ProviderBindingError("openrouter_frozen_input_byte_bound_exceeded")
    body = {
        "model": binding.model_id,
        "messages": [
            {"role": "system", "content": binding.prompt_bytes.decode("utf-8")},
            {"role": "user", "content": binding.context_bytes.decode("utf-8")},
        ],
        "max_tokens": OPENROUTER_MAX_OUTPUT_TOKENS,
        "reasoning_effort": "medium",
        "stream": False,
        "response_format": {"type": "json_object"},
        "provider": {
            "only": [OPENROUTER_E1_PROFILES[binding.model_id]["endpoint_tag"]],
            "order": [OPENROUTER_E1_PROFILES[binding.model_id]["endpoint_tag"]],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
        },
        "plugins": [
            {"id": "response-healing", "enabled": False},
            {"id": "context-compression", "enabled": False},
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "X-OpenRouter-Metadata": "enabled",
        "X-OpenRouter-Cache": "false",
    }
    body_hash = _hash(body)
    headers_hash = _hash(headers)
    payload = {
        "schema_version": OPENROUTER_OUTBOUND_SCHEMA,
        "request_contract_hash": binding.request_contract_hash,
        "requested_model": binding.model_id,
        "body_sha256": body_hash,
        "headers_sha256": headers_hash,
    }
    return OpenRouterOutboundBinding(
        request_contract_hash=binding.request_contract_hash,
        requested_model=binding.model_id,
        body=body,
        headers=headers,
        body_sha256=body_hash,
        headers_sha256=headers_hash,
        binding_sha256=_hash(payload),
    )


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderTransportError(f"openrouter_{label}_missing_or_invalid")
    return value


def _money(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProviderTransportError(f"openrouter_{label}_missing_or_invalid")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ProviderTransportError(f"openrouter_{label}_missing_or_invalid")
    return result


def _public_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() in _PUBLIC_RESPONSE_HEADERS
    }


def classify_openrouter_provider_error(
    status: int, envelope: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return a public-safe classification without retaining raw message text."""

    error = envelope.get("error") if isinstance(envelope, Mapping) else None
    valid_error = isinstance(error, Mapping)
    code = error.get("code") if valid_error else None
    error_type = error.get("type") if valid_error else None
    message = error.get("message") if valid_error else None
    metadata = error.get("metadata") if valid_error else None
    message_bytes = (
        message.encode("utf-8") if isinstance(message, str) else b""
    )
    searchable_parts = [
        str(value).lower()
        for value in (code, error_type, message)
        if isinstance(value, (str, int)) and not isinstance(value, bool)
    ]
    if isinstance(metadata, Mapping):
        searchable_parts.extend(
            str(value).lower()
            for value in metadata.values()
            if isinstance(value, (str, int)) and not isinstance(value, bool)
        )
    searchable = " ".join(searchable_parts)

    if valid_error:
        if any(
            marker in searchable
            for marker in (
                "insufficient credit",
                "insufficient fund",
                "credit limit",
                "key limit",
                "budget",
                "payment",
                "quota exceeded",
            )
        ):
            category = "PAYMENT_OR_BUDGET"
        elif any(
            marker in searchable
            for marker in (
                "authentication",
                "invalid api key",
                "invalid key",
                "unauthorized",
                "missing api key",
            )
        ):
            category = "AUTHENTICATION_OR_KEY"
        elif any(
            marker in searchable
            for marker in ("permission", "workspace", "not allowed", "forbidden")
        ):
            category = "PERMISSION_OR_WORKSPACE"
        elif any(
            marker in searchable
            for marker in (
                "guardrail",
                "moderation",
                "prompt block",
                "prompt_block",
                "content policy",
                "safety block",
            )
        ):
            category = "GUARDRAIL_OR_PROMPT_BLOCK"
        elif any(
            marker in searchable
            for marker in ("rate limit", "rate_limit", "too many requests")
        ) or code == 429:
            category = "RATE_LIMIT"
        elif any(
            marker in searchable
            for marker in ("upstream", "server error", "service unavailable", "gateway")
        ) or (isinstance(code, int) and code >= 500):
            category = "UPSTREAM_OR_SERVER"
        elif 400 <= status < 500:
            category = "UNCLASSIFIED_4XX"
        else:
            category = "OTHER_TRANSPORT"
    elif status in {401}:
        category = "AUTHENTICATION_OR_KEY"
    elif status in {402}:
        category = "PAYMENT_OR_BUDGET"
    elif status in {403}:
        category = "UNCLASSIFIED_4XX"
    elif status == 429:
        category = "RATE_LIMIT"
    elif status >= 500:
        category = "UPSTREAM_OR_SERVER"
    elif 400 <= status < 500:
        category = "UNCLASSIFIED_4XX"
    else:
        category = "OTHER_TRANSPORT"

    identifiers: dict[str, str] = {}
    if isinstance(metadata, Mapping):
        for key in ("stage", "pipeline_stage", "guardrail", "provider_name"):
            value = metadata.get(key)
            if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
                identifiers[key] = value
    public_code = (
        code
        if isinstance(code, int) and not isinstance(code, bool)
        else (code if isinstance(code, str) and _SAFE_IDENTIFIER.fullmatch(code) else None)
    )
    public_type = (
        error_type
        if isinstance(error_type, str) and _SAFE_IDENTIFIER.fullmatch(error_type)
        else None
    )
    return {
        "schema_version": OPENROUTER_PROVIDER_ERROR_SCHEMA,
        "classifier_version": OPENROUTER_PROVIDER_ERROR_CLASSIFIER_VERSION,
        "category": category,
        "http_status": status,
        "valid_error_envelope": valid_error,
        "error_code": public_code,
        "error_type": public_type,
        "message_sha256": _hash(message_bytes),
        "message_size_bytes": len(message_bytes),
        "safe_identifiers": identifiers,
    }


def _transport_failure_class(error: BaseException, *, response_observed: bool) -> str:
    message = str(error)
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "TRANSPORT_TIMEOUT"
    if "cleanup" in message:
        return "CLEANUP_FAILURE"
    if "http_status" in message or "http_error" in message:
        return "HTTP_ERROR"
    if "substitution" in message or "route_drift" in message:
        return "MODEL_OR_ROUTE_DRIFT"
    if "usage" in message or "cost" in message or "route_evidence_missing" in message:
        return "ACCOUNTING_UNAVAILABLE"
    if response_observed:
        return "RESPONSE_EVIDENCE_INVALID"
    if "response_not_constructed" in message:
        return "RESPONSE_NOT_OBSERVED"
    return "TRANSPORT_ERROR"


class OpenRouterChatCompletionsTransport:
    """One physical OpenRouter request with fail-closed route accounting."""

    transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
    internal_retries_disabled = True
    requires_openrouter_outbound_binding = True
    adapter_version = OPENROUTER_ADAPTER_VERSION

    def __init__(
        self,
        *,
        api_key: str,
        expected_outbound_binding: OpenRouterOutboundBinding,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ProviderBindingError("openrouter_credential_unavailable")
        if not isinstance(expected_outbound_binding, OpenRouterOutboundBinding):
            raise ProviderBindingError("openrouter_outbound_binding_required")
        self._api_key = api_key
        self._expected = expected_outbound_binding
        self._client_factory = client_factory
        self.send_attempts = 0
        self.physical_requests = 0
        self.cleanup_status = "NOT_ATTEMPTED"
        self.cleanup_error: str | None = None
        self.prepared_outbound_binding: OpenRouterOutboundBinding | None = None
        self.last_exchange: dict[str, Any] | None = None
        self.accounting_lookup_requests = 0
        self.reconciled_accounting: dict[str, Any] | None = None
        self.private_response_bytes: bytes | None = None

    async def _observe_request(self, _request: Any) -> None:
        if self.physical_requests != 0:
            raise ProviderBindingError("openrouter_single_request_exhausted")
        self.physical_requests += 1

    async def _reconcile_generation(
        self,
        *,
        generation_id: str,
        binding: ProviderRequestBinding,
        factory: Callable[..., Any],
    ) -> None:
        """Perform the single permitted accounting-only generation lookup."""

        if self.accounting_lookup_requests != 0:
            raise ProviderBindingError("openrouter_generation_lookup_cap_exceeded")
        self.accounting_lookup_requests = 1
        assert self.last_exchange is not None
        self.last_exchange["accounting_lookup_requests"] = 1
        lookup: dict[str, Any] = {
            "endpoint": OPENROUTER_BASE_URL + OPENROUTER_GENERATION_ENDPOINT,
            "generation_id": generation_id,
            "status": "FAILED",
        }
        client = factory(
            base_url=OPENROUTER_BASE_URL,
            timeout=binding.settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            event_hooks={},
        )
        try:
            response = await client.get(
                OPENROUTER_GENERATION_ENDPOINT,
                params={"id": generation_id},
                headers={"Authorization": "Bearer " + self._api_key},
            )
            raw = bytes(response.content)
            lookup.update(
                {
                    "http_status": int(response.status_code),
                    "response_headers": _public_headers(response.headers),
                    "response_sha256": _hash(raw),
                    "response_size_bytes": len(raw),
                }
            )
            if int(response.status_code) < 200 or int(response.status_code) >= 300:
                raise ProviderTransportError("openrouter_generation_lookup_http_error")
            envelope = _strict_json_object(raw, "openrouter_generation_response")
            data = envelope.get("data")
            if not isinstance(data, Mapping):
                raise ProviderTransportError("openrouter_generation_data_missing")
            returned_model = data.get("model")
            upstream_provider = data.get("provider_name", data.get("provider"))
            if returned_model != binding.model_id or upstream_provider != (
                OPENROUTER_E1_PROFILES[binding.model_id]["upstream_provider"]
            ):
                self.last_exchange["failure_class"] = "MODEL_OR_ROUTE_DRIFT"
                raise ProviderTransportError("openrouter_generation_route_drift")
            input_tokens = _integer(
                data.get("tokens_prompt", data.get("prompt_tokens")),
                "generation_prompt_tokens",
            )
            output_tokens = _integer(
                data.get("tokens_completion", data.get("completion_tokens")),
                "generation_completion_tokens",
            )
            cost = _money(
                data.get("total_cost", data.get("cost")),
                "generation_total_cost",
            )
            accounting = {
                "generation_id": generation_id,
                "returned_model": returned_model,
                "upstream_provider": upstream_provider,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost_usd": cost,
                "cost_source": "openrouter_generation.total_cost",
            }
            self.reconciled_accounting = accounting
            self.last_exchange["route"] = {
                "strategy": "generation_accounting_lookup_only",
                "requested_model": binding.model_id,
                "returned_model": returned_model,
                "selected_upstream_provider": upstream_provider,
                "required_endpoint_tag": OPENROUTER_E1_PROFILES[binding.model_id][
                    "endpoint_tag"
                ],
                "fallback_allowed": False,
                "attempt": 1,
            }
            self.last_exchange["usage"] = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost": cost,
                "source": "openrouter_generation.total_cost",
            }
            lookup.update(
                {
                    "status": "SUCCEEDED",
                    "accounting": accounting,
                }
            )
            self.last_exchange["accounting_reconciled"] = True
        except Exception as exc:
            lookup["error_class"] = type(exc).__name__
            if self.last_exchange.get("failure_class") == "MODEL_OR_ROUTE_DRIFT":
                lookup["integrity_violation"] = True
        finally:
            try:
                await client.aclose()
                lookup["cleanup_status"] = "SUCCEEDED"
            except Exception as exc:
                lookup["cleanup_status"] = "FAILED"
                lookup["cleanup_error_class"] = type(exc).__name__
            self.last_exchange["accounting_lookup"] = lookup

    async def send(
        self,
        binding: ProviderRequestBinding,
        retry_index: int,
    ) -> RawProviderResponse:
        if retry_index != 0 or self.send_attempts != 0:
            raise ProviderBindingError("openrouter_single_request_exhausted")
        self.send_attempts += 1
        outbound = build_openrouter_outbound(binding)
        if outbound.to_dict() != self._expected.to_dict():
            raise ProviderBindingError("openrouter_outbound_binding_mismatch")
        self.prepared_outbound_binding = outbound
        if self._client_factory is None:
            import httpx

            factory = httpx.AsyncClient
        else:
            factory = self._client_factory
        client = factory(
            base_url=OPENROUTER_BASE_URL,
            timeout=binding.settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            event_hooks={"request": [self._observe_request]},
        )
        self.last_exchange = {
            "schema_version": OPENROUTER_TRANSPORT_TERMINAL_SCHEMA,
            "attempt": 1,
            "request_url": OPENROUTER_BASE_URL + OPENROUTER_CHAT_ENDPOINT,
            "outbound_binding": outbound.to_dict(),
            "send_attempted": True,
            "physical_requests": 0,
            "transport_cleanup_status": "NOT_ATTEMPTED",
            "response_headers_observed": False,
            "response_body_observed": False,
            "response_headers": {},
            "generation_id": None,
            "route": None,
            "usage": None,
            "failure_class": None,
            "accounting_lookup_requests": 0,
            "accounting_reconciled": False,
        }
        response = None
        primary_error: BaseException | None = None
        try:
            response = await client.post(
                OPENROUTER_CHAT_ENDPOINT,
                headers=dict(outbound.headers)
                | {"Authorization": "Bearer " + self._api_key},
                content=_canonical_bytes(dict(outbound.body)),
            )
        except Exception as exc:
            primary_error = exc
        finally:
            try:
                await client.aclose()
                self.cleanup_status = "SUCCEEDED"
            except Exception as exc:
                self.cleanup_status = "FAILED"
                self.cleanup_error = type(exc).__name__
        assert self.last_exchange is not None
        self.last_exchange["physical_requests"] = self.physical_requests
        self.last_exchange["transport_cleanup_status"] = self.cleanup_status
        if primary_error is not None:
            failure_class = (
                "CLEANUP_FAILURE"
                if self.cleanup_status != "SUCCEEDED"
                else _transport_failure_class(
                    primary_error, response_observed=False
                )
            )
            self.last_exchange["failure_class"] = failure_class
            raise primary_error
        if self.cleanup_status != "SUCCEEDED":
            self.last_exchange["failure_class"] = "CLEANUP_FAILURE"
            raise ProviderTransportError("openrouter_transport_cleanup_failure")
        if response is None:
            self.last_exchange["failure_class"] = "RESPONSE_NOT_OBSERVED"
            raise ProviderTransportError("openrouter_response_not_constructed")
        try:
            public_headers = _public_headers(response.headers)
            self.last_exchange["response_headers_observed"] = True
            self.last_exchange["response_headers"] = public_headers
            header_generation = public_headers.get("x-openrouter-generation-id")
            if header_generation:
                self.last_exchange["generation_id"] = header_generation
            status = int(response.status_code)
            self.last_exchange["http_status"] = status
            raw_envelope = bytes(response.content)
            self.last_exchange["response_body_observed"] = True
            self.last_exchange["raw_response_sha256"] = _hash(raw_envelope)
            self.last_exchange["raw_response_size_bytes"] = len(raw_envelope)
            if status < 200 or status >= 300:
                self.private_response_bytes = raw_envelope
                try:
                    error_envelope = _strict_json_object(
                        raw_envelope, "openrouter_error_response"
                    )
                except ProviderTransportError:
                    error_envelope = None
                provider_error = classify_openrouter_provider_error(
                    status, error_envelope
                )
                self.last_exchange["provider_error"] = provider_error
                raise ProviderTransportError(
                    "openrouter_http_error:" + provider_error["category"]
                )
            self.last_exchange["raw_response_bytes_b64"] = base64.b64encode(
                raw_envelope
            ).decode("ascii")
            envelope = _strict_json_object(raw_envelope, "openrouter_response")
            self.last_exchange["response"] = envelope
            envelope_generation = envelope.get("id")
            if isinstance(envelope_generation, str) and envelope_generation:
                self.last_exchange["generation_id"] = envelope_generation
            requested_model = binding.model_id
            returned_model = envelope.get("model")
            upstream_provider = envelope.get("provider")
            if returned_model != requested_model:
                raise ProviderTransportError("openrouter_model_substitution_detected")
            if not isinstance(upstream_provider, str) or not upstream_provider.strip():
                raise ProviderTransportError("openrouter_route_evidence_missing")
            profile = OPENROUTER_E1_PROFILES[requested_model]
            if upstream_provider != profile["upstream_provider"]:
                raise ProviderTransportError("openrouter_route_drift_detected")
            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ProviderTransportError("openrouter_choice_cardinality_invalid")
            choice = choices[0]
            if not isinstance(choice, Mapping) or not isinstance(
                choice.get("finish_reason"), str
            ):
                raise ProviderTransportError("openrouter_finish_reason_missing")
            message = choice.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            if not isinstance(content, str):
                raise ProviderTransportError("openrouter_content_missing")
            usage = envelope.get("usage")
            if not isinstance(usage, Mapping):
                raise ProviderTransportError("openrouter_usage_accounting_incomplete")
            prompt_tokens = _integer(usage.get("prompt_tokens"), "prompt_tokens")
            completion_tokens = _integer(
                usage.get("completion_tokens"), "completion_tokens"
            )
            total_tokens = _integer(usage.get("total_tokens"), "total_tokens")
            if total_tokens != prompt_tokens + completion_tokens:
                raise ProviderTransportError(
                    "openrouter_usage_accounting_contradictory"
                )
            cost = _money(usage.get("cost"), "usage_cost")
            self.last_exchange["schema_version"] = OPENROUTER_EXCHANGE_SCHEMA
            self.last_exchange["route"] = {
                "strategy": "exact_model_endpoint_pinned_no_fallback",
                "requested_model": requested_model,
                "returned_model": returned_model,
                "selected_upstream_provider": upstream_provider,
                "required_endpoint_tag": profile["endpoint_tag"],
                "fallback_allowed": False,
                "attempt": 1,
            }
            self.last_exchange["usage"] = json.loads(_canonical_bytes(dict(usage)))
            self.last_exchange["finish_reason"] = choice["finish_reason"]
            return RawProviderResponse(
                content_bytes=content.encode("utf-8"),
                actual_provider="openrouter",
                provider_request_id=(
                    envelope_generation
                    if isinstance(envelope_generation, str) and envelope_generation
                    else None
                ),
                returned_model_id=returned_model,
                returned_model_revision=upstream_provider,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost,
                cost_source="openrouter_usage.cost",
                observed_request_contract_hash=binding.request_contract_hash,
            )
        except BaseException as exc:
            failure_class = _transport_failure_class(
                exc,
                response_observed=bool(
                    self.last_exchange.get("response_headers_observed")
                    or self.last_exchange.get("response_body_observed")
                ),
            )
            if failure_class not in _TRANSPORT_FAILURE_CLASSES:
                failure_class = "TRANSPORT_ERROR"
            self.last_exchange["failure_class"] = failure_class
            generation_id = self.last_exchange.get("generation_id")
            if (
                isinstance(generation_id, str)
                and generation_id
                and failure_class != "MODEL_OR_ROUTE_DRIFT"
                and not isinstance(exc, asyncio.CancelledError)
            ):
                await self._reconcile_generation(
                    generation_id=generation_id,
                    binding=binding,
                    factory=factory,
                )
            raise


async def discover_openrouter_models(
    api_key: str,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Query the Models API once and require all exact frozen E1 slugs."""

    if not isinstance(api_key, str) or not api_key:
        raise ProviderBindingError("openrouter_credential_unavailable")
    if client_factory is None:
        import httpx

        factory = httpx.AsyncClient
    else:
        factory = client_factory
    observed_requests = 0

    async def observe(_request: Any) -> None:
        nonlocal observed_requests
        observed_requests += 1
        if observed_requests > 1:
            raise ProviderBindingError("openrouter_discovery_request_cap_exceeded")

    client = factory(
        base_url=OPENROUTER_BASE_URL,
        timeout=60.0,
        follow_redirects=False,
        trust_env=False,
        event_hooks={"request": [observe]},
    )
    try:
        response = await client.get(
            OPENROUTER_MODELS_ENDPOINT,
            headers={"Authorization": "Bearer " + api_key},
        )
    finally:
        await client.aclose()
    raw = bytes(response.content)
    payload = _strict_json_object(raw, "openrouter_models_response")
    if int(response.status_code) < 200 or int(response.status_code) >= 300:
        raise ProviderTransportError(
            f"openrouter_models_http_status:{int(response.status_code)}"
        )
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ProviderTransportError("openrouter_models_data_missing")
    available = sorted(
        {
            row["id"]
            for row in rows
            if isinstance(row, Mapping)
            and isinstance(row.get("id"), str)
            and row["id"]
        }
    )
    selected = {
        model: model in available for model in OPENROUTER_E1_MODELS
    }
    evidence = {
        "schema_version": OPENROUTER_DISCOVERY_SCHEMA,
        "endpoint": OPENROUTER_BASE_URL + OPENROUTER_MODELS_ENDPOINT,
        "physical_requests": observed_requests,
        "http_status": int(response.status_code),
        "response_headers": _public_headers(response.headers),
        "response_sha256": _hash(raw),
        "response_size_bytes": len(raw),
        "available_model_count": len(available),
        "required_models": selected,
        "all_required_available": all(selected.values()),
    }
    return evidence


async def discover_openrouter_endpoints(
    api_key: str,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Query each frozen model once and validate its pinned E1 endpoint."""

    if not isinstance(api_key, str) or not api_key:
        raise ProviderBindingError("openrouter_credential_unavailable")
    if client_factory is None:
        import httpx

        factory = httpx.AsyncClient
    else:
        factory = client_factory
    observed_requests = 0

    async def observe(_request: Any) -> None:
        nonlocal observed_requests
        observed_requests += 1
        if observed_requests > len(OPENROUTER_E1_MODELS):
            raise ProviderBindingError("openrouter_endpoint_discovery_request_cap_exceeded")

    client = factory(
        base_url=OPENROUTER_BASE_URL,
        timeout=60.0,
        follow_redirects=False,
        trust_env=False,
        event_hooks={"request": [observe]},
    )
    rows: dict[str, Any] = {}
    try:
        for model in OPENROUTER_E1_MODELS:
            endpoint = OPENROUTER_ENDPOINTS_TEMPLATE.format(model=model)
            response = await client.get(
                endpoint,
                headers={"Authorization": "Bearer " + api_key},
            )
            raw = bytes(response.content)
            payload = _strict_json_object(raw, "openrouter_endpoints_response")
            status = int(response.status_code)
            if status < 200 or status >= 300:
                raise ProviderTransportError(
                    f"openrouter_endpoints_http_status:{model}:{status}"
                )
            data = payload.get("data")
            endpoints = data.get("endpoints") if isinstance(data, Mapping) else data
            if not isinstance(endpoints, list):
                raise ProviderTransportError("openrouter_endpoints_data_missing")
            profile = OPENROUTER_E1_PROFILES[model]
            required = {"max_tokens", "response_format", "reasoning_effort"}
            matching = [
                item
                for item in endpoints
                if isinstance(item, Mapping)
                and item.get("tag") == profile["endpoint_tag"]
                and item.get("provider_name") == profile["upstream_provider"]
            ]
            supported = (
                set(matching[0].get("supported_parameters", ())) if matching else set()
            )
            rows[model] = {
                "endpoint": OPENROUTER_BASE_URL + endpoint,
                "http_status": status,
                "response_sha256": _hash(raw),
                "response_size_bytes": len(raw),
                "pinned_endpoint_tag": profile["endpoint_tag"],
                "expected_upstream_provider": profile["upstream_provider"],
                "endpoint_available": bool(matching),
                "required_parameters": sorted(required),
                "required_parameters_supported": required <= supported,
                "supported_parameters": sorted(supported),
                "context_length": (
                    matching[0].get("context_length")
                    if matching else None
                ),
                "max_completion_tokens": (
                    matching[0].get("max_completion_tokens")
                    if matching else None
                ),
                "pricing": (
                    json.loads(_canonical_bytes(dict(matching[0].get("pricing", {}))))
                    if matching and isinstance(matching[0].get("pricing"), Mapping)
                    else None
                ),
            }
    finally:
        await client.aclose()
    return {
        "schema_version": "dof.openrouter-endpoint-discovery.v1",
        "physical_requests": observed_requests,
        "models": rows,
        "all_profiles_available": all(
            row["endpoint_available"] and row["required_parameters_supported"]
            for row in rows.values()
        ),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def verify_openrouter_unknown_cost_reserve(
    endpoint_discovery: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove the frozen USD 0.10 reserve from public endpoint pricing.

    One UTF-8 byte per input token is deliberately pessimistic.  The output
    ceiling is the exact request ``max_tokens`` value.  Per-request pricing is
    included when the endpoint publishes it; unsupported/non-numeric pricing
    fails closed.
    """

    rows: dict[str, Any] = {}
    models = endpoint_discovery.get("models", {})
    for model in OPENROUTER_E1_MODELS:
        source = models.get(model, {}) if isinstance(models, Mapping) else {}
        pricing = source.get("pricing") if isinstance(source, Mapping) else None
        accepted = isinstance(pricing, Mapping)
        reason = None
        upper = None
        fields: dict[str, str] = {}
        if accepted:
            try:
                prompt = Decimal(str(pricing["prompt"]))
                completion = Decimal(str(pricing["completion"]))
                request = Decimal(str(pricing.get("request", "0")))
                if min(prompt, completion, request) < 0:
                    raise InvalidOperation
            except (KeyError, InvalidOperation, ValueError):
                accepted = False
                reason = "pricing_fields_missing_or_invalid"
            else:
                upper_value = (
                    prompt * OPENROUTER_MAX_BILLABLE_INPUT_BYTES
                    + completion * OPENROUTER_MAX_OUTPUT_TOKENS
                    + request
                )
                upper = float(upper_value)
                accepted = upper_value <= UNKNOWN_PROVIDER_REQUEST_RESERVE_USD
                reason = None if accepted else "reserve_not_conservative"
                fields = {
                    "prompt_usd_per_token": str(prompt),
                    "completion_usd_per_token": str(completion),
                    "request_usd": str(request),
                }
        else:
            reason = "pricing_unavailable"
        rows[model] = {
            "source_endpoint": source.get("endpoint") if isinstance(source, Mapping) else None,
            "source_response_sha256": (
                source.get("response_sha256") if isinstance(source, Mapping) else None
            ),
            "pricing": fields,
            "max_billable_input_bytes": OPENROUTER_MAX_BILLABLE_INPUT_BYTES,
            "input_token_bound_assumption": "one_utf8_byte_per_token",
            "max_output_tokens": OPENROUTER_MAX_OUTPUT_TOKENS,
            "calculated_upper_bound_usd": upper,
            "reserve_usd": float(UNKNOWN_PROVIDER_REQUEST_RESERVE_USD),
            "accepted": accepted,
            "reason": reason,
        }
    payload = {
        "schema_version": "dof.openrouter-unknown-cost-reserve-verification.v1",
        "retrieved_at": endpoint_discovery.get("retrieved_at"),
        "models": rows,
        "all_models_accepted": all(row["accepted"] for row in rows.values()),
    }
    return payload | {"verification_sha256": _hash(payload)}


def write_discovery_evidence(evidence: Mapping[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    target.write_bytes(json.dumps(dict(evidence), sort_keys=True, indent=2).encode() + b"\n")


__all__ = [
    "OPENROUTER_ADAPTER_VERSION",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_CREDENTIAL_ENV",
    "OPENROUTER_E0_MODELS",
    "OPENROUTER_E1_MODELS",
    "OPENROUTER_E1_PROFILES",
    "OPENROUTER_MAX_BILLABLE_INPUT_BYTES",
    "OPENROUTER_MAX_OUTPUT_TOKENS",
    "OPENROUTER_WP013A_MAX_BILLABLE_INPUT_BYTES",
    "OPENROUTER_WP013B_MAX_BILLABLE_INPUT_BYTES",
    "OpenRouterChatCompletionsTransport",
    "OpenRouterOutboundBinding",
    "build_openrouter_outbound",
    "discover_openrouter_models",
    "discover_openrouter_endpoints",
    "verify_openrouter_unknown_cost_reserve",
    "write_discovery_evidence",
]
