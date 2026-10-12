"""Tool registry for online deep analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .tools.artifact_tools import (
    AnalyzeMotifsTool,
    InspectValidationEventsTool,
    LoadAnalysisContextTool,
    LoadFrontierOutcomeMemoryTool,
    LoadNodeSummaryTool,
    LoadTrajectoryOutcomesTool,
)
from .tools.act_probe_tools import ProbePhysOptDirectiveTool, ProbePlaceDirectiveTool, ProbeRouteDirectiveTool
from .tools.retrieval_tools import QueryRetrievalMemoryTool
from .tools.report_probe_tools import ReportRouteStatusTool, ReportTimingSummaryTool, ReportUtilizationTool
from .tools.rescue_tools import (
    ClassifyAttemptFailureTool,
    CompareParentChildQorTool,
    LoadBestValidBranchesTool,
    LoadKnownPublicRecipePriorTool,
    LoadPrimitiveEffectHistoryTool,
    LoadRescueAttemptsTool,
)


@dataclass
class AnalysisToolRegistry:
    tools: Dict[str, Any] = field(default_factory=dict)

    def register(self, tool: Any) -> None:
        self.tools[str(tool.name)] = tool

    def get(self, name: str) -> Optional[Any]:
        return self.tools.get(str(name))

    def names(self) -> Iterable[str]:
        return tuple(sorted(self.tools))

    def allowed_tools(self, policy: Any) -> List[Any]:
        out: List[Any] = []
        for tool in self.tools.values():
            cls = getattr(tool, "tool_class", "inspect")
            if cls == "report_probe" and not getattr(policy, "allow_report_probes", False):
                continue
            if cls == "act_probe" and not getattr(policy, "allow_act_probes", False):
                continue
            if getattr(tool, "mutates_design", False) and not getattr(policy, "allow_act_probes", False):
                continue
            out.append(tool)
        return out


def default_tool_registry() -> AnalysisToolRegistry:
    registry = AnalysisToolRegistry()
    for tool in [
        LoadNodeSummaryTool(),
        LoadAnalysisContextTool(),
        LoadTrajectoryOutcomesTool(),
        LoadFrontierOutcomeMemoryTool(),
        InspectValidationEventsTool(),
        AnalyzeMotifsTool(),
        QueryRetrievalMemoryTool(),
        ReportTimingSummaryTool(),
        ReportRouteStatusTool(),
        ReportUtilizationTool(),
        LoadRescueAttemptsTool(),
        CompareParentChildQorTool(),
        ClassifyAttemptFailureTool(),
        LoadBestValidBranchesTool(),
        LoadPrimitiveEffectHistoryTool(),
        LoadKnownPublicRecipePriorTool(),
        ProbePlaceDirectiveTool(),
        ProbePhysOptDirectiveTool(),
        ProbeRouteDirectiveTool(),
    ]:
        registry.register(tool)
    return registry
