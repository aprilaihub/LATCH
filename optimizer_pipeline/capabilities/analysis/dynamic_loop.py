"""Provider-neutral closed-loop policies for managed analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from .schemas import AnalysisLoopDecision, AnalysisToolCall


@dataclass
class AnalysisLoopState:
    session_id: str
    node_id: str
    trigger_reason: str
    elapsed_sec: float
    remaining_budget_sec: float
    allowed_flows: List[str] = field(default_factory=list)
    tool_calls: List[AnalysisToolCall] = field(default_factory=list)
    candidate_priors: List[Dict[str, Any]] = field(default_factory=list)
    candidate_priorities: List[Dict[str, Any]] = field(default_factory=list)
    evidence_summary: Dict[str, Any] = field(default_factory=dict)
    optimization_state_context: Dict[str, Any] = field(default_factory=dict)
    required_tools: List[str] = field(default_factory=list)
    fast_skip_suggestions: List[Dict[str, Any]] = field(default_factory=list)
    max_tool_calls: int = 0
    fresh_probe_contract: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "node_id": self.node_id,
            "trigger_reason": self.trigger_reason,
            "elapsed_sec": self.elapsed_sec,
            "remaining_budget_sec": self.remaining_budget_sec,
            "allowed_flows": list(self.allowed_flows),
            "called_tools": [call.tool_name for call in self.tool_calls],
            "candidate_priors": list(self.candidate_priors[:8]),
            "candidate_priorities": list(self.candidate_priorities[:8]),
            "evidence_summary": self.evidence_summary,
            "optimization_state_context": self.optimization_state_context,
            "required_tools": list(self.required_tools),
            "fast_skip_suggestions": list(self.fast_skip_suggestions),
            "max_tool_calls": self.max_tool_calls,
            "fresh_probe_contract": self.fresh_probe_contract,
        }


class AnalysisDecisionPolicy(Protocol):
    name: str

    def decide_next_tool(
        self, *, state: AnalysisLoopState, available_tool_names: List[str]
    ) -> AnalysisLoopDecision:
        ...


class StaticPlanDecisionPolicy:
    name = "static_plan"

    def decide_next_tool(self, *, state: AnalysisLoopState, available_tool_names: List[str]) -> AnalysisLoopDecision:
        return AnalysisLoopDecision(
            False, stop_reason="static_plan_controller_owns_tool_sequence",
            confidence=1.0, action="stop",
        )


class ReservedLLMSelfStopPolicy:
    name = "dynamic_llm_reserved"

    def decide_next_tool(self, *, state: AnalysisLoopState, available_tool_names: List[str]) -> AnalysisLoopDecision:
        return AnalysisLoopDecision(
            False, stop_reason="external_llm_analysis_policy_not_supplied",
            confidence=1.0,
            rationale="Provider-backed analysis policies are owned by experiments/llm.",
        )


@dataclass
class MockLLMSelfStopPolicy:
    """Deterministic local policy implementing the provider-neutral contract."""

    name: str = "mock_llm_closed_loop"
    required_tools: List[str] = field(default_factory=lambda: [
        "load_node_summary",
        "load_trajectory_outcomes",
        "query_retrieval_memory",
        "vivado_report_timing_summary",
        "vivado_report_route_status",
        "vivado_report_utilization",
    ])
    optional_tools: List[str] = field(default_factory=lambda: [
        "load_frontier_outcome_memory", "inspect_validation_events",
        "analyze_trajectory_motifs",
    ])
    min_required_tools_before_stop: int = 4

    def decide_next_tool(self, *, state: AnalysisLoopState, available_tool_names: List[str]) -> AnalysisLoopDecision:
        available = set(available_tool_names)
        called = {call.tool_name for call in state.tool_calls}
        required = state.required_tools or self.required_tools
        enough = len(called.intersection(required)) >= min(len(required), self.min_required_tools_before_stop)
        for tool in required:
            if enough:
                break
            if tool in available and tool not in called:
                return AnalysisLoopDecision(
                    True, action="call_tool", next_tool_name=tool,
                    next_tool_args={"node_id": state.node_id, "flows": state.allowed_flows, "k": 5},
                    expected_information_gain="high" if tool.startswith("vivado_report") else "medium",
                    decision_that_may_change="candidate_ranking_or_fast_skip",
                    confidence=0.70, rationale=f"Need `{tool}` evidence before stopping.",
                    raw_payload={"action": "call_tool", "policy": self.name},
                )
        if len(called) < self.min_required_tools_before_stop:
            for tool in self.optional_tools:
                if tool in available and tool not in called:
                    return AnalysisLoopDecision(
                        True, action="call_tool", next_tool_name=tool,
                        next_tool_args={"node_id": state.node_id, "flows": state.allowed_flows, "k": 5},
                        confidence=0.60, rationale=f"Collect optional `{tool}` evidence.",
                    )
        priors = [
            {
                "flow": flow,
                "priority_delta": round(max(-0.25, 0.08 - index * 0.01), 6),
                "risk_delta": 0.0,
                "confidence": 0.55,
                "reason": "Deterministic local analysis prior.",
            }
            for index, flow in enumerate(state.allowed_flows[:5])
        ]
        return AnalysisLoopDecision(
            False, action="stop", stop_reason="sufficient_evidence_for_candidate_priors",
            confidence=0.78 if priors else 0.55,
            rationale="Required evidence collected.", candidate_priors=priors,
            raw_payload={"action": "stop", "policy": self.name},
        )


def build_analysis_decision_policy(mode: str, **_: Any) -> AnalysisDecisionPolicy:
    key = (mode or "static_plan").strip().lower()
    if key in {"static", "static_plan"}:
        return StaticPlanDecisionPolicy()
    if key in {
        "mock_llm", "mock_llm_closed_loop", "scripted_llm_closed_loop",
        "heuristic_llm", "heuristic_closed_loop", "dynamic_mock",
    }:
        return MockLLMSelfStopPolicy()
    return ReservedLLMSelfStopPolicy()
