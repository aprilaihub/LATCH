"""LLM client registry."""

from __future__ import annotations

import os
from typing import Any, Optional

from .adapters import (AnthropicNativeClient, GoogleNativeClient, MockLLMClient,
                       OpenAIChatClient, OpenAICompatibleChatClient)
from .client import LLMClient
from .pricing import pricing_profile_from_name_or_model
from .provider_profiles import ProviderRequestPolicy, load_provider_profile


def build_llm_client(
    *,
    provider: str,
    api_key: Optional[str] = None,
    provider_base_url: Optional[str] = None,
    provider_api_key_env: Optional[str] = None,
    pricing_profile: Optional[str] = None,
    model: Optional[str] = None,
    client: Optional[Any] = None,
    client_factory: Optional[Any] = None,
) -> LLMClient:
    normalized = str(provider or "openai").lower()
    pricing = pricing_profile_from_name_or_model(pricing_profile, provider=normalized, model=model)
    if normalized == "mock":
        return MockLLMClient(pricing_profile=pricing)

    profile = load_provider_profile(normalized)
    if normalized == "openai_compatible":
        if not provider_base_url:
            raise ValueError("provider=openai_compatible requires provider_base_url")
        profile_adapter = "openai_compatible"
        profile_base_url = provider_base_url
        profile_api_key_env = provider_api_key_env or "OPENAI_API_KEY"
        profile_pricing = pricing_profile
        request_policy = ProviderRequestPolicy()
    elif profile is None:
        raise ValueError(f"Unknown LLM provider '{provider}'. Use a known provider profile or provider=openai_compatible with provider_base_url.")
    else:
        profile_adapter = profile.adapter_type
        profile_base_url = provider_base_url or profile.base_url
        profile_api_key_env = provider_api_key_env or profile.api_key_env
        profile_pricing = pricing_profile or profile.pricing_profile
        request_policy = profile.request_policy()
    if normalized == "local" and not profile_base_url:
        profile_base_url = os.environ.get("OPENAI_BASE_URL")

    pricing = pricing_profile_from_name_or_model(profile_pricing, provider=normalized, model=model)
    if profile_adapter == "anthropic_native":
        return AnthropicNativeClient(
            api_key=_resolve_api_key(api_key, profile_api_key_env, "ANTHROPIC_API_KEY"),
            client=client, pricing_profile=pricing, request_policy=request_policy,
        )
    if profile_adapter == "google_native":
        return GoogleNativeClient(
            api_key=_resolve_api_key(api_key, profile_api_key_env, "GOOGLE_API_KEY"),
            client=client, pricing_profile=pricing, request_policy=request_policy,
        )
    if profile_adapter == "openai":
        return OpenAIChatClient(
            api_key=_resolve_api_key(api_key, profile_api_key_env, "OPENAI_API_KEY"),
            client=client,
            client_factory=client_factory,
            pricing_profile=pricing,
            request_policy=request_policy,
        )
    if profile_adapter == "openai_compatible":
        default_env = profile_api_key_env or ("OPENROUTER_API_KEY" if normalized == "openrouter" else "OPENAI_API_KEY")
        resolved_api_key = _resolve_api_key(api_key, profile_api_key_env, default_env)
        if normalized == "local" and not resolved_api_key:
            resolved_api_key = "local"
        if normalized != "local" and not profile_base_url and provider_base_url is None:
            raise ValueError(f"Provider profile '{normalized}' does not define base_url; pass provider_base_url explicitly.")
        return OpenAICompatibleChatClient(
            api_key=resolved_api_key,
            base_url=profile_base_url,
            client=client,
            client_factory=client_factory,
            pricing_profile=pricing,
            request_policy=request_policy,
        )
    raise ValueError(f"Unsupported adapter_type '{profile_adapter}' for provider '{provider}'")


def _resolve_api_key(api_key: Optional[str], provider_api_key_env: Optional[str], default_env: str) -> Optional[str]:
    if api_key:
        return api_key
    if provider_api_key_env:
        return os.environ.get(provider_api_key_env)
    return os.environ.get(default_env)
