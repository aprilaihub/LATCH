"""Health audit helpers for analysis-context artifacts."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List


def audit_analysis_contexts(root: Path) -> Dict[str, Any]:
    """Summarise whether experiment analysis contexts contain real evidence."""
    paths = sorted(Path(root).glob("**/analysis_context.json"))
    counts = Counter()
    provider_errors: Counter[str] = Counter()
    read_errors: List[Dict[str, str]] = []

    for path in paths:
        try:
            ctx = json.loads(path.read_text())
        except Exception as exc:
            read_errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not isinstance(ctx, dict):
            read_errors.append({"path": str(path), "error": f"unexpected {type(ctx).__name__}"})
            continue

        counts["total_analysis_contexts"] += 1
        if _nonempty_mapping(ctx.get("timing")):
            counts["timing_nonempty"] += 1
        if _nonempty_mapping(ctx.get("critical_path_features"), ignore_values={"unknown"}):
            counts["critical_path_nonempty"] += 1
        if _critical_path_artifact_nonempty(ctx.get("critical_path_features")):
            counts["critical_path_artifact_nonempty"] += 1
        if _nonempty_mapping(ctx.get("congestion")):
            counts["congestion_nonempty"] += 1
        if _congestion_checked_nonempty(ctx.get("congestion")):
            counts["congestion_checked_nonempty"] += 1
        if _nonempty_mapping(ctx.get("fanout")):
            counts["fanout_nonempty"] += 1
        if _fanout_proxy_nonempty(ctx.get("fanout")):
            counts["fanout_proxy_nonempty"] += 1

        candidate = ctx.get("candidate_flows") if isinstance(ctx.get("candidate_flows"), dict) else {}
        if _nonempty_sequence(candidate.get("deterministic_scores") or ctx.get("flow_scores")):
            counts["deterministic_flow_scores_nonempty"] += 1
        if _nonempty_sequence(candidate.get("selected_top3") or ctx.get("selected_top3")):
            counts["selected_top3_nonempty"] += 1

        error = ctx.get("analysis_provider_error")
        if error:
            provider_errors[str(error)] += 1

    total = counts["total_analysis_contexts"]
    diagnostic_total = sum(
        counts[key]
        for key in (
            "timing_nonempty",
            "critical_path_nonempty",
            "congestion_nonempty",
            "fanout_nonempty",
            "deterministic_flow_scores_nonempty",
            "selected_top3_nonempty",
        )
    )
    return {
        "schema_version": "analysis_context_health.v1",
        "root": str(Path(root)),
        "total_analysis_contexts": total,
        "timing_nonempty": counts["timing_nonempty"],
        "critical_path_nonempty": counts["critical_path_nonempty"],
        "critical_path_artifact_nonempty": counts["critical_path_artifact_nonempty"],
        "congestion_nonempty": counts["congestion_nonempty"],
        "congestion_checked_nonempty": counts["congestion_checked_nonempty"],
        "fanout_nonempty": counts["fanout_nonempty"],
        "fanout_proxy_nonempty": counts["fanout_proxy_nonempty"],
        "deterministic_flow_scores_nonempty": counts["deterministic_flow_scores_nonempty"],
        "selected_top3_nonempty": counts["selected_top3_nonempty"],
        "provider_error_histogram": dict(provider_errors),
        "read_errors": read_errors,
        "evidence_starved": bool(total > 0 and diagnostic_total == 0),
    }


def _nonempty_mapping(value: Any, *, ignore_values: Iterable[Any] = ()) -> bool:
    if not isinstance(value, dict):
        return False
    ignored = set(ignore_values)
    for item in value.values():
        if _has_signal(item, ignore_values=ignored):
            return True
    return False


def _critical_path_artifact_nonempty(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return _nonempty_sequence(value.get("source_artifacts")) and (
        _has_signal(value.get("num_paths_analyzed"))
        or _has_signal(value.get("primitive_mix"))
        or _has_signal(value.get("top_repeated_cells"))
    )


def _fanout_proxy_nonempty(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return bool(value.get("proxy_available")) and _nonempty_sequence(value.get("top_recurrent_cells"))


def _congestion_checked_nonempty(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return bool(value.get("evidence_checked")) and str(value.get("hint") or "unknown") in {"present", "not_observed"}


def _nonempty_sequence(value: Any) -> bool:
    return isinstance(value, list) and any(_has_signal(item) for item in value)


def _has_signal(value: Any, *, ignore_values: set[Any] | None = None) -> bool:
    ignored = ignore_values or set()
    if value is None or value == "":
        return False
    try:
        if value in ignored:
            return False
    except TypeError:
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return any(_has_signal(item, ignore_values=ignored) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_signal(item, ignore_values=ignored) for item in value)
    return True
