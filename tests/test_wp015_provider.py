from datetime import datetime, timezone
from decimal import Decimal

import pytest

from optimizer_pipeline.dof.provider import ProviderBindingError, RawProviderResponse
from optimizer_pipeline.dof.wp015_provider import (
    StrictWP015MultiProviderTransport, derive_cost_usd, runtime_profile,
)


def test_openai_cost_uses_wp015_snapshot():
    value, window = derive_cost_usd(
        "openai/gpt-5.6-sol",
        {
            "uncached_input_tokens": 1000, "cached_input_tokens": 1000,
            "cache_write_input_tokens": 0, "output_tokens": 1000,
        },
    )
    assert value == Decimal("0.0355")
    assert window == "2026-08-14"


def test_deepseek_effective_window_changes_after_boundary():
    usage = {
        "cache_hit_input_tokens": 0, "cache_miss_input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }
    before, before_window = derive_cost_usd(
        "deepseek/deepseek-v4-pro", usage,
        at=datetime(2026, 8, 16, 15, 59, tzinfo=timezone.utc),
    )
    peak, peak_window = derive_cost_usd(
        "deepseek/deepseek-v4-pro", usage,
        at=datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc),
    )
    off_peak, off_peak_window = derive_cost_usd(
        "deepseek/deepseek-v4-pro", usage,
        at=datetime(2026, 8, 17, 5, 0, tzinfo=timezone.utc),
    )
    assert before == Decimal("1.305")
    assert peak == Decimal("5.28")
    assert off_peak == Decimal("2.64")
    assert before_window.startswith("until_")
    assert peak_window.endswith("_peak")
    assert off_peak_window.endswith("_off_peak")


def test_runtime_profiles_are_direct_and_gemini_is_first_party():
    assert runtime_profile("openai/gpt-5.6-luna")["provider"] == "openai"
    gemini = runtime_profile("google/gemini-3.6-flash")
    assert gemini["provider"] == "google_gemini"
    assert gemini["credential_env"] == "GEMINI_API_KEY"


def test_naive_pricing_time_is_rejected():
    with pytest.raises(ProviderBindingError, match="timezone_aware"):
        derive_cost_usd(
            "deepseek/deepseek-v4-flash",
            {"cache_hit_input_tokens": 0, "cache_miss_input_tokens": 0, "output_tokens": 0},
            at=datetime(2026, 8, 17, 5, 0),
        )


def test_transport_replaces_legacy_cost_with_effective_wp015_cost():
    class Inner:
        physical_requests = 1
        last_exchange = {
            "native_usage": {
                "uncached_input_tokens": 1000, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 1000,
            },
            "derived_cost_usd": "999",
        }

        async def send(self, _binding, _retry_index):
            return RawProviderResponse(
                content_bytes=b"{}", actual_provider="openai",
                input_tokens=1000, output_tokens=1000, total_tokens=2000,
                cost_usd=999.0, cost_source="legacy",
            )

    transport = StrictWP015MultiProviderTransport(
        model="openai/gpt-5.6-sol", api_key="not-sent",
    )
    transport.inner = Inner()
    import asyncio
    result = asyncio.run(transport.send(None, 0))
    assert result.cost_usd == 0.035
    assert result.cost_source == "derived_wp015_frozen_pricing"
    assert transport.last_exchange["legacy_transport_derived_cost_usd"] == "999"
    assert transport.last_exchange["pricing_snapshot_id"] == "WP015_PRICING_2026-08-14"
