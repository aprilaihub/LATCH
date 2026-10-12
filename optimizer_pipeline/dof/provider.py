"""Strict, offline-testable provider binding for DoF L2/L3 decisions.

Legacy provider helpers intentionally remain permissive for their original
applications.  This module does not consume their repaired ``parsed_json``.
It owns one exact request contract, records every fake/real adapter attempt,
parses the returned content bytes as exactly one JSON object, and binds the
typed proposal to the active decision before it may reach the action gate.

WP-003 exercises this module with fake transports only; it performs no network
access and contains no provider SDK imports.  A concrete SDK adapter is a
strict trust boundary: one ``send`` call must map to exactly one physical
provider request and SDK-internal retries must be disabled, so every retry is
visible to this observer and its persistent budget.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import asyncio
import base64
import hashlib
import inspect
import json
import math
import re
import secrets
import threading
import time
from types import MappingProxyType
from typing import Any, Protocol

from optimizer_pipeline.core.domain import ArtifactKind

from .evidence import ImmutableEvidenceWriter, ImmutableSidecar
from .gate import ActionProposal, ProposalParseError, parse_action_proposal


PROVIDER_REQUEST_CONTRACT_VERSION = "dof.provider-request.v1"
PROVIDER_ATTEMPT_SCHEMA_VERSION = "dof.provider-attempt.v2"
PROVIDER_TRACE_ROLE = "provider_call_trace"
STRICT_PROVIDER_PARSER_VERSION = "dof.strict-provider-json.v1"
PROVIDER_TRANSPORT_CONTRACT_VERSION = "dof.single-request-transport.v2"
NOT_PROVIDED = "NOT_PROVIDED"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIAGNOSTIC_PATH_START = r"(?:file://|/(?!/)|~[/\\]|[A-Za-z]:[/\\])"
_QUOTED_DIAGNOSTIC_PATH_RE = re.compile(
    rf"(?ix)(?P<quote>[\"'])"
    rf"(?P<path>{_DIAGNOSTIC_PATH_START}"
    rf"(?:(?!(?P=quote))[\s\S])*)"
    rf"(?P=quote)"
)
_UNQUOTED_DIAGNOSTIC_PATH_RE = re.compile(
    # Fail closed over spaces until the next structured field or ordinary
    # punctuation.  A URL such as https://... cannot satisfy the path start:
    # its first slash is followed by a slash and its second is slash-prefixed.
    rf"(?ix)(?<![A-Za-z0-9_~/]){_DIAGNOSTIC_PATH_START}.*?"
    rf"(?=(?:\s+[A-Za-z_][A-Za-z0-9_.-]{{0,63}}\s*"
    rf"(?::|=|\bis\b))|[,;)\]}}]|$)"
)
_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?ix)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|passwd|credential|authorization|"
    r"license(?:[_-]?(?:key|server))?)\b"
    r"\s*(?:[:=]|\bis\b)\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)


class ProviderAttemptStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    BINDING_REJECTED = "BINDING_REJECTED"
    POLICY_OUTPUT_REJECTED = "POLICY_OUTPUT_REJECTED"
    COST_REJECTED = "COST_REJECTED"
    USAGE_REJECTED = "USAGE_REJECTED"


class ProviderBindingError(ValueError):
    pass


class StrictProviderParseError(ValueError):
    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(str(reason) for reason in reasons)
        super().__init__("; ".join(self.reasons))


class ProviderTransportError(RuntimeError):
    pass


class ProviderTimeout(ProviderTransportError):
    pass


class ProviderCancelled(ProviderTransportError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    temperature: float | None = 0.0
    max_tokens: int | None = None
    seed: int | None = None
    response_format: str = "json_object"
    timeout_seconds: float = 60.0
    max_retries: int = 0
    retry_on: tuple[str, ...] = ("PROVIDER_ERROR", "TIMED_OUT")
    max_requests: int = 1
    max_usd: float | None = None
    max_cost_per_request_usd: float | None = None
    unknown_cost_policy: str = "reject"
    unknown_usage_policy: str = "reject"

    def __post_init__(self) -> None:
        if self.temperature is not None:
            value = float(self.temperature)
            if not math.isfinite(value):
                raise ProviderBindingError("temperature must be finite")
            object.__setattr__(self, "temperature", value)
        if self.max_tokens is not None and (
            not isinstance(self.max_tokens, int)
            or isinstance(self.max_tokens, bool)
            or self.max_tokens <= 0
        ):
            raise ProviderBindingError("max_tokens must be a positive integer")
        if self.seed is not None and (
            not isinstance(self.seed, int) or isinstance(self.seed, bool)
        ):
            raise ProviderBindingError("seed must be an integer")
        if self.response_format != "json_object":
            raise ProviderBindingError("DoF response_format must be json_object")
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ProviderBindingError("timeout_seconds must be finite and positive")
        object.__setattr__(self, "timeout_seconds", timeout)
        if (
            not isinstance(self.max_retries, int)
            or isinstance(self.max_retries, bool)
            or not 0 <= self.max_retries <= 3
        ):
            raise ProviderBindingError("max_retries must be an integer from zero to three")
        if (
            not isinstance(self.max_requests, int)
            or isinstance(self.max_requests, bool)
            or self.max_requests <= 0
        ):
            raise ProviderBindingError("max_requests must be positive")
        if self.max_requests < self.max_retries + 1:
            raise ProviderBindingError("max_requests cannot be smaller than retry envelope")
        allowed_retry = {"PROVIDER_ERROR", "TIMED_OUT"}
        normalized_retry = tuple(str(item) for item in self.retry_on)
        if set(normalized_retry) - allowed_retry:
            raise ProviderBindingError("retry_on contains an unsupported status")
        object.__setattr__(self, "retry_on", normalized_retry)
        if self.max_usd is not None:
            amount = float(self.max_usd)
            if not math.isfinite(amount) or amount < 0:
                raise ProviderBindingError("max_usd must be finite and non-negative")
            object.__setattr__(self, "max_usd", amount)
        if self.max_cost_per_request_usd is not None:
            amount = float(self.max_cost_per_request_usd)
            if not math.isfinite(amount) or amount < 0:
                raise ProviderBindingError(
                    "max_cost_per_request_usd must be finite and non-negative"
                )
            object.__setattr__(self, "max_cost_per_request_usd", amount)
        if self.unknown_cost_policy not in {"reject", "allow_unpriced"}:
            raise ProviderBindingError(
                "unknown_cost_policy must be reject or allow_unpriced"
            )
        if self.unknown_usage_policy not in {"reject", "allow_unknown"}:
            raise ProviderBindingError(
                "unknown_usage_policy must be reject or allow_unknown"
            )
        if self.max_usd is not None and self.max_cost_per_request_usd is None:
            raise ProviderBindingError(
                "fixed cost cap requires a per-request upper bound"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "response_format": self.response_format,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "retry_on": list(self.retry_on),
            "max_requests": self.max_requests,
            "max_usd": self.max_usd,
            "max_cost_per_request_usd": self.max_cost_per_request_usd,
            "unknown_cost_policy": self.unknown_cost_policy,
            "unknown_usage_policy": self.unknown_usage_policy,
        }


@dataclass(frozen=True, slots=True)
class ProviderRequestBinding:
    run_id: str
    spec_hash: str
    experiment_id: str
    condition_id: str
    repeat_id: str
    decision_id: str
    context_bytes: bytes
    prompt_bytes: bytes
    prompt_version: str
    action_interface_version: str
    action_interface_hash: str
    context_interface_version: str
    context_interface_hash: str
    catalogue_version: str
    catalogue_hash: str
    capability_hash: str
    mask_evidence_hash: str
    provider_role: str
    provider: str
    model_id: str
    adapter_version: str
    settings: ProviderSettings = field(default_factory=ProviderSettings)
    request_contract_version: str = PROVIDER_REQUEST_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "experiment_id",
            "condition_id",
            "repeat_id",
            "decision_id",
            "prompt_version",
            "action_interface_version",
            "context_interface_version",
            "catalogue_version",
            "provider_role",
            "provider",
            "model_id",
            "adapter_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ProviderBindingError(f"{name} must be a non-empty string")
        if not _ID_RE.fullmatch(self.decision_id):
            raise ProviderBindingError("decision_id is not canonical")
        for name in (
            "spec_hash",
            "action_interface_hash",
            "context_interface_hash",
            "catalogue_hash",
            "capability_hash",
            "mask_evidence_hash",
        ):
            if not _SHA256_RE.fullmatch(str(getattr(self, name))):
                raise ProviderBindingError(f"{name} must be a lowercase SHA-256")
        if not isinstance(self.context_bytes, bytes) or not self.context_bytes:
            raise ProviderBindingError("context_bytes must be non-empty immutable bytes")
        if not isinstance(self.prompt_bytes, bytes) or not self.prompt_bytes:
            raise ProviderBindingError("prompt_bytes must be non-empty immutable bytes")
        # The projector/prompt surface is UTF-8 by contract.  Decode now so an
        # adapter can never substitute lossy replacement text later.
        try:
            self.context_bytes.decode("utf-8")
            self.prompt_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderBindingError("context and prompt must be UTF-8 bytes") from exc
        if not isinstance(self.settings, ProviderSettings):
            raise ProviderBindingError("settings must be ProviderSettings")
        if self.request_contract_version != PROVIDER_REQUEST_CONTRACT_VERSION:
            raise ProviderBindingError("unsupported request contract version")

    @property
    def context_hash(self) -> str:
        return hashlib.sha256(self.context_bytes).hexdigest()

    @property
    def prompt_hash(self) -> str:
        return hashlib.sha256(self.prompt_bytes).hexdigest()

    @property
    def request_contract_hash(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.to_evidence_dict())).hexdigest()

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "request_contract_version": self.request_contract_version,
            "run_id": self.run_id,
            "spec_hash": self.spec_hash,
            "experiment_id": self.experiment_id,
            "condition_id": self.condition_id,
            "repeat_id": self.repeat_id,
            "decision_id": self.decision_id,
            "context_bytes_b64": _b64(self.context_bytes),
            "context_hash": self.context_hash,
            "prompt_bytes_b64": _b64(self.prompt_bytes),
            "prompt_hash": self.prompt_hash,
            "prompt_version": self.prompt_version,
            "action_interface_version": self.action_interface_version,
            "action_interface_hash": self.action_interface_hash,
            "context_interface_version": self.context_interface_version,
            "context_interface_hash": self.context_interface_hash,
            "catalogue_version": self.catalogue_version,
            "catalogue_hash": self.catalogue_hash,
            "capability_hash": self.capability_hash,
            "mask_evidence_hash": self.mask_evidence_hash,
            "provider_role": self.provider_role,
            "configured_provider": self.provider,
            "configured_model_id": self.model_id,
            "adapter_version": self.adapter_version,
            "settings": self.settings.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RawProviderResponse:
    content_bytes: bytes
    actual_provider: str
    provider_request_id: str | None = None
    returned_model_id: str | None = None
    returned_model_revision: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: str | None = None
    observed_request_contract_hash: str | None = None
    claimed_parsed_proposal_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content_bytes, bytes):
            raise ProviderBindingError("provider content must be immutable bytes")
        if not isinstance(self.actual_provider, str) or not self.actual_provider.strip():
            raise ProviderBindingError("actual_provider must be reported by the adapter")
        if self.actual_provider == NOT_PROVIDED:
            raise ProviderBindingError(
                "RawProviderResponse cannot impersonate a missing response"
            )
        for name in (
            "provider_request_id",
            "returned_model_id",
            "returned_model_revision",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or value == NOT_PROVIDED
            ):
                raise ProviderBindingError(f"{name} must be non-empty or None")
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ProviderBindingError(f"{name} must be a non-negative integer")
        supplied_usage = tuple(
            getattr(self, name)
            for name in ("input_tokens", "output_tokens", "total_tokens")
        )
        if any(value is None for value in supplied_usage) and not all(
            value is None for value in supplied_usage
        ):
            raise ProviderBindingError(
                "provider token usage must be complete or wholly unknown"
            )
        if self.cost_usd is not None:
            amount = float(self.cost_usd)
            if not math.isfinite(amount) or amount < 0:
                raise ProviderBindingError("cost_usd must be finite and non-negative")
            object.__setattr__(self, "cost_usd", amount)
            if not isinstance(self.cost_source, str) or not self.cost_source.strip():
                raise ProviderBindingError("known provider cost requires cost_source")
        elif self.cost_source is not None:
            raise ProviderBindingError("cost_source cannot be set when cost is unknown")
        if all(
            value is not None
            for value in (self.input_tokens, self.output_tokens, self.total_tokens)
        ) and self.total_tokens != self.input_tokens + self.output_tokens:
            raise ProviderBindingError("provider token totals do not reconcile")
        if self.observed_request_contract_hash is not None and not _SHA256_RE.fullmatch(
            self.observed_request_contract_hash
        ):
            raise ProviderBindingError(
                "observed_request_contract_hash must be a SHA-256"
            )
        if self.claimed_parsed_proposal_hash is not None and not _SHA256_RE.fullmatch(
            self.claimed_parsed_proposal_hash
        ):
            raise ProviderBindingError(
                "claimed_parsed_proposal_hash must be a SHA-256"
            )


class ProviderTransport(Protocol):
    """One-physical-request adapter with all opaque SDK retries disabled."""

    transport_contract_version: str
    internal_retries_disabled: bool

    async def send(
        self, binding: ProviderRequestBinding, retry_index: int
    ) -> RawProviderResponse: ...


@dataclass(frozen=True, slots=True)
class ProviderAttemptRecord:
    sequence: int
    retry_index: int
    run_ledger_id: str
    global_request_sequence: int
    global_requests_after_attempt: int
    contract_requests_after_attempt: int
    global_accounted_cost_usd_after_attempt: float
    status: ProviderAttemptStatus
    endpoint: str
    request_contract_hash: str
    observed_request_contract_hash: str
    request: Mapping[str, Any]
    started_monotonic_seconds: float
    finished_monotonic_seconds: float
    latency_seconds: float
    actual_provider: str
    provider_request_id: str
    returned_model_id: str
    returned_model_revision: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    accounted_cost_usd: float
    cost_source: str
    raw_response_bytes_b64: str
    raw_response_hash: str
    claimed_parsed_proposal_hash: str
    parsed_proposal_hash: str | None
    parser_version: str
    rejection_reasons: tuple[str, ...] = ()
    schema_version: str = PROVIDER_ATTEMPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROVIDER_ATTEMPT_SCHEMA_VERSION:
            raise ProviderBindingError("unsupported provider attempt schema")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise ProviderBindingError("attempt sequence must be non-negative")
        if not isinstance(self.retry_index, int) or isinstance(self.retry_index, bool) or self.retry_index < 0:
            raise ProviderBindingError("retry_index must be non-negative")
        if not _SHA256_RE.fullmatch(self.run_ledger_id):
            raise ProviderBindingError("run_ledger_id must be a SHA-256 token")
        for name in (
            "global_request_sequence",
            "global_requests_after_attempt",
            "contract_requests_after_attempt",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ProviderBindingError(f"{name} must be non-negative")
        if self.global_requests_after_attempt != self.global_request_sequence + 1:
            raise ProviderBindingError("global request sequence/count mismatch")
        if not 1 <= self.contract_requests_after_attempt <= self.global_requests_after_attempt:
            raise ProviderBindingError("contract request count is inconsistent")
        if (
            not isinstance(self.global_accounted_cost_usd_after_attempt, (int, float))
            or isinstance(self.global_accounted_cost_usd_after_attempt, bool)
            or not math.isfinite(float(self.global_accounted_cost_usd_after_attempt))
            or float(self.global_accounted_cost_usd_after_attempt) < 0
        ):
            raise ProviderBindingError("global accounted cost must be non-negative")
        if not isinstance(self.status, ProviderAttemptStatus):
            raise ProviderBindingError("attempt status must be typed")
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ProviderBindingError("attempt endpoint must be non-empty")
        for name in ("request_contract_hash", "raw_response_hash"):
            if not isinstance(getattr(self, name), str) or not _SHA256_RE.fullmatch(
                getattr(self, name)
            ):
                raise ProviderBindingError(f"{name} must be a SHA-256")
        for name in (
            "observed_request_contract_hash",
            "claimed_parsed_proposal_hash",
        ):
            value = getattr(self, name)
            if value != NOT_PROVIDED and (
                not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
            ):
                raise ProviderBindingError(f"{name} must be a SHA-256 or unknown")
        if self.parsed_proposal_hash is not None and (
            not isinstance(self.parsed_proposal_hash, str)
            or not _SHA256_RE.fullmatch(self.parsed_proposal_hash)
        ):
            raise ProviderBindingError("parsed_proposal_hash must be a SHA-256 or None")
        if not isinstance(self.request, Mapping):
            raise ProviderBindingError("provider attempt request must be a mapping")
        for name in (
            "started_monotonic_seconds",
            "finished_monotonic_seconds",
            "latency_seconds",
            "accounted_cost_usd",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ProviderBindingError(f"{name} must be finite and non-negative")
        if self.finished_monotonic_seconds < self.started_monotonic_seconds:
            raise ProviderBindingError("provider attempt timestamps are reversed")
        if not math.isclose(
            self.latency_seconds,
            self.finished_monotonic_seconds - self.started_monotonic_seconds,
            abs_tol=1e-9,
        ):
            raise ProviderBindingError("provider attempt latency does not reconcile")
        for name in (
            "actual_provider",
            "provider_request_id",
            "returned_model_id",
            "returned_model_revision",
            "cost_source",
            "parser_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ProviderBindingError(f"{name} must be a non-empty string")
        if not isinstance(self.raw_response_bytes_b64, str):
            raise ProviderBindingError("raw_response_bytes_b64 must be a string")
        if self.parser_version != STRICT_PROVIDER_PARSER_VERSION:
            raise ProviderBindingError("provider parser version is unsupported")
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ProviderBindingError(f"{name} must be a non-negative integer")
        usage = (self.input_tokens, self.output_tokens, self.total_tokens)
        if any(value is None for value in usage) and not all(
            value is None for value in usage
        ):
            raise ProviderBindingError("attempt token usage must be complete or unknown")
        if all(value is not None for value in usage) and (
            self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ProviderBindingError("attempt token totals do not reconcile")
        if self.cost_usd is not None and (
            not isinstance(self.cost_usd, (int, float))
            or isinstance(self.cost_usd, bool)
            or not math.isfinite(float(self.cost_usd))
            or float(self.cost_usd) < 0
        ):
            raise ProviderBindingError("attempt cost must be finite and non-negative")
        if self.cost_usd is None and self.cost_source != NOT_PROVIDED:
            raise ProviderBindingError("unknown attempt cost cannot have a source")
        if self.cost_usd is not None and self.cost_source == NOT_PROVIDED:
            raise ProviderBindingError("known attempt cost requires a source")
        try:
            raw = base64.b64decode(
                self.raw_response_bytes_b64.encode("ascii"), validate=True
            )
        except (UnicodeEncodeError, ValueError) as exc:
            raise ProviderBindingError("attempt raw response is not base64") from exc
        if _b64(raw) != self.raw_response_bytes_b64:
            raise ProviderBindingError("attempt raw response base64 is not canonical")
        if hashlib.sha256(raw).hexdigest() != self.raw_response_hash:
            raise ProviderBindingError("attempt raw response hash mismatch")
        has_response = self.actual_provider != NOT_PROVIDED
        if not has_response:
            if any(
                value != NOT_PROVIDED
                for value in (
                    self.observed_request_contract_hash,
                    self.provider_request_id,
                    self.returned_model_id,
                    self.returned_model_revision,
                    self.claimed_parsed_proposal_hash,
                )
            ) or raw or any(value is not None for value in usage) or self.cost_usd is not None:
                raise ProviderBindingError(
                    "missing provider response has response-only evidence"
                )
            if self.parsed_proposal_hash is not None:
                raise ProviderBindingError(
                    "missing provider response cannot have a parsed proposal"
                )
        if self.status is ProviderAttemptStatus.SUCCEEDED:
            if not has_response:
                raise ProviderBindingError("successful attempt requires a response")
            if self.observed_request_contract_hash == NOT_PROVIDED:
                raise ProviderBindingError(
                    "successful attempt requires observed request binding"
                )
            if self.parsed_proposal_hash is None:
                raise ProviderBindingError(
                    "successful attempt requires parsed proposal evidence"
                )
        elif self.parsed_proposal_hash is not None:
            raise ProviderBindingError(
                "non-success attempt cannot carry parsed proposal evidence"
            )
        object.__setattr__(self, "request", _deep_freeze(self.request))
        object.__setattr__(self, "rejection_reasons", tuple(self.rejection_reasons))

    def to_dict(self) -> dict[str, Any]:
        reserved_unknown_cost_usd = (
            self.accounted_cost_usd if self.cost_usd is None else 0.0
        )
        usage_unknown = self.total_tokens is None
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "retry_index": self.retry_index,
            "run_ledger_id": self.run_ledger_id,
            "global_request_sequence": self.global_request_sequence,
            "global_requests_after_attempt": self.global_requests_after_attempt,
            "contract_requests_after_attempt": self.contract_requests_after_attempt,
            "global_accounted_cost_usd_after_attempt": (
                self.global_accounted_cost_usd_after_attempt
            ),
            "status": self.status.value,
            "endpoint": self.endpoint,
            "request_contract_hash": self.request_contract_hash,
            "observed_request_contract_hash": self.observed_request_contract_hash,
            "request": _deep_thaw(self.request),
            "started_monotonic_seconds": self.started_monotonic_seconds,
            "finished_monotonic_seconds": self.finished_monotonic_seconds,
            "latency_seconds": self.latency_seconds,
            "actual_provider": self.actual_provider,
            "provider_request_id": self.provider_request_id,
            "returned_model_id": self.returned_model_id,
            "returned_model_revision": self.returned_model_revision,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "actual_input_tokens": self.input_tokens,
            "actual_output_tokens": self.output_tokens,
            "actual_total_tokens": self.total_tokens,
            "actual_cost_usd": self.cost_usd,
            "reserved_unknown_cost_usd": reserved_unknown_cost_usd,
            "usage_status": "UNKNOWN" if usage_unknown else "EXACT",
            "cost_status": (
                "UNKNOWN_RESERVED" if self.cost_usd is None else "EXACT"
            ),
            "accounted_cost_usd": self.accounted_cost_usd,
            "cost_source": self.cost_source,
            "raw_response_bytes_b64": self.raw_response_bytes_b64,
            "raw_response_hash": self.raw_response_hash,
            "claimed_parsed_proposal_hash": self.claimed_parsed_proposal_hash,
            "parsed_proposal_hash": self.parsed_proposal_hash,
            "parser_version": self.parser_version,
            "rejection_reasons": list(self.rejection_reasons),
        }


@dataclass(frozen=True, slots=True)
class BoundProviderProposal:
    proposal: ActionProposal
    request_contract_hash: str
    raw_response_bytes: bytes
    raw_response_hash: str
    parsed_proposal_hash: str
    parser_version: str = STRICT_PROVIDER_PARSER_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_response_bytes", bytes(self.raw_response_bytes))

    def verify_for_dispatch(
        self,
        active_binding: ProviderRequestBinding,
        *,
        current_raw_response: bytes | None = None,
    ) -> ActionProposal:
        reasons: list[str] = []
        if active_binding.request_contract_hash != self.request_contract_hash:
            reasons.append("active_request_contract_changed")
        raw = self.raw_response_bytes if current_raw_response is None else bytes(current_raw_response)
        if hashlib.sha256(raw).hexdigest() != self.raw_response_hash:
            reasons.append("raw_response_changed_after_parse")
        try:
            reparsed = parse_exact_action_proposal(raw)
        except StrictProviderParseError:
            reasons.append("raw_response_no_longer_strictly_parseable")
        else:
            if reparsed.proposal_hash != self.parsed_proposal_hash:
                reasons.append("raw_parsed_hash_disagreement")
            if reparsed != self.proposal:
                reasons.append("parsed_proposal_changed")
        reasons.extend(_proposal_binding_reasons(self.proposal, active_binding))
        if reasons:
            raise ProviderBindingError("; ".join(reasons))
        return self.proposal


@dataclass(frozen=True, slots=True)
class ProviderDecision:
    accepted: bool
    endpoint: str
    attempts: tuple[ProviderAttemptRecord, ...]
    bound_proposal: BoundProviderProposal | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempts", tuple(self.attempts))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if self.accepted != (self.bound_proposal is not None and not self.reasons):
            raise ValueError("provider decision is internally inconsistent")


class ProviderTaskCancelled(asyncio.CancelledError):
    """Task cancellation propagated only after immutable attempt evidence exists.

    ``asyncio.CancelledError`` must keep its control-flow meaning.  The observer
    therefore does not turn task cancellation into a normal ``ProviderDecision``;
    it raises this typed subtype with the complete, already-validated attempt
    records attached for persistence by the run-level evidence owner.
    """

    def __init__(
        self,
        *,
        request_contract_hash: str,
        attempts: Sequence[ProviderAttemptRecord],
        evidence_errors: Sequence[str] = (),
    ) -> None:
        super().__init__("provider_task_cancelled")
        self.request_contract_hash = request_contract_hash
        self.attempts = tuple(attempts)
        self.evidence_errors = tuple(str(item) for item in evidence_errors)
        self.endpoint = "CANCELLATION"
        self.reasons = ("provider_task_cancelled",)


@dataclass(frozen=True, slots=True)
class ProviderRequestAuthorization:
    run_ledger_id: str
    request_contract_hash: str
    global_request_sequence: int
    global_requests_after_authorization: int
    contract_requests_after_authorization: int

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.run_ledger_id) or not _SHA256_RE.fullmatch(
            self.request_contract_hash
        ):
            raise ProviderBindingError("provider authorization hashes are invalid")
        if (
            not isinstance(self.global_request_sequence, int)
            or isinstance(self.global_request_sequence, bool)
            or self.global_request_sequence < 0
            or not isinstance(self.global_requests_after_authorization, int)
            or isinstance(self.global_requests_after_authorization, bool)
            or self.global_requests_after_authorization
            != self.global_request_sequence + 1
            or not isinstance(self.contract_requests_after_authorization, int)
            or isinstance(self.contract_requests_after_authorization, bool)
            or not 1
            <= self.contract_requests_after_authorization
            <= self.global_requests_after_authorization
        ):
            raise ProviderBindingError("provider authorization counters are invalid")


@dataclass(frozen=True, slots=True)
class ProviderRunLedgerSnapshot:
    run_ledger_id: str
    run_id: str
    spec_hash: str
    global_requests: int
    cumulative_accounted_cost_usd: float
    requests_by_contract: Mapping[str, int]
    unsettled_requests: int

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.run_ledger_id):
            raise ProviderBindingError("run ledger snapshot ID is invalid")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ProviderBindingError("run ledger snapshot run_id is invalid")
        if not _SHA256_RE.fullmatch(self.spec_hash):
            raise ProviderBindingError("run ledger snapshot spec_hash is invalid")
        if (
            not isinstance(self.global_requests, int)
            or isinstance(self.global_requests, bool)
            or self.global_requests < 0
            or not isinstance(self.unsettled_requests, int)
            or isinstance(self.unsettled_requests, bool)
            or self.unsettled_requests not in {0, 1}
        ):
            raise ProviderBindingError("run ledger snapshot counters are invalid")
        if (
            not isinstance(self.cumulative_accounted_cost_usd, (int, float))
            or isinstance(self.cumulative_accounted_cost_usd, bool)
            or not math.isfinite(float(self.cumulative_accounted_cost_usd))
            or float(self.cumulative_accounted_cost_usd) < 0
        ):
            raise ProviderBindingError("run ledger snapshot cost is invalid")
        if not isinstance(self.requests_by_contract, Mapping) or any(
            not isinstance(key, str)
            or _SHA256_RE.fullmatch(key) is None
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for key, count in self.requests_by_contract.items()
        ):
            raise ProviderBindingError("run ledger snapshot contract counts are invalid")
        if sum(self.requests_by_contract.values()) != self.global_requests:
            raise ProviderBindingError("run ledger snapshot counts do not reconcile")
        object.__setattr__(
            self,
            "requests_by_contract",
            MappingProxyType(dict(self.requests_by_contract)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_ledger_id": self.run_ledger_id,
            "run_id": self.run_id,
            "spec_hash": self.spec_hash,
            "global_requests": self.global_requests,
            "cumulative_accounted_cost_usd": self.cumulative_accounted_cost_usd,
            "requests_by_contract": dict(self.requests_by_contract),
            "unsettled_requests": self.unsettled_requests,
        }


class ProviderCallBudget:
    """Pre-authorize and account every actual provider request."""

    def __init__(self, settings: ProviderSettings) -> None:
        self.settings = settings
        self.requests = 0
        self.cost_usd = 0.0
        self._reserved_cost_usd = 0.0
        self._unsettled_requests = 0
        self._requests_by_contract: dict[str, int] = {}
        self._unsettled_by_contract: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def requests_by_contract(self) -> Mapping[str, int]:
        with self._lock:
            return MappingProxyType(dict(self._requests_by_contract))

    def authorize_request(self, request_contract_hash: str) -> None:
        if not _SHA256_RE.fullmatch(request_contract_hash):
            raise ProviderBindingError("request contract hash is invalid")
        with self._lock:
            # Keep zero-valued contract counters too.  This makes a rejected
            # attempt to bypass the run cap by changing context/decision
            # directly visible in an observer snapshot.
            self._requests_by_contract.setdefault(request_contract_hash, 0)
            self._unsettled_by_contract.setdefault(request_contract_hash, 0)
            if self.requests + 1 > self.settings.max_requests:
                raise ProviderBindingError("llm_request_budget_exhausted")
            upper = self.settings.max_cost_per_request_usd
            if (
                self.settings.max_usd is not None
                and upper is not None
                and self.cost_usd + self._reserved_cost_usd + upper
                > self.settings.max_usd
            ):
                raise ProviderBindingError(
                    "provider_cost_budget_exhausted_before_call"
                )
            self.requests += 1
            self._unsettled_requests += 1
            self._requests_by_contract[request_contract_hash] += 1
            self._unsettled_by_contract[request_contract_hash] += 1
            if upper is not None:
                self._reserved_cost_usd += upper

    def cancel_authorization(self, request_contract_hash: str) -> None:
        """Roll back a preauthorization when no transport call was entered."""

        with self._lock:
            if (
                self._unsettled_requests <= 0
                or self.requests <= 0
                or self._unsettled_by_contract.get(request_contract_hash, 0) <= 0
                or self._requests_by_contract.get(request_contract_hash, 0) <= 0
            ):
                raise RuntimeError("provider authorization is not pending")
            self.requests -= 1
            self._unsettled_requests -= 1
            self._requests_by_contract[request_contract_hash] -= 1
            self._unsettled_by_contract[request_contract_hash] -= 1
            upper = self.settings.max_cost_per_request_usd
            if upper is not None:
                self._reserved_cost_usd = max(
                    0.0, self._reserved_cost_usd - upper
                )

    def _begin_settlement(self, request_contract_hash: str) -> None:
        if (
            self._unsettled_requests <= 0
            or self._unsettled_by_contract.get(request_contract_hash, 0) <= 0
        ):
            raise RuntimeError("provider request was not preauthorized")
        self._unsettled_requests -= 1
        self._unsettled_by_contract[request_contract_hash] -= 1
        upper = self.settings.max_cost_per_request_usd
        if upper is not None:
            self._reserved_cost_usd = max(
                0.0, self._reserved_cost_usd - upper
            )

    def settle_known(
        self, request_contract_hash: str, cost_usd: float
    ) -> tuple[float, str | None]:
        amount = float(cost_usd)
        if not math.isfinite(amount) or amount < 0:
            raise ProviderBindingError("settled provider cost must be non-negative")
        with self._lock:
            self._begin_settlement(request_contract_hash)
            self.cost_usd += amount
            upper = self.settings.max_cost_per_request_usd
            if upper is not None and amount > upper:
                return amount, (
                    "provider_cost_exceeded_declared_upper:"
                    f"{amount:.9f}>{upper:.9f}"
                )
            if (
                self.settings.max_usd is not None
                and self.cost_usd > self.settings.max_usd
            ):
                return amount, (
                    f"provider_cost_cap:{self.cost_usd:.9f}>"
                    f"{self.settings.max_usd:.9f}"
                )
            return amount, None

    def settle_unknown(
        self,
        request_contract_hash: str,
        *,
        missing_response_evidence: bool,
    ) -> tuple[float, str | None]:
        with self._lock:
            self._begin_settlement(request_contract_hash)
            upper = self.settings.max_cost_per_request_usd
            if upper is not None:
                self.cost_usd += upper
                if (
                    self.settings.max_usd is not None
                    and self.cost_usd > self.settings.max_usd
                ):
                    return upper, "provider_cost_cap_exceeded_by_unknown_attempt"
                if (
                    missing_response_evidence
                    and self.settings.unknown_cost_policy == "reject"
                ):
                    return upper, "unknown_provider_cost_rejected"
                return upper, None
            if self.settings.unknown_cost_policy == "reject":
                return 0.0, "unknown_provider_cost_rejected"
            return 0.0, None


class ProviderRunLedger:
    """One identity- and policy-bound ledger owned by an entire DoF run.

    A run owner creates this object once and injects it into every provider
    observer.  Configuration and identity are immutable; only its serialized,
    lock-protected accounting state advances.  Exactly one unsettled physical
    request is allowed so global request order and cumulative cost remain
    unambiguous in independently persisted decision traces.
    """

    __slots__ = (
        "_run_id",
        "_spec_hash",
        "_settings",
        "_run_ledger_id",
        "_budget",
        "_active_authorization",
        "_lock",
    )

    def __init__(
        self,
        *,
        run_id: str,
        spec_hash: str,
        settings: ProviderSettings,
        run_ledger_id: str | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ProviderBindingError("run ledger run_id must be non-empty")
        if not _SHA256_RE.fullmatch(spec_hash):
            raise ProviderBindingError("run ledger spec_hash must be SHA-256")
        if not isinstance(settings, ProviderSettings):
            raise ProviderBindingError("run ledger settings must be ProviderSettings")
        ledger_id = run_ledger_id or secrets.token_hex(32)
        if not _SHA256_RE.fullmatch(ledger_id):
            raise ProviderBindingError("run_ledger_id must be a SHA-256 token")
        self._run_id = run_id
        self._spec_hash = spec_hash
        self._settings = settings
        self._run_ledger_id = ledger_id
        self._budget = ProviderCallBudget(settings)
        self._active_authorization: ProviderRequestAuthorization | None = None
        self._lock = threading.Lock()

    @classmethod
    def for_binding(cls, binding: ProviderRequestBinding) -> "ProviderRunLedger":
        return cls(
            run_id=binding.run_id,
            spec_hash=binding.spec_hash,
            settings=binding.settings,
        )

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def spec_hash(self) -> str:
        return self._spec_hash

    @property
    def settings(self) -> ProviderSettings:
        return self._settings

    @property
    def run_ledger_id(self) -> str:
        return self._run_ledger_id

    def ensure_binding(self, binding: ProviderRequestBinding) -> None:
        if (binding.run_id, binding.spec_hash) != (self.run_id, self.spec_hash):
            raise ProviderBindingError("provider_run_ledger_binding_mismatch")
        if _budget_policy(binding.settings) != _budget_policy(self.settings):
            raise ProviderBindingError("provider_run_budget_policy_mismatch")

    @property
    def requests_by_contract(self) -> Mapping[str, int]:
        return self._budget.requests_by_contract

    def authorize_request(
        self, binding: ProviderRequestBinding
    ) -> ProviderRequestAuthorization:
        self.ensure_binding(binding)
        contract_hash = binding.request_contract_hash
        with self._lock:
            if self._active_authorization is not None:
                raise ProviderBindingError("concurrent_provider_request_not_supported")
            self._budget.authorize_request(contract_hash)
            contract_count = self._budget.requests_by_contract[contract_hash]
            authorization = ProviderRequestAuthorization(
                self.run_ledger_id,
                contract_hash,
                self._budget.requests - 1,
                self._budget.requests,
                contract_count,
            )
            self._active_authorization = authorization
            return authorization

    def cancel_authorization(
        self, authorization: ProviderRequestAuthorization
    ) -> None:
        with self._lock:
            self._require_active(authorization)
            self._budget.cancel_authorization(
                authorization.request_contract_hash
            )
            self._active_authorization = None

    def settle_known(
        self,
        authorization: ProviderRequestAuthorization,
        cost_usd: float,
    ) -> tuple[float, str | None, ProviderRunLedgerSnapshot]:
        with self._lock:
            self._require_active(authorization)
            amount, reason = self._budget.settle_known(
                authorization.request_contract_hash, cost_usd
            )
            self._active_authorization = None
            return amount, reason, self._snapshot_unlocked()

    def settle_unknown(
        self,
        authorization: ProviderRequestAuthorization,
        *,
        missing_response_evidence: bool,
    ) -> tuple[float, str | None, ProviderRunLedgerSnapshot]:
        with self._lock:
            self._require_active(authorization)
            amount, reason = self._budget.settle_unknown(
                authorization.request_contract_hash,
                missing_response_evidence=missing_response_evidence,
            )
            self._active_authorization = None
            return amount, reason, self._snapshot_unlocked()

    def snapshot(self) -> ProviderRunLedgerSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> ProviderRunLedgerSnapshot:
        return ProviderRunLedgerSnapshot(
            self.run_ledger_id,
            self.run_id,
            self.spec_hash,
            self._budget.requests,
            self._budget.cost_usd,
            self._budget.requests_by_contract,
            1 if self._active_authorization is not None else 0,
        )

    def _require_active(
        self, authorization: ProviderRequestAuthorization
    ) -> None:
        if authorization is not self._active_authorization:
            raise RuntimeError("provider authorization is not active")


class DoFProviderObserver:
    """Issue bounded provider attempts and return only a verified typed proposal.

    Construction without ``run_ledger`` is permitted for wiring/parser-only
    code, but every transport request fails closed until the run owner injects
    its unique shared :class:`ProviderRunLedger`.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        request_authorizer: Callable[[], None] | None = None,
        run_ledger: ProviderRunLedger | None = None,
    ) -> None:
        self.monotonic = monotonic
        self.request_authorizer = request_authorizer or (lambda: None)
        self._budget_lock = threading.Lock()
        if run_ledger is not None and not isinstance(run_ledger, ProviderRunLedger):
            raise TypeError("run_ledger must be ProviderRunLedger")
        self._run_ledger = run_ledger

    @property
    def request_counts_by_contract(self) -> Mapping[str, int]:
        """Read-only diagnostic counters within the shared observer/run cap."""

        with self._budget_lock:
            ledger = self._run_ledger
        if ledger is None:
            return MappingProxyType({})
        return ledger.requests_by_contract

    @property
    def run_ledger(self) -> ProviderRunLedger | None:
        with self._budget_lock:
            return self._run_ledger

    def _budget_for(self, binding: ProviderRequestBinding) -> ProviderRunLedger:
        with self._budget_lock:
            ledger = self._run_ledger
            if ledger is None:
                # Construction remains cheap for parser-only callers, but no
                # transport may be entered without a run-owner capability.
                # Creating a private ledger here would let a second observer
                # reset the physical-request cap.
                raise ProviderBindingError(
                    "explicit_run_owner_provider_ledger_required"
                )
            ledger.ensure_binding(binding)
            return ledger

    def request(
        self,
        binding: ProviderRequestBinding,
        transport: ProviderTransport,
    ) -> ProviderDecision:
        """Synchronous convenience for non-async callers and tests."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.request_async(binding, transport))
        raise RuntimeError("use request_async inside an active event loop")

    async def request_async(
        self,
        binding: ProviderRequestBinding,
        transport: ProviderTransport,
    ) -> ProviderDecision:
        if not inspect.iscoroutinefunction(getattr(transport, "send", None)):
            raise ProviderBindingError("provider transport send must be async")
        if (
            getattr(transport, "transport_contract_version", None)
            != PROVIDER_TRANSPORT_CONTRACT_VERSION
            or getattr(transport, "internal_retries_disabled", None) is not True
        ):
            raise ProviderBindingError(
                "provider transport must declare one physical request per send "
                "with SDK retries disabled"
            )
        budget = self._budget_for(binding)
        attempts: list[ProviderAttemptRecord] = []
        last_reasons: tuple[str, ...] = ()
        last_endpoint = "PROVIDER_ERROR"
        for retry_index in range(binding.settings.max_retries + 1):
            try:
                authorization = budget.authorize_request(binding)
            except ProviderBindingError as exc:
                return ProviderDecision(
                    False,
                    "BUDGET_REJECTION",
                    tuple(attempts),
                    reasons=(str(exc),),
                )
            try:
                self.request_authorizer()
            except ProviderBindingError as exc:
                budget.cancel_authorization(authorization)
                return ProviderDecision(
                    False,
                    "BUDGET_REJECTION",
                    tuple(attempts),
                    reasons=(_bounded_diagnostic(exc),),
                )
            except BaseException:
                # The transport was never entered, so do not consume the
                # persistent local request/cost reservation on an external
                # authorizer failure.
                budget.cancel_authorization(authorization)
                raise
            started = self.monotonic()
            response: RawProviderResponse | None = None
            status = ProviderAttemptStatus.PROVIDER_ERROR
            endpoint = "PROVIDER_ERROR"
            reasons: tuple[str, ...] = ()
            task_cancelled = False
            try:
                async with asyncio.timeout(binding.settings.timeout_seconds):
                    response = await transport.send(binding, retry_index)
                if not isinstance(response, RawProviderResponse):
                    raise ProviderTransportError("provider returned the wrong response contract")
            except TimeoutError:
                status = ProviderAttemptStatus.TIMED_OUT
                endpoint = "TIMEOUT"
                reasons = ("provider_timeout_enforced",)
            except asyncio.CancelledError:
                status = ProviderAttemptStatus.CANCELLED
                endpoint = "CANCELLATION"
                reasons = ("provider_task_cancelled",)
                task_cancelled = True
                # Do not retain the source cancellation message: task.cancel()
                # accepts arbitrary text and it is not trusted evidence.
            except ProviderCancelled as exc:
                status = ProviderAttemptStatus.CANCELLED
                endpoint = "CANCELLATION"
                reasons = (_bounded_diagnostic(exc, fallback="provider_cancelled"),)
            except ProviderTimeout as exc:
                status = ProviderAttemptStatus.TIMED_OUT
                endpoint = "TIMEOUT"
                reasons = (_bounded_diagnostic(exc, fallback="provider_timeout"),)
            except Exception as exc:
                status = ProviderAttemptStatus.PROVIDER_ERROR
                endpoint = "PROVIDER_ERROR"
                reasons = (_bounded_diagnostic(exc),)
            finished = self.monotonic()

            proposal: ActionProposal | None = None
            accounted_cost_usd = 0.0
            accounting_snapshot: ProviderRunLedgerSnapshot
            if response is not None:
                binding_reasons = _response_binding_reasons(response, binding)
                if response.cost_usd is None:
                    (
                        accounted_cost_usd,
                        cost_reason,
                        accounting_snapshot,
                    ) = budget.settle_unknown(
                        authorization,
                        missing_response_evidence=True
                    )
                else:
                    (
                        accounted_cost_usd,
                        cost_reason,
                        accounting_snapshot,
                    ) = budget.settle_known(
                        authorization,
                        response.cost_usd
                    )
                usage_missing = any(
                    value is None
                    for value in (
                        response.input_tokens,
                        response.output_tokens,
                        response.total_tokens,
                    )
                )
                usage_reason = (
                    "unknown_provider_usage_rejected"
                    if usage_missing
                    and binding.settings.unknown_usage_policy == "reject"
                    else None
                )
                if binding_reasons:
                    status = ProviderAttemptStatus.BINDING_REJECTED
                    endpoint = "PROVIDER_BINDING_MISMATCH"
                    reasons = tuple(binding_reasons)
                elif cost_reason is not None:
                    status = ProviderAttemptStatus.COST_REJECTED
                    endpoint = "BUDGET_REJECTION"
                    reasons = (cost_reason,)
                elif usage_reason is not None:
                    status = ProviderAttemptStatus.USAGE_REJECTED
                    endpoint = "BUDGET_REJECTION"
                    reasons = (usage_reason,)
                else:
                    try:
                        proposal = parse_exact_action_proposal(response.content_bytes)
                    except StrictProviderParseError as exc:
                        status = ProviderAttemptStatus.POLICY_OUTPUT_REJECTED
                        endpoint = "INVALID_POLICY_OUTPUT"
                        reasons = exc.reasons
                    else:
                        proposal_reasons = _proposal_binding_reasons(proposal, binding)
                        if proposal_reasons:
                            # The provider response itself is exactly bound at
                            # this point.  These fields are generated inside
                            # the model's action envelope, so a mismatch is a
                            # strict no-dispatch policy-output terminal rather
                            # than provider/route evidence corruption.
                            status = ProviderAttemptStatus.POLICY_OUTPUT_REJECTED
                            endpoint = "INVALID_POLICY_OUTPUT"
                            reasons = tuple(proposal_reasons)
                            proposal = None
                        elif (
                            response.claimed_parsed_proposal_hash is not None
                            and response.claimed_parsed_proposal_hash
                            != proposal.proposal_hash
                        ):
                            status = ProviderAttemptStatus.BINDING_REJECTED
                            endpoint = "PROVIDER_BINDING_MISMATCH"
                            reasons = ("raw_parsed_hash_disagreement",)
                            proposal = None
                        else:
                            status = ProviderAttemptStatus.SUCCEEDED
                            endpoint = "PROVIDER_RESPONSE_BOUND"
                            reasons = ()
            else:
                (
                    accounted_cost_usd,
                    cost_reason,
                    accounting_snapshot,
                ) = budget.settle_unknown(
                    authorization,
                    missing_response_evidence=False
                )
                if cost_reason is not None:
                    # With no verified response, timeout/cancellation/provider
                    # error is the primary attempt fact.  Unknown actual cost is
                    # a separate accounting property and must never overwrite
                    # that status with COST_REJECTED.
                    reasons = (*reasons, cost_reason)

            reasons = tuple(_sanitize_diagnostic_text(reason) for reason in reasons)

            record = _attempt_record(
                binding=binding,
                sequence=len(attempts),
                retry_index=retry_index,
                status=status,
                endpoint=endpoint,
                started=started,
                finished=finished,
                response=response,
                proposal=proposal,
                reasons=reasons,
                accounted_cost_usd=accounted_cost_usd,
                authorization=authorization,
                accounting_snapshot=accounting_snapshot,
            )
            attempts.append(record)
            if task_cancelled:
                evidence_errors = verify_provider_attempts(attempts, binding)
                cancelled = ProviderTaskCancelled(
                    request_contract_hash=binding.request_contract_hash,
                    attempts=attempts,
                    evidence_errors=evidence_errors,
                )
                raise cancelled from None
            if status is ProviderAttemptStatus.SUCCEEDED and response is not None and proposal is not None:
                bound = BoundProviderProposal(
                    proposal,
                    binding.request_contract_hash,
                    response.content_bytes,
                    record.raw_response_hash,
                    proposal.proposal_hash,
                )
                # Re-verify at the observer/gate hand-off.  The caller must do
                # the same immediately before dispatch.
                bound.verify_for_dispatch(binding)
                return ProviderDecision(True, endpoint, tuple(attempts), bound)

            last_reasons = reasons
            last_endpoint = endpoint
            retry_key = status.value
            can_retry = (
                retry_index < binding.settings.max_retries
                and retry_key in binding.settings.retry_on
                and not task_cancelled
            )
            if not can_retry:
                break
        return ProviderDecision(
            False,
            last_endpoint,
            tuple(attempts),
            reasons=last_reasons or ("provider_attempt_failed",),
        )


class ScriptedFakeProvider:
    """One-call-per-script-entry transport used by offline acceptance tests."""

    def __init__(self, script: Sequence[RawProviderResponse | BaseException]) -> None:
        self.script = tuple(script)
        self.calls: list[tuple[str, int]] = []
        self.transport_contract_version = PROVIDER_TRANSPORT_CONTRACT_VERSION
        self.internal_retries_disabled = True

    async def send(
        self, binding: ProviderRequestBinding, retry_index: int
    ) -> RawProviderResponse:
        self.calls.append((binding.request_contract_hash, retry_index))
        if retry_index >= len(self.script):
            raise ProviderTransportError("fake provider script exhausted")
        item = self.script[retry_index]
        if isinstance(item, BaseException):
            raise item
        if item.observed_request_contract_hash is not None:
            return item
        return RawProviderResponse(
            content_bytes=item.content_bytes,
            actual_provider=item.actual_provider,
            provider_request_id=item.provider_request_id,
            returned_model_id=item.returned_model_id,
            returned_model_revision=item.returned_model_revision,
            input_tokens=item.input_tokens,
            output_tokens=item.output_tokens,
            total_tokens=item.total_tokens,
            cost_usd=item.cost_usd,
            cost_source=item.cost_source,
            observed_request_contract_hash=binding.request_contract_hash,
            claimed_parsed_proposal_hash=item.claimed_parsed_proposal_hash,
        )


def parse_exact_action_proposal(raw: bytes) -> ActionProposal:
    """Parse one UTF-8 JSON object with no repair, salvage or duplicate keys."""

    if not isinstance(raw, bytes):
        raise StrictProviderParseError(("raw_response_not_bytes",))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise StrictProviderParseError(("raw_response_not_utf8",)) from None
    if text.startswith("\ufeff"):
        raise StrictProviderParseError(("utf8_bom_not_allowed",))
    duplicate_paths: list[str] = []

    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicate_paths.append(key)
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"non_standard_json_constant:{value}")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=strict_object,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise StrictProviderParseError((f"exact_json_parse_failed:{exc}",)) from exc
    if duplicate_paths:
        raise StrictProviderParseError(
            ("duplicate_json_keys:" + ",".join(sorted(set(duplicate_paths))),)
        )
    if not isinstance(payload, dict):
        raise StrictProviderParseError(("json_root_not_object",))
    try:
        return parse_action_proposal(payload)
    except ProposalParseError as exc:
        raise StrictProviderParseError(exc.reasons) from exc


def write_provider_attempt_trace(
    writer: ImmutableEvidenceWriter,
    attempts: Sequence[ProviderAttemptRecord],
    *,
    binding: ProviderRequestBinding,
    uri: str = "evidence/provider_attempts.jsonl",
) -> ImmutableSidecar:
    """Seal exact request/response attempts as one registered JSONL sidecar."""

    errors = verify_provider_attempts(attempts, binding)
    if errors:
        raise ProviderBindingError("provider trace verification failed: " + "; ".join(errors))
    return writer.write_jsonl(
        uri,
        (attempt.to_dict() for attempt in attempts),
        role=PROVIDER_TRACE_ROLE,
        schema_version=PROVIDER_ATTEMPT_SCHEMA_VERSION,
    )


def verify_provider_attempts(
    attempts: Sequence[ProviderAttemptRecord],
    binding: ProviderRequestBinding,
) -> tuple[str, ...]:
    errors: list[str] = []
    if len(attempts) > binding.settings.max_requests:
        errors.append("provider attempts exceed max_requests")
    if len(attempts) > binding.settings.max_retries + 1:
        errors.append("provider attempts exceed retry envelope")
    expected_endpoints = {
        ProviderAttemptStatus.SUCCEEDED: "PROVIDER_RESPONSE_BOUND",
        ProviderAttemptStatus.PROVIDER_ERROR: "PROVIDER_ERROR",
        ProviderAttemptStatus.TIMED_OUT: "TIMEOUT",
        ProviderAttemptStatus.CANCELLED: "CANCELLATION",
        ProviderAttemptStatus.BINDING_REJECTED: "PROVIDER_BINDING_MISMATCH",
        ProviderAttemptStatus.POLICY_OUTPUT_REJECTED: "INVALID_POLICY_OUTPUT",
        ProviderAttemptStatus.COST_REJECTED: "BUDGET_REJECTION",
        ProviderAttemptStatus.USAGE_REJECTED: "BUDGET_REJECTION",
    }
    cumulative_accounted_cost = 0.0
    success_seen = False
    trace_ledger_id: str | None = None
    previous_global_sequence: int | None = None
    previous_contract_count: int | None = None
    previous_global_cost: float | None = None
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, ProviderAttemptRecord):
            errors.append(f"attempt {index} is not a ProviderAttemptRecord")
            continue
        if (
            not isinstance(attempt.sequence, int)
            or isinstance(attempt.sequence, bool)
            or not isinstance(attempt.retry_index, int)
            or isinstance(attempt.retry_index, bool)
            or attempt.sequence != index
            or attempt.retry_index != index
        ):
            errors.append(f"attempt {index} has non-contiguous sequence/retry index")
        if attempt.schema_version != PROVIDER_ATTEMPT_SCHEMA_VERSION:
            errors.append(f"attempt {index} has wrong schema version")
        if not isinstance(attempt.run_ledger_id, str) or not _SHA256_RE.fullmatch(
            attempt.run_ledger_id
        ):
            errors.append(f"attempt {index} run ledger ID is invalid")
        elif trace_ledger_id is None:
            trace_ledger_id = attempt.run_ledger_id
        elif attempt.run_ledger_id != trace_ledger_id:
            errors.append(f"attempt {index} changed run ledger ID")
        global_fields_ok = all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            for value in (
                attempt.global_request_sequence,
                attempt.global_requests_after_attempt,
                attempt.contract_requests_after_attempt,
            )
        )
        if not global_fields_ok:
            errors.append(f"attempt {index} global request counters are invalid")
        else:
            if (
                attempt.global_requests_after_attempt
                != attempt.global_request_sequence + 1
            ):
                errors.append(f"attempt {index} global request count mismatch")
            if not 1 <= attempt.contract_requests_after_attempt <= (
                attempt.global_requests_after_attempt
            ):
                errors.append(f"attempt {index} contract request count mismatch")
            if previous_global_sequence is not None and (
                attempt.global_request_sequence != previous_global_sequence + 1
            ):
                errors.append(f"attempt {index} global request sequence is not contiguous")
            if previous_contract_count is not None and (
                attempt.contract_requests_after_attempt
                != previous_contract_count + 1
            ):
                errors.append(f"attempt {index} contract request count is not contiguous")
            previous_global_sequence = attempt.global_request_sequence
            previous_contract_count = attempt.contract_requests_after_attempt
        global_cost = attempt.global_accounted_cost_usd_after_attempt
        if (
            not isinstance(global_cost, (int, float))
            or isinstance(global_cost, bool)
            or not math.isfinite(float(global_cost))
            or float(global_cost) < 0
        ):
            errors.append(f"attempt {index} global accounted cost is invalid")
        else:
            accounted_for_delta_ok = (
                isinstance(attempt.accounted_cost_usd, (int, float))
                and not isinstance(attempt.accounted_cost_usd, bool)
                and math.isfinite(float(attempt.accounted_cost_usd))
                and float(attempt.accounted_cost_usd) >= 0
            )
            if accounted_for_delta_ok:
                if previous_global_cost is not None and not math.isclose(
                    float(global_cost) - previous_global_cost,
                    float(attempt.accounted_cost_usd),
                    abs_tol=1e-12,
                ):
                    errors.append(
                        f"attempt {index} global accounted cost delta mismatch"
                    )
                elif (
                    previous_global_cost is None
                    and float(global_cost) + 1e-12
                    < float(attempt.accounted_cost_usd)
                ):
                    errors.append(
                        f"attempt {index} global accounted cost is too small"
                    )
            previous_global_cost = float(global_cost)
        if not isinstance(attempt.status, ProviderAttemptStatus):
            errors.append(f"attempt {index} has untyped status")
        if attempt.request_contract_hash != binding.request_contract_hash:
            errors.append(f"attempt {index} has wrong request contract hash")
        if not isinstance(attempt.request_contract_hash, str) or not _SHA256_RE.fullmatch(
            attempt.request_contract_hash
        ):
            errors.append(f"attempt {index} request contract hash is invalid")
        if attempt.parser_version != STRICT_PROVIDER_PARSER_VERSION:
            errors.append(f"attempt {index} parser version mismatch")
        if not isinstance(attempt.raw_response_hash, str) or not _SHA256_RE.fullmatch(
            attempt.raw_response_hash
        ):
            errors.append(f"attempt {index} raw response hash is invalid")
        if attempt.observed_request_contract_hash != NOT_PROVIDED and (
            not isinstance(attempt.observed_request_contract_hash, str)
            or not _SHA256_RE.fullmatch(attempt.observed_request_contract_hash)
        ):
            errors.append(f"attempt {index} observed request binding is invalid")
        if attempt.claimed_parsed_proposal_hash != NOT_PROVIDED and (
            not isinstance(attempt.claimed_parsed_proposal_hash, str)
            or not _SHA256_RE.fullmatch(attempt.claimed_parsed_proposal_hash)
        ):
            errors.append(f"attempt {index} claimed parsed hash is invalid")
        if attempt.parsed_proposal_hash is not None and (
            not isinstance(attempt.parsed_proposal_hash, str)
            or not _SHA256_RE.fullmatch(attempt.parsed_proposal_hash)
        ):
            errors.append(f"attempt {index} parsed proposal hash is invalid")
        if attempt.endpoint != expected_endpoints.get(attempt.status):
            errors.append(f"attempt {index} endpoint/status mismatch")
        if (
            not isinstance(attempt.rejection_reasons, tuple)
            or not all(
                isinstance(reason, str) and bool(reason)
                for reason in attempt.rejection_reasons
            )
        ):
            errors.append(f"attempt {index} rejection reasons are invalid")
        if attempt.status is ProviderAttemptStatus.SUCCEEDED:
            if attempt.rejection_reasons:
                errors.append(f"attempt {index} success has rejection reasons")
            if success_seen:
                errors.append("multiple successful provider attempts")
            success_seen = True
        elif not attempt.rejection_reasons:
            errors.append(f"attempt {index} non-success lacks rejection reasons")
        if success_seen and index + 1 < len(attempts):
            errors.append("provider attempts continue after success")
        request = _deep_thaw(attempt.request)
        if request != binding.to_evidence_dict():
            errors.append(f"attempt {index} request bytes/binding mismatch")
        try:
            raw = base64.b64decode(
                attempt.raw_response_bytes_b64.encode("ascii"), validate=True
            )
        except Exception:
            errors.append(f"attempt {index} raw response base64 is invalid")
            continue
        if hashlib.sha256(raw).hexdigest() != attempt.raw_response_hash:
            errors.append(f"attempt {index} raw response hash mismatch")
        has_response = (
            isinstance(attempt.actual_provider, str)
            and bool(attempt.actual_provider)
            and attempt.actual_provider != NOT_PROVIDED
        )
        if not isinstance(attempt.actual_provider, str) or not attempt.actual_provider:
            errors.append(f"attempt {index} actual provider is invalid")
        if not has_response and attempt.observed_request_contract_hash != NOT_PROVIDED:
            errors.append(f"attempt {index} response binding exists without response")
        if not has_response:
            if raw:
                errors.append(f"attempt {index} raw response exists without response")
            for field_name in (
                "provider_request_id",
                "returned_model_id",
                "returned_model_revision",
                "claimed_parsed_proposal_hash",
            ):
                if getattr(attempt, field_name) != NOT_PROVIDED:
                    errors.append(
                        f"attempt {index} {field_name} exists without response"
                    )
            if any(
                value is not None
                for value in (
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.total_tokens,
                    attempt.cost_usd,
                )
            ):
                errors.append(f"attempt {index} usage/cost exists without response")
        if attempt.status is ProviderAttemptStatus.SUCCEEDED:
            if not has_response:
                errors.append(f"attempt {index} success has no provider response")
            if attempt.observed_request_contract_hash != binding.request_contract_hash:
                errors.append(f"attempt {index} success has no exact response binding")
            if attempt.parsed_proposal_hash is None:
                errors.append(f"attempt {index} success has no parsed proposal hash")
        elif attempt.parsed_proposal_hash is not None:
            errors.append(f"attempt {index} non-success has parsed proposal evidence")
        if (
            has_response
            and attempt.observed_request_contract_hash != binding.request_contract_hash
            and attempt.status is not ProviderAttemptStatus.BINDING_REJECTED
        ):
            errors.append(f"attempt {index} stale request binding was not rejected")
        if has_response and attempt.actual_provider != binding.provider:
            if attempt.status is not ProviderAttemptStatus.BINDING_REJECTED:
                errors.append(f"attempt {index} provider substitution not rejected")
        if (
            has_response
            and attempt.returned_model_id != binding.model_id
            and attempt.status is not ProviderAttemptStatus.BINDING_REJECTED
        ):
            errors.append(f"attempt {index} model substitution not rejected")
        cost_domain_ok = attempt.cost_usd is None or (
            isinstance(attempt.cost_usd, (int, float))
            and not isinstance(attempt.cost_usd, bool)
            and math.isfinite(float(attempt.cost_usd))
            and float(attempt.cost_usd) >= 0
        )
        accounted_domain_ok = (
            isinstance(attempt.accounted_cost_usd, (int, float))
            and not isinstance(attempt.accounted_cost_usd, bool)
            and math.isfinite(float(attempt.accounted_cost_usd))
            and float(attempt.accounted_cost_usd) >= 0
        )
        if not cost_domain_ok:
            errors.append(f"attempt {index} cost domain is invalid")
        if not accounted_domain_ok:
            errors.append(f"attempt {index} accounted cost domain is invalid")
        if not isinstance(attempt.cost_source, str) or not attempt.cost_source:
            errors.append(f"attempt {index} cost source is invalid")
        if attempt.cost_usd is None:
            expected_accounted = binding.settings.max_cost_per_request_usd or 0.0
            if attempt.cost_source != NOT_PROVIDED:
                errors.append(f"attempt {index} unknown cost has a source")
        elif cost_domain_ok:
            expected_accounted = float(attempt.cost_usd)
            if attempt.cost_source == NOT_PROVIDED:
                errors.append(f"attempt {index} known cost has no source")
            upper = binding.settings.max_cost_per_request_usd
            if upper is not None and attempt.cost_usd > upper and (
                attempt.status is not ProviderAttemptStatus.COST_REJECTED
            ):
                errors.append(f"attempt {index} exceeded price bound without rejection")
        else:
            expected_accounted = 0.0
        if accounted_domain_ok and not math.isclose(
            float(attempt.accounted_cost_usd), expected_accounted, abs_tol=1e-12
        ):
            errors.append(f"attempt {index} accounted cost mismatch")
        if accounted_domain_ok:
            cumulative_accounted_cost += float(attempt.accounted_cost_usd)
        token_values = (
            attempt.input_tokens,
            attempt.output_tokens,
            attempt.total_tokens,
        )
        token_domain_ok = not any(
            value is not None
            and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            )
            for value in token_values
        )
        if not token_domain_ok:
            errors.append(f"attempt {index} token domain is invalid")
        if any(value is None for value in token_values) and not all(
            value is None for value in token_values
        ):
            errors.append(f"attempt {index} token usage is partial")
        if token_domain_ok and all(value is not None for value in token_values) and (
            attempt.total_tokens != attempt.input_tokens + attempt.output_tokens
        ):
            errors.append(f"attempt {index} token totals do not reconcile")
        usage_missing = any(
            value is None
            for value in (
                attempt.input_tokens,
                attempt.output_tokens,
                attempt.total_tokens,
            )
        )
        if (
            has_response
            and usage_missing
            and binding.settings.unknown_usage_policy == "reject"
            and attempt.status is not ProviderAttemptStatus.USAGE_REJECTED
        ):
            errors.append(f"attempt {index} unknown usage was not rejected")
        if attempt.status is ProviderAttemptStatus.SUCCEEDED:
            try:
                parsed = parse_exact_action_proposal(raw)
            except StrictProviderParseError:
                errors.append(f"attempt {index} accepted raw response no longer parses")
            else:
                if parsed.proposal_hash != attempt.parsed_proposal_hash:
                    errors.append(f"attempt {index} parsed proposal hash mismatch")
                errors.extend(
                    f"attempt {index}: {reason}"
                    for reason in _proposal_binding_reasons(parsed, binding)
                )
                expected_claim = attempt.claimed_parsed_proposal_hash
                if expected_claim not in {NOT_PROVIDED, parsed.proposal_hash}:
                    errors.append(f"attempt {index} claimed parsed hash mismatch")
        timing_values = (
            attempt.started_monotonic_seconds,
            attempt.finished_monotonic_seconds,
            attempt.latency_seconds,
        )
        timing_domain_ok = all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
            for value in timing_values
        )
        if not timing_domain_ok:
            errors.append(f"attempt {index} monotonic timing domain is invalid")
        else:
            if attempt.finished_monotonic_seconds < attempt.started_monotonic_seconds:
                errors.append(f"attempt {index} monotonic timestamps reversed")
            expected_latency = (
                attempt.finished_monotonic_seconds
                - attempt.started_monotonic_seconds
            )
            if not math.isclose(
                attempt.latency_seconds, expected_latency, abs_tol=1e-9
            ):
                errors.append(f"attempt {index} latency does not reconcile")
        if index + 1 < len(attempts):
            if not isinstance(attempt.status, ProviderAttemptStatus):
                errors.append(f"attempt {index} retry source status is invalid")
            elif attempt.status.value not in binding.settings.retry_on:
                errors.append(
                    f"attempt {index} was retried from a non-retryable status"
                )
    if (
        binding.settings.max_usd is not None
        and cumulative_accounted_cost > binding.settings.max_usd + 1e-12
    ):
        errors.append("provider accounted cost exceeds max_usd")
    return tuple(errors)


def verify_provider_attempt_aggregate(
    traces: Sequence[
        tuple[ProviderRequestBinding, Sequence[ProviderAttemptRecord]]
    ],
    *,
    ledger_snapshot: ProviderRunLedgerSnapshot | None = None,
) -> tuple[str, ...]:
    """Verify complete run-global accounting across per-decision trace files."""

    errors: list[str] = []
    run_identity: tuple[str, str] | None = None
    all_attempts: list[ProviderAttemptRecord] = []
    expected_contracts: dict[str, int] = {}
    for trace_index, partition in enumerate(traces):
        if (
            not isinstance(partition, tuple)
            or len(partition) != 2
            or not isinstance(partition[0], ProviderRequestBinding)
        ):
            errors.append(f"trace {trace_index} has invalid partition shape")
            continue
        binding, attempts = partition
        identity = (binding.run_id, binding.spec_hash)
        if run_identity is None:
            run_identity = identity
        elif identity != run_identity:
            errors.append(f"trace {trace_index} changed run identity")
        expected_contracts.setdefault(binding.request_contract_hash, 0)
        local_errors = verify_provider_attempts(attempts, binding)
        errors.extend(f"trace {trace_index}: {item}" for item in local_errors)
        for attempt in attempts:
            if (
                isinstance(attempt, ProviderAttemptRecord)
                and isinstance(attempt.run_ledger_id, str)
                and _SHA256_RE.fullmatch(attempt.run_ledger_id) is not None
                and isinstance(attempt.request_contract_hash, str)
                and _SHA256_RE.fullmatch(attempt.request_contract_hash) is not None
                and isinstance(attempt.global_request_sequence, int)
                and not isinstance(attempt.global_request_sequence, bool)
                and attempt.global_request_sequence >= 0
                and isinstance(attempt.global_requests_after_attempt, int)
                and not isinstance(attempt.global_requests_after_attempt, bool)
                and attempt.global_requests_after_attempt >= 0
                and isinstance(attempt.contract_requests_after_attempt, int)
                and not isinstance(attempt.contract_requests_after_attempt, bool)
                and attempt.contract_requests_after_attempt >= 0
                and isinstance(attempt.accounted_cost_usd, (int, float))
                and not isinstance(attempt.accounted_cost_usd, bool)
                and math.isfinite(float(attempt.accounted_cost_usd))
                and float(attempt.accounted_cost_usd) >= 0
                and isinstance(
                    attempt.global_accounted_cost_usd_after_attempt, (int, float)
                )
                and not isinstance(
                    attempt.global_accounted_cost_usd_after_attempt, bool
                )
                and math.isfinite(
                    float(attempt.global_accounted_cost_usd_after_attempt)
                )
                and float(attempt.global_accounted_cost_usd_after_attempt) >= 0
            ):
                all_attempts.append(attempt)

    ledger_ids = {attempt.run_ledger_id for attempt in all_attempts}
    if len(ledger_ids) > 1:
        errors.append("aggregate uses multiple run ledger IDs")
    ordered = sorted(all_attempts, key=lambda item: item.global_request_sequence)
    sequences = [item.global_request_sequence for item in ordered]
    if sequences != list(range(len(ordered))):
        errors.append("aggregate global request sequence is not exact/contiguous")

    running_cost = 0.0
    for expected_sequence, attempt in enumerate(ordered):
        contract_hash = attempt.request_contract_hash
        expected_contracts[contract_hash] = expected_contracts.get(contract_hash, 0) + 1
        running_cost += float(attempt.accounted_cost_usd)
        if attempt.global_requests_after_attempt != expected_sequence + 1:
            errors.append(
                f"aggregate attempt {expected_sequence} global count mismatch"
            )
        if attempt.contract_requests_after_attempt != expected_contracts[contract_hash]:
            errors.append(
                f"aggregate attempt {expected_sequence} contract count mismatch"
            )
        if not math.isclose(
            attempt.global_accounted_cost_usd_after_attempt,
            running_cost,
            abs_tol=1e-12,
        ):
            errors.append(
                f"aggregate attempt {expected_sequence} cumulative cost mismatch"
            )

    if ledger_snapshot is not None:
        if not isinstance(ledger_snapshot, ProviderRunLedgerSnapshot):
            errors.append("aggregate ledger snapshot has wrong type")
        else:
            if run_identity is not None and (
                ledger_snapshot.run_id,
                ledger_snapshot.spec_hash,
            ) != run_identity:
                errors.append("aggregate ledger snapshot run identity mismatch")
            if ledger_ids and ledger_snapshot.run_ledger_id not in ledger_ids:
                errors.append("aggregate ledger snapshot ID mismatch")
            if ledger_snapshot.global_requests != len(ordered):
                errors.append("aggregate ledger snapshot request count mismatch")
            if ledger_snapshot.unsettled_requests != 0:
                errors.append("aggregate ledger has unsettled request")
            if dict(ledger_snapshot.requests_by_contract) != expected_contracts:
                errors.append("aggregate ledger per-contract counts mismatch")
            if not math.isclose(
                ledger_snapshot.cumulative_accounted_cost_usd,
                running_cost,
                abs_tol=1e-12,
            ):
                errors.append("aggregate ledger cumulative cost mismatch")
    return tuple(errors)


def _response_binding_reasons(
    response: RawProviderResponse,
    binding: ProviderRequestBinding,
) -> list[str]:
    reasons: list[str] = []
    if response.observed_request_contract_hash != binding.request_contract_hash:
        reasons.append("stale_or_unbound_request_contract")
    if response.actual_provider != binding.provider:
        reasons.append("provider_substitution_detected")
    if response.returned_model_id != binding.model_id:
        reasons.append("returned_model_id_mismatch")
    return reasons


def _proposal_binding_reasons(
    proposal: ActionProposal,
    binding: ProviderRequestBinding,
) -> list[str]:
    reasons: list[str] = []
    if proposal.decision_id != binding.decision_id:
        reasons.append("proposal_decision_id_mismatch")
    if proposal.catalogue_version != binding.catalogue_version:
        reasons.append("proposal_catalogue_version_mismatch")
    if proposal.catalogue_hash != binding.catalogue_hash:
        reasons.append("proposal_catalogue_hash_mismatch")
    return reasons


def _attempt_record(
    *,
    binding: ProviderRequestBinding,
    sequence: int,
    retry_index: int,
    status: ProviderAttemptStatus,
    endpoint: str,
    started: float,
    finished: float,
    response: RawProviderResponse | None,
    proposal: ActionProposal | None,
    reasons: tuple[str, ...],
    accounted_cost_usd: float,
    authorization: ProviderRequestAuthorization,
    accounting_snapshot: ProviderRunLedgerSnapshot,
) -> ProviderAttemptRecord:
    raw = response.content_bytes if response is not None else b""
    return ProviderAttemptRecord(
        sequence=sequence,
        retry_index=retry_index,
        run_ledger_id=authorization.run_ledger_id,
        global_request_sequence=authorization.global_request_sequence,
        global_requests_after_attempt=accounting_snapshot.global_requests,
        contract_requests_after_attempt=(
            accounting_snapshot.requests_by_contract[
                authorization.request_contract_hash
            ]
        ),
        global_accounted_cost_usd_after_attempt=(
            accounting_snapshot.cumulative_accounted_cost_usd
        ),
        status=status,
        endpoint=endpoint,
        request_contract_hash=binding.request_contract_hash,
        observed_request_contract_hash=(
            response.observed_request_contract_hash or NOT_PROVIDED
            if response
            else NOT_PROVIDED
        ),
        request=binding.to_evidence_dict(),
        started_monotonic_seconds=started,
        finished_monotonic_seconds=finished,
        latency_seconds=finished - started,
        actual_provider=(response.actual_provider if response else NOT_PROVIDED),
        provider_request_id=(
            response.provider_request_id or NOT_PROVIDED
            if response
            else NOT_PROVIDED
        ),
        returned_model_id=(
            response.returned_model_id or NOT_PROVIDED
            if response
            else NOT_PROVIDED
        ),
        returned_model_revision=(
            response.returned_model_revision or NOT_PROVIDED
            if response
            else NOT_PROVIDED
        ),
        input_tokens=response.input_tokens if response else None,
        output_tokens=response.output_tokens if response else None,
        total_tokens=response.total_tokens if response else None,
        cost_usd=response.cost_usd if response else None,
        accounted_cost_usd=accounted_cost_usd,
        cost_source=(response.cost_source or NOT_PROVIDED if response else NOT_PROVIDED),
        raw_response_bytes_b64=_b64(raw),
        raw_response_hash=hashlib.sha256(raw).hexdigest(),
        claimed_parsed_proposal_hash=(
            response.claimed_parsed_proposal_hash or NOT_PROVIDED
            if response
            else NOT_PROVIDED
        ),
        parsed_proposal_hash=proposal.proposal_hash if proposal else None,
        parser_version=STRICT_PROVIDER_PARSER_VERSION,
        rejection_reasons=reasons,
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _budget_policy(settings: ProviderSettings) -> tuple[Any, ...]:
    """Fields that define one observer/run-wide request and cost envelope."""

    return (
        settings.max_requests,
        settings.max_usd,
        settings.max_cost_per_request_usd,
        settings.unknown_cost_policy,
    )


def _sanitize_diagnostic_text(value: object) -> str:
    """Bound and redact an untrusted provider/authorizer diagnostic string."""

    text = str(value).replace("\x00", " ")
    text = re.sub(r"[\r\n\t]+", " ", text)
    # Redact labelled credentials before URI/path handling so a secret that
    # happens to contain slashes cannot be partially retained.
    text = _DIAGNOSTIC_SECRET_RE.sub(
        lambda match: f"{match.group(1)}=[REDACTED_SECRET]",
        text,
    )
    text = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED_SECRET]",
        text,
    )
    text = _QUOTED_DIAGNOSTIC_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _UNQUOTED_DIAGNOSTIC_PATH_RE.sub("[REDACTED_PATH]", text)
    return text[:512]


def _bounded_diagnostic(
    exc: BaseException, *, fallback: str | None = None
) -> str:
    detail = _sanitize_diagnostic_text(str(exc))
    if not detail:
        detail = fallback or "provider_failure"
    exception_name = type(exc).__name__
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", exception_name):
        exception_name = "ProviderException"
    return _sanitize_diagnostic_text(f"{exception_name}: {detail}")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _deep_freeze(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value
