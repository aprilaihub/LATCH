"""Matrix expansion for frontier LLM tuning experiments."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schema import ExperimentSpec


TRIAL_LABELS = [
    "primary",
    "secondary",
    "tertiary",
    "quaternary",
    "quinary",
    "senary",
    "septenary",
    "octonary",
    "nonary",
    "denary",
]


@dataclass(frozen=True)
class ExperimentRunSpec:
    experiment_id: str
    input_dcp: str
    variant_slug: str
    trial_slug: str
    run_dir: Path
    log_path: Path
    factors: Dict[str, Any]
    base: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "input_dcp": self.input_dcp,
            "variant_slug": self.variant_slug,
            "trial_slug": self.trial_slug,
            "run_dir": str(self.run_dir),
            "log_path": str(self.log_path),
            "factors": dict(self.factors),
            "base": dict(self.base),
        }


def expand_experiment_matrix(spec: ExperimentSpec, *, repo_root: Path, output_root: Optional[Path] = None) -> List[ExperimentRunSpec]:
    if spec.repeats > len(TRIAL_LABELS):
        raise ValueError(f"repeats is limited to {len(TRIAL_LABELS)} semantic trial labels in the MVP")
    root = output_root or (Path(spec.output_root) if spec.output_root else repo_root / "experiments" / "frontier_llm_tuning")
    experiment_root = root / safe_slug(spec.experiment_id)
    runs: List[ExperimentRunSpec] = []
    for input_dcp in spec.input_dcps:
        design_slug = safe_slug(Path(input_dcp).stem)
        variants = spec.variants or [{}]
        for variant in variants:
            variant_name = variant.get("name") if isinstance(variant, dict) else None
            variant_factors = {key: value for key, value in dict(variant).items() if key != "name"}
            matrix = _matrix_without_variant_keys(spec.matrix, variant_factors) if spec.variants else spec.matrix
            combos = _factor_combinations(matrix)
            for sweep_factors in combos:
                factors = {**variant_factors, **sweep_factors}
                slug_factors = sweep_factors if variant_name else factors
                variant_slug = variant_slug_for(
                    input_dcp,
                    slug_factors,
                    include_design=len(spec.input_dcps) > 1,
                    variant_name=str(variant_name) if variant_name else None,
                )
                for trial_slug in TRIAL_LABELS[: spec.repeats]:
                    run_dir = experiment_root / "runs" / variant_slug / trial_slug
                    log_path = experiment_root / "logs" / variant_slug / f"{trial_slug}.log"
                    if len(spec.input_dcps) > 1 and not variant_slug.startswith(f"design-{design_slug}"):
                        variant_slug = f"design-{design_slug}__{variant_slug}"
                        run_dir = experiment_root / "runs" / variant_slug / trial_slug
                        log_path = experiment_root / "logs" / variant_slug / f"{trial_slug}.log"
                    runs.append(
                        ExperimentRunSpec(
                            experiment_id=spec.experiment_id,
                            input_dcp=input_dcp,
                            variant_slug=variant_slug,
                            trial_slug=trial_slug,
                            run_dir=run_dir,
                            log_path=log_path,
                            factors=dict(factors),
                            base=dict(spec.base),
                        )
                    )
    return runs


def variant_slug_for(input_dcp: str, factors: Dict[str, Any], *, include_design: bool = False, variant_name: Optional[str] = None) -> str:
    pieces: List[str] = []
    if include_design:
        pieces.append(f"design-{safe_slug(Path(input_dcp).stem)}")
    if variant_name:
        pieces.append(safe_slug(variant_name))
    for key, value in factors.items():
        pieces.append(f"{factor_key_slug(key)}-{factor_value_slug(key, value)}")
    return "__".join(pieces) or "baseline"


def safe_slug(value: Any) -> str:
    text = str(value).strip().replace("_", "-")
    text = re.sub(r"[^A-Za-z0-9.-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-.").lower()
    return text or "item"


def factor_value_slug(key: str, value: Any) -> str:
    if key == "prompt_id":
        return safe_slug(str(value).split(".")[-1])
    if key == "temperature":
        return temperature_slug(value)
    return safe_slug(value)


def factor_key_slug(key: str) -> str:
    if key == "prompt_id":
        return "prompt"
    if key == "llm_response_mode":
        return "response"
    return safe_slug(key)


def temperature_slug(value: Any) -> str:
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return safe_slug(value)
    if temperature == 0.0:
        return "cold"
    if 0.0 < temperature <= 0.2:
        return "steady"
    if 0.2 < temperature <= 0.5:
        return "mild"
    if 0.5 < temperature <= 0.8:
        return "warm"
    return "exploratory"


def _factor_combinations(matrix: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    if not matrix:
        return [{}]
    keys = list(matrix.keys())
    return [dict(zip(keys, values)) for values in itertools.product(*(matrix[key] for key in keys))]


def _matrix_without_variant_keys(matrix: Dict[str, List[Any]], variant_factors: Dict[str, Any]) -> Dict[str, List[Any]]:
    variant_keys = set(variant_factors)
    return {key: values for key, values in matrix.items() if key not in variant_keys}
