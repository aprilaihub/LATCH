"""Markdown reporting for frontier LLM tuning experiments."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


REPORT_COLUMNS = [
    "variant_slug",
    "requested_provider",
    "actual_provider",
    "requested_model",
    "actual_model",
    "requested_prompt_id",
    "actual_prompt_id",
    "config_mismatch",
    "temperature",
    "runs",
    "success_rate",
    "pipeline_valid_decision_rate",
    "llm_attempt_valid_rate",
    "llm_parse_failure_count",
    "llm_domain_validation_failure_count",
    "llm_policy_failure_count",
    "fallback_event_rate",
    "best_pure_qor_score_mean",
    "best_pure_qor_score_std",
    "total_llm_calls",
    "total_tokens",
    "estimated_or_reported_cost_usd",
    "cost_source",
    "cost_per_valid_llm_attempt",
    "cost_per_successful_run",
]


def render_comparison_report(experiment_id: str, aggregate_rows: Iterable[Dict[str, Any]]) -> str:
    rows = list(aggregate_rows)
    lines: List[str] = [
        f"# Frontier LLM Tuning Report: {experiment_id}",
        "",
        "This report is generated from pipeline artifacts only.",
        "",
    ]
    if not rows:
        lines.append("No completed run summaries were found.")
        return "\n".join(lines) + "\n"
    lines.append(_markdown_table(rows))
    lines.append("")
    return "\n".join(lines) + "\n"


def _markdown_table(rows: List[Dict[str, Any]]) -> str:
    header = "| " + " | ".join(REPORT_COLUMNS) + " |"
    divider = "| " + " | ".join(["---"] * len(REPORT_COLUMNS)) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(_format_cell(row.get(column)) for column in REPORT_COLUMNS) + " |")
    return "\n".join([header, divider, *body])


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)
