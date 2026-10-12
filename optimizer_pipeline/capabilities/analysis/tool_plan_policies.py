"""Configurable tool-plan policies for online deep analysis.

 keeps tool sequencing out of the controller. The controller only asks
this strategy for tool names, so FPGA-team-specific tool plans can be added
without touching the runner or analysis orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Protocol


class AnalysisToolPlanPolicy(Protocol):
    name: str

    def plan_tools(self, *, node: Any, trigger_reason: str, budget: Any, available_tool_names: Iterable[str]) -> List[str]:
        ...


@dataclass
class StaticAnalysisToolPlanPolicy:
    name: str = "static_root_timing_route_v1"
    root_tools: List[str] = field(default_factory=lambda: [
        "load_node_summary",
        "load_analysis_context",
        "load_trajectory_outcomes",
        "load_frontier_outcome_memory",
        "query_retrieval_memory",
        "vivado_report_timing_summary",
        "vivado_report_route_status",
        "vivado_report_utilization",
        "inspect_validation_events",
        "analyze_trajectory_motifs",
    ])
    keynode_tools: List[str] = field(default_factory=lambda: [
        "load_node_summary",
        "load_analysis_context",
        "load_trajectory_outcomes",
        "load_frontier_outcome_memory",
        "query_retrieval_memory",
        "inspect_validation_events",
        "vivado_report_timing_summary",
        "analyze_trajectory_motifs",
    ])
    failure_tools: List[str] = field(default_factory=lambda: [
        "load_node_summary",
        "load_trajectory_outcomes",
        "load_frontier_outcome_memory",
        "inspect_validation_events",
        "vivado_report_route_status",
        "query_retrieval_memory",
        "analyze_trajectory_motifs",
    ])

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "StaticAnalysisToolPlanPolicy":
        if not isinstance(data, Mapping):
            return cls()
        default = cls()
        return cls(
            name=str(data.get("name") or data.get("type") or default.name),
            root_tools=_list_or_default(data.get("root_tools"), default.root_tools),
            keynode_tools=_list_or_default(data.get("keynode_tools"), default.keynode_tools),
            failure_tools=_list_or_default(data.get("failure_tools"), default.failure_tools),
        )

    def plan_tools(self, *, node: Any, trigger_reason: str, budget: Any, available_tool_names: Iterable[str]) -> List[str]:
        available = set(str(name) for name in available_tool_names)
        rescue_tools = [
            "load_rescue_attempts",
            "classify_attempt_failure",
            "load_best_valid_branches",
            "load_primitive_effect_history",
            "load_known_public_recipe_prior",
            "compare_parent_child_qor",
        ]
        rescue_act_tools = [
            "probe_place_directive",
            "probe_phys_opt_directive",
            "probe_route_directive",
        ]
        status = str(getattr(node, "validation_status", "") or "")
        if status in {"INVALID", "WARNING", "FALLBACK_USED", "REGRESSED"} or "validation" in str(trigger_reason):
            preferred = self.failure_tools
        elif str(trigger_reason).startswith("root") or getattr(node, "depth", None) == 0:
            preferred = self.root_tools
        else:
            preferred = self.keynode_tools
        if str(trigger_reason).startswith("rescue_"):
            preferred = rescue_tools + preferred
            if bool(getattr(budget, "allow_act_probes", False)):
                preferred = preferred + rescue_act_tools
        return [name for name in _dedupe(preferred) if name in available]

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "root_tools": list(self.root_tools), "keynode_tools": list(self.keynode_tools), "failure_tools": list(self.failure_tools)}


@dataclass
class MinimalReadOnlyToolPlanPolicy(StaticAnalysisToolPlanPolicy):
    name: str = "minimal_read_only_v1"
    root_tools: List[str] = field(default_factory=lambda: ["load_node_summary", "load_analysis_context", "load_trajectory_outcomes", "query_retrieval_memory"])
    keynode_tools: List[str] = field(default_factory=lambda: ["load_node_summary", "load_trajectory_outcomes", "inspect_validation_events", "query_retrieval_memory"])
    failure_tools: List[str] = field(default_factory=lambda: ["load_node_summary", "load_trajectory_outcomes", "inspect_validation_events", "query_retrieval_memory"])


def build_analysis_tool_plan_policy(
    name: str | None,
    *,
    root_tools: List[str] | None = None,
    keynode_tools: List[str] | None = None,
    failure_tools: List[str] | None = None,
) -> StaticAnalysisToolPlanPolicy:
    """Build the configured analysis tool-plan policy."""

    static_default = StaticAnalysisToolPlanPolicy()
    key = str(name or static_default.name).strip() or static_default.name
    policy_cls = MinimalReadOnlyToolPlanPolicy if key == "minimal_read_only_v1" else StaticAnalysisToolPlanPolicy
    default = policy_cls()
    return policy_cls(
        name=key,
        root_tools=list(root_tools if root_tools is not None else default.root_tools),
        keynode_tools=list(keynode_tools if keynode_tools is not None else default.keynode_tools),
        failure_tools=list(failure_tools if failure_tools is not None else default.failure_tools),
    )


def _list_or_default(value: Any, default: List[str]) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return list(default)


def _dedupe(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
