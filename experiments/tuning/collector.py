"""Collect and aggregate tuning-harness outputs."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from experiments.llm.runtime.decision_events import read_llm_decision_events

from .matrix import ExperimentRunSpec


def collect_run_rows(runs: Iterable[ExperimentRunSpec]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for run in runs:
        summary = _read_json(run.run_dir / "pipeline_summary.json")
        if not isinstance(summary, dict):
            summary = {}
        policy_events = _read_json(run.run_dir / "policy_events.json")
        if not isinstance(policy_events, list):
            policy_events = []
        llm_decision_events = read_llm_decision_events(run.run_dir)
        pipeline_valid_decisions, pipeline_total_decisions = _decision_counts(policy_events)
        llm_attempt_metrics = _llm_attempt_metrics(llm_decision_events)
        fallback_event_count = _fallback_count(policy_events, summary)
        requested_provider = run.factors.get("provider", run.base.get("provider"))
        requested_model = run.factors.get("model", run.base.get("model"))
        requested_prompt_id = run.factors.get("prompt_id", run.base.get("prompt_id"))
        requested_temperature = run.factors.get("temperature", run.base.get("temperature"))
        actual_provider = summary.get("llm_provider")
        actual_model = summary.get("llm_model")
        actual_prompt_id = summary.get("llm_prompt_id")
        actual_temperature = summary.get("llm_temperature")
        row = {
            "experiment_id": run.experiment_id,
            "variant_slug": run.variant_slug,
            "trial_slug": run.trial_slug,
            "input_dcp": run.input_dcp,
            "run_dir": str(run.run_dir),
            "requested_provider": requested_provider,
            "actual_provider": actual_provider,
            "provider": actual_provider or requested_provider,
            "requested_model": requested_model,
            "actual_model": actual_model,
            "model": actual_model or requested_model,
            "requested_prompt_id": requested_prompt_id,
            "actual_prompt_id": actual_prompt_id,
            "prompt_id": actual_prompt_id or requested_prompt_id,
            "requested_temperature": requested_temperature,
            "actual_temperature": actual_temperature,
            "temperature": actual_temperature if actual_temperature is not None else requested_temperature,
            "max_tokens": run.factors.get("max_tokens", summary.get("llm_max_tokens")),
            "response_mode": run.factors.get("llm_response_mode", summary.get("llm_response_mode")),
            "config_mismatch": _config_mismatch(
                requested_provider=requested_provider,
                actual_provider=actual_provider,
                requested_model=requested_model,
                actual_model=actual_model,
                requested_prompt_id=requested_prompt_id,
                actual_prompt_id=actual_prompt_id,
            ),
            "success": bool(summary.get("success")) if summary else False,
            "pipeline_valid_decision_count": pipeline_valid_decisions,
            "pipeline_total_decision_count": pipeline_total_decisions,
            "valid_decision_count": pipeline_valid_decisions,
            "total_decision_count": pipeline_total_decisions,
            "fallback_event_count": fallback_event_count,
            "fallback_count": fallback_event_count,
            **llm_attempt_metrics,
            "best_pure_qor_score": _as_float(summary.get("best_pure_qor_score")),
            "best_validation_status": summary.get("best_validation_status"),
            "total_llm_calls": _as_float(summary.get("total_llm_calls"), 0.0),
            "total_prompt_tokens": _as_float(summary.get("total_prompt_tokens")),
            "total_completion_tokens": _as_float(summary.get("total_completion_tokens")),
            "total_tokens": _as_float(summary.get("total_tokens")),
            "total_cost_usd": _as_float(summary.get("total_cost_usd")),
            "estimated_or_reported_cost_usd": _as_float(summary.get("total_cost_usd")),
            "cost_source": summary.get("cost_source"),
            "pricing_profile_id": summary.get("pricing_profile_id"),
            "total_latency_sec": _as_float(summary.get("total_latency_sec")),
        }
        for key, value in run.factors.items():
            row[f"factor_{key}"] = value
        rows.append(row)
    return rows


def aggregate_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get("variant_slug")), []).append(row)
    aggregate: List[Dict[str, Any]] = []
    for variant_slug, group in groups.items():
        runs = len(group)
        pipeline_valid = sum(_as_float(row.get("pipeline_valid_decision_count"), 0.0) or 0.0 for row in group)
        pipeline_decisions = sum(_as_float(row.get("pipeline_total_decision_count"), 0.0) or 0.0 for row in group)
        llm_attempts = sum(_as_float(row.get("llm_attempt_count"), 0.0) or 0.0 for row in group)
        llm_valid = sum(_as_float(row.get("llm_attempt_valid_count"), 0.0) or 0.0 for row in group)
        fallbacks = sum(_as_float(row.get("fallback_event_count"), 0.0) or 0.0 for row in group)
        llm_calls = sum(_as_float(row.get("total_llm_calls"), 0.0) or 0.0 for row in group)
        tokens = _sum_optional(row.get("total_tokens") for row in group)
        cost = _sum_optional(row.get("estimated_or_reported_cost_usd") for row in group)
        best_scores = [_as_float(row.get("best_pure_qor_score")) for row in group]
        best_scores = [value for value in best_scores if value is not None]
        first = group[0]
        aggregate.append({
            "variant_slug": variant_slug,
            "requested_provider": first.get("requested_provider"),
            "actual_provider": first.get("actual_provider"),
            "provider": first.get("provider"),
            "requested_model": first.get("requested_model"),
            "actual_model": first.get("actual_model"),
            "model": first.get("model"),
            "requested_prompt_id": first.get("requested_prompt_id"),
            "actual_prompt_id": first.get("actual_prompt_id"),
            "prompt_id": first.get("prompt_id"),
            "temperature": first.get("temperature"),
            "config_mismatch": any(bool(row.get("config_mismatch")) for row in group),
            "runs": runs,
            "success_rate": _rate(sum(1 for row in group if row.get("success")), runs),
            "pipeline_valid_decision_rate": _rate(pipeline_valid, pipeline_decisions),
            "valid_decision_rate": _rate(pipeline_valid, pipeline_decisions),
            "llm_attempt_count": llm_attempts,
            "llm_attempt_valid_count": llm_valid,
            "llm_attempt_valid_rate": _rate(llm_valid, llm_attempts),
            "llm_parse_failure_count": sum(_as_float(row.get("llm_parse_failure_count"), 0.0) or 0.0 for row in group),
            "llm_domain_validation_failure_count": sum(_as_float(row.get("llm_domain_validation_failure_count"), 0.0) or 0.0 for row in group),
            "llm_policy_failure_count": sum(_as_float(row.get("llm_policy_failure_count"), 0.0) or 0.0 for row in group),
            "fallback_event_count": fallbacks,
            "fallback_event_rate": _rate(fallbacks, max(pipeline_decisions, runs)),
            "fallback_rate": _rate(fallbacks, max(pipeline_decisions, runs)),
            "best_pure_qor_score_mean": _mean(best_scores),
            "best_pure_qor_score_std": _stddev(best_scores),
            "total_llm_calls": llm_calls,
            "total_tokens": tokens,
            "total_cost_usd": cost,
            "estimated_or_reported_cost_usd": cost,
            "cost_source": _merged_cost_source(group),
            "cost_per_valid_llm_attempt": None if cost is None or llm_valid <= 0 else cost / llm_valid,
            "cost_per_successful_run": None if cost is None or not any(row.get("success") for row in group) else cost / sum(1 for row in group if row.get("success")),
            "cost_per_valid_decision": None if cost is None or pipeline_valid <= 0 else cost / pipeline_valid,
        })
    return sorted(aggregate, key=lambda row: str(row.get("variant_slug")))


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fieldnames: List[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _decision_counts(policy_events: List[Any]) -> tuple[int, int]:
    total = 0
    valid = 0
    for event in policy_events:
        if not isinstance(event, dict):
            continue
        validation = event.get("validation") if isinstance(event.get("validation"), dict) else {}
        total += 1
        if validation.get("valid") is True:
            valid += 1
    return valid, total


def _llm_attempt_metrics(llm_decision_events: List[Any]) -> Dict[str, int]:
    metrics = {
        "llm_attempt_count": 0,
        "llm_attempt_valid_count": 0,
        "llm_parse_failure_count": 0,
        "llm_domain_validation_failure_count": 0,
        "llm_policy_failure_count": 0,
    }
    for event in llm_decision_events:
        if not isinstance(event, dict):
            continue
        metrics["llm_attempt_count"] += 1
        if event.get("domain_validation_success") is True:
            metrics["llm_attempt_valid_count"] += 1
            continue
        errors = _attempt_error_text(event)
        if _is_policy_failure(errors):
            metrics["llm_policy_failure_count"] += 1
        elif event.get("json_parse_success") is False or _is_parse_failure(errors):
            metrics["llm_parse_failure_count"] += 1
        else:
            metrics["llm_domain_validation_failure_count"] += 1
    return metrics


def _fallback_count(policy_events: List[Any], summary: Dict[str, Any]) -> int:
    count = 0
    for event in policy_events:
        if not isinstance(event, dict):
            continue
        decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
        if decision.get("fallback_used"):
            count += 1
    if count:
        return count
    fallback_events = summary.get("fallback_events") if isinstance(summary.get("fallback_events"), list) else []
    return len(fallback_events)


def _attempt_error_text(attempt: Dict[str, Any]) -> str:
    parts: List[str] = []
    errors = attempt.get("validation_errors") if isinstance(attempt.get("validation_errors"), list) else []
    parts.extend(str(error) for error in errors)
    return " | ".join(parts).lower()


def _is_parse_failure(error_text: str) -> bool:
    return any(fragment in error_text for fragment in ("selectorinvalidresponse", "json parse", "malformed json", "did not return a json", "empty content"))


def _is_policy_failure(error_text: str) -> bool:
    return any(fragment in error_text for fragment in ("provider call failed", "no api key", "llm frontier policy call failed", "llmclienterror"))


def _config_mismatch(
    *,
    requested_provider: Any,
    actual_provider: Any,
    requested_model: Any,
    actual_model: Any,
    requested_prompt_id: Any,
    actual_prompt_id: Any,
) -> bool:
    pairs = [
        (requested_provider, actual_provider),
        (requested_model, actual_model),
        (requested_prompt_id, actual_prompt_id),
    ]
    for requested, actual in pairs:
        if requested is not None and actual is not None and str(requested) != str(actual):
            return True
    return False


def _merged_cost_source(group: List[Dict[str, Any]]) -> Optional[str]:
    sources = {str(row.get("cost_source")) for row in group if row.get("cost_source")}
    if not sources:
        return None
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _sum_optional(values: Iterable[Any]) -> Optional[float]:
    total: Optional[float] = None
    for value in values:
        parsed = _as_float(value)
        if parsed is None:
            continue
        total = parsed if total is None else total + parsed
    return total


def _rate(numerator: float, denominator: float) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _stddev(values: List[float]) -> Optional[float]:
    if len(values) < 2:
        return 0.0 if values else None
    mean = _mean(values) or 0.0
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))
