"""Provider-neutral LLM client base classes."""

from __future__ import annotations

from .types import LLMRequest, LLMResponse


class LLMClientError(RuntimeError):
    """Raised when an LLM provider call fails."""


class LLMClient:
    def complete_json(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError
