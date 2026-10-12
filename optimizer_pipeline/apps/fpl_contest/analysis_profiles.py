"""Analysis-mode resolver for the contest-compatible entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from optimizer_pipeline.apps.fpl_contest.network_policy import RunModeDecision


ANALYSIS_CHOICES = ("none", "static", "fast_llm", "deep_llm")


class AnalysisProfileError(RuntimeError):
    """Raised when the requested analysis mode is not allowed for this run."""


@dataclass(frozen=True)
class AnalysisProfileSpec:
    requested: str
    enabled: bool
    analysis_level: str = "none"
    loop_mode: str = "static_plan"
    trigger_reason: str = "analysis_profile"
    tool_plan_policy: Optional[str] = None
    uses_llm: bool = False


def resolve_analysis_profile(
    args: Any,
    policy: Optional[RunModeDecision],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> AnalysisProfileSpec:
    """Resolve public CLI analysis intent into internal controller settings."""

    requested = str(getattr(args, "analysis", "none") or "none")
    if requested not in ANALYSIS_CHOICES:
        raise AnalysisProfileError(f"Unsupported analysis mode: {requested}")
    if requested == "none":
        return AnalysisProfileSpec(requested=requested, enabled=False)
    if requested == "static":
        return AnalysisProfileSpec(
            requested=requested,
            enabled=True,
            analysis_level="read_only",
            loop_mode="static_plan",
            trigger_reason="static_analysis_profile",
        )

    mock = bool(getattr(args, "mock", False))
    if mock:
        return AnalysisProfileSpec(
            requested=requested,
            enabled=True,
            analysis_level="read_only" if requested == "fast_llm" else "report_probe",
            loop_mode="mock_llm_closed_loop",
            trigger_reason=f"{requested}_analysis_profile",
            tool_plan_policy="minimal_read_only_v1" if requested == "fast_llm" else None,
            uses_llm=True,
        )

    if policy is None:
        raise AnalysisProfileError(f"--analysis {requested} requires a resolved run-mode policy")
    if policy.run_mode == "contest-strict":
        if policy.llm_provider != "openrouter":
            raise AnalysisProfileError(
                f"--analysis {requested} in contest-strict requires --llm-provider openrouter"
            )
        if policy.api_key_source != "OPENROUTER_API_KEY":
            raise AnalysisProfileError(f"--analysis {requested} requires OPENROUTER_API_KEY")
        return AnalysisProfileSpec(
            requested=requested,
            enabled=True,
            analysis_level="read_only" if requested == "fast_llm" else "report_probe",
            loop_mode="openai_llm_closed_loop",
            trigger_reason=f"{requested}_analysis_profile",
            tool_plan_policy="minimal_read_only_v1" if requested == "fast_llm" else None,
            uses_llm=True,
        )

    if policy.run_mode != "research-dev" or policy.llm_provider not in {"openai", "openrouter"}:
        raise AnalysisProfileError(
            f"--analysis {requested} requires --run-mode research-dev with --llm-provider openai or openrouter for real LLM analysis"
        )
    if policy.api_key_source == "missing":
        key_name = "OPENROUTER_API_KEY" if policy.llm_provider == "openrouter" else "OPENAI_API_KEY"
        raise AnalysisProfileError(f"--analysis {requested} requires {key_name} or --api-key")

    return AnalysisProfileSpec(
        requested=requested,
        enabled=True,
        analysis_level="read_only" if requested == "fast_llm" else "report_probe",
        loop_mode="openai_llm_closed_loop",
        trigger_reason=f"{requested}_analysis_profile",
        tool_plan_policy="minimal_read_only_v1" if requested == "fast_llm" else None,
        uses_llm=True,
    )


def resolved_analysis_profile(args: Any) -> AnalysisProfileSpec:
    """Return the profile resolved by the entry adapter."""

    spec = getattr(args, "_analysis_profile", None)
    if isinstance(spec, AnalysisProfileSpec):
        return spec
    policy = getattr(args, "_run_mode_policy", None)
    return resolve_analysis_profile(args, policy)
