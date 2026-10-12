"""Read-only report-probe backend abstractions.

The default backend is cached/read-only and never launches Vivado. This module
also provides reserved hooks for local Vivado and MCP-backed read-only report
providers; both fail closed unless a concrete provider is configured.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol

from optimizer_pipeline.backends.xilinx.tcl import tcl_word


class ReportProbeBackend(Protocol):
    name: str
    mutates_design: bool
    fresh_vivado_report: bool

    def report_timing_summary(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]: ...
    def report_route_status(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]: ...
    def report_utilization(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]: ...


@dataclass
class CachedAnalysisContextReportBackend:
    name: str = "cached_analysis_context"
    mutates_design: bool = False
    fresh_vivado_report: bool = False

    def report_timing_summary(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]:
        node_id = getattr(context.node, "node_id", None)
        ctx = context.analysis_contexts.get(str(node_id), {}) if node_id else {}
        cpf = ctx.get("critical_path_features") if isinstance(ctx.get("critical_path_features"), dict) else {}
        timing = ctx.get("timing") if isinstance(ctx.get("timing"), dict) else {}
        metrics = getattr(context.node, "metrics", {}) or {}
        return _with_provenance({
            "mode": "cached_report_probe",
            "node_id": node_id,
            "timing": {
                "wns_ns": metrics.get("final_wns_ns") or ctx.get("final_wns_ns") or timing.get("final_wns_ns") or ctx.get("initial_wns_ns") or timing.get("initial_wns_ns"),
                "tns_ns": metrics.get("final_tns_ns") or ctx.get("final_tns_ns") or timing.get("final_tns_ns") or ctx.get("initial_tns_ns") or timing.get("initial_tns_ns"),
                "fmax_mhz": metrics.get("final_fmax_mhz") or ctx.get("final_fmax_mhz") or timing.get("final_fmax_mhz") or ctx.get("initial_fmax_mhz") or timing.get("initial_fmax_mhz"),
                "failing_endpoints": ctx.get("failing_endpoints") or timing.get("failing_endpoints"),
            },
            "critical_path_features": {
                "route_delay_ratio": cpf.get("route_delay_ratio"),
                "dominant_path_type": cpf.get("dominant_path_type"),
                "avg_logic_levels": cpf.get("avg_logic_levels"),
                "max_logic_levels": cpf.get("max_logic_levels"),
            },
            "note": "Cached report-probe backend; no Vivado process launched and no design mutation performed.",
        }, self)

    def report_route_status(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]:
        node = context.node
        metrics = getattr(node, "metrics", {}) or {}
        evidence = metrics.get("validation_evidence") if isinstance(metrics.get("validation_evidence"), dict) else {}
        return _with_provenance({
            "mode": "cached_report_probe",
            "node_id": getattr(node, "node_id", None),
            "route_status": {
                "validation_status": getattr(node, "validation_status", None),
                "fallback_used": bool(getattr(node, "fallback_used", False)),
                "route_design_failed": bool(evidence.get("route_design_failed")),
                "router_failed": bool(evidence.get("router_failed")),
                "physical_net_conflict": bool(evidence.get("physical_net_conflict")),
            },
            "warnings": list(getattr(node, "validation_warnings", []) or [])[:10],
            "errors": list(getattr(node, "validation_errors", []) or [])[:10],
            "note": "Cached report-probe backend; no Vivado process launched and no design mutation performed.",
        }, self)

    def report_utilization(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]:
        node_id = getattr(context.node, "node_id", None)
        ctx = context.analysis_contexts.get(str(node_id), {}) if node_id else {}
        util = ctx.get("utilization_summary") if isinstance(ctx.get("utilization_summary"), dict) else {}
        return _with_provenance({
            "mode": "cached_report_probe",
            "node_id": node_id,
            "utilization_summary": util,
            "note": "Cached report-probe backend; no Vivado process launched and no design mutation performed.",
        }, self)


@dataclass
class LocalVivadoReportBackend:
    """Optional fixed Tcl report backend.

    It writes a small read-only Tcl script with fixed report commands.  No LLM
    input is interpolated as Tcl except the checked DCP path and report file
    paths. The subprocess uses owned descendant cleanup and a timeout.
    """

    vivado_bin: str = "vivado"
    timeout_sec: int = 300
    name: str = "local_vivado"
    mutates_design: bool = False
    fresh_vivado_report: bool = True

    def report_timing_summary(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]:
        return self._run_fixed_report(
            context=context,
            report_name="timing_summary",
            commands=[
                "report_timing_summary -delay_type max",
                "report_timing -delay_type max -max_paths 5 -nworst 1",
            ],
        )

    def report_route_status(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]:
        return self._run_fixed_report(context=context, report_name="route_status", commands=["report_route_status"])

    def report_utilization(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]:
        return self._run_fixed_report(context=context, report_name="utilization", commands=["report_utilization"])

    def _run_fixed_report(self, *, context: Any, report_name: str, commands: list[str]) -> Dict[str, Any]:
        dcp = getattr(context.node, "output_dcp", None) or getattr(context.node, "input_dcp", None)
        if not dcp or not Path(dcp).exists():
            return _with_provenance({"mode": "vivado_report_unavailable", "available": False, "fresh_vivado_report": False, "error": "node DCP path missing"}, self)
        report_dir = (Path(context.run_dir) / "analysis" / "vivado_reports" / str(getattr(context.node, "node_id", "node"))).resolve()
        report_dir.mkdir(parents=True, exist_ok=True)
        # Repeated probes retain their diagnostics and cannot reuse an older
        # report if the new process exits without writing one.
        report_dir = Path(tempfile.mkdtemp(prefix=f"{report_name}-", dir=report_dir))
        dcp_path = Path(dcp).resolve()
        report_path = report_dir / f"{report_name}.rpt"
        script_path = report_dir / f"{report_name}.tcl"
        lines = [f"open_checkpoint {tcl_word(dcp_path)}"]
        for index, cmd in enumerate(commands):
            file_mode = "-file" if index == 0 else "-append -file"
            lines.append(f"{cmd} {file_mode} {tcl_word(report_path)}")
        lines.extend(["close_design", "exit"])
        script_path.write_text("\n".join(lines) + "\n")
        try:
            from optimizer_pipeline.backends.xilinx.owned_process import run_owned

            proc = run_owned([self.vivado_bin, "-mode", "batch", "-source", str(script_path)], cwd=str(report_dir), timeout=self.timeout_sec, text=True)
            log_path = report_dir / f"{report_name}.vivado.log"
            log_path.write_text((proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else ""))
            preview = report_path.read_text(errors="ignore")[:4000] if report_path.exists() else ""
            if proc.returncode != 0 or not preview:
                return _with_provenance({
                    "mode": "vivado_report_failed", "status": "INFRASTRUCTURE_FAILURE",
                    "available": False, "fresh_vivado_report": False,
                    "exit_code": proc.returncode,
                    "error": "Vivado report process failed" if proc.returncode != 0 else "report text empty",
                    "report_path": str(report_path) if report_path.exists() else None,
                    "log_path": str(log_path), "preview": preview,
                }, self)
            parsed = _parse_report_preview(report_name, preview)
            return _with_provenance({
                "mode": "vivado_report_probe",
                "available": True,
                "exit_code": proc.returncode,
                "report_path": str(report_path) if report_path.exists() else None,
                "log_path": str(log_path),
                "preview": preview,
                "parsed": parsed,
                **parsed,
            }, self)
        except Exception as exc:  # pragma: no cover - environment dependent
            return _with_provenance({"mode": "vivado_report_failed", "status": "INFRASTRUCTURE_FAILURE", "available": False, "fresh_vivado_report": False, "error": f"{type(exc).__name__}: {exc}"}, self)


@dataclass
class VivadoMCPReportBackend:
    """Reserved typed hook for a future MCP client implementation."""

    client: Any = None
    name: str = "vivado_mcp"
    mutates_design: bool = False
    fresh_vivado_report: bool = True

    def _unconfigured(self, report_name: str) -> Dict[str, Any]:
        return _with_provenance({"mode": "vivado_mcp_unconfigured", "available": False, "report_name": report_name, "error": "No MCP report client configured."}, self)

    def report_timing_summary(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]:
        if self.client is None:
            return self._unconfigured("timing_summary")
        return self.client.report_timing_summary(args=args, context=context)

    def report_route_status(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]:
        if self.client is None:
            return self._unconfigured("route_status")
        return self.client.report_route_status(args=args, context=context)

    def report_utilization(self, *, args: Mapping[str, Any], context: Any) -> Dict[str, Any]:
        if self.client is None:
            return self._unconfigured("utilization")
        return self.client.report_utilization(args=args, context=context)


def build_report_probe_backend(name: str | ReportProbeBackend | None = None) -> ReportProbeBackend:
    if name is not None and not isinstance(name, str):
        return name
    key = (
        name
        or os.environ.get("ANALYSIS_REPORT_PROBE_BACKEND")
        or os.environ.get("DEEP_ANALYSIS_REPORT_PROBE_BACKEND")
        or os.environ.get("LLM_ANALYSIS_REPORT_PROBE_BACKEND")
        or "cached"
    ).strip().lower()
    if key in {"cached", "analysis_context", "cached_analysis_context"}:
        return CachedAnalysisContextReportBackend()
    if key in {"local", "local_vivado", "vivado"}:
        vivado_bin = os.environ.get("VIVADO_BIN") or os.environ.get("VIVADO_EXEC") or "vivado"
        timeout_sec = int(os.environ.get("ANALYSIS_REPORT_PROBE_TIMEOUT_SEC") or os.environ.get("ANALYSIS_TOOL_TIMEOUT_SEC") or "300")
        return LocalVivadoReportBackend(vivado_bin=vivado_bin, timeout_sec=timeout_sec)
    if key in {"mcp", "vivado_mcp"}:
        return VivadoMCPReportBackend()
    return CachedAnalysisContextReportBackend()


def _with_provenance(payload: Dict[str, Any], backend: ReportProbeBackend) -> Dict[str, Any]:
    payload.setdefault("probe_backend", getattr(backend, "name", "unknown"))
    payload.setdefault("fresh_vivado_report", bool(getattr(backend, "fresh_vivado_report", False)))
    payload.setdefault("mutates_design", bool(getattr(backend, "mutates_design", False)))
    payload.setdefault("evidence_provenance", {
        "probe_backend": payload.get("probe_backend"),
        "fresh_vivado_report": payload.get("fresh_vivado_report"),
        "mutates_design": payload.get("mutates_design"),
    })
    return payload


def _parse_report_preview(report_name: str, preview: str) -> Dict[str, Any]:
    text = preview or ""
    if not text:
        return {"diagnostic_missing_reasons": {report_name: "report preview empty"}}
    key = str(report_name or "").lower()
    if "timing" in key:
        return _parse_timing_report(text)
    if "route" in key:
        return _parse_route_status_report(text)
    if "utilization" in key:
        return _parse_utilization_report(text)
    return {}


def _parse_timing_report(text: str) -> Dict[str, Any]:
    wns = _first_float(r"\bWNS\(ns\)\s+([-+]?\d+(?:\.\d+)?)", text)
    tns = _first_float(r"\bTNS\(ns\)\s+([-+]?\d+(?:\.\d+)?)", text)
    failing = _first_int(r"\bFailing Endpoints\s+(\d+)", text)
    data_delay = _first_float(r"\bData Path Delay:\s*([-+]?\d+(?:\.\d+)?)", text)
    logic_delay = _first_float(r"\blogic\s+([-+]?\d+(?:\.\d+)?)", text)
    route_delay = _first_float(r"\broute\s+([-+]?\d+(?:\.\d+)?)", text)
    levels = _first_int(r"\bLogic Levels:\s*(\d+)", text)
    route_ratio = None
    if route_delay is not None and logic_delay is not None and route_delay + logic_delay > 0:
        route_ratio = round(route_delay / (route_delay + logic_delay), 6)
    dominant = None
    if route_ratio is not None:
        dominant = "route" if route_ratio >= 0.55 else "logic" if route_ratio <= 0.45 else "mixed"
    return {
        "timing": {
            "wns_ns": wns,
            "tns_ns": tns,
            "failing_endpoints": failing,
        },
        "critical_path_features": {
            "data_path_delay_ns": data_delay,
            "logic_delay_ns": logic_delay,
            "route_delay_ns": route_delay,
            "route_delay_ratio": route_ratio,
            "dominant_path_type": dominant,
            "max_logic_levels": levels,
        },
        "route_vs_logic_delay_hint": dominant,
        "diagnostic_missing_reasons": _missing_report_reasons({
            "critical_path_features": any(v is not None for v in (data_delay, logic_delay, route_delay, route_ratio, levels)),
            "timing": any(v is not None for v in (wns, tns, failing)),
        }),
    }


def _parse_route_status_report(text: str) -> Dict[str, Any]:
    lower = text.lower()
    fully_routed = "fully routed" in lower or "design is routed" in lower
    failed = "failed" in lower or "unrouted" in lower or "routing failed" in lower
    congestion_terms = sum(lower.count(term) for term in ("congestion", "overlap", "unrouted", "conflict"))
    return {
        "route_status": {
            "is_fully_routed": fully_routed if fully_routed or failed else None,
            "route_design_failed": failed,
        },
        "congestion": {
            "text_mentions": congestion_terms,
            "hint": "present" if congestion_terms else "not_observed",
        },
        "diagnostic_missing_reasons": _missing_report_reasons({
            "congestion": bool(congestion_terms),
            "route_status": fully_routed or failed,
        }),
    }


def _parse_utilization_report(text: str) -> Dict[str, Any]:
    rows: Dict[str, Any] = {}
    for resource in ("CLB LUTs", "CLB Registers", "Block RAM Tile", "DSPs", "URAM", "BUFG"):
        pattern = rf"{re.escape(resource)}\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|\s*([-+]?\d+(?:\.\d+)?)"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            rows[resource] = {
                "used": _int_from_text(match.group(1)),
                "fixed": _int_from_text(match.group(2)),
                "available": _int_from_text(match.group(3)),
                "utilization_pct": float(match.group(4)),
            }
    high_fanout = _first_int(r"High Fanout.*?(\d+)", text)
    fanout = {"high_fanout_count": high_fanout} if high_fanout is not None else {}
    hardblock_pressure = {
        key: value for key, value in rows.items()
        if any(token in key.lower() for token in ("ram", "dsp", "uram", "bufg"))
    }
    return {
        "utilization_summary": rows,
        "fanout": fanout,
        "hardblock_resource_pressure": hardblock_pressure,
        "diagnostic_missing_reasons": _missing_report_reasons({
            "utilization_summary": bool(rows),
            "fanout": bool(fanout),
            "hardblock_resource_pressure": bool(hardblock_pressure),
        }),
    }


def _missing_report_reasons(present: Dict[str, bool]) -> Dict[str, str]:
    return {key: "not found in report preview" for key, ok in present.items() if not ok}


def _first_float(pattern: str, text: str) -> Optional[float]:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _first_int(pattern: str, text: str) -> Optional[int]:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _int_from_text(match.group(1))


def _int_from_text(value: str) -> Optional[int]:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
