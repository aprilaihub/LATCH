"""First-class LLM decision attempt event helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from optimizer_pipeline.models import to_jsonable


LLM_DECISION_EVENT_SCHEMA_VERSION = "llm_decision_attempt.v1"
LLM_DECISION_EVENTS_FILENAME = "llm_decision_events.jsonl"


def build_llm_decision_event_rows(
    *,
    run_id: Optional[str],
    event_index: int,
    attempts: Iterable[Dict[str, Any]],
    final_decision: Dict[str, Any],
    task: str = "frontier_expansion",
) -> List[Dict[str, Any]]:
    attempt_rows = [attempt for attempt in attempts if isinstance(attempt, dict)]
    fallback_used = bool(final_decision.get("fallback_used")) if isinstance(final_decision, dict) else False
    last_index = len(attempt_rows) - 1
    rows: List[Dict[str, Any]] = []
    for index, attempt in enumerate(attempt_rows):
        request = attempt.get("request") if isinstance(attempt.get("request"), dict) else {}
        validation = attempt.get("validation") if isinstance(attempt.get("validation"), dict) else {}
        metadata = attempt.get("llm_metadata") if isinstance(attempt.get("llm_metadata"), dict) else {}
        errors = validation.get("errors") if isinstance(validation.get("errors"), list) else []
        row = {
            "schema_version": LLM_DECISION_EVENT_SCHEMA_VERSION,
            "task": task,
            "run_id": run_id,
            "event_index": event_index,
            "attempt_index": attempt.get("attempt_index") or index + 1,
            "provider": metadata.get("provider"),
            "model": metadata.get("model"),
            "prompt_id": metadata.get("prompt_id"),
            "prompt_hash": metadata.get("prompt_hash"),
            "response_schema_hash": metadata.get("response_schema_hash"),
            "temperature": metadata.get("temperature"),
            "max_tokens": metadata.get("max_tokens"),
            "request_context_hash": request_context_hash(request),
            "frontier_node_count": _count(request.get("frontier_node_ids")),
            "allowed_flow_count": _count(request.get("allowed_flows")),
            "json_parse_success": attempt.get("decision") is not None,
            "domain_validation_success": validation.get("valid") is True,
            "validation_errors": [str(error) for error in errors],
            "fallback_triggered_after_attempt": fallback_used and index == last_index,
            "prompt_tokens": metadata.get("prompt_tokens"),
            "completion_tokens": metadata.get("completion_tokens"),
            "cost_usd": metadata.get("cost_usd"),
            "cost_source": metadata.get("cost_source"),
            "latency_sec": metadata.get("latency_sec"),
        }
        rows.append(to_jsonable(row))
    return rows


def request_context_hash(request_summary: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(request_summary).encode("utf-8")).hexdigest()


def read_llm_decision_events(run_dir_or_path: Path) -> List[Dict[str, Any]]:
    path = Path(run_dir_or_path)
    if path.name != LLM_DECISION_EVENTS_FILENAME:
        path = path / LLM_DECISION_EVENTS_FILENAME
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:
        return []
    return rows


def write_llm_decision_events(run_dir_or_path: Path, rows: Iterable[Dict[str, Any]]) -> Path:
    path = Path(run_dir_or_path)
    if path.name != LLM_DECISION_EVENTS_FILENAME:
        path = path / LLM_DECISION_EVENTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(_canonical_json(row) + "\n")
    return path


def llm_metadata_by_event_index(rows: Iterable[Dict[str, Any]]) -> Dict[Any, Dict[str, Any]]:
    latest: Dict[Any, Dict[str, Any]] = {}
    latest_attempt: Dict[Any, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        event_index = row.get("event_index")
        attempt_index = _safe_int(row.get("attempt_index"), 0)
        if event_index not in latest or attempt_index >= latest_attempt.get(event_index, -1):
            latest[event_index] = llm_metadata_from_decision_event(row)
            latest_attempt[event_index] = attempt_index
    return latest


def llm_metadata_from_decision_event(row: Dict[str, Any]) -> Dict[str, Any]:
    prompt_tokens = row.get("prompt_tokens")
    completion_tokens = row.get("completion_tokens")
    total_tokens = _sum_optional_int(prompt_tokens, completion_tokens)
    errors = row.get("validation_errors") if isinstance(row.get("validation_errors"), list) else []
    return {
        "provider": row.get("provider"),
        "model": row.get("model"),
        "prompt_id": row.get("prompt_id"),
        "prompt_hash": row.get("prompt_hash"),
        "response_schema_hash": row.get("response_schema_hash"),
        "temperature": row.get("temperature"),
        "max_tokens": row.get("max_tokens"),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": row.get("cost_usd"),
        "cost_source": row.get("cost_source"),
        "latency_sec": row.get("latency_sec"),
        "error": " | ".join(str(error) for error in errors) if errors else None,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"))


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sum_optional_int(left: Any, right: Any) -> Optional[int]:
    try:
        if left is None or right is None:
            return None
        return int(left) + int(right)
    except (TypeError, ValueError):
        return None
