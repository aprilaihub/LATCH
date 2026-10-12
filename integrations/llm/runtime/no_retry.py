"""Explicit one-attempt SDK construction for bounded studies.

Normal shared clients retain their historical defaults (including Ablation).
"""
import os
from .provider_profiles import load_provider_profile
from .registry import build_llm_client


def build_one_attempt_client(*, provider, model, timeout_seconds):
    profile = load_provider_profile(provider)
    if profile is None:
        raise ValueError("An exact configured provider profile is required")
    key = os.environ.get(profile.api_key_env)
    if not key:
        raise RuntimeError("Required provider credential is not configured")
    adapter = profile.adapter_type
    if adapter in {"openai", "openai_compatible"}:
        from openai import OpenAI
        kwargs = dict(api_key=key, max_retries=0, timeout=timeout_seconds)
        if profile.base_url:
            kwargs["base_url"] = profile.base_url
        sdk = OpenAI(**kwargs)
    elif adapter == "anthropic_native":
        from anthropic import Anthropic
        sdk = Anthropic(api_key=key, max_retries=0, timeout=timeout_seconds)
    elif adapter == "google_native":
        from google import genai
        from google.genai import types
        # Fail visibly on an SDK without explicit retry control. Do not fall
        # back to default SDK retries or another model/provider.
        sdk = genai.Client(api_key=key, http_options=types.HttpOptions(
            timeout=int(timeout_seconds * 1000), retry_options=types.HttpRetryOptions(attempts=1)))
    else:
        raise ValueError("Unsupported one-attempt adapter: " + adapter)
    return build_llm_client(provider=provider, model=model, client=sdk)
