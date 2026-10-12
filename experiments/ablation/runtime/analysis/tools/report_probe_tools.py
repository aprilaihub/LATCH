"""Typed read-only report-probe tool wrappers."""

from __future__ import annotations

from typing import Any, Dict

from ..report_probe_backends import ReportProbeBackend, build_report_probe_backend
from .base import ToolContext


class _ReportProbeToolBase:
    name = "base_report_probe"
    tool_class = "report_probe"
    mutates_design = False
    default_timeout_sec = 300.0

    def _backend(self, context: ToolContext) -> ReportProbeBackend:
        return context.report_probe_backend or build_report_probe_backend(None)


class ReportTimingSummaryTool(_ReportProbeToolBase):
    name = "vivado_report_timing_summary"

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        return self._backend(context).report_timing_summary(args=args, context=context)


class ReportRouteStatusTool(_ReportProbeToolBase):
    name = "vivado_report_route_status"

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        return self._backend(context).report_route_status(args=args, context=context)


class ReportUtilizationTool(_ReportProbeToolBase):
    name = "vivado_report_utilization"

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        return self._backend(context).report_utilization(args=args, context=context)
