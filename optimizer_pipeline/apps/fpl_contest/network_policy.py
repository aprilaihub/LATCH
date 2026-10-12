"""Run-mode and LLM provider enforcement for contest-compatible runs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


class NetworkPolicyError(RuntimeError):
    """Raised before any external call when a provider violates run-mode rules."""


@dataclass(frozen=True)
class RunModeDecision:
    run_mode: str
    llm_provider: str
    contest_eligible: bool
    reason: Optional[str]
    api_key_source: Optional[str]
    cost_source: str

    def to_dict(self) -> dict:
        return {
            "run_mode": self.run_mode,
            "llm_provider": self.llm_provider,
            "contest_eligible": self.contest_eligible,
            "reason": self.reason,
            "api_key_source": self.api_key_source,
            "cost_source": self.cost_source,
        }


def enforce_network_policy(args: object, env: Optional[Mapping[str, str]] = None) -> RunModeDecision:
    """Validate the requested run mode and LLM provider before any external call."""

    environ = env if env is not None else os.environ
    run_mode = str(getattr(args, "run_mode", "contest-strict") or "contest-strict")
    provider = str(getattr(args, "llm_provider", "none") or "none")
    api_key = getattr(args, "api_key", None)

    if run_mode not in {"contest-strict", "research-dev"}:
        raise NetworkPolicyError(f"Unsupported run mode: {run_mode}")
    if provider not in {"none", "openrouter", "openai", "local"}:
        raise NetworkPolicyError(f"Unsupported LLM provider: {provider}")

    if provider == "none":
        source = None
    elif api_key:
        source = "cli"
    elif provider == "openrouter" and environ.get("OPENROUTER_API_KEY"):
        source = "OPENROUTER_API_KEY"
    elif provider == "openai" and environ.get("OPENAI_API_KEY"):
        source = "OPENAI_API_KEY"
    else:
        source = "missing"

    if run_mode == "contest-strict":
        if api_key:
            raise NetworkPolicyError("run_mode=contest-strict forbids --api-key; use organizer-provided OPENROUTER_API_KEY.")
        if provider in {"openai", "local"}:
            raise NetworkPolicyError(
                f"run_mode=contest-strict does not allow llm_provider={provider}; use research-dev for non-contest experiments."
            )
        if provider == "openrouter" and source == "missing":
            raise NetworkPolicyError("run_mode=contest-strict with llm_provider=openrouter requires OPENROUTER_API_KEY.")
        return RunModeDecision(
            run_mode=run_mode,
            llm_provider=provider,
            contest_eligible=True,
            reason=None,
            api_key_source=source,
            cost_source="contest" if provider == "openrouter" else "none",
        )

    reason = None
    if provider in {"openai", "local"}:
        reason = f"non-OpenRouter LLM provider used in research-dev mode: {provider}"
    elif provider == "openrouter":
        reason = "research-dev mode is marked non-contest-eligible by policy"
    return RunModeDecision(
        run_mode=run_mode,
        llm_provider=provider,
        contest_eligible=False,
        reason=reason or "research-dev mode is not contest-eligible",
        api_key_source=source,
        cost_source="research",
    )
