"""Policy factory registry for research frontier expansion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Generic, Optional, TypeVar

# auto_top8 is referenced from exactly one place in the codebase: this
# registry. A replacement heuristic is added as another registry.register(...)
# line and needs no change anywhere else in the pipeline.
from experiments.ablation.runtime.auto_top8 import build_auto_top8_expansion_policy
from optimizer_pipeline.reliability import ReliabilityTable
from optimizer_pipeline.retrieval import TrajectoryRetrievalIndex

from .core import (
    AnalysisPriorityExpansionPolicy,
    DeterministicExpansionPolicy,
    ReliabilityAwareExpansionPolicy,
    RetrievalAwareExpansionPolicy,
)

T = TypeVar("T")


class PolicyRegistry(Generic[T]):
    def __init__(self) -> None:
        self._factories: Dict[str, Callable[..., T]] = {}

    def register(self, name: str, factory: Callable[..., T]) -> None:
        if not name:
            raise ValueError("Policy name must be non-empty")
        self._factories[name] = factory

    def create(self, name: str, **kwargs: Any) -> T:
        if name not in self._factories:
            raise KeyError(f"Unknown policy '{name}'. Available: {sorted(self._factories)}")
        return self._factories[name](**kwargs)

    def names(self) -> list[str]:
        return sorted(self._factories)


@dataclass
class PolicyResources:
    config: Any
    api_key: Optional[str] = None
    reliability_table: Any = None
    retrieval_index: Any = None


def build_policy_registry() -> PolicyRegistry[Any]:
    registry: PolicyRegistry[Any] = PolicyRegistry()
    registry.register("analysis_priority", _make_analysis_priority)
    registry.register("deterministic", _make_deterministic)
    registry.register("reliability", _make_reliability)
    registry.register("retrieval", _make_retrieval)
    registry.register("auto_top8", _make_auto_top8)
    return registry


def _make_auto_top8(*, resources: PolicyResources) -> Any:
    _assert_no_external_resources(resources)
    return build_auto_top8_expansion_policy(resources=resources)


def _common_reliability_kwargs(config: Any, table: Optional[ReliabilityTable]) -> dict[str, Any]:
    return dict(
        reliability_table=table,
        invalid_penalty=config.reliability_penalty_invalid,
        fallback_penalty=config.reliability_penalty_fallback,
        warning_penalty=config.reliability_penalty_warning,
        regression_penalty=config.reliability_penalty_regression,
        clean_bonus=config.reliability_clean_bonus,
        expected_qor_gain_weight=config.reliability_expected_qor_gain_weight,
        runtime_penalty=config.reliability_runtime_penalty,
        min_clean_prob=config.reliability_min_clean_prob,
    )


def _load_reliability(config: Any) -> Optional[ReliabilityTable]:
    if not config.reliability_stats_path:
        return None
    if getattr(config, "contest_clean", False):
        raise ValueError("contest_clean forbids external reliability_stats_path")
    path = Path(config.reliability_stats_path)
    return ReliabilityTable.from_jsonl(path) if path.name.endswith(".jsonl") else ReliabilityTable.from_summary_json(path)


def _load_retrieval(config: Any) -> Optional[TrajectoryRetrievalIndex]:
    if not config.retrieval_index_path:
        return None
    if getattr(config, "contest_clean", False):
        raise ValueError("contest_clean forbids external retrieval_index_path")
    return TrajectoryRetrievalIndex.load(Path(config.retrieval_index_path))


def _assert_no_external_resources(resources: PolicyResources) -> None:
    if not getattr(resources.config, "contest_clean", False):
        return
    violations = []
    if resources.reliability_table is not None:
        violations.append("reliability_table")
    if resources.retrieval_index is not None:
        violations.append("retrieval_index")
    if violations:
        raise ValueError(
            "contest_clean forbids externally supplied policy resources: "
            + ", ".join(violations)
        )


def _make_deterministic(*, resources: PolicyResources) -> DeterministicExpansionPolicy:
    _assert_no_external_resources(resources)
    return DeterministicExpansionPolicy(policy_name="deterministic")


def _make_analysis_priority(*, resources: PolicyResources) -> AnalysisPriorityExpansionPolicy:
    _assert_no_external_resources(resources)
    return AnalysisPriorityExpansionPolicy()


def _make_reliability(*, resources: PolicyResources) -> ReliabilityAwareExpansionPolicy:
    _assert_no_external_resources(resources)
    table = resources.reliability_table if resources.reliability_table is not None else _load_reliability(resources.config)
    return ReliabilityAwareExpansionPolicy(**_common_reliability_kwargs(resources.config, table))


def _make_retrieval(*, resources: PolicyResources) -> RetrievalAwareExpansionPolicy:
    _assert_no_external_resources(resources)
    validate = getattr(resources.config, "validate_contest_clean", None)
    if callable(validate):
        validate()
    table = resources.reliability_table if resources.reliability_table is not None else _load_reliability(resources.config)
    retrieval_index = resources.retrieval_index if resources.retrieval_index is not None else _load_retrieval(resources.config)
    config = resources.config
    return RetrievalAwareExpansionPolicy(
        retrieval_index=retrieval_index,
        retrieval_success_weight=config.retrieval_success_weight,
        retrieval_invalid_penalty=config.retrieval_invalid_penalty,
        retrieval_fallback_penalty=config.retrieval_fallback_penalty,
        retrieval_expected_qor_gain_weight=config.retrieval_expected_qor_gain_weight,
        retrieval_motif_bonus_weight=config.retrieval_motif_bonus_weight,
        retrieval_k=config.retrieval_k,
        **_common_reliability_kwargs(config, table),
    )
