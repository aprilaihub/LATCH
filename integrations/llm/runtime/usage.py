"""LLM usage aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .types import LLMResponse


@dataclass
class LLMUsageAccumulator:
    total_llm_calls: int = 0
    total_prompt_tokens: Optional[int] = None
    total_completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    total_cost_usd: Optional[float] = None
    total_provider_reported_cost_usd: Optional[float] = None
    total_estimated_cost_usd: Optional[float] = None
    cost_source: Optional[str] = None
    pricing_profile_id: Optional[str] = None
    total_latency_sec: Optional[float] = None

    def record(self, response: LLMResponse) -> None:
        self.total_llm_calls += 1
        self.total_prompt_tokens = _sum_optional_int(self.total_prompt_tokens, response.prompt_tokens)
        self.total_completion_tokens = _sum_optional_int(self.total_completion_tokens, response.completion_tokens)
        self.total_tokens = _sum_optional_int(self.total_tokens, response.total_tokens)
        self.total_cost_usd = _sum_optional_float(self.total_cost_usd, response.cost_usd)
        self.total_provider_reported_cost_usd = _sum_optional_float(self.total_provider_reported_cost_usd, response.provider_reported_cost_usd)
        self.total_estimated_cost_usd = _sum_optional_float(self.total_estimated_cost_usd, response.estimated_cost_usd)
        self.cost_source = _merge_cost_source(self.cost_source, response.cost_source)
        if response.pricing_profile_id:
            self.pricing_profile_id = response.pricing_profile_id if self.pricing_profile_id in {None, response.pricing_profile_id} else "mixed"
        self.total_latency_sec = _sum_optional_float(self.total_latency_sec, response.latency_sec)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_llm_calls": self.total_llm_calls,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "total_provider_reported_cost_usd": self.total_provider_reported_cost_usd,
            "total_estimated_cost_usd": self.total_estimated_cost_usd,
            "cost_source": self.cost_source or ("unknown" if self.total_llm_calls else None),
            "pricing_profile_id": self.pricing_profile_id,
            "total_latency_sec": self.total_latency_sec,
        }


def _sum_optional_int(current: Optional[int], value: Optional[int]) -> Optional[int]:
    if value is None:
        return current
    return int(value) if current is None else current + int(value)


def _sum_optional_float(current: Optional[float], value: Optional[float]) -> Optional[float]:
    if value is None:
        return current
    return float(value) if current is None else current + float(value)


def _merge_cost_source(current: Optional[str], value: Optional[str]) -> Optional[str]:
    if not value:
        return current
    if current is None:
        return value
    if current == value:
        return current
    return "mixed"
