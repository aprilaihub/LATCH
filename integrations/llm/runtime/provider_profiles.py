"""Provider profiles for OpenAI-compatible LLM adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_PROVIDER_ROOT = Path(__file__).resolve().parents[3] / "configs" / "providers"
if not DEFAULT_PROVIDER_ROOT.is_dir():
    DEFAULT_PROVIDER_ROOT = Path(__file__).resolve().parent / "data" / "providers"
RESPONSE_FORMAT_POLICIES = {"json_object", "omit", "text_extract"}
TEMPERATURE_POLICIES = {"pass_through", "omit", "zero_only"}
MAX_TOKENS_PARAMS = {"max_tokens", "max_completion_tokens"}


@dataclass(frozen=True)
class ProviderRequestPolicy:
    response_format_policy: str = "json_object"
    temperature_policy: str = "pass_through"
    max_tokens_param: str = "max_tokens"


@dataclass(frozen=True)
class LLMProviderProfile:
    provider: str
    adapter_type: str
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    supports_json_object: bool = True
    response_format_policy: str = "json_object"
    temperature_policy: str = "pass_through"
    max_tokens_param: str = "max_tokens"
    pricing_profile: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LLMProviderProfile":
        return cls(
            provider=str(data.get("provider") or ""),
            adapter_type=str(data.get("adapter_type") or "openai_compatible"),
            base_url=str(data["base_url"]) if data.get("base_url") else None,
            api_key_env=str(data["api_key_env"]) if data.get("api_key_env") else None,
            supports_json_object=bool(data.get("supports_json_object", True)),
            response_format_policy=_response_format_policy(data),
            temperature_policy=str(data.get("temperature_policy") or "pass_through"),
            max_tokens_param=str(data.get("max_tokens_param") or "max_tokens"),
            pricing_profile=str(data["pricing_profile"]) if data.get("pricing_profile") else None,
        )

    def request_policy(self) -> ProviderRequestPolicy:
        _validate_choice("response_format_policy", self.response_format_policy, RESPONSE_FORMAT_POLICIES)
        _validate_choice("temperature_policy", self.temperature_policy, TEMPERATURE_POLICIES)
        _validate_choice("max_tokens_param", self.max_tokens_param, MAX_TOKENS_PARAMS)
        return ProviderRequestPolicy(
            response_format_policy=self.response_format_policy,
            temperature_policy=self.temperature_policy,
            max_tokens_param=self.max_tokens_param,
        )


def load_provider_profile(provider: str, *, root: Optional[Path] = None) -> Optional[LLMProviderProfile]:
    normalized = str(provider or "").lower()
    profile_path = (root or DEFAULT_PROVIDER_ROOT) / f"{normalized}.json"
    if not profile_path.exists():
        return None
    data = json.loads(profile_path.read_text())
    profile = LLMProviderProfile.from_dict(data if isinstance(data, dict) else {})
    if profile.provider and profile.provider != normalized:
        raise ValueError(f"Provider profile mismatch: requested {normalized}, file declares {profile.provider}")
    return profile if profile.provider else None


def _response_format_policy(data: Dict[str, Any]) -> str:
    explicit = data.get("response_format_policy")
    if explicit:
        return str(explicit)
    return "json_object" if bool(data.get("supports_json_object", True)) else "omit"


def _validate_choice(field: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise ValueError(f"Invalid provider profile {field}={value!r}; expected one of {sorted(allowed)}")
