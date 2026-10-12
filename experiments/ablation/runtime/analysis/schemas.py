"""Typed schemas for online budgeted deep-analysis sessions.

 keeps the analysis layer separate from QoR scoring.  Analysis
records are persisted as evidence for reports and future world-model training;
they do not change measured QoR outcomes.  The schemas also define interfaces
for future LLM self-stop / dynamic tool-loop policies without committing to a
specific strategy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from optimizer_pipeline.models import to_jsonable


@dataclass
class AnalysisAgentConfig:
    enabled: bool = False
    analysis_level: str = "report_probe"  # none | read_only | report_probe | act_probe
    root_budget_sec: float = 600.0
    node_budget_sec: float = 120.0
    max_nodes_per_search: int = 5
    allow_report_probes: bool = True
    allow_act_probes: bool = False
    max_tool_calls: int = 10
    max_llm_calls: int = 6
    max_report_probe_calls: int = 6
    max_report_probe_time_sec: float = 540.0
    no_improvement_window: int = 5
    priority_delta_scale: float = 0.08
    soft_stop_frac: float = 0.55
    strong_stop_frac: float = 0.80
    report_probe_backend: str = "cached"  # cached | local_vivado | vivado_mcp
    tool_loop_mode: str = "static_plan"  # static_plan | dynamic_llm_reserved
    tool_timeout_sec: float = 300.0

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class AnalysisBudget:
    max_wall_time_sec: float = 600.0
    max_tool_calls: int = 10
    max_llm_calls: int = 6
    max_report_probe_calls: int = 6
    max_report_probe_time_sec: float = 540.0
    allow_report_probes: bool = True
    allow_act_probes: bool = False
    default_tool_timeout_sec: float = 300.0

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class AnalysisTriggerDecision:
    should_analyze: bool
    node_id: Optional[str]
    trigger_reason: str
    priority: float = 0.0
    budget_profile: str = "node"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class AnalysisToolCall:
    tool_name: str
    args: Dict[str, Any]
    started_at: str
    elapsed_sec: float
    success: bool
    output: Optional[Dict[str, Any]] = None
    output_ref: Optional[str] = None
    error: Optional[str] = None
    tool_class: str = "inspect"  # inspect | report_probe | act_probe
    timed_out: bool = False
    timeout_sec: Optional[float] = None
    mutates_design: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class AnalysisStopDecision:
    continue_analysis: bool
    stop_reason: str
    confidence: float = 0.0
    expected_next_information_gain: str = "low"
    next_tool_name: Optional[str] = None
    next_tool_args: Optional[Dict[str, Any]] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class AnalysisLoopDecision:
    """LLM/self-stop dynamic-tool-loop decision schema."""

    continue_analysis: bool
    next_tool_name: Optional[str] = None
    next_tool_args: Dict[str, Any] = field(default_factory=dict)
    expected_information_gain: str = "low"
    decision_that_may_change: str = "candidate_ranking"
    confidence: float = 0.0
    stop_reason: str = ""
    rationale: str = ""
    action: str = "stop"  # call_tool | stop
    candidate_priors: List[Dict[str, Any]] = field(default_factory=list)
    candidate_priorities: List[Dict[str, Any]] = field(default_factory=list)
    fast_skip_suggestions: List[Dict[str, Any]] = field(default_factory=list)
    empirical_observations: List[str] = field(default_factory=list)
    heuristic_priors: List[str] = field(default_factory=list)
    retrieved_motifs: List[str] = field(default_factory=list)
    bottleneck_hypotheses: List[str] = field(default_factory=list)
    remaining_uncertainty: List[str] = field(default_factory=list)
    payload_chars: Optional[int] = None
    estimated_prompt_tokens: Optional[int] = None
    estimated_total_tokens: Optional[int] = None
    analysis_context_mode: str = ""
    analysis_token_budget_soft: Optional[int] = None
    analysis_token_budget_hard: Optional[int] = None
    fresh_probe_contract: Dict[str, Any] = field(default_factory=dict)
    fresh_probe_override: bool = False
    fresh_probe_incomplete: bool = False
    raw_payload: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisLoopDecision":
        if not isinstance(data, dict):
            data = {}
        action = str(data.get("action") or ("call_tool" if data.get("continue_analysis") else "stop")).strip()
        continue_analysis = bool(data.get("continue_analysis", action == "call_tool"))
        priors = data.get("candidate_priors") if isinstance(data.get("candidate_priors"), list) else []
        priorities = data.get("candidate_priorities") if isinstance(data.get("candidate_priorities"), list) else []
        skips = data.get("fast_skip_suggestions") if isinstance(data.get("fast_skip_suggestions"), list) else []
        uncertainty = data.get("remaining_uncertainty") if isinstance(data.get("remaining_uncertainty"), list) else []
        observations = data.get("empirical_observations") if isinstance(data.get("empirical_observations"), list) else []
        heuristic_priors = data.get("heuristic_priors") if isinstance(data.get("heuristic_priors"), list) else []
        retrieved_motifs = data.get("retrieved_motifs") if isinstance(data.get("retrieved_motifs"), list) else []
        bottleneck_hypotheses = data.get("bottleneck_hypotheses") if isinstance(data.get("bottleneck_hypotheses"), list) else []
        fresh_probe_contract = data.get("fresh_probe_contract") if isinstance(data.get("fresh_probe_contract"), dict) else {}
        return cls(
            action=action,
            continue_analysis=continue_analysis,
            next_tool_name=data.get("next_tool_name") or data.get("tool_name"),
            next_tool_args=data.get("next_tool_args") if isinstance(data.get("next_tool_args"), dict) else data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {},
            expected_information_gain=str(data.get("expected_information_gain") or "low"),
            decision_that_may_change=str(data.get("decision_that_may_change") or "candidate_ranking"),
            confidence=_safe_float(data.get("confidence"), 0.0),
            stop_reason=str(data.get("stop_reason") or data.get("reason") or ""),
            rationale=str(data.get("rationale") or data.get("why_needed") or data.get("reason") or ""),
            candidate_priors=[x for x in priors if isinstance(x, dict)],
            candidate_priorities=[x for x in priorities if isinstance(x, dict)],
            fast_skip_suggestions=[x for x in skips if isinstance(x, dict)],
            empirical_observations=[str(x) for x in observations],
            heuristic_priors=[str(x) for x in heuristic_priors],
            retrieved_motifs=[str(x) for x in retrieved_motifs],
            bottleneck_hypotheses=[str(x) for x in bottleneck_hypotheses],
            remaining_uncertainty=[str(x) for x in uncertainty],
            fresh_probe_contract=dict(fresh_probe_contract),
            fresh_probe_override=bool(data.get("fresh_probe_override")),
            fresh_probe_incomplete=bool(data.get("fresh_probe_incomplete")),
            raw_payload=data,
        )

    def to_stop_decision(self) -> AnalysisStopDecision:
        return AnalysisStopDecision(
            continue_analysis=self.continue_analysis,
            stop_reason=self.stop_reason or ("continue_dynamic_tool_loop" if self.continue_analysis else "dynamic_tool_loop_stop"),
            confidence=self.confidence,
            expected_next_information_gain=self.expected_information_gain,
            next_tool_name=self.next_tool_name,
            next_tool_args=self.next_tool_args,
            reason=self.rationale,
        )

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class AnalysisRunRecord:
    """Search-graph-style analysis run record.

    Unlike implementation nodes, this consumes a target state node and emits
    structured evidence/priors rather than a new DCP.
    """

    analysis_run_id: str
    target_node_id: str
    analysis_type: str = "root"
    status: str = "PENDING"
    session_id: Optional[str] = None
    tool_loop_mode: str = "static_plan"
    loop_decisions: List[AnalysisLoopDecision] = field(default_factory=list)
    fast_skip_suggestions: List[Dict[str, Any]] = field(default_factory=list)
    budget_profile: str = "root"
    stop_reason: Optional[str] = None
    candidate_prior_count: int = 0
    tool_call_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class CandidatePrior:
    node_id: str
    flow: str
    priority_delta: float = 0.0
    risk_delta: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    source_analysis_session_id: str = ""

    @property
    def target_node_id(self) -> str:
        return self.node_id

    @property
    def candidate_flow(self) -> str:
        return self.flow

    def to_dict(self) -> Dict[str, Any]:
        data = to_jsonable(self)
        # Include aliases consumed by analysis-prior scorer and trace-lake tools.
        data["target_node_id"] = self.node_id
        data["candidate_flow"] = self.flow
        data["analysis_session_id"] = self.source_analysis_session_id
        return data


@dataclass
class CandidatePriority:
    """Absolute analysis-layer priority for a legal optimizer transition.

    These rows are globally comparable ``(node_id, flow)`` priorities.  The
    analysis layer owns diagnosis and ranking rationale; the expansion policy
    only schedules legal transitions from these scores.
    """

    node_id: str
    flow: str
    priority_score: float = 0.5
    risk_score: float = 0.0
    confidence: float = 0.0
    expected_effect: str = ""
    evidence_refs: List[Dict[str, Any]] = field(default_factory=list)
    evidence_basis: str = "heuristic_prior"  # current_observation | trajectory_observation | retrieved_motif | heuristic_prior
    expected_gain_class: str = "unknown"  # low | medium | high | unknown
    novelty_score: Optional[float] = None
    contraindications: List[str] = field(default_factory=list)
    why_now: str = ""
    do_not_repeat_if: str = ""
    reason: str = ""
    remaining_uncertainty: List[str] = field(default_factory=list)
    source_analysis_session_id: str = ""

    @property
    def target_node_id(self) -> str:
        return self.node_id

    @property
    def candidate_flow(self) -> str:
        return self.flow

    def to_dict(self) -> Dict[str, Any]:
        data = to_jsonable(self)
        data["target_node_id"] = self.node_id
        data["candidate_flow"] = self.flow
        data["analysis_session_id"] = self.source_analysis_session_id
        return data

    @classmethod
    def from_prior(cls, prior: CandidatePrior) -> "CandidatePriority":
        priority = max(0.0, min(1.0, 0.5 + float(prior.priority_delta or 0.0) - max(0.0, float(prior.risk_delta or 0.0)) * 0.05))
        return cls(
            node_id=prior.node_id,
            flow=prior.flow,
            priority_score=round(priority, 6),
            risk_score=round(max(0.0, float(prior.risk_delta or 0.0)), 6),
            confidence=round(max(0.0, min(1.0, float(prior.confidence or 0.0))), 6),
            expected_effect=prior.reason,
            evidence_basis="heuristic_prior",
            expected_gain_class="unknown",
            reason=prior.reason,
            source_analysis_session_id=prior.source_analysis_session_id,
        )


@dataclass
class NodeAnalysisPlan:
    node_id: str
    analysis_type: str
    bottleneck_class: str = "unknown"
    bottleneck_confidence: float = 0.0
    evidence_summary: List[Dict[str, Any]] = field(default_factory=list)
    risks: List[Dict[str, Any]] = field(default_factory=list)
    candidate_priorities: List[CandidatePriority] = field(default_factory=list)
    recommended_evidence_to_collect_next: List[str] = field(default_factory=list)
    remaining_uncertainty: List[str] = field(default_factory=list)
    source_analysis_session_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = to_jsonable(self)
        data["candidate_priorities"] = [
            p.to_dict() if hasattr(p, "to_dict") else p
            for p in self.candidate_priorities
        ]
        return data


@dataclass
class AnalysisFeedbackRecord:
    """Join online-analysis priors with planner ranking and eventual outcomes."""

    feedback_id: str
    analysis_session_id: str
    node_id: str
    candidate_flow: str
    run_id: Optional[str] = None
    event_index: Optional[int] = None
    candidate_id: Optional[str] = None
    was_emitted: bool = False
    was_selected: bool = False
    was_executed: bool = False
    schedule_stage: str = "unselected_prior"
    candidate_rank_after_analysis: Optional[int] = None
    candidate_rank_before_analysis: Optional[int] = None
    selected_priority: Optional[float] = None
    adjusted_priority_with_analysis: Optional[float] = None
    approximate_base_priority_without_analysis: Optional[float] = None
    priority_delta: float = 0.0
    risk_delta: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    outcome_node_id: Optional[str] = None
    validation_status: Optional[str] = None
    qor_status: Optional[str] = None
    pure_qor_score: Optional[float] = None
    parent_pure_qor_score: Optional[float] = None
    delta_pure_qor_from_parent: Optional[float] = None
    recommendation_validated: Optional[bool] = None
    feedback_kind: str = "unselected_prior"  # selected_prior | unselected_prior | emitted_not_executed

    def to_dict(self) -> Dict[str, Any]:
        data = to_jsonable(self)
        # Short audit names used by review scripts and trace exports.
        data["rank_before_analysis"] = self.candidate_rank_before_analysis
        data["rank_after_analysis"] = self.candidate_rank_after_analysis
        return data


@dataclass
class NodeDiagnosis:
    node_id: str
    analysis_type: str
    bottleneck_class: str = "unknown"
    bottleneck_confidence: float = 0.0
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    risks: List[Dict[str, Any]] = field(default_factory=list)
    candidate_priors: List[CandidatePrior] = field(default_factory=list)
    candidate_priorities: List[CandidatePriority] = field(default_factory=list)
    recommended_action: str = "rank_frontier"
    remaining_uncertainty: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = to_jsonable(self)
        data["candidate_priors"] = [p.to_dict() if hasattr(p, "to_dict") else p for p in self.candidate_priors]
        data["candidate_priorities"] = [p.to_dict() if hasattr(p, "to_dict") else p for p in self.candidate_priorities]
        return data


@dataclass
class AnalysisSessionResult:
    session_id: str
    node_id: str
    trigger_reason: str
    budget: AnalysisBudget
    elapsed_sec: float
    stop_decision: AnalysisStopDecision
    tool_calls: List[AnalysisToolCall] = field(default_factory=list)
    diagnosis: Optional[NodeDiagnosis] = None
    candidate_priors: List[CandidatePrior] = field(default_factory=list)
    candidate_priorities: List[CandidatePriority] = field(default_factory=list)
    analysis_plan: Optional[NodeAnalysisPlan] = None
    fast_skip_suggestions: List[Dict[str, Any]] = field(default_factory=list)
    report_path: Optional[str] = None
    llm_calls: int = 0
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    analysis_model: Optional[str] = None
    analysis_provider: Optional[str] = None
    started_at: str = ""
    tool_loop_mode: str = "static_plan"
    loop_decisions: List[AnalysisLoopDecision] = field(default_factory=list)
    analysis_run: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "node_id": self.node_id,
            "trigger_reason": self.trigger_reason,
            "budget": self.budget.to_dict(),
            "started_at": self.started_at,
            "elapsed_sec": self.elapsed_sec,
            "stop_decision": self.stop_decision.to_dict(),
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
            "candidate_priors": [p.to_dict() for p in self.candidate_priors],
            "candidate_priorities": [p.to_dict() for p in self.candidate_priorities],
            "analysis_plan": self.analysis_plan.to_dict() if self.analysis_plan else None,
            "fast_skip_suggestions": self.fast_skip_suggestions,
            "report_path": self.report_path,
            "llm_calls": self.llm_calls,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "analysis_model": self.analysis_model,
            "analysis_provider": self.analysis_provider,
            "tool_loop_mode": self.tool_loop_mode,
            "loop_decisions": [d.to_dict() if hasattr(d, "to_dict") else d for d in self.loop_decisions],
            "analysis_run": self.analysis_run,
        }


@dataclass
class AnalysisUpdate:
    sessions: List[AnalysisSessionResult] = field(default_factory=list)
    candidate_priors: List[CandidatePrior] = field(default_factory=list)
    candidate_priorities: List[CandidatePriority] = field(default_factory=list)
    analysis_plans: List[NodeAnalysisPlan] = field(default_factory=list)
    node_diagnoses: List[NodeDiagnosis] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
