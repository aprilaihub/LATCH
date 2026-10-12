"""Frozen direct-provider routing and cost accounting for WP-015.

The earlier multi-provider transport remains the byte-level HTTP authority for
OpenAI, Anthropic and DeepSeek.  This module narrows it to the WP-015 routes,
re-prices the native usage with the effective-dated WP-015 snapshot, and binds
Gemini to its separate first-party transport.  No path retries, falls back or
repairs a response.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

from .gemini_direct import PROFILE as GEMINI_PROFILE
from .gemini_direct import StrictGeminiDirectTransport
from .multi_provider import PROFILES, StrictMultiProviderTransport
from .provider import ProviderBindingError, ProviderRequestBinding, RawProviderResponse


PRICING_PATH = Path(__file__).parents[2] / ".research-flow/source/WP015-Pricing-Snapshot-2026-08-14.json"
PRICING_SNAPSHOT_ID = "WP015_PRICING_2026-08-14"
GEMINI_MODEL = GEMINI_PROFILE.canonical_model_id
FINAL_ANSWER_CAPACITY_TOKENS = 4_096
TOTAL_OUTPUT_TOKEN_CEILING = 12_288

# One-turn reservations are deliberately conservative.  They are concurrency
# reservations, not attributed cost, and are settled against native usage.
MODEL_RESERVES_USD: Mapping[str, float] = {
    "openai/gpt-5.6-luna": 0.10,
    "openai/gpt-5.6-terra": 0.25,
    "openai/gpt-5.6-sol": 0.75,
    "anthropic/claude-sonnet-5": 0.35,
    "deepseek/deepseek-v4-flash": 0.08,
    "deepseek/deepseek-v4-pro": 0.16,
    GEMINI_MODEL: 0.20,
}

PROVIDER_CAPS_USD: Mapping[str, float] = {
    "openai": 100.0,
    "anthropic": 45.0,
    "deepseek": 15.0,
    "google_gemini": 20.0,
}


def _pricing() -> dict[str, Any]:
    value = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
    if value.get("snapshot_id") != PRICING_SNAPSHOT_ID:
        raise ProviderBindingError("wp015_pricing_snapshot_mismatch")
    return value


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _n(usage: Mapping[str, Any], key: str) -> Decimal:
    value = usage.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderBindingError("wp015_usage_invalid:" + key)
    return Decimal(value)


def _deepseek_window(now: datetime) -> tuple[str, Mapping[str, Any]]:
    snapshot = _pricing()["providers"]["deepseek"]
    boundary = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
    if now < boundary:
        return "until_2026-08-16T16:00:00Z", snapshot["per_million_tokens"]
    current = now.astimezone(timezone.utc).time()
    peak = time(1, 0) <= current < time(4, 0) or time(6, 0) <= current < time(10, 0)
    window = "from_2026-08-16T16:00:00Z_peak" if peak else "from_2026-08-16T16:00:00Z_off_peak"
    table = {
        model: row["peak" if peak else "off_peak"]
        for model, row in snapshot["from_2026_08_16_16Z"].items()
        if isinstance(row, Mapping)
    }
    return window, table


def derive_cost_usd(
    model: str,
    usage: Mapping[str, Any],
    *,
    at: datetime | None = None,
) -> tuple[Decimal, str]:
    """Return cost and the effective frozen-pricing window for native usage."""

    now = at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ProviderBindingError("wp015_pricing_time_must_be_timezone_aware")
    pricing = _pricing()["providers"]
    api_model = model.split("/", 1)[1]
    million = Decimal(1_000_000)
    if model.startswith("openai/"):
        row = pricing["openai"]["per_million_tokens"][api_model]
        total = (
            _n(usage, "uncached_input_tokens") * _d(row["input"])
            + _n(usage, "cached_input_tokens") * _d(row["cached_input"])
            + _n(usage, "cache_write_input_tokens")
            * _d(row["input"]) * _d(row["cache_write_multiplier"])
            + _n(usage, "output_tokens") * _d(row["output"])
        )
        return total / million, pricing["openai"]["effective_from"]
    if model.startswith("anthropic/"):
        row = pricing["anthropic"]["per_million_tokens"][api_model]
        total = (
            _n(usage, "uncached_input_tokens") * _d(row["input"])
            + _n(usage, "cache_write_5m_tokens") * _d(row["cache_write_5m"])
            + _n(usage, "cache_write_1h_tokens") * _d(row["cache_write_1h"])
            + _n(usage, "cache_read_input_tokens") * _d(row["cache_read"])
            + _n(usage, "output_tokens") * _d(row["output_including_thinking"])
        )
        return total / million, pricing["anthropic"]["effective_from"]
    if model.startswith("deepseek/"):
        window, table = _deepseek_window(now)
        row = table[api_model]
        total = (
            _n(usage, "cache_hit_input_tokens") * _d(row["cache_hit"])
            + _n(usage, "cache_miss_input_tokens") * _d(row["cache_miss"])
            + _n(usage, "output_tokens") * _d(row["output"])
        )
        return total / million, window
    raise ProviderBindingError("wp015_cost_model_not_direct_multi_provider:" + model)


def runtime_profile(model: str) -> dict[str, str]:
    if model == GEMINI_MODEL:
        return {
            "provider": GEMINI_PROFILE.provider,
            "route": GEMINI_PROFILE.route,
            "expected_returned_model": GEMINI_PROFILE.canonical_model_id,
            "expected_returned_upstream": GEMINI_PROFILE.api_model_id,
            "credential_env": GEMINI_PROFILE.credential_env,
            "adapter_version": GEMINI_PROFILE.adapter_version,
        }
    profile = PROFILES[model]
    if profile.provider not in {"openai", "anthropic", "deepseek"}:
        raise ProviderBindingError("wp015_non_direct_provider_forbidden")
    return {
        "provider": profile.provider,
        "route": profile.route,
        "expected_returned_model": profile.canonical_model_id,
        "expected_returned_upstream": profile.provider + "_direct",
        "credential_env": profile.credential_env,
        "adapter_version": profile.adapter_version,
    }


class StrictWP015MultiProviderTransport:
    """Direct transport adapter with WP-015 effective-dated accounting."""

    internal_retries_disabled = True

    def __init__(self, *, model: str, api_key: str, now: datetime | None = None) -> None:
        if model == GEMINI_MODEL:
            raise ProviderBindingError("gemini_requires_first_party_transport")
        self.model = model
        self.now = now
        self.inner = StrictMultiProviderTransport(
            profile=PROFILES[model], api_key=api_key,
            output_token_limit=TOTAL_OUTPUT_TOKEN_CEILING,
        )
        self.last_exchange: dict[str, Any] | None = None

    @property
    def physical_requests(self) -> int:
        return self.inner.physical_requests

    async def send(self, binding: ProviderRequestBinding, retry_index: int) -> RawProviderResponse:
        response = await self.inner.send(binding, retry_index)
        exchange = dict(self.inner.last_exchange or {})
        usage = exchange.get("native_usage")
        if not isinstance(usage, Mapping):
            raise ProviderBindingError("wp015_native_usage_missing")
        cost, window = derive_cost_usd(self.model, usage, at=self.now)
        exchange.update({
            "legacy_transport_derived_cost_usd": exchange.get("derived_cost_usd"),
            "derived_cost_usd": str(cost),
            "pricing_snapshot_id": PRICING_SNAPSHOT_ID,
            "effective_price_window": window,
            "cost_formula_version": "dof.wp015-effective-dated-cost.v1",
        })
        self.last_exchange = exchange
        return replace(response, cost_usd=float(cost), cost_source="derived_wp015_frozen_pricing")


def transport(binding: ProviderRequestBinding, model: str, api_key: str):
    if model == GEMINI_MODEL:
        return StrictGeminiDirectTransport(
            api_key=api_key, output_token_limit=TOTAL_OUTPUT_TOKEN_CEILING,
        )
    return StrictWP015MultiProviderTransport(model=model, api_key=api_key)


__all__ = [
    "FINAL_ANSWER_CAPACITY_TOKENS", "GEMINI_MODEL", "MODEL_RESERVES_USD", "PROVIDER_CAPS_USD",
    "PRICING_SNAPSHOT_ID", "StrictWP015MultiProviderTransport",
    "TOTAL_OUTPUT_TOKEN_CEILING", "derive_cost_usd", "runtime_profile", "transport",
]
