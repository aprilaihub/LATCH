"""Log-aware validation helpers for research frontier-expansion runs.

These helpers intentionally use conservative, explainable pattern matching.  They
are not a replacement for official Vivado signoff checks; they catch known soft
failures that otherwise appear as successful artifact-producing runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

INVALID_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("route_design_failed", re.compile(r"\broute_design\s+failed\b", re.IGNORECASE)),
    ("router_failed_pin_assignments", re.compile(r"Router failed to deposit pin-assignments", re.IGNORECASE)),
    ("physical_connection_conflict", re.compile(r"Conflicting nets for physical connection", re.IGNORECASE)),
    ("routing_error_message", re.compile(r"ERROR:\s*\[Route\s+", re.IGNORECASE)),
]

COUNT_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("routing_errors", re.compile(r"#\s*of\s+nets\s+with\s+routing\s+errors\D+(\d+)", re.IGNORECASE)),
    ("unrouted_pins", re.compile(r"#\s*of\s+nets\s+with\s+some\s+unrouted\s+pins\D+(\d+)", re.IGNORECASE)),
    ("antennas_or_islands", re.compile(r"#\s*of\s+nets\s+with\s+antennas/islands\D+(\d+)", re.IGNORECASE)),
]

FALLBACK_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("vivado_not_on_path_for_rapidwright", re.compile(r"Couldn't find vivado on PATH", re.IGNORECASE)),
    ("rapidwright_unreadable_edif", re.compile(r"Unable to find a readable EDIF file", re.IGNORECASE)),
    ("rapidwright_edif_autogen_failed", re.compile(r"Failed to auto-generate an EDIF file", re.IGNORECASE)),
    ("rapidwright_readable_edif_hint", re.compile(r"readable EDIF", re.IGNORECASE)),
]

WARNING_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("critical_warning", re.compile(r"CRITICAL WARNING", re.IGNORECASE)),
    ("vivado_warning", re.compile(r"\bWARNING:\s*\[", re.IGNORECASE)),
]

LOG_NAMES = (
    "vivado.log",
    "vivado-mcp.log",
    "rapidwright.log",
    "rapidwright-mcp.log",
    "terminal.log",
)


def _read_text(path: Path, max_chars: int = 2_000_000) -> str:
    try:
        text = path.read_text(errors="ignore")
    except Exception:
        return ""
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


def _line_context(text: str, match_start: int, window: int = 160) -> str:
    lo = max(0, match_start - window)
    hi = min(len(text), match_start + window)
    snippet = text[lo:hi].replace("\n", " ")
    return re.sub(r"\s+", " ", snippet).strip()


def _scan_patterns(text: str, path: Path, patterns: Iterable[Tuple[str, re.Pattern[str]]], limit: int = 20) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for code, pattern in patterns:
        for match in pattern.finditer(text):
            issues.append({
                "code": code,
                "file": str(path),
                "context": _line_context(text, match.start()),
            })
            if len(issues) >= limit:
                return issues
    return issues


def parse_validation_logs(run_dir: Path, *, include_warnings: bool = True) -> Dict[str, Any]:
    """Parse known Vivado/RapidWright soft-failure signals from a run directory."""
    run_dir = Path(run_dir)
    invalid_issues: List[Dict[str, Any]] = []
    fallback_issues: List[Dict[str, Any]] = []
    warning_issues: List[Dict[str, Any]] = []
    count_metrics: Dict[str, int] = {}
    scanned_files: List[str] = []

    candidate_paths: List[Path] = []
    for name in LOG_NAMES:
        path = run_dir / name
        if path.exists() and path.is_file():
            candidate_paths.append(path)
    # Some flows place nested logs under the run directory. Keep this bounded.
    for path in sorted(run_dir.rglob("*.log")):
        if path not in candidate_paths:
            candidate_paths.append(path)
        if len(candidate_paths) >= 32:
            break

    for path in candidate_paths:
        text = _read_text(path)
        if not text:
            continue
        scanned_files.append(str(path))
        invalid_issues.extend(_scan_patterns(text, path, INVALID_PATTERNS))
        fallback_issues.extend(_scan_patterns(text, path, FALLBACK_PATTERNS))
        if include_warnings:
            warning_issues.extend(_scan_patterns(text, path, WARNING_PATTERNS, limit=10))
        for code, pattern in COUNT_PATTERNS:
            for match in pattern.finditer(text):
                try:
                    value = int(match.group(1))
                except Exception:
                    continue
                count_metrics[code] = max(count_metrics.get(code, 0), value)
                if value > 0:
                    invalid_issues.append({
                        "code": f"{code}_nonzero",
                        "file": str(path),
                        "value": value,
                        "context": _line_context(text, match.start()),
                    })

    status = "CLEAN"
    if invalid_issues:
        status = "INVALID"
    elif fallback_issues:
        status = "FALLBACK_USED"
    elif warning_issues:
        status = "WARNING"

    return {
        "status": status,
        "invalid_issues": invalid_issues[:50],
        "fallback_issues": fallback_issues[:50],
        "warning_issues": warning_issues[:50],
        "count_metrics": count_metrics,
        "scanned_files": scanned_files,
    }


def issue_codes(issues: Iterable[Dict[str, Any]]) -> List[str]:
    codes: List[str] = []
    for issue in issues:
        code = issue.get("code") if isinstance(issue, dict) else None
        if code and code not in codes:
            codes.append(str(code))
    return codes
