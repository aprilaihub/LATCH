"""OpenAI-compatible chat adapter."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from integrations.llm.runtime.adapters.openai_chat import OpenAIChatClient
from integrations.llm.runtime.pricing import PricingProfile
from integrations.llm.runtime.provider_profiles import ProviderRequestPolicy

logger = logging.getLogger(__name__)


class OpenAICompatibleChatClient(OpenAIChatClient):
    def __init__(
        self,
        *,
        api_key: Optional[str],
        base_url: Optional[str],
        client: Optional[Any] = None,
        client_factory: Optional[Any] = None,
        pricing_profile: Optional[PricingProfile] = None,
        request_policy: Optional[ProviderRequestPolicy] = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            client=client,
            client_factory=client_factory,
            base_url=base_url,
            pricing_profile=pricing_profile,
            request_policy=request_policy,
        )

    @staticmethod
    def _completion_kwargs(request: Any, policy: Optional[ProviderRequestPolicy] = None) -> Dict[str, Any]:
        """OpenRouter (and other OpenAI-compatible hosts) take a nested
        ``reasoning: {"effort": ...}`` object rather than OpenAI's native
        top-level ``reasoning_effort`` field -- see
        https://openrouter.ai/docs/use-cases/reasoning-tokens. Anthropic
        models routed through OpenRouter only accept ``reasoning.max_tokens``
        (a token budget), not an effort string, and there is no configured
        "High" -> token-budget mapping, so the parameter is dropped for those
        models with a loud warning rather than silently sent as a no-op.
        """
        kwargs = OpenAIChatClient._completion_kwargs(request, policy)
        effort = kwargs.pop("reasoning_effort", None)
        if effort is None:
            return kwargs
        model = str(getattr(request, "model", "") or "")
        if model.startswith("anthropic/"):
            logger.warning(
                "reasoning_effort=%r requested for Anthropic model %r via an OpenAI-compatible provider, but "
                "OpenRouter's Anthropic path takes reasoning.max_tokens (a token budget), not an effort string, "
                "and no 'High'-equivalent token budget is configured. Dropping the reasoning parameter for this "
                "call instead of guessing a budget.",
                effort,
                model,
            )
            return kwargs
        kwargs["reasoning"] = {"effort": effort}
        return kwargs
