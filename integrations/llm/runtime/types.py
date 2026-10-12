"""Provider-neutral LLM data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class RequestNotSent(RuntimeError):
    """Adapter certifies failure before any provider transport was attempted.

    Never use for an ambiguous transport, response, identity or usage failure.
    Such failures must retain the existing conservative request reservation.
    """


@dataclass(frozen=True)
class LLMRequest:
    provider: str
    model: str
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = 0.0
    max_tokens: Optional[int] = None
    response_format: str = "json_object"
    timeout_sec: Optional[float] = None
    reasoning_effort: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: Optional[str]
    provider: str
    model: str
    parsed_json: Optional[Dict[str, Any]] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    provider_reported_cost_usd: Optional[float] = None
    estimated_cost_usd: Optional[float] = None
    cost_source: Optional[str] = None
    pricing_profile_id: Optional[str] = None
    latency_sec: Optional[float] = None
    raw_response: Any = None
    error: Optional[str] = None
