"""Optional local pricing estimates for providers that do not report cost."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_PRICING_TABLE = Path(__file__).resolve().parents[3] / "configs" / "pricing" / "llm_pricing.json"
if not DEFAULT_PRICING_TABLE.exists():
    DEFAULT_PRICING_TABLE = Path(__file__).resolve().parent / "data" / "pricing" / "llm_pricing.json"


@dataclass(frozen=True)
class PricingProfile:
    name: str
    prompt_usd_per_million_tokens: Optional[float] = None
    completion_usd_per_million_tokens: Optional[float] = None

    def estimate_cost(self, *, prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> Optional[float]:
        if (
            self.prompt_usd_per_million_tokens is None
            or self.completion_usd_per_million_tokens is None
            or prompt_tokens is None
            or completion_tokens is None
        ):
            return None
        return (
            float(prompt_tokens) * self.prompt_usd_per_million_tokens
            + float(completion_tokens) * self.completion_usd_per_million_tokens
        ) / 1_000_000.0


def pricing_profile_from_name(name: Optional[str]) -> Optional[PricingProfile]:
    return pricing_profile_from_name_or_model(name)


def pricing_profile_from_name_or_model(
    name: Optional[str],
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    table_path: Optional[Path] = None,
) -> Optional[PricingProfile]:
    profile_id = str(name) if name else _provider_model_key(provider, model)
    if not profile_id:
        return None
    data = _load_pricing_table(table_path or DEFAULT_PRICING_TABLE)
    entry = data.get(profile_id)
    if entry is None and provider and model:
        entry = data.get(_provider_model_key(provider, model) or "")
    if entry is None and provider:
        entry = data.get(str(provider).lower())
    if not isinstance(entry, dict):
        return PricingProfile(name=profile_id)
    return PricingProfile(
        name=str(entry.get("id") or profile_id),
        prompt_usd_per_million_tokens=_optional_float(entry.get("prompt_usd_per_million_tokens")),
        completion_usd_per_million_tokens=_optional_float(entry.get("completion_usd_per_million_tokens")),
    )


def _load_pricing_table(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _provider_model_key(provider: Optional[str], model: Optional[str]) -> Optional[str]:
    if not provider or not model:
        return None
    return f"{str(provider).lower()}:{model}"


def _optional_float(value: object) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
