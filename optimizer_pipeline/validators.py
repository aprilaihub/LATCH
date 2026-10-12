"""Output validators for constrained expansion execution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import (
    ExecutionResult,
    OutputValidationResult,
    ValidationStatus,
)


class OutputValidator:
    """Log-aware output validation for deterministic optimisation runs.

    The validator still checks the basic artifacts, but it also parses Vivado and
    RapidWright logs for known soft failures.  Routing failures are invalid;
    RapidWright readable-EDIF/fallback issues are warnings unless they also
    produce routing or execution errors.
    """

    INVALID_PATTERNS: Tuple[Tuple[str, str], ...] = (
        ("route_design_failed", r"(?i)(Common 17-39).*route_design.*failed|route_design['\"]? failed|route_design failed"),
        ("router_failed", r"(?i)Router failed|failed to deposit pin-assignments"),
        ("physical_net_conflict", r"(?i)Conflicting nets for physical connection"),
        ("nonzero_routing_errors", r"(?i)#\s*of\s+nets\s+with\s+routing\s+errors\D+([1-9]\d*)"),
        ("nonzero_unrouted_pins", r"(?i)#\s*of\s+nets\s+with\s+some\s+unrouted\s+pins\D+([1-9]\d*)"),
        ("nonzero_antennas_islands", r"(?i)#\s*of\s+nets\s+with\s+antennas/islands\D+([1-9]\d*)"),
        ("timing_route_status_error", r"(?i)routing\s+errors\s*:\s*([1-9]\d*)"),
        ("unplaced_or_unrouted_design", r"(?i)design\s+is\s+unrouted|unrouted\s+pins"),
    )

    WARNING_PATTERNS: Tuple[Tuple[str, str], ...] = (
        ("rapidwright_error_loading_design", r"(?i)ERROR\s*-\s*Error loading design"),
        ("rapidwright_unreadable_edif", r"(?i)Unable to find a readable EDIF file for the DCP"),
        ("rapidwright_edif_autogen_failed", r"(?i)Failed to auto-generate an EDIF file"),
        ("vivado_not_on_path_for_rapidwright", r"(?im)^.*(?:couldn'?t|could not)\s+find\s+vivado\s+on\s+PATH.*$"),
        ("fallback_used", r"(?i)fallback(?:_|\s|-)?used|fallback:"),
        ("rapidwright_warning", r"(?i)RapidWright.*warning|WARNING\s*-"),
    )

    LOG_GLOBS = (
        "terminal.log",
        "*.log",
        "*.jou",
        "run_summary.json",
    )

    def __init__(self, validation_mode: str = "log_aware"):
        self.validation_mode = validation_mode

    def validate(self, execution: ExecutionResult) -> OutputValidationResult:
        errors: List[str] = []
        warnings: List[str] = []
        evidence: Dict[str, Any] = {
            "artifact_checks": {},
            "invalid_matches": [],
            "warning_matches": [],
            "routing_counts": {},
        }
        output_path = Path(execution.output_dcp)
        summary_path = Path(execution.summary_path) if execution.summary_path else None

        if not execution.success:
            errors.append(f"executor failed with exit_code={execution.exit_code}")
        evidence["artifact_checks"]["executor_success"] = bool(execution.success)

        if not output_path.exists():
            errors.append(f"output DCP not found: {output_path}")
        evidence["artifact_checks"]["output_dcp_exists"] = output_path.exists()

        summary_exists = bool(summary_path and summary_path.exists())
        metrics_available = bool(execution.metrics)
        if not summary_exists and not metrics_available:
            errors.append("executor produced neither run_summary.json nor in-memory metrics")
        evidence["artifact_checks"]["summary_exists"] = summary_exists
        evidence["artifact_checks"]["in_memory_metrics_available"] = metrics_available

        run_dir = Path(execution.run_dir)
        log_paths = self._collect_logs(run_dir)
        text_by_path = self._read_logs(log_paths)
        combined_text = "\n".join(text_by_path.values())

        for key, pattern in self.INVALID_PATTERNS:
            match = re.search(pattern, combined_text, flags=re.MULTILINE)
            if match:
                message = f"{key}: {self._short_match(match)}"
                errors.append(message)
                evidence["invalid_matches"].append({"key": key, "match": self._short_match(match)})

        for key, pattern in self.WARNING_PATTERNS:
            match = re.search(pattern, combined_text, flags=re.MULTILINE)
            if match:
                message = f"{key}: {self._short_match(match)}"
                warnings.append(message)
                evidence["warning_matches"].append({"key": key, "match": self._short_match(match)})

        evidence["routing_counts"] = self._extract_routing_counts(combined_text)
        fallback_used = any(
            item["key"] in {
                "rapidwright_error_loading_design",
                "rapidwright_unreadable_edif",
                "rapidwright_edif_autogen_failed",
                "vivado_not_on_path_for_rapidwright",
                "fallback_used",
            }
            for item in evidence["warning_matches"]
        )

        status = ValidationStatus.CLEAN.value
        if errors:
            status = ValidationStatus.INVALID.value
        elif fallback_used:
            status = ValidationStatus.FALLBACK_USED.value
        elif warnings:
            status = ValidationStatus.WARNING.value

        details = {
            "log_count": len(log_paths),
            "logs_scanned": [str(p) for p in log_paths],
            "validation_mode": self.validation_mode,
        }
        metadata = {
            "flow": execution.flow,
            "exit_code": execution.exit_code,
            "executor_error": execution.error,
        }
        return OutputValidationResult(
            valid=status != ValidationStatus.INVALID.value,
            output_dcp=str(output_path),
            summary_path=str(summary_path) if summary_path else None,
            validation_mode=self.validation_mode,
            errors=_dedupe(errors),
            warnings=_dedupe(warnings),
            status=status,
            fallback_used=fallback_used,
            evidence=evidence,
            log_paths=[str(path) for path in log_paths],
            details=details,
            metadata=metadata,
        )

    def _collect_logs(self, run_dir: Path) -> List[Path]:
        if not run_dir.exists():
            return []
        paths: List[Path] = []
        for pattern in self.LOG_GLOBS:
            paths.extend(p for p in run_dir.rglob(pattern) if p.is_file())
        # Also include parent terminal/log files if executor_run sits under a node directory.
        parent = run_dir.parent
        if run_dir.name == "executor_run" and parent.exists() and parent != run_dir:
            for pattern in self.LOG_GLOBS:
                paths.extend(p for p in parent.glob(pattern) if p.is_file())
        # Stable unique order.
        seen = set()
        unique: List[Path] = []
        for path in sorted(paths):
            resolved = str(path.resolve())
            if resolved not in seen:
                unique.append(path)
                seen.add(resolved)
        return unique

    @staticmethod
    def _read_logs(paths: Iterable[Path], max_bytes_per_file: int = 2_000_000) -> Dict[str, str]:
        data: Dict[str, str] = {}
        for path in paths:
            try:
                raw = path.read_bytes()
                if len(raw) > max_bytes_per_file:
                    raw = raw[-max_bytes_per_file:]
                if path.suffix == ".json":
                    try:
                        parsed = json.loads(raw.decode("utf-8", errors="replace"))
                        text = json.dumps(parsed, indent=2, sort_keys=True)
                    except Exception:
                        text = raw.decode("utf-8", errors="replace")
                else:
                    text = raw.decode("utf-8", errors="replace")
                data[str(path)] = text
            except Exception:
                continue
        return data

    @staticmethod
    def _short_match(match: re.Match[str], max_len: int = 220) -> str:
        text = match.group(0).strip().replace("\n", " ")
        return text[:max_len]

    @staticmethod
    def _extract_routing_counts(text: str) -> Dict[str, int]:
        patterns = {
            "routing_errors": r"(?i)#\s*of\s+nets\s+with\s+routing\s+errors\D+(\d+)",
            "unrouted_pins": r"(?i)#\s*of\s+nets\s+with\s+some\s+unrouted\s+pins\D+(\d+)",
            "antennas_islands": r"(?i)#\s*of\s+nets\s+with\s+antennas/islands\D+(\d+)",
        }
        counts: Dict[str, int] = {}
        for key, pattern in patterns.items():
            found = [int(m.group(1)) for m in re.finditer(pattern, text)]
            if found:
                counts[key] = max(found)
        return counts


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out
