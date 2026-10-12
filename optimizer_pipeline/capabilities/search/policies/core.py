"""Deterministic policies for research frontier expansion.

The analysis-priority scheduler consumes structured analysis output and only
ranks allowlisted deterministic flows. It cannot generate scripts or execute EDA
commands; the runner validates and executes every selected transition.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from optimizer_pipeline.models import DcpStateNode, to_jsonable
from optimizer_pipeline.reliability import ReliabilityTable
from optimizer_pipeline.retrieval import TrajectoryRetrievalIndex
from optimizer_pipeline.capabilities.search.policies.scorers import AnalysisPriorScorer


class SelectorInvalidResponse(RuntimeError):
    """Raised when an LLM expansion policy returns malformed JSON."""

    def __init__(self, message: str, raw_content: Optional[str] = None):
        super().__init__(message)
        self.raw_content = raw_content


CONFIDENCE_VALUES = {"low", "medium", "high"}
RISK_VALUES = {"low", "medium", "high"}
FORBIDDEN_KEYS = {
    "bash",
    "cmd",
    "command",
    "exec",
    "free_agent",
    "mcp",
    "mcp_tool",
    "python",
    "script",
    "shell",
    "subprocess",
    "tcl",
    "tool",
    "tool_call",
    "tools",
}
FORBIDDEN_TEXT_PATTERNS = [
    r"```",
    r"\bsubprocess\b",
    r"\bexec\s*\(",
    r"\bshell\b",
    r"\btcl\b",
    r"vivado\s+-",
    r"open_checkpoint\s+",
    r"write_checkpoint\s+",
    r"launch_runs\s+",
]


@dataclass
class TransitionCandidate:
    flow: str
    expected_effect: str = ""
    risk: str = "medium"
    confidence: str = "medium"
    target_node_id: Optional[str] = None
    priority: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_node_id": self.target_node_id,
            "flow": self.flow,
            "priority": self.priority,
            "expected_effect": self.expected_effect,
            "risk": self.risk,
            "confidence": self.confidence,
        }


@dataclass
class ExpansionDecision:
    decision_type: str = "expand_node"
    target_node_id: Optional[str] = None
    candidates: List[TransitionCandidate] = field(default_factory=list)
    prune_node_ids: List[str] = field(default_factory=list)
    stop: bool = False
    reason: str = ""
    fallback_used: bool = False
    errors: List[str] = field(default_factory=list)
    raw_payload: Optional[Any] = None
    model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_type": self.decision_type,
            "target_node_id": self.target_node_id,
            "candidates": [c.to_dict() for c in self.candidates],
            "prune_node_ids": self.prune_node_ids,
            "stop": self.stop,
            "reason": self.reason,
            "fallback_used": self.fallback_used,
            "errors": self.errors,
            "raw_payload": self.raw_payload,
            "model": self.model,
        }


@dataclass
class ExpansionValidationResult:
    valid: bool
    decision: ExpansionDecision
    errors: List[str] = field(default_factory=list)
    rejected_payload: Optional[Any] = None
    llm_attempts: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "decision": self.decision.to_dict(),
            "errors": self.errors,
            "rejected_payload": self.rejected_payload,
            "llm_attempts": self.llm_attempts,
        }


class ExpansionPolicyUnavailable(RuntimeError):
    pass


class BaseExpansionPolicy:
    def propose(self, request: Dict[str, Any]) -> ExpansionDecision:
        raise NotImplementedError

    def usage_summary(self) -> Dict[str, Any]:
        return {
            "provider": None,
            "model": None,
            "total_llm_calls": 0,
            "total_tokens": None,
            "total_cost_usd": None,
        }


class DeterministicExpansionPolicy(BaseExpansionPolicy):
    """No-LLM baseline policy using deterministic flow ranking and beam scores."""

    def __init__(self, policy_name: str = "deterministic", analysis_prior_scorer: Optional[AnalysisPriorScorer] = None):
        self.policy_name = policy_name
        self.analysis_prior_scorer = analysis_prior_scorer or AnalysisPriorScorer()

    def propose(self, request: Dict[str, Any]) -> ExpansionDecision:
        frontier = request.get("frontier") or []
        allowed_flows = list(request.get("allowed_flows") or [])
        batch_size = int(request.get("batch_size") or request.get("branching_factor") or 1)
        existing_sequences = {tuple(seq) for seq in (request.get("existing_sequences") or [])}
        contexts = request.get("analysis_context_by_node") or {}
        if not frontier:
            return ExpansionDecision(stop=True, reason="No frontier nodes available", model=self.policy_name)

        # Rank frontier nodes first, but add a small exploration component by
        # leaving the runner to include more than one frontier node in the request.
        frontier_sorted = sorted(
            [node for node in frontier if isinstance(node, dict)],
            key=lambda n: _request_node_objective_score(n),
            reverse=True,
        )
        proposals: List[TransitionCandidate] = []
        for node in frontier_sorted:
            node_id = str(node.get("node_id"))
            seq = list(node.get("flow_sequence") or [])
            last_flow = seq[-1] if seq else None
            flow_scores = self._ranked_flows_for_node(node_id, contexts.get(node_id, {}), allowed_flows)
            for flow, flow_score in flow_scores:
                if flow == last_flow:
                    continue
                proposed_seq = tuple(seq + [flow])
                if proposed_seq in existing_sequences:
                    continue
                priority = self._priority_with_analysis(node, flow, flow_score, request)
                proposals.append(
                    TransitionCandidate(
                        target_node_id=node_id,
                        flow=flow,
                        priority=priority,
                        expected_effect="Deterministic ranked expansion from structured report heuristics.",
                        risk="medium",
                        confidence="medium",
                    )
                )
                if len(proposals) >= batch_size:
                    break
            if len(proposals) >= batch_size:
                break

        decision_type = "expansion_plan" if len({c.target_node_id for c in proposals}) > 1 else "expand_node"
        return ExpansionDecision(
            decision_type=decision_type,
            target_node_id=proposals[0].target_node_id if proposals else frontier_sorted[0].get("node_id"),
            candidates=proposals,
            stop=not bool(proposals),
            reason="Deterministic baseline selected top-ranked allowlisted expansion candidates.",
            model=self.policy_name,
        )

    @staticmethod
    def _ranked_flows_for_node(node_id: str, analysis: Dict[str, Any], allowed_flows: Sequence[str]) -> List[tuple[str, float]]:
        flow_scores: List[tuple[str, float]] = []
        candidate_flows = analysis.get("candidate_flows") if isinstance(analysis.get("candidate_flows"), dict) else {}
        deterministic_scores = candidate_flows.get("deterministic_scores") or analysis.get("flow_scores") or []
        if isinstance(deterministic_scores, list):
            for item in deterministic_scores:
                if isinstance(item, dict) and item.get("flow") in allowed_flows:
                    try:
                        flow_scores.append((item.get("flow"), float(item.get("flow_heuristic_score") or 0.0)))
                    except (TypeError, ValueError):
                        flow_scores.append((item.get("flow"), 0.0))
        if not flow_scores:
            flow_scores = [(flow, 0.0) for flow in allowed_flows]
        # Stable tie-breaker preserves configured order.
        order = {flow: idx for idx, flow in enumerate(allowed_flows)}
        return sorted(flow_scores, key=lambda item: (item[1], -order.get(item[0], 999)), reverse=True)

    @staticmethod
    def _priority(node: Dict[str, Any], flow_score: float) -> float:
        try:
            node_objective_score = _request_node_objective_score(node)
        except (TypeError, ValueError):
            node_objective_score = 0.0
        # Keep inside [0, 1] for JSON stability; it is only an ordering hint.
        return max(0.0, min(1.0, 0.5 + flow_score / 100.0 + node_objective_score / 200.0))

    def _priority_with_analysis(self, node: Dict[str, Any], flow: str, flow_score: float, request: Dict[str, Any]) -> float:
        base = self._priority(node, flow_score)
        return self._apply_analysis_priority(node, flow, base, request)

    def _apply_analysis_priority(self, node: Dict[str, Any], flow: str, base_priority: float, request: Dict[str, Any]) -> float:
        node_id = str(node.get("node_id") or "")
        adj = self.analysis_prior_scorer.priority_adjustment(node_id=node_id, flow=flow, request=request)
        adjusted = base_priority + adj.priority_delta - max(0.0, adj.risk_delta) * 0.05
        return max(0.0, min(1.0, adjusted))


class AnalysisPriorityExpansionPolicy(BaseExpansionPolicy):
    """Scheduler that follows global analysis-layer transition priorities.

    ``candidate_priorities`` are absolute priorities for legal ``(node, flow)``
    transitions.  The analysis layer owns diagnosis and ranking rationale; this
    policy only enumerates legal transitions, supplies neutral-low scores for
    missing rows, and globally sorts them for execution.
    """

    def __init__(self, *, neutral_missing_priority: float = 0.20, policy_name: str = "analysis_priority"):
        self.neutral_missing_priority = max(0.0, min(1.0, float(neutral_missing_priority)))
        self.policy_name = policy_name

    def propose(self, request: Dict[str, Any]) -> ExpansionDecision:
        frontier = [node for node in (request.get("frontier") or []) if isinstance(node, dict)]
        allowed_flows = [str(flow) for flow in (request.get("allowed_flows") or []) if flow]
        batch_size = int(request.get("batch_size") or request.get("branching_factor") or 1)
        existing_sequences = {tuple(seq) for seq in (request.get("existing_sequences") or [])}
        if not frontier:
            return ExpansionDecision(stop=True, reason="No frontier nodes available", model=self.policy_name)

        priority_index = _candidate_priority_index(request)
        flow_order = {flow: idx for idx, flow in enumerate(allowed_flows)}
        scored: List[tuple[float, float, float, int, int, TransitionCandidate]] = []

        for node_index, node in enumerate(frontier):
            node_id = str(node.get("node_id") or "")
            if not node_id:
                continue
            seq = list(node.get("flow_sequence") or [])
            last_flow = seq[-1] if seq else None
            for flow in allowed_flows:
                if flow == last_flow:
                    continue
                proposed_seq = tuple(seq + [flow])
                if proposed_seq in existing_sequences:
                    continue
                priority_row = priority_index.get((node_id, flow))
                candidate = self._candidate_from_priority_row(
                    node_id=node_id,
                    flow=flow,
                    row=priority_row,
                )
                confidence = _priority_row_confidence(priority_row)
                risk = _priority_row_risk(priority_row)
                scored.append((
                    candidate.priority,
                    confidence,
                    -risk,
                    -node_index,
                    -flow_order.get(flow, 999),
                    candidate,
                ))

        scored.sort(key=lambda item: item[:5], reverse=True)
        proposals = [item[5] for item in scored[:batch_size]]
        decision_type = "expansion_plan" if len({c.target_node_id for c in proposals}) > 1 else "expand_node"
        return ExpansionDecision(
            decision_type=decision_type,
            target_node_id=proposals[0].target_node_id if proposals else frontier[0].get("node_id"),
            candidates=proposals,
            stop=not bool(proposals),
            reason="Analysis-priority scheduler selected top legal global transitions from analysis priorities.",
            model=self.policy_name,
        )

    def _candidate_from_priority_row(
        self,
        *,
        node_id: str,
        flow: str,
        row: Optional[Dict[str, Any]],
    ) -> TransitionCandidate:
        if not row:
            return TransitionCandidate(
                target_node_id=node_id,
                flow=flow,
                priority=self.neutral_missing_priority,
                expected_effect="No analysis priority was provided for this legal candidate; preserved with neutral-low priority.",
                risk="medium",
                confidence="low",
            )
        priority_value = row.get("priority_score")
        if priority_value is None:
            priority_value = row.get("candidate_priority_score")
        risk_value = row.get("risk_score")
        if risk_value is None:
            risk_value = row.get("risk_delta")
        priority = _clamped_float(priority_value, 0.0, 1.0, self.neutral_missing_priority)
        risk_score = _clamped_float(risk_value, 0.0, 1.0, 0.0)
        confidence = _clamped_float(row.get("confidence"), 0.0, 1.0, 0.0)
        return TransitionCandidate(
            target_node_id=node_id,
            flow=flow,
            priority=priority,
            expected_effect=str(row.get("expected_effect") or row.get("reason") or "Analysis-ranked candidate."),
            risk=_risk_label_from_score(risk_score),
            confidence=_confidence_label_from_score(confidence),
        )




class ReliabilityAwareExpansionPolicy(DeterministicExpansionPolicy):
    """Deterministic frontier planner with explicit reliability risk penalties.

    This is the first online version of reliability-constrained frontier
    planning.  It does not require a learned world model; it uses validation
    statistics from the current graph and optional priors exported from the trace
    lake.  The policy still only ranks candidates.  The runner executes and
    validates every selected candidate deterministically.
    """

    def __init__(
        self,
        *,
        reliability_table: Optional[ReliabilityTable] = None,
        invalid_penalty: float = 0.35,
        fallback_penalty: float = 0.10,
        warning_penalty: float = 0.05,
        regression_penalty: float = 0.15,
        clean_bonus: float = 0.08,
        expected_qor_gain_weight: float = 0.02,
        runtime_penalty: float = 0.00002,
        min_clean_prob: float = 0.0,
        policy_name: str = "reliability",
    ):
        super().__init__(policy_name=policy_name)
        self.reliability_table = reliability_table or ReliabilityTable()
        self.invalid_penalty = float(invalid_penalty)
        self.fallback_penalty = float(fallback_penalty)
        self.warning_penalty = float(warning_penalty)
        self.regression_penalty = float(regression_penalty)
        self.clean_bonus = float(clean_bonus)
        self.expected_qor_gain_weight = float(expected_qor_gain_weight)
        self.runtime_penalty = float(runtime_penalty)
        self.min_clean_prob = float(min_clean_prob)

    def propose(self, request: Dict[str, Any]) -> ExpansionDecision:
        frontier = request.get("frontier") or []
        allowed_flows = list(request.get("allowed_flows") or [])
        batch_size = int(request.get("batch_size") or request.get("branching_factor") or 1)
        existing_sequences = {tuple(seq) for seq in (request.get("existing_sequences") or [])}
        contexts = request.get("analysis_context_by_node") or {}
        flow_obs = request.get("flow_observations") or {}
        design = _request_design(request)
        if not frontier:
            return ExpansionDecision(stop=True, reason="No frontier nodes available", model=self.policy_name)

        frontier_sorted = sorted(
            [node for node in frontier if isinstance(node, dict)],
            key=lambda n: _request_node_objective_score(n),
            reverse=True,
        )
        proposals: List[TransitionCandidate] = []
        for node in frontier_sorted:
            node_id = str(node.get("node_id"))
            seq = list(node.get("flow_sequence") or [])
            last_flow = seq[-1] if seq else None
            flow_scores = self._ranked_flows_for_node(node_id, contexts.get(node_id, {}), allowed_flows)
            for flow, flow_score in flow_scores:
                if flow == last_flow:
                    continue
                proposed_seq = tuple(seq + [flow])
                if proposed_seq in existing_sequences:
                    continue
                local_obs = flow_obs.get(flow) if isinstance(flow_obs, dict) and isinstance(flow_obs.get(flow), dict) else None
                risk = self.reliability_table.risk_for(
                    flow=flow,
                    last_flow=last_flow,
                    design=design,
                    local_observation=local_obs,
                )
                if float(risk.get("clean_prob") or 0.0) < self.min_clean_prob:
                    continue
                priority = self._apply_analysis_priority(node, flow, self._risk_adjusted_priority(node, flow_score, risk), request)
                proposals.append(
                    TransitionCandidate(
                        target_node_id=node_id,
                        flow=flow,
                        priority=priority,
                        expected_effect=(
                            "Reliability-aware expansion: deterministic QoR ranking adjusted by "
                            f"clean_prob={risk.get('clean_prob'):.2f}, invalid_risk={risk.get('invalid_risk'):.2f}, "
                            f"fallback_risk={risk.get('fallback_risk'):.2f}."
                        ),
                        risk=self._risk_label(risk),
                        confidence=self._confidence_label(risk),
                    )
                )
        proposals.sort(key=lambda c: c.priority, reverse=True)
        proposals = proposals[:batch_size]
        decision_type = "expansion_plan" if len({c.target_node_id for c in proposals}) > 1 else "expand_node"
        return ExpansionDecision(
            decision_type=decision_type,
            target_node_id=proposals[0].target_node_id if proposals else frontier_sorted[0].get("node_id"),
            candidates=proposals,
            stop=not bool(proposals),
            reason=(
                "Reliability-aware deterministic policy selected candidates by combining QoR potential, "
                "current-graph validation observations, and optional trace-lake reliability priors."
            ),
            model=self.policy_name,
        )

    def _risk_adjusted_priority(self, node: Dict[str, Any], flow_score: float, risk: Dict[str, Any]) -> float:
        base = self._priority(node, flow_score)
        clean = float(risk.get("clean_prob") or 0.0)
        invalid = float(risk.get("invalid_risk") or 0.0)
        fallback = float(risk.get("fallback_risk") or 0.0)
        warning = float(risk.get("warning_risk") or 0.0)
        regressed = float(risk.get("regression_risk") or 0.0)
        expected_gain = float(risk.get("expected_delta_pure_qor") or 0.0)
        expected_runtime = float(risk.get("expected_runtime_seconds") or 0.0)
        adjusted = (
            base
            + self.clean_bonus * clean
            + self.expected_qor_gain_weight * expected_gain
            - self.invalid_penalty * invalid
            - self.fallback_penalty * fallback
            - self.warning_penalty * warning
            - self.regression_penalty * regressed
            - self.runtime_penalty * max(0.0, expected_runtime)
        )
        return max(0.0, min(1.0, adjusted))

    @staticmethod
    def _risk_label(risk: Dict[str, Any]) -> str:
        invalid = float(risk.get("invalid_risk") or 0.0)
        fallback = float(risk.get("fallback_risk") or 0.0)
        non_clean = invalid + fallback + float(risk.get("warning_risk") or 0.0)
        if invalid >= 0.35 or non_clean >= 0.60:
            return "high"
        if invalid >= 0.15 or non_clean >= 0.30:
            return "medium"
        return "low"

    @staticmethod
    def _confidence_label(risk: Dict[str, Any]) -> str:
        sources = risk.get("sources") if isinstance(risk.get("sources"), list) else []
        weight = sum(float(item.get("weight") or 0.0) for item in sources if isinstance(item, dict))
        if weight >= 12:
            return "high"
        if weight >= 4:
            return "medium"
        return "low"


class RetrievalAwareExpansionPolicy(ReliabilityAwareExpansionPolicy):
    """Deterministic planner using validated trajectory-memory retrieval.

    This extends reliability with  case-based evidence: similar
    state/action/outcome transitions and successful/failing trajectory motifs.
    It still proposes only allowlisted deterministic flows; Vivado validation
    remains the source of truth.
    """

    def __init__(
        self,
        *,
        retrieval_index: Optional[TrajectoryRetrievalIndex] = None,
        retrieval_success_weight: float = 0.12,
        retrieval_invalid_penalty: float = 0.25,
        retrieval_fallback_penalty: float = 0.08,
        retrieval_expected_qor_gain_weight: float = 0.015,
        retrieval_motif_bonus_weight: float = 0.20,
        retrieval_k: int = 5,
        **kwargs: Any,
    ):
        super().__init__(policy_name=kwargs.pop("policy_name", "retrieval"), **kwargs)
        self.retrieval_index = retrieval_index
        self.retrieval_success_weight = float(retrieval_success_weight)
        self.retrieval_invalid_penalty = float(retrieval_invalid_penalty)
        self.retrieval_fallback_penalty = float(retrieval_fallback_penalty)
        self.retrieval_expected_qor_gain_weight = float(retrieval_expected_qor_gain_weight)
        self.retrieval_motif_bonus_weight = float(retrieval_motif_bonus_weight)
        self.retrieval_k = max(1, int(retrieval_k))

    def propose(self, request: Dict[str, Any]) -> ExpansionDecision:
        frontier = request.get("frontier") or []
        allowed_flows = list(request.get("allowed_flows") or [])
        batch_size = int(request.get("batch_size") or request.get("branching_factor") or 1)
        existing_sequences = {tuple(seq) for seq in (request.get("existing_sequences") or [])}
        contexts = request.get("analysis_context_by_node") or {}
        flow_obs = request.get("flow_observations") or {}
        design = _request_design(request)
        if not frontier:
            return ExpansionDecision(stop=True, reason="No frontier nodes available", model=self.policy_name)

        frontier_sorted = sorted(
            [node for node in frontier if isinstance(node, dict)],
            key=lambda n: _request_node_objective_score(n),
            reverse=True,
        )
        proposals: List[TransitionCandidate] = []
        for node in frontier_sorted:
            node_id = str(node.get("node_id"))
            seq = list(node.get("flow_sequence") or [])
            last_flow = seq[-1] if seq else None
            flow_scores = self._ranked_flows_for_node(node_id, contexts.get(node_id, {}), allowed_flows)
            for flow, flow_score in flow_scores:
                if flow == last_flow:
                    continue
                proposed_seq = tuple(seq + [flow])
                if proposed_seq in existing_sequences:
                    continue
                local_obs = flow_obs.get(flow) if isinstance(flow_obs, dict) and isinstance(flow_obs.get(flow), dict) else None
                risk = self.reliability_table.risk_for(
                    flow=flow,
                    last_flow=last_flow,
                    design=design,
                    local_observation=local_obs,
                )
                if float(risk.get("clean_prob") or 0.0) < self.min_clean_prob:
                    continue
                retrieval = self._retrieval_evidence(node, contexts.get(node_id, {}), flow)
                priority = self._apply_analysis_priority(node, flow, self._retrieval_adjusted_priority(node, flow_score, risk, retrieval), request)
                proposals.append(
                    TransitionCandidate(
                        target_node_id=node_id,
                        flow=flow,
                        priority=priority,
                        expected_effect=(
                            "Retrieval-aware expansion: deterministic QoR ranking adjusted by "
                            f"reliability clean_prob={risk.get('clean_prob'):.2f}, "
                            f"retrieved_success={retrieval.get('success_prior', 0.0):.2f}, "
                            f"retrieved_invalid={retrieval.get('invalid_risk', 0.0):.2f}."
                        ),
                        risk=self._merged_risk_label(risk, retrieval),
                        confidence=self._retrieval_confidence_label(risk, retrieval),
                    )
                )
        proposals.sort(key=lambda c: c.priority, reverse=True)
        proposals = proposals[:batch_size]
        decision_type = "expansion_plan" if len({c.target_node_id for c in proposals}) > 1 else "expand_node"
        return ExpansionDecision(
            decision_type=decision_type,
            target_node_id=proposals[0].target_node_id if proposals else frontier_sorted[0].get("node_id"),
            candidates=proposals,
            stop=not bool(proposals),
            reason=(
                "Retrieval-aware deterministic policy selected candidates using QoR heuristics, "
                "smoothed reliability priors, nearest validated transitions, and motif evidence."
            ),
            model=self.policy_name,
        )

    def _retrieval_evidence(self, node: Dict[str, Any], analysis_context: Dict[str, Any], flow: str) -> Dict[str, Any]:
        if self.retrieval_index is None:
            return {"available": False, "k": 0, "success_prior": 0.0, "invalid_risk": 0.0, "fallback_risk": 0.0, "expected_delta_pure_qor": 0.0, "motif_bonus": 0.0}
        try:
            return self.retrieval_index.candidate_evidence(
                node=node,
                analysis_context=analysis_context,
                candidate_flow=flow,
                k=self.retrieval_k,
            )
        except Exception as exc:
            return {"available": False, "error": f"{type(exc).__name__}: {exc}", "success_prior": 0.0, "invalid_risk": 0.0, "fallback_risk": 0.0, "expected_delta_pure_qor": 0.0, "motif_bonus": 0.0}

    def _retrieval_adjusted_priority(self, node: Dict[str, Any], flow_score: float, risk: Dict[str, Any], retrieval: Dict[str, Any]) -> float:
        adjusted = self._risk_adjusted_priority(node, flow_score, risk)
        if retrieval.get("available"):
            adjusted += self.retrieval_success_weight * float(retrieval.get("success_prior") or 0.0)
            adjusted += self.retrieval_expected_qor_gain_weight * float(retrieval.get("expected_delta_pure_qor") or 0.0)
            adjusted += self.retrieval_motif_bonus_weight * float(retrieval.get("motif_bonus") or 0.0)
            adjusted -= self.retrieval_invalid_penalty * float(retrieval.get("invalid_risk") or 0.0)
            adjusted -= self.retrieval_fallback_penalty * float(retrieval.get("fallback_risk") or 0.0)
        return max(0.0, min(1.0, adjusted))

    @staticmethod
    def _merged_risk_label(risk: Dict[str, Any], retrieval: Dict[str, Any]) -> str:
        merged = dict(risk)
        if retrieval.get("available"):
            merged["invalid_risk"] = max(float(risk.get("invalid_risk") or 0.0), float(retrieval.get("invalid_risk") or 0.0))
            merged["fallback_risk"] = max(float(risk.get("fallback_risk") or 0.0), float(retrieval.get("fallback_risk") or 0.0))
            merged["warning_risk"] = max(float(risk.get("warning_risk") or 0.0), float(retrieval.get("warning_risk") or 0.0))
        return ReliabilityAwareExpansionPolicy._risk_label(merged)

    @staticmethod
    def _retrieval_confidence_label(risk: Dict[str, Any], retrieval: Dict[str, Any]) -> str:
        if retrieval.get("available") and int(retrieval.get("k") or 0) >= 5:
            return "high"
        if retrieval.get("available") and int(retrieval.get("k") or 0) >= 2:
            return "medium"
        return ReliabilityAwareExpansionPolicy._confidence_label(risk)




def _request_node_objective_score(node: Dict[str, Any]) -> float:
    try:
        value = node.get("trajectory_objective_score")
        if value is None:
            value = node.get("pure_qor_score")
        return float(value if value is not None else 0.0)
    except (TypeError, ValueError):
        return 0.0


def _candidate_priority_index(request: Dict[str, Any]) -> Dict[tuple[str, str], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    explicit = request.get("candidate_priorities")
    if isinstance(explicit, list):
        rows.extend(item for item in explicit if isinstance(item, dict))
    for plan in request.get("analysis_plans") or []:
        if not isinstance(plan, dict):
            continue
        plan_node_id = str(plan.get("node_id") or plan.get("target_node_id") or "")
        plan_rows = plan.get("candidate_priorities") if isinstance(plan.get("candidate_priorities"), list) else []
        for item in plan_rows:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("node_id", plan_node_id)
            row.setdefault("target_node_id", plan_node_id)
            rows.append(row)

    best: Dict[tuple[str, str], Dict[str, Any]] = {}
    best_conf: Dict[tuple[str, str], float] = {}
    for row in rows:
        node_id = str(row.get("node_id") or row.get("target_node_id") or "")
        flow = str(row.get("flow") or row.get("candidate_flow") or "")
        if not node_id or not flow:
            continue
        key = (node_id, flow)
        confidence = _clamped_float(row.get("confidence"), 0.0, 1.0, 0.0)
        if key not in best or confidence >= best_conf.get(key, -1.0):
            best[key] = row
            best_conf[key] = confidence
    return best


def _priority_row_confidence(row: Optional[Dict[str, Any]]) -> float:
    if not row:
        return 0.0
    return _clamped_float(row.get("confidence"), 0.0, 1.0, 0.0)


def _priority_row_risk(row: Optional[Dict[str, Any]]) -> float:
    if not row:
        return 0.5
    value = row.get("risk_score")
    if value is None:
        value = row.get("risk_delta")
    return _clamped_float(value, 0.0, 1.0, 0.5)


def _clamped_float(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _risk_label_from_score(value: float) -> str:
    if value >= 0.67:
        return "high"
    if value >= 0.33:
        return "medium"
    return "low"


def _confidence_label_from_score(value: float) -> str:
    if value >= 0.67:
        return "high"
    if value >= 0.33:
        return "medium"
    return "low"


def _analysis_prior_for(request: Dict[str, Any], node_id: str, flow: str) -> Optional[Dict[str, Any]]:
    priors = request.get("analysis_priors") or []
    best: Optional[Dict[str, Any]] = None
    best_conf = float("-inf")
    if not isinstance(priors, list):
        return None
    for item in priors:
        if not isinstance(item, dict):
            continue
        if str(item.get("node_id") or item.get("target_node_id") or "") != str(node_id):
            continue
        if str(item.get("flow") or item.get("candidate_flow") or "") != str(flow):
            continue
        try:
            conf = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf > best_conf:
            best = item
            best_conf = conf
    return best


def _apply_analysis_prior(base_priority: float, prior: Optional[Dict[str, Any]]) -> float:
    if not prior:
        return max(0.0, min(1.0, base_priority))
    try:
        delta = float(prior.get("priority_delta") or 0.0)
    except (TypeError, ValueError):
        delta = 0.0
    try:
        risk_delta = float(prior.get("risk_delta") or 0.0)
    except (TypeError, ValueError):
        risk_delta = 0.0
    return max(0.0, min(1.0, base_priority + delta - risk_delta))


def parse_json_object(content: Optional[str]) -> Dict[str, Any]:
    if not content:
        raise SelectorInvalidResponse("LLM returned empty content", content)
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise SelectorInvalidResponse("LLM did not return a JSON object", content)
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise SelectorInvalidResponse(f"LLM JSON parse failed: {exc}", content) from exc
    if not isinstance(payload, dict):
        raise SelectorInvalidResponse("LLM JSON output must be an object", content)
    return payload


def payload_to_expansion_decision(payload: Any, *, model: Optional[str] = None) -> ExpansionDecision:
    if not isinstance(payload, dict):
        return ExpansionDecision(stop=True, fallback_used=True, errors=["payload is not object"], raw_payload=payload, model=model)
    candidates: List[TransitionCandidate] = []
    raw_candidates = payload.get("plan") if isinstance(payload.get("plan"), list) else payload.get("candidates")
    for item in raw_candidates or []:
        if not isinstance(item, dict):
            continue
        priority_raw = item.get("priority", 0.5)
        try:
            priority = float(priority_raw)
        except (TypeError, ValueError):
            priority = 0.5
        candidates.append(
            TransitionCandidate(
                target_node_id=item.get("target_node_id") or payload.get("target_node_id"),
                flow=str(item.get("flow") or ""),
                priority=priority,
                expected_effect=str(item.get("expected_effect") or item.get("reason") or ""),
                risk=str(item.get("risk") or "medium"),
                confidence=str(item.get("confidence") or "medium"),
            )
        )
    prune = payload.get("prune_node_ids") or payload.get("prune") or []
    if not isinstance(prune, list):
        prune = []
    # Some LLMs return prune entries as objects.
    prune_ids = []
    for item in prune:
        if isinstance(item, dict):
            node_id = item.get("node_id") or item.get("target_node_id")
            if node_id:
                prune_ids.append(str(node_id))
        else:
            prune_ids.append(str(item))
    return ExpansionDecision(
        decision_type=str(payload.get("decision_type") or ("expansion_plan" if candidates else "expand_node")),
        target_node_id=payload.get("target_node_id"),
        candidates=candidates,
        prune_node_ids=prune_ids,
        stop=bool(payload.get("stop", False)),
        reason=str(payload.get("reason") or ""),
        raw_payload=payload,
        model=model,
    )


def validate_expansion_decision(
    decision: ExpansionDecision,
    *,
    allowed_flows: Sequence[str],
    existing_node_ids: Sequence[str],
    branching_factor: int,
    target_node: Optional[DcpStateNode] = None,
    target_node_map: Optional[Dict[str, DcpStateNode]] = None,
    existing_sequences: Optional[Iterable[Sequence[str]]] = None,
    prunable_node_ids: Optional[Sequence[str]] = None,
    disallow_consecutive_repeat: bool = True,
) -> ExpansionValidationResult:
    errors: List[str] = []
    allowed = set(allowed_flows)
    existing = set(existing_node_ids)
    node_map = target_node_map or {}
    if target_node is not None and target_node.node_id not in node_map:
        node_map = {**node_map, target_node.node_id: target_node}
    if decision.decision_type not in {"expand_node", "expansion_plan", "stop", "fallback"}:
        errors.append("decision_type must be expand_node, expansion_plan, stop, or fallback")
    if decision.stop and not decision.candidates:
        return ExpansionValidationResult(valid=True, decision=decision)
    if len(decision.candidates) > branching_factor:
        errors.append(f"candidate count exceeds branching_factor={branching_factor}")
    forbidden = find_forbidden_content(decision.to_dict())
    if forbidden:
        errors.append("forbidden execution content present: " + ", ".join(sorted(set(forbidden))))
    if prunable_node_ids is not None:
        prunable = set(prunable_node_ids)
        for node_id in decision.prune_node_ids:
            if node_id not in prunable:
                allowed_prune = ", ".join(str(item) for item in sorted(prunable))
                errors.append(
                    f"prune_node_id is not in current prunable frontier: {node_id}"
                    + (f" (prunable frontier: {allowed_prune})" if allowed_prune else " (prunable frontier is empty)")
                )

    existing_sequence_set = {tuple(seq) for seq in (existing_sequences or [])}
    valid_candidates: List[TransitionCandidate] = []
    seen_pairs = set()
    for cand in decision.candidates:
        cand_target_id = cand.target_node_id or decision.target_node_id
        cand.target_node_id = cand_target_id
        cand_target = node_map.get(cand_target_id or "") if cand_target_id else target_node
        if not cand_target_id or cand_target_id not in existing:
            allowed_targets = ", ".join(str(node_id) for node_id in sorted(existing))
            errors.append(
                f"target_node_id is not in current frontier: {cand_target_id}"
                + (f" (frontier: {allowed_targets})" if allowed_targets else " (frontier is empty)")
            )
            continue
        if cand.flow not in allowed:
            errors.append(f"unknown flow: {cand.flow}")
            continue
        if cand.risk not in RISK_VALUES:
            errors.append(f"risk for {cand.flow} must be low, medium, or high")
            continue
        if cand.confidence not in CONFIDENCE_VALUES:
            errors.append(f"confidence for {cand.flow} must be low, medium, or high")
            continue
        try:
            cand.priority = max(0.0, min(1.0, float(cand.priority)))
        except (TypeError, ValueError):
            cand.priority = 0.5
        seq = list(cand_target.flow_sequence if cand_target else [])
        last_flow = seq[-1] if seq else None
        if disallow_consecutive_repeat and last_flow and cand.flow == last_flow:
            errors.append(f"consecutive repeated flow disallowed: {cand.flow}")
            continue
        proposed_seq = tuple(seq + [cand.flow])
        if proposed_seq in existing_sequence_set:
            errors.append(f"duplicate flow sequence disallowed: {' -> '.join(proposed_seq)}")
            continue
        pair = (cand_target_id, cand.flow)
        if pair in seen_pairs:
            continue
        valid_candidates.append(cand)
        seen_pairs.add(pair)

    valid_candidates.sort(key=lambda c: c.priority, reverse=True)
    decision.candidates = valid_candidates[:branching_factor]
    if decision.candidates:
        decision.target_node_id = decision.candidates[0].target_node_id

    valid = not errors and (decision.stop or bool(decision.candidates))
    if not valid and not errors:
        errors.append("no valid candidates")
    decision.errors = errors
    return ExpansionValidationResult(valid=valid, decision=decision, errors=errors, rejected_payload=decision.raw_payload)


def fallback_decision(
    reason: str,
    *,
    target_node_id: Optional[str],
    flows: Sequence[str],
    model: Optional[str] = None,
) -> ExpansionDecision:
    return ExpansionDecision(
        decision_type="expand_node",
        target_node_id=target_node_id,
        candidates=[
            TransitionCandidate(target_node_id=target_node_id, flow=flow, expected_effect="Fallback deterministic candidate.", risk="medium", confidence="medium")
            for flow in flows
        ],
        stop=not bool(flows),
        reason=f"Fallback selected because {reason}",
        fallback_used=True,
        errors=[reason],
        model=model,
    )


def find_forbidden_content(value: Any) -> List[str]:
    found: List[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in FORBIDDEN_KEYS:
                    found.append(str(key))
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            for pattern in FORBIDDEN_TEXT_PATTERNS:
                if re.search(pattern, item, flags=re.IGNORECASE):
                    found.append(pattern)
    walk(value)
    return found


def _is_unsupported_temperature_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "temperature" in message and ("unsupported" in message or "does not support" in message)


def _compact_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Limit prompt size while preserving decision-critical information."""
    context_mode = request.get("llm_context_mode", "balanced")
    base = {
        "objective": request.get("objective"),
        "qor_weights": request.get("qor_weights"),
        "allowed_flows": request.get("allowed_flows"),
        "batch_size": request.get("batch_size"),
        "branching_factor": request.get("branching_factor"),
        "scheduler_mode": request.get("scheduler_mode"),
        "active_node_ids": request.get("active_node_ids"),
        "scheduled_runs": request.get("scheduled_runs"),
        "free_slots": request.get("free_slots"),
        "max_children_per_node": request.get("max_children_per_node"),
        "node_child_counts": request.get("node_child_counts"),
        "max_depth": request.get("max_depth"),
        "jobs": request.get("jobs"),
        "max_parallel_jobs": request.get("max_parallel_jobs") or request.get("jobs"),
        "remaining_runs": request.get("remaining_runs"),
        "frontier_node_ids": request.get("frontier_node_ids"),
        "frontier": request.get("frontier"),
        "root": request.get("root") or request.get("root_baseline") or request.get("root_summary"),
        "best_node": request.get("best_node"),
        "best_trajectory": request.get("best_trajectory"),
        "flow_observations": request.get("flow_observations"),
        "reliability_profile": request.get("reliability_profile"),
        "retrieval_profile": request.get("retrieval_profile"),
        "risk_budget": request.get("risk_budget"),
        "analysis_priors": request.get("analysis_priors"),
        "node_diagnoses": request.get("node_diagnoses"),
        "analysis_evidence": request.get("analysis_evidence"),
        "existing_sequences": request.get("existing_sequences"),
        "previous_invalid_decisions": request.get("previous_invalid_decisions"),
    }
    if context_mode == "full":
        base["analysis_context_by_node"] = request.get("analysis_context_by_node")
        base["recent_completed"] = request.get("recent_completed")
        base["recent_events"] = request.get("recent_events")
    elif context_mode == "compact":
        base["analysis_context_by_node"] = _truncate_mapping(request.get("analysis_context_by_node"), limit=5)
        base["recent_completed"] = (request.get("recent_completed") or [])[-5:]
        base["recent_events"] = (request.get("recent_events") or [])[-5:]
        base["existing_sequences"] = (request.get("existing_sequences") or [])[-30:]
    elif context_mode in {"reliability", "retrieval"}:
        base["analysis_context_by_node"] = request.get("analysis_context_by_node")
        base["recent_completed"] = (request.get("recent_completed") or [])[-12:]
        base["recent_events"] = (request.get("recent_events") or [])[-10:]
        base["existing_sequences"] = (request.get("existing_sequences") or [])[-120:]
    else:  # balanced
        base["analysis_context_by_node"] = request.get("analysis_context_by_node")
        base["recent_completed"] = (request.get("recent_completed") or [])[-10:]
        base["recent_events"] = (request.get("recent_events") or [])[-8:]
        base["existing_sequences"] = (request.get("existing_sequences") or [])[-80:]
    return to_jsonable(base)


def _truncate_mapping(value: Any, limit: int) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: value[key] for key in list(value)[:limit]}


def _request_design(request: Dict[str, Any]) -> Optional[str]:
    root = request.get("root") if isinstance(request.get("root"), dict) else {}
    diag = root.get("key_diagnostics") if isinstance(root.get("key_diagnostics"), dict) else {}
    timing = diag.get("timing") if isinstance(diag.get("timing"), dict) else {}
    if timing.get("benchmark_name"):
        return str(timing.get("benchmark_name"))
    best = request.get("best_node") if isinstance(request.get("best_node"), dict) else {}
    if best.get("benchmark_name"):
        return str(best.get("benchmark_name"))
    return None
