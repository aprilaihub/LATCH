"""History retrieval -- select a small, relevant subset of benchmark_history.

A retrieval STRATEGY is a plain function registered by name:

    @register_strategy("my_strategy")
    def _my_strategy(records, live_design_context, params):
        ...
        return examples, meta

``records``            -- the full benchmark_history list from the KB.
``live_design_context`` -- flat dict of whatever is known about the design
                           being optimised right now (benchmark_name, lut_count,
                           ff_count, dsp_count, bram_count, initial_wns_ns,
                           initial_fmax_mhz). Any of these may be None.
``params``              -- the resolved config dict (see
                           ``retrieval_params_from_config``): strategy, max_examples,
                           fields, include_confidence.
``examples``            -- a list of FULL history records (not yet field-projected),
                           each carrying an injected ``_outcome_quality`` label.
``meta``                -- a flat, JSON-safe dict describing how selection went
                           (used for the optional history_retrieval_meta block).

Adding a new strategy is exactly this: write the function, decorate it, done --
no changes needed here or in projector.py. ``select_history`` is the only
dispatch point and never raises (unknown/missing strategy name falls back to
"complexity_bucket").
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from optimizer_pipeline.knowledge.signals import resolve_complexity_class

logger = logging.getLogger(__name__)

StrategyFn = Callable[
    [List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]],
    Tuple[List[Dict[str, Any]], Dict[str, Any]],
]

_STRATEGIES: Dict[str, StrategyFn] = {}


def register_strategy(name: str) -> Callable[[StrategyFn], StrategyFn]:
    def _decorator(fn: StrategyFn) -> StrategyFn:
        _STRATEGIES[name] = fn
        return fn

    return _decorator


def select_history(
    records: List[Dict[str, Any]],
    live_design_context: Optional[Dict[str, Any]],
    params: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Dispatch to the configured strategy; fail-safe on an unknown name."""
    name = params.get("strategy") or "complexity_bucket"
    fn = _STRATEGIES.get(name)
    if fn is None:
        logger.warning(
            "knowledgebase: unknown history_retrieval_strategy '%s'; using complexity_bucket",
            name,
        )
        fn = _STRATEGIES["complexity_bucket"]
    return fn(list(records or []), dict(live_design_context or {}), dict(params or {}))


def _outcome_fmax_delta(record: Dict[str, Any]) -> Optional[float]:
    outcome = record.get("outcome")
    return outcome.get("fmax_delta_mhz") if isinstance(outcome, dict) else None


def label_outcome_quality(record: Dict[str, Any]) -> str:
    delta = _outcome_fmax_delta(record)
    if delta is None:
        return "unknown_result"
    return "positive_result" if delta > 0 else "non_positive_result"


def _sort_key(record: Dict[str, Any]) -> Tuple[float, float, float, str]:
    """Ranking order: fmax desc, wns desc, runtime asc, name asc."""
    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
    fmax = outcome.get("fmax_delta_mhz")
    wns = outcome.get("wns_delta_ns")
    runtime = outcome.get("runtime_s")
    return (
        -(fmax if fmax is not None else float("-inf")),
        -(wns if wns is not None else float("-inf")),
        runtime if runtime is not None else float("inf"),
        str(record.get("flow_applied") or ""),
    )


@register_strategy("complexity_bucket")
def _complexity_bucket_strategy(
    records: List[Dict[str, Any]],
    live_design_context: Dict[str, Any],
    params: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Match the live design's complexity_class; rank real wins first.

    Records with no measured outcome (fmax_delta_mhz is None) are dropped --
    they carry no usable signal either way. Within the matched pool, positive
    results (fmax_delta_mhz > 0) are always ranked ahead of non-positive ones;
    non-positive records are only included to fill remaining slots and are
    labelled "non_positive_result" so the LLM never mistakes a flat/regressed
    run for a win. If the resolved bucket is empty (no coverage yet, e.g.
    "large") or the complexity itself is unresolvable, falls back to a global
    pool across all records rather than returning nothing.
    """
    max_examples = max(1, int(params.get("max_examples", 4) or 4))
    complexity, source = resolve_complexity_class(live_design_context)

    if complexity is not None:
        pool = [r for r in records if r.get("complexity_class") == complexity]
        bucket_match = "direct"
    else:
        pool = []
        bucket_match = "direct"

    if not pool:
        bucket_match = (
            "global_fallback_unknown_complexity"
            if complexity is None
            else "global_fallback_empty_bucket"
        )
        pool = list(records)

    pool_valid = [r for r in pool if _outcome_fmax_delta(r) is not None]
    pool_valid.sort(key=_sort_key)

    positive = [r for r in pool_valid if label_outcome_quality(r) == "positive_result"]
    non_positive = [r for r in pool_valid if label_outcome_quality(r) != "positive_result"]
    selected = (positive + non_positive)[:max_examples]

    examples = [dict(r, _outcome_quality=label_outcome_quality(r)) for r in selected]
    meta = {
        "strategy": "complexity_bucket",
        "query_complexity_class": complexity,
        "complexity_source": source,
        "bucket_match": bucket_match,
        "bucket_size": len(pool_valid),
        "positive_count": len(positive),
        "examples_returned": len(examples),
    }
    return examples, meta


# --- field projection -------------------------------------------------------
#
# Each surfaced example is compacted to exactly the configured field list.
# Adding a field is: add one entry here (name -> extractor) and reference the
# name from history_retrieval_fields in config. No other code changes.

_FIELD_EXTRACTORS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "record_id": lambda r: r.get("record_id"),
    "benchmark_id": lambda r: r.get("benchmark_id"),
    "benchmark_family": lambda r: r.get("benchmark_family"),
    "complexity_class": lambda r: r.get("complexity_class"),
    "flow_applied": lambda r: r.get("flow_applied"),
    "flow_sequence_before": lambda r: r.get("flow_sequence_before"),
    "fmax_delta_mhz": lambda r: (r.get("outcome") or {}).get("fmax_delta_mhz"),
    "wns_delta_ns": lambda r: (r.get("outcome") or {}).get("wns_delta_ns"),
    "runtime_s": lambda r: (r.get("outcome") or {}).get("runtime_s"),
    "verification_passed": lambda r: (r.get("outcome") or {}).get("verification_passed"),
    "was_optimal": lambda r: r.get("was_optimal"),
    "optimality_rank": lambda r: r.get("optimality_rank"),
    "alternatives_tested": lambda r: r.get("alternatives_tested"),
    "notes": lambda r: r.get("notes"),
    "outcome_quality": lambda r: r.get("_outcome_quality"),
}

DEFAULT_EXAMPLE_FIELDS: Tuple[str, ...] = (
    "benchmark_id",
    "complexity_class",
    "flow_applied",
    "fmax_delta_mhz",
    "wns_delta_ns",
    "outcome_quality",
    "verification_passed",
    "optimality_rank",
)


def project_example(record: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in fields:
        extractor = _FIELD_EXTRACTORS.get(name)
        if extractor is None:
            logger.warning("knowledgebase: unknown history_retrieval field '%s'; skipped", name)
            continue
        out[name] = extractor(record)
    return out


def retrieval_params_from_config(config: Any) -> Optional[Dict[str, Any]]:
    """Build the strategy-facing params dict from OptimizationConfig, or None
    when retrieval is disabled (the off-switch -> full pass-through)."""
    if not getattr(config, "history_retrieval_enabled", False):
        return None
    fields = getattr(config, "history_retrieval_fields", None)
    return {
        "strategy": getattr(config, "history_retrieval_strategy", None) or "complexity_bucket",
        "max_examples": max(1, int(getattr(config, "history_retrieval_max_examples", 4) or 4)),
        "fields": [str(f) for f in fields if f] if fields else list(DEFAULT_EXAMPLE_FIELDS),
        "include_confidence": bool(getattr(config, "history_retrieval_include_confidence", True)),
    }
