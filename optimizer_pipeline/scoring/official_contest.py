"""Canonical FPL26 official contest score calculator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from optimizer_pipeline.models import to_jsonable


FPL26_OFFICIAL_FORMULA_VERSION = "fpl26.official.v1"


@dataclass(frozen=True)
class OfficialContestScore:
    """Structured result for the official FPL26 contest formula."""

    score: float
    formula_version: str
    alpha_fmax_improvement_mhz: Optional[float]
    beta_openrouter_cost_usd: float
    gamma_runtime_hours: float
    gamma_capped: bool
    zero_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


def compute_fpl26_official_contest_score(
    *,
    fmax_input_mhz: Optional[float],
    fmax_output_mhz: Optional[float],
    openrouter_cost_usd: float = 0.0,
    runtime_seconds: float = 0.0,
    valid_for_scoring: bool = True,
    produced_output_dcp: bool = True,
) -> OfficialContestScore:
    """Compute the official FPL26 contest score.

    Formula:
        score = max(0, alpha - 0.1 * alpha * beta - 0.1 * alpha * gamma)

    where alpha is Fmax improvement in MHz, beta is OpenRouter cost in USD,
    and gamma is wall-clock runtime in hours capped at 1.0.
    """

    runtime = _nonnegative_float(runtime_seconds)
    raw_gamma = runtime / 3600.0
    gamma = min(raw_gamma, 1.0)
    beta = _nonnegative_float(openrouter_cost_usd)
    alpha = _delta(fmax_input_mhz, fmax_output_mhz)

    if not valid_for_scoring:
        return _zero(alpha, beta, gamma, raw_gamma, "validation_failed")
    if not produced_output_dcp:
        return _zero(alpha, beta, gamma, raw_gamma, "missing_output_dcp")
    if alpha is None:
        return _zero(alpha, beta, gamma, raw_gamma, "missing_fmax")
    if alpha <= 0.0:
        return _zero(alpha, beta, gamma, raw_gamma, "no_fmax_improvement")

    score = alpha - 0.1 * alpha * beta - 0.1 * alpha * gamma
    if score <= 0.0:
        return _zero(alpha, beta, gamma, raw_gamma, "formula_nonpositive")

    return OfficialContestScore(
        score=round(float(score), 6),
        formula_version=FPL26_OFFICIAL_FORMULA_VERSION,
        alpha_fmax_improvement_mhz=round(float(alpha), 6),
        beta_openrouter_cost_usd=round(beta, 6),
        gamma_runtime_hours=round(gamma, 6),
        gamma_capped=raw_gamma > 1.0,
        zero_reason=None,
    )


def _zero(
    alpha: Optional[float],
    beta: float,
    gamma: float,
    raw_gamma: float,
    zero_reason: str,
) -> OfficialContestScore:
    return OfficialContestScore(
        score=0.0,
        formula_version=FPL26_OFFICIAL_FORMULA_VERSION,
        alpha_fmax_improvement_mhz=None if alpha is None else round(float(alpha), 6),
        beta_openrouter_cost_usd=round(beta, 6),
        gamma_runtime_hours=round(gamma, 6),
        gamma_capped=raw_gamma > 1.0,
        zero_reason=zero_reason,
    )


def _delta(fmax_input_mhz: Optional[float], fmax_output_mhz: Optional[float]) -> Optional[float]:
    try:
        if fmax_input_mhz is None or fmax_output_mhz is None:
            return None
        return float(fmax_output_mhz) - float(fmax_input_mhz)
    except (TypeError, ValueError):
        return None


def _nonnegative_float(value: Any) -> float:
    try:
        return max(0.0, float(value if value is not None else 0.0))
    except (TypeError, ValueError):
        return 0.0
