"""Parse executor-produced diagnostic artifacts into analysis-context fields."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def parse_executor_diagnostics(executor_run_dir: Path) -> Dict[str, Any]:
    """Return structured diagnostics derived from executor-run side artifacts."""
    root = Path(executor_run_dir)
    diagnostics: Dict[str, Any] = {}
    critical = _parse_critical_path_artifacts(root)
    if critical:
        diagnostics["critical_path_features"] = critical
        recurrence = critical.get("top_repeated_cells") if isinstance(critical.get("top_repeated_cells"), list) else []
        if recurrence:
            diagnostics["fanout"] = {
                "proxy_available": True,
                "proxy_source": "critical_path_cell_recurrence",
                "top_recurrent_cells": recurrence[:10],
                "max_path_recurrence_count": recurrence[0].get("count") if isinstance(recurrence[0], dict) else None,
                "source_artifacts": critical.get("source_artifacts", []),
            }
    congestion = _parse_congestion_evidence(root)
    if congestion:
        diagnostics["congestion"] = congestion
    return diagnostics


def apply_executor_diagnostic_backfill(analysis_context: Dict[str, Any], executor_run_dir: Path) -> None:
    """Merge parsed executor diagnostics without overwriting stronger evidence."""
    diagnostics = parse_executor_diagnostics(executor_run_dir)
    if not diagnostics:
        return
    provenance = analysis_context.get("diagnostic_provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    for section in ("critical_path_features", "fanout", "congestion"):
        parsed = diagnostics.get(section)
        if not isinstance(parsed, dict) or not parsed:
            continue
        current = analysis_context.get(section)
        if not isinstance(current, dict):
            current = {}
        merged, changed = _merge_missing(current, parsed)
        if changed:
            analysis_context[section] = merged
            artifacts = parsed.get("source_artifacts") or parsed.get("source_logs") or []
            provenance[section] = {
                "source": "executor_artifact" if section != "congestion" else "executor_log_or_route_report",
                "artifacts": list(artifacts) if isinstance(artifacts, list) else [],
            }
    if provenance:
        analysis_context["diagnostic_provenance"] = provenance


def _parse_critical_path_artifacts(executor_run_dir: Path) -> Dict[str, Any]:
    paths: List[List[str]] = []
    artifacts: List[str] = []
    for path in sorted(executor_run_dir.glob("critical_paths*.json")):
        loaded = _load_json(path)
        parsed = _critical_paths_from_payload(loaded)
        if not parsed:
            continue
        paths.extend(parsed)
        artifacts.append(f"executor_run/{path.name}")
    if not paths:
        return {}
    cell_counts = [len(path) for path in paths]
    all_cells = [cell for path in paths for cell in path]
    cell_counter = Counter(all_cells)
    primitive_counter = Counter(_primitive_hint(cell) for cell in all_cells)
    primitive_counter.pop("unknown", None)
    module_counter = Counter(_module_motif(cell) for cell in all_cells if _module_motif(cell))
    hardblock = any(_primitive_hint(cell) in {"bram", "uram", "dsp"} for cell in all_cells)
    return {
        "num_paths_analyzed": len(paths),
        "avg_path_cell_count": round(sum(cell_counts) / len(cell_counts), 6),
        "max_path_cell_count": max(cell_counts),
        "primitive_mix": dict(sorted(primitive_counter.items())),
        "hardblock_on_path": hardblock,
        "top_repeated_cells": _top_counts(cell_counter),
        "top_module_motifs": _top_counts(module_counter),
        "source_artifacts": artifacts,
    }


def _critical_paths_from_payload(payload: Any) -> List[List[str]]:
    if isinstance(payload, dict):
        for key in ("critical_paths", "paths", "path_cells"):
            if key in payload:
                return _critical_paths_from_payload(payload.get(key))
        return []
    if not isinstance(payload, list):
        return []
    out: List[List[str]] = []
    for item in payload:
        if isinstance(item, list):
            cells = [str(cell) for cell in item if cell is not None and str(cell)]
            if cells:
                out.append(cells)
        elif isinstance(item, dict):
            cells = item.get("cells") or item.get("cell_names") or item.get("path")
            if isinstance(cells, list):
                parsed = [str(cell) for cell in cells if cell is not None and str(cell)]
                if parsed:
                    out.append(parsed)
    return out


def _parse_congestion_evidence(executor_run_dir: Path) -> Dict[str, Any]:
    candidates: List[Path] = []
    for pattern in ("*route*.rpt", "*route*.log", "vivado.log", "*.rpt"):
        candidates.extend(sorted(executor_run_dir.glob(pattern)))
    seen = set()
    checked: List[Path] = []
    term_hits = Counter()
    route_status_hint: Optional[str] = None
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        text = _read_text_limited(path)
        if not text:
            continue
        checked.append(path)
        lower = text.lower()
        for term in ("congestion", "overflow", "unrouted", "routing failed", "route_design failed", "overutilized", "conflict"):
            count = lower.count(term)
            if count:
                term_hits[term] += count
        if "fully routed" in lower or "route_design completed successfully" in lower:
            route_status_hint = route_status_hint or "routed"
        if "unrouted" in lower or "routing failed" in lower:
            route_status_hint = "route_issue"
    if not checked:
        return {}
    hint = "present" if _has_positive_congestion_hint(term_hits, checked) else "not_observed"
    return {
        "evidence_checked": True,
        "source": "executor_log_or_route_report",
        "hint": hint,
        "route_status_hint": route_status_hint or "unknown",
        "term_hits": dict(term_hits),
        "source_logs": [f"executor_run/{path.name}" for path in checked[:10]],
    }


def _has_positive_congestion_hint(term_hits: Counter[str], checked: List[Path]) -> bool:
    if not term_hits:
        return False
    positive_terms = {
        term
        for term in term_hits
        if term not in {"congestion"}
    }
    if positive_terms:
        return True
    for path in checked:
        text = _read_text_limited(path).lower()
        if re.search(r"\b(no|without)\s+(routing\s+)?congestion\b", text):
            continue
        if re.search(r"\bcongestion\s+(was\s+)?(not\s+)?observed\b", text) and "not observed" in text:
            continue
        if "congestion" in text:
            return True
    return False


def _merge_missing(current: Dict[str, Any], parsed: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    merged = dict(current)
    changed = False
    for key, value in parsed.items():
        if key in {"source_artifacts", "source_logs"}:
            existing = merged.get(key) if isinstance(merged.get(key), list) else []
            combined = _dedupe_list(existing + list(value if isinstance(value, list) else []))
            if combined != existing:
                merged[key] = combined
                changed = True
            continue
        if not _has_signal(merged.get(key)) and _has_signal(value):
            merged[key] = value
            changed = True
    return merged, changed


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_text_limited(path: Path, limit: int = 1_000_000) -> str:
    try:
        return path.read_text(errors="ignore")[:limit]
    except Exception:
        return ""


def _primitive_hint(name: str) -> str:
    lower = str(name).lower()
    if "uram" in lower:
        return "uram"
    if "bram" in lower or "ramb" in lower or "sdpram" in lower:
        return "bram"
    if "dsp" in lower:
        return "dsp"
    if "lut" in lower or re.search(r"_i(_|\\b|\\d)", lower):
        return "lut"
    if "_reg" in lower or "fdre" in lower or "flop" in lower:
        return "ff"
    return "unknown"


def _module_motif(name: str) -> str:
    parts = [part for part in str(name).split("/") if part]
    if len(parts) >= 3:
        return "/".join(parts[:3])
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0] if parts else ""


def _top_counts(counter: Counter[str], limit: int = 10) -> List[Dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in counter.most_common(limit)
        if name and count > 1
    ]


def _dedupe_list(values: Iterable[Any]) -> List[Any]:
    out: List[Any] = []
    seen = set()
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _has_signal(value: Any) -> bool:
    if value is None or value == "" or value == "unknown":
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return any(_has_signal(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_signal(item) for item in value)
    return True
