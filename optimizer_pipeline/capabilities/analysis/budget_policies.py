"""Budget policies for online deep analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .schemas import AnalysisBudget, AnalysisTriggerDecision


class AnalysisBudgetPolicy(Protocol):
    def budget_for(self, trigger: AnalysisTriggerDecision) -> AnalysisBudget:
        ...


@dataclass
class StaticAnalysisBudgetPolicy:
    root_budget_sec: float = 600.0
    node_budget_sec: float = 120.0
    allow_report_probes: bool = True
    allow_act_probes: bool = False
    root_max_tool_calls: int = 10
    node_max_tool_calls: int = 6
    max_act_probe_calls: int = 0
    max_act_probe_time_sec: float = 0.0

    def budget_for(self, trigger: AnalysisTriggerDecision) -> AnalysisBudget:
        if trigger.budget_profile == "root":
            return AnalysisBudget(
                max_wall_time_sec=self.root_budget_sec,
                max_tool_calls=self.root_max_tool_calls,
                max_llm_calls=6,
                max_report_probe_calls=6,
                max_report_probe_time_sec=max(1.0, self.root_budget_sec * 0.90),
                max_act_probe_calls=max(0, int(self.max_act_probe_calls)),
                max_act_probe_time_sec=max(0.0, float(self.max_act_probe_time_sec)),
                allow_report_probes=self.allow_report_probes,
                allow_act_probes=self.allow_act_probes,
            )
        return AnalysisBudget(
            max_wall_time_sec=self.node_budget_sec,
            max_tool_calls=self.node_max_tool_calls,
            max_llm_calls=4,
            max_report_probe_calls=3,
            max_report_probe_time_sec=max(1.0, self.node_budget_sec * 0.75),
            max_act_probe_calls=max(0, int(self.max_act_probe_calls)),
            max_act_probe_time_sec=max(0.0, float(self.max_act_probe_time_sec)),
            allow_report_probes=self.allow_report_probes,
            allow_act_probes=self.allow_act_probes,
        )
