"""Closed-loop decision policies for insertable LLM analysis runs.

 turns the previous placeholder into a usable interface.  The default
``mock_llm_closed_loop`` policy is deterministic and local, but follows the same
schema as the optional OpenAI-backed policy: after every observation it either
asks for another allowlisted read-only/report-probe tool or stops with a
candidate-prior / fast-skip decision.  The orchestrator remains the hard-stop
and safety authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence

from integrations.llm.runtime import LLMRequest, build_llm_client
from integrations.llm.runtime.client import LLMClientError
from integrations.llm.runtime.types import LLMResponse
from integrations.llm.runtime.usage import LLMUsageAccumulator

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
            "allowed_flows": self.allowed_flows,
            "called_tools": [call.tool_name for call in self.tool_calls],
            "candidate_priors": _limit_rows(self.candidate_priors, 8),
            "candidate_priorities": _limit_rows(self.candidate_priorities, 8),
            "evidence_summary": _compact_value(self.evidence_summary),
            "optimization_state_context": _compact_value(self.optimization_state_context),
            "required_tools": self.required_tools,
            "fast_skip_suggestions": self.fast_skip_suggestions,
            "max_tool_calls": self.max_tool_calls,
            "fresh_probe_contract": _compact_value(self.fresh_probe_contract),
        }


class AnalysisDecisionPolicy(Protocol):
    name: str

    def decide_next_tool(self, *, state: AnalysisLoopState, available_tool_names: List[str]) -> AnalysisLoopDecision:
        ...


class StaticPlanDecisionPolicy:
    """Adapter for fixed tool-plan mode; controller owns the sequence."""

    name = "static_plan"

    def decide_next_tool(self, *, state: AnalysisLoopState, available_tool_names: List[str]) -> AnalysisLoopDecision:
        return AnalysisLoopDecision(False, stop_reason="static_plan_controller_owns_tool_sequence", confidence=1.0, action="stop")


class ReservedLLMSelfStopPolicy:
    """Fail-closed placeholder for external policies not configured in this run."""

    name = "dynamic_llm_reserved"

    def decide_next_tool(self, *, state: AnalysisLoopState, available_tool_names: List[str]) -> AnalysisLoopDecision:
        return AnalysisLoopDecision(
            False,
            stop_reason="llm_dynamic_tool_loop_not_configured",
            confidence=1.0,
            rationale="No concrete LLM analysis decision policy was supplied.",
        )


@dataclass
class MockLLMSelfStopPolicy:
    """Deterministic stand-in for the LLM closed loop.

    It calls required tools one by one, stops once the minimum evidence set is
    collected, and emits candidate priors plus advisory fast-skip suggestions.
    This gives a local, reproducible test path for the same lifecycle as a real
    LLM-backed policy.
    """

    name: str = "mock_llm_closed_loop"
    required_tools: List[str] = field(default_factory=lambda: [
        "load_node_summary",
        "load_trajectory_outcomes",
        "query_retrieval_memory",
        "vivado_report_timing_summary",
        "vivado_report_route_status",
        "vivado_report_utilization",
    ])
    optional_tools: List[str] = field(default_factory=lambda: ["load_frontier_outcome_memory", "inspect_validation_events", "analyze_trajectory_motifs"])
    min_required_tools_before_stop: int = 4

    def decide_next_tool(self, *, state: AnalysisLoopState, available_tool_names: List[str]) -> AnalysisLoopDecision:
        available = set(available_tool_names)
        called = {call.tool_name for call in state.tool_calls}
        required = state.required_tools or self.required_tools
        enough_required_evidence = len(called.intersection(required)) >= min(len(required), self.min_required_tools_before_stop)
        for tool in required:
            if enough_required_evidence:
                break
            if tool in available and tool not in called:
                return AnalysisLoopDecision(
                    True,
                    action="call_tool",
                    next_tool_name=tool,
                    next_tool_args={"node_id": state.node_id, "flows": state.allowed_flows, "k": 5},
                    expected_information_gain="high" if tool.startswith("vivado_report") else "medium",
                    decision_that_may_change="candidate_ranking_or_fast_skip",
                    confidence=0.70,
                    rationale=f"Need required evidence tool `{tool}` before stopping.",
                    raw_payload={"action": "call_tool", "policy": self.name},
                )
        # Optional tools are useful if no diagnosis evidence was collected.
        if len(called) < self.min_required_tools_before_stop:
            for tool in self.optional_tools:
                if tool in available and tool not in called:
                    return AnalysisLoopDecision(
                    True,
                    action="call_tool",
                    next_tool_name=tool,
                    next_tool_args={"node_id": state.node_id, "flows": state.allowed_flows, "k": 5},
                        expected_information_gain="medium",
                        decision_that_may_change="failure_risk_or_motif_prior",
                        confidence=0.60,
                        rationale=f"Collect optional evidence `{tool}` because required evidence is sparse.",
                        raw_payload={"action": "call_tool", "policy": self.name},
                    )
        priors = _candidate_priors_from_evidence(state)
        skips = _fast_skip_suggestions_from_evidence(state)
        return AnalysisLoopDecision(
            False,
            action="stop",
            stop_reason="sufficient_evidence_for_candidate_priors",
            confidence=0.78 if priors else 0.55,
            rationale="Required evidence has been collected; emit candidate priors and advisory skip suggestions.",
            candidate_priors=priors,
            fast_skip_suggestions=skips,
            raw_payload={"action": "stop", "policy": self.name},
        )


class ProviderAnalysisDecisionPolicy:
    """Optional provider-backed LLM analysis decision policy.

    The policy is still constrained: it can only return a JSON decision over the
    available tool names supplied by the orchestrator.  Tool execution, timeout,
    mutability checks and budget enforcement remain outside the LLM.
    """

    name = "provider_llm_closed_loop"

    def __init__(
        self,
        *,
        provider: str = "openai",
        api_key: Optional[str] = None,
        model: str = "gpt-5.4-mini",
        client: Optional[Any] = None,
        temperature: Optional[float] = 0.0,
        provider_base_url: Optional[str] = None,
        provider_api_key_env: Optional[str] = None,
        pricing_profile: Optional[str] = None,
        timeout_sec: Optional[float] = None,
        cost_budget_usd: Optional[float] = None,
        knowledge_context: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
        policy_name: str = "provider_llm_closed_loop",
    ) -> None:
        self.provider = str(provider or "openai").lower()
        self.name = str(policy_name or "provider_llm_closed_loop")
        self.api_key = api_key
        self.model = model
        self.client = client
        self.temperature = temperature
        self.provider_base_url = provider_base_url
        self.provider_api_key_env = provider_api_key_env
        self.pricing_profile = pricing_profile
        self.timeout_sec = timeout_sec
        self.cost_budget_usd = _optional_float(cost_budget_usd)
        self.knowledge_context = _compact_knowledge_context(knowledge_context)
        self.reasoning_effort = reasoning_effort
        self.usage = LLMUsageAccumulator()
        self.total_llm_calls = 0
        self.total_tokens: Optional[int] = None
        self.total_prompt_tokens: Optional[int] = None
        self.total_completion_tokens: Optional[int] = None
        self.total_cost_usd: Optional[float] = None
        self.total_provider_reported_cost_usd: Optional[float] = None
        self.total_estimated_cost_usd: Optional[float] = None
        self.cost_source: Optional[str] = None
        self.pricing_profile_id: Optional[str] = None
        self.total_latency_sec: Optional[float] = None
        self.analysis_context_mode = "compact_v1"
        self.soft_token_budget, self.hard_token_budget = _model_token_budgets(model)

    def decide_next_tool(self, *, state: AnalysisLoopState, available_tool_names: List[str]) -> AnalysisLoopDecision:
        if self._cost_budget_reached():
            return AnalysisLoopDecision(
                False,
                stop_reason="analysis_cost_budget_reached",
                confidence=1.0,
                rationale="Known or estimated analysis LLM cost budget reached; stop new LLM analysis for this node.",
                action="stop",
                estimated_total_tokens=self.total_tokens,
                analysis_context_mode=self.analysis_context_mode,
                analysis_token_budget_soft=self.soft_token_budget,
                analysis_token_budget_hard=self.hard_token_budget,
            )
        total_tokens = int(self.total_tokens or 0)
        if total_tokens >= self.hard_token_budget:
            return AnalysisLoopDecision(
                False,
                stop_reason="analysis_token_budget_hard_reached",
                confidence=1.0,
                rationale="Hard LLM analysis token budget reached; stop new LLM analysis for this node.",
                action="stop",
                estimated_total_tokens=total_tokens,
                analysis_context_mode=self.analysis_context_mode,
                analysis_token_budget_soft=self.soft_token_budget,
                analysis_token_budget_hard=self.hard_token_budget,
            )
        llm_client = self._client()
        if llm_client is None:
            return AnalysisLoopDecision(
                False,
                stop_reason="llm_provider_not_available",
                confidence=1.0,
                rationale=f"Provider {self.provider!r} is not configured or unavailable.",
            )
        available_for_budget = _available_tools_for_budget(
            available_tool_names,
            state=state,
            total_tokens=total_tokens,
            soft_token_budget=self.soft_token_budget,
        )
        payload = {
            "state": state.to_dict(),
            "available_tool_names": available_for_budget,
            "decision_schema": {
                "continue_analysis": "bool",
                "next_tool_name": "one of available_tool_names or null",
                "next_tool_args": "object",
                "expected_information_gain": "low|medium|high",
                "confidence": "0..1",
                "stop_reason": "string when continue_analysis=false",
                "rationale": "short reason",
                "empirical_observations": "list of current-run observations only; cite state/tool evidence",
                "heuristic_priors": "list of generic FPGA priors only; do not present as current-design evidence",
                "retrieved_motifs": "list of retrieved or current-run motifs with outcome deltas when available",
                "bottleneck_hypotheses": "list of specific bottleneck hypotheses and evidence refs",
                "remaining_uncertainty": "list of missing evidence, especially missing critical_path/congestion/fanout",
                "fresh_probe_contract": "read-only state object. If enabled=true and stop_allowed=false, call next_required_tool instead of stopping. Fresh quorum is satisfied only by tool outputs with fresh_vivado_report=true; cached report probes do not count.",
                "candidate_priorities": "preferred list of absolute transition priorities: {flow, priority_score 0..1, risk_score 0..1, confidence 0..1, evidence_basis current_observation|trajectory_observation|retrieved_motif|heuristic_prior, expected_gain_class low|medium|high|unknown, novelty_score 0..1, expected_effect, contraindications, why_now, do_not_repeat_if, reason, evidence_refs as dict refs {basis,ref,node_id?,flow?,metric?,value?,source?}}",
                "candidate_priors": "legacy compatibility list of {flow, priority_delta, risk_delta, confidence, reason}",
                "fast_skip_suggestions": "list of {flow, would_skip, confidence, reason}",
            },
        }
        if self.knowledge_context is not None:
            payload["knowledge_context"] = self.knowledge_context
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        payload_chars = len(payload_text)
        estimated_prompt_tokens = _estimate_tokens(payload_text)
        messages = [
            {"role": "system", "content": (
                "You are a bounded FPGA implementation analysis advisor for contest-style optimizer search. "
                "Return exactly one valid JSON object and no extra text. You may only request one of the provided "
                "read-only/report-probe tools or stop with structured candidate_priorities. candidate_priorities are "
                "absolute, globally comparable priorities for legal next transitions from the current node. Separate "
                "empirical_observations, heuristic_priors, retrieved_motifs, bottleneck_hypotheses, and remaining_uncertainty. "
                "Do not treat generic FPGA knowledge as current-design evidence. Every high-priority candidate "
                "(priority_score >= 0.70) must cite evidence_refs and explain whether why_now is exploiting a proven "
                "trajectory or exploring a new hypothesis. Use evidence_basis=current_observation only for evidence from "
                "the current node reports, trajectory_observation only for current-run action/outcome history, "
                "retrieved_motif only for retrieval memory, and heuristic_prior only for generic prior knowledge. "
                "If knowledge_context.flow_cards is present, treat it as a static optimizer reference manual and cite it "
                "only as heuristic_prior evidence; it is not current-design evidence and it is not proof that a flow will "
                "work on this benchmark. "
                "When a candidate is mostly heuristic_prior, keep confidence <= 0.45 unless strong current evidence exists. "
                "evidence_refs must be compact objects, not stringified dictionaries. "
                "If critical-path, congestion, or fanout evidence is missing, mention it in remaining_uncertainty and "
                "avoid high confidence. Include contraindications and do_not_repeat_if for risky or plateau-prone flows. "
                "If optimization_state_context includes invalid_sequence_motifs, invalid_action_effects, or mixed_evidence_flows, "
                "reflect that risk in contraindications/do_not_repeat_if and cite it with structured evidence_refs. "
                "If fresh_probe_contract.enabled is true and fresh_probe_contract.stop_allowed is false, you must call "
                "fresh_probe_contract.next_required_tool when it appears in available_tool_names; do not stop with "
                "candidate_priorities yet. Cached report probes never satisfy fresh_probe_contract. After the fresh "
                "quorum is satisfied, stop and emit candidate_priorities unless exactly one optional fresh report probe "
                "has high expected information gain and optional_after_quorum_used is below optional_after_quorum_max. "
                "When fresh_probe_contract.incomplete is true, include fresh_probe_incomplete in remaining_uncertainty "
                "and keep confidence conservative. "
                "If called_tools is empty and max_tool_calls is greater than zero, do not stop: call load_node_summary "
                "if available, otherwise call the highest-information available tool. Before stopping, prefer gathering "
                "state/action/outcome evidence with load_node_summary, load_trajectory_outcomes, load_frontier_outcome_memory, "
                "load_analysis_context, query_retrieval_memory, and available Vivado report tools. Never request Tcl, "
                "shell, file edits, or mutating optimisation."
            )},
            {"role": "user", "content": payload_text},
        ]
        try:
            response = llm_client.complete_json(
                LLMRequest(
                    provider=self.provider,
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    response_format="json_object",
                    timeout_sec=self.timeout_sec,
                    reasoning_effort=self.reasoning_effort,
                )
            )
        except LLMClientError as exc:
            return AnalysisLoopDecision(
                False,
                stop_reason="llm_provider_call_failed",
                confidence=0.0,
                rationale=str(exc),
                raw_payload={"error": str(exc), "provider": self.provider, "model": self.model},
                payload_chars=payload_chars,
                estimated_prompt_tokens=estimated_prompt_tokens,
                estimated_total_tokens=self.total_tokens,
                analysis_context_mode=self.analysis_context_mode,
                analysis_token_budget_soft=self.soft_token_budget,
                analysis_token_budget_hard=self.hard_token_budget,
            )
        self._record_response(response)
        content = response.content
        try:
            raw = _load_json_object(content or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return AnalysisLoopDecision(False, stop_reason="llm_json_parse_failed", confidence=0.0, rationale="LLM did not return valid JSON.", raw_payload={"raw_content": content}, payload_chars=payload_chars, estimated_prompt_tokens=estimated_prompt_tokens, estimated_total_tokens=self.total_tokens, analysis_context_mode=self.analysis_context_mode, analysis_token_budget_soft=self.soft_token_budget, analysis_token_budget_hard=self.hard_token_budget)
        decision = _decision_from_payload(raw, available_for_budget)
        decision.payload_chars = payload_chars
        decision.estimated_prompt_tokens = estimated_prompt_tokens
        decision.estimated_total_tokens = self.total_tokens
        decision.analysis_context_mode = self.analysis_context_mode
        decision.analysis_token_budget_soft = self.soft_token_budget
        decision.analysis_token_budget_hard = self.hard_token_budget
        decision.raw_payload = raw
        return decision

    def usage_summary(self) -> Dict[str, Any]:
        data = self.usage.to_dict()
        data.update(
            {
                "provider": self.provider,
                "model": self.model,
                "cost_budget_usd": self.cost_budget_usd,
            }
        )
        return data

    def _client(self) -> Any:
        if self.client is not None and hasattr(self.client, "complete_json"):
            return self.client
        if self.client is None and self._missing_required_api_key():
            return None
        try:
            self.client = build_llm_client(
                provider=self.provider,
                api_key=self.api_key,
                provider_base_url=self.provider_base_url,
                provider_api_key_env=self.provider_api_key_env,
                pricing_profile=self.pricing_profile,
                model=self.model,
                client=self.client,
            )
        except Exception:
            return None
        return self.client

    def _missing_required_api_key(self) -> bool:
        if self.provider in {"mock", "local"}:
            return False
        if self.api_key:
            return False
        import os

        env_name = self.provider_api_key_env
        if not env_name:
            env_name = "OPENROUTER_API_KEY" if self.provider == "openrouter" else "OPENAI_API_KEY"
        return not bool(os.environ.get(env_name))

    def _record_response(self, response: LLMResponse) -> None:
        self.usage.record(response)
        summary = self.usage.to_dict()
        self.total_llm_calls = int(summary.get("total_llm_calls") or 0)
        self.total_prompt_tokens = summary.get("total_prompt_tokens")
        self.total_completion_tokens = summary.get("total_completion_tokens")
        self.total_tokens = summary.get("total_tokens")
        self.total_cost_usd = summary.get("total_cost_usd")
        self.total_provider_reported_cost_usd = summary.get("total_provider_reported_cost_usd")
        self.total_estimated_cost_usd = summary.get("total_estimated_cost_usd")
        self.cost_source = summary.get("cost_source")
        self.pricing_profile_id = summary.get("pricing_profile_id")
        self.total_latency_sec = summary.get("total_latency_sec")

    def _cost_budget_reached(self) -> bool:
        if self.cost_budget_usd is None:
            return False
        cost = _optional_float(self.total_cost_usd)
        return cost is not None and cost >= self.cost_budget_usd


class OpenAIAnalysisDecisionPolicy(ProviderAnalysisDecisionPolicy):
    """Compatibility wrapper for existing imports and tests."""

    name = "openai_llm_closed_loop"

    def __init__(self, *, api_key: Optional[str] = None, model: str = "gpt-5.4-mini", client: Optional[Any] = None, temperature: Optional[float] = 0.0) -> None:
        super().__init__(
            provider="openai",
            api_key=api_key,
            model=model,
            client=client,
            temperature=temperature,
            policy_name="openai_llm_closed_loop",
        )


def build_analysis_decision_policy(
    mode: str,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    provider_base_url: Optional[str] = None,
    provider_api_key_env: Optional[str] = None,
    pricing_profile: Optional[str] = None,
    timeout_sec: Optional[float] = None,
    cost_budget_usd: Optional[float] = None,
    knowledge_context: Optional[Dict[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
    client: Optional[Any] = None,
) -> AnalysisDecisionPolicy:
    key = (mode or "static_plan").strip().lower()
    if key in {"static", "static_plan"}:
        return StaticPlanDecisionPolicy()
    if key in {"mock_llm", "mock_llm_closed_loop", "scripted_llm_closed_loop", "heuristic_llm", "heuristic_closed_loop", "dynamic_mock"}:
        return MockLLMSelfStopPolicy()
    if key in {"openai", "openai_json", "openai_llm", "openai_llm_closed_loop", "llm", "llm_closed_loop"}:
        return ProviderAnalysisDecisionPolicy(
            client=client,
            provider=provider or "openai",
            api_key=api_key,
            model=model or _default_model_for_provider(provider or "openai"),
            provider_base_url=provider_base_url,
            provider_api_key_env=provider_api_key_env,
            pricing_profile=pricing_profile,
            reasoning_effort=reasoning_effort,
            timeout_sec=timeout_sec,
            cost_budget_usd=cost_budget_usd,
            knowledge_context=knowledge_context,
            policy_name=key,
        )
    if key in {"reserved", "dynamic_llm_reserved"}:
        return ReservedLLMSelfStopPolicy()
    return StaticPlanDecisionPolicy()


def _decision_from_payload(payload: Dict[str, Any], available_tool_names: Sequence[str]) -> AnalysisLoopDecision:
    available = set(available_tool_names)
    tool = payload.get("next_tool_name") or payload.get("tool_name")
    action = str(payload.get("action") or "").strip().lower()
    explicit_continue = payload.get("continue_analysis")
    if explicit_continue is None and action not in {"stop", "emit_prior"} and tool:
        cont = True
    else:
        cont = bool(explicit_continue or action == "call_tool")
    if cont and tool not in available:
        return AnalysisLoopDecision(False, stop_reason="llm_requested_disallowed_tool", confidence=0.0, rationale=f"Tool `{tool}` is not in available tool list.", raw_payload=payload)
    return AnalysisLoopDecision(
        continue_analysis=cont,
        next_tool_name=tool if cont else None,
        next_tool_args=payload.get("next_tool_args") or payload.get("tool_args") or {},
        expected_information_gain=str(payload.get("expected_information_gain") or "medium"),
        decision_that_may_change=str(payload.get("decision_that_may_change") or "candidate_ranking"),
        confidence=_float(payload.get("confidence"), 0.5),
        stop_reason=str(payload.get("stop_reason") or payload.get("reason") or ("continue" if cont else "llm_stop")),
        rationale=str(payload.get("rationale") or payload.get("why_needed") or ""),
        candidate_priors=payload.get("candidate_priors") if isinstance(payload.get("candidate_priors"), list) else [],
        candidate_priorities=payload.get("candidate_priorities") if isinstance(payload.get("candidate_priorities"), list) else [],
        fast_skip_suggestions=payload.get("fast_skip_suggestions") if isinstance(payload.get("fast_skip_suggestions"), list) else [],
        empirical_observations=[str(x) for x in payload.get("empirical_observations", [])] if isinstance(payload.get("empirical_observations"), list) else [],
        heuristic_priors=[str(x) for x in payload.get("heuristic_priors", [])] if isinstance(payload.get("heuristic_priors"), list) else [],
        retrieved_motifs=[str(x) for x in payload.get("retrieved_motifs", [])] if isinstance(payload.get("retrieved_motifs"), list) else [],
        bottleneck_hypotheses=[str(x) for x in payload.get("bottleneck_hypotheses", [])] if isinstance(payload.get("bottleneck_hypotheses"), list) else [],
        remaining_uncertainty=[str(x) for x in payload.get("remaining_uncertainty", [])] if isinstance(payload.get("remaining_uncertainty"), list) else [],
        action="call_tool" if cont else "stop",
        raw_payload=payload,
    )


def _load_json_object(content: str) -> Dict[str, Any]:
    """Load one JSON object, tolerating harmless duplicated trailing output.

    Some chat-completions JSON-mode responses can still include a repeated JSON
    object after the first valid object.  We keep the safety boundary by parsing
    only the first object and still routing the decoded tool name through the
    allowlist validator.
    """

    stripped = (content or "").strip()
    try:
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError("LLM JSON payload is not an object")
        return parsed
    except json.JSONDecodeError:
        parsed, index = json.JSONDecoder().raw_decode(stripped)
        if not isinstance(parsed, dict):
            raise ValueError("LLM JSON payload is not an object")
        trailing = stripped[index:].strip()
        if trailing:
            parsed = dict(parsed)
            parsed["_parse_warning"] = "ignored_extra_content_after_first_json_object"
        return parsed


def _candidate_priors_from_evidence(state: AnalysisLoopState) -> List[Dict[str, Any]]:
    flows = state.allowed_flows or []
    retrieval = state.evidence_summary.get("retrieval_by_flow") if isinstance(state.evidence_summary.get("retrieval_by_flow"), dict) else {}
    timing = state.evidence_summary.get("timing") if isinstance(state.evidence_summary.get("timing"), dict) else {}
    route = state.evidence_summary.get("route_status") if isinstance(state.evidence_summary.get("route_status"), dict) else {}
    util = state.evidence_summary.get("utilization") if isinstance(state.evidence_summary.get("utilization"), dict) else {}
    wns = _float(timing.get("wns_ns"), None)
    route_clean = bool(route.get("is_fully_routed") or not route.get("route_design_failed"))
    priors: List[Dict[str, Any]] = []
    for index, flow in enumerate(flows):
        ev = retrieval.get(flow, {}) if isinstance(retrieval.get(flow), dict) else {}
        success = _float(ev.get("success_prior"), 0.0)
        invalid = _float(ev.get("invalid_risk"), 0.0)
        delta = 0.04 + 0.10 * success - 0.18 * invalid
        if flow in {"auto_pblock", "phys_opt", "memory_path_opt"} and wns is not None and wns < 0 and route_clean:
            delta += 0.06
        if flow in {"lut_cone", "net_swap"} and invalid >= 0.25:
            delta -= 0.08
        priors.append({
            "flow": flow,
            "priority_delta": round(max(-0.25, min(0.25, delta)), 6),
            "risk_delta": round(max(0.0, invalid), 6),
            "confidence": round(min(0.85, 0.45 + success * 0.25 + (0.10 if wns is not None else 0.0)), 3),
            "reason": "Closed-loop analysis prior from timing/route/utilization/retrieval evidence.",
        })
    priors.sort(key=lambda item: (item["priority_delta"] - item["risk_delta"], item["confidence"]), reverse=True)
    return priors[:5]


def _fast_skip_suggestions_from_evidence(state: AnalysisLoopState) -> List[Dict[str, Any]]:
    retrieval = state.evidence_summary.get("retrieval_by_flow") if isinstance(state.evidence_summary.get("retrieval_by_flow"), dict) else {}
    route = state.evidence_summary.get("route_status") if isinstance(state.evidence_summary.get("route_status"), dict) else {}
    out: List[Dict[str, Any]] = []
    for flow, ev in retrieval.items():
        if not isinstance(ev, dict):
            continue
        invalid = _float(ev.get("invalid_risk"), 0.0)
        if invalid >= 0.35:
            out.append({"flow": flow, "would_skip": True, "confidence": min(0.9, 0.55 + invalid * 0.4), "reason": "Retrieved neighbours show high invalid risk; advisory only."})
    if route.get("route_design_failed"):
        out.append({"flow": "lut_cone", "would_skip": True, "confidence": 0.65, "reason": "Current route-status evidence indicates route failure risk; advisory only."})
    return out[:5]


def _float(value: Any, default: Optional[float]) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_model_for_provider(provider: str) -> str:
    key = str(provider or "openai").lower()
    if key == "openrouter":
        import os

        return os.environ.get("OPENROUTER_MODEL") or os.environ.get("FPL_AGENT_MODEL") or "openai/gpt-5.4-mini"
    import os

    return os.environ.get("FPL_AGENT_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-5.4-mini"


def _compact_value(value: Any, *, max_string: int = 1200, max_list: int = 8, max_dict: int = 40) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "...[truncated]"
    if isinstance(value, list):
        return [_compact_value(item, max_string=max_string, max_list=max_list, max_dict=max_dict) for item in value[:max_list]]
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_dict:
                out["_truncated_keys"] = len(value) - max_dict
                break
            out[str(key)] = _compact_value(item, max_string=max_string, max_list=max_list, max_dict=max_dict)
        return out
    return value


def _compact_knowledge_context(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    flow_cards = value.get("flow_cards")
    if not flow_cards:
        return None
    return {
        "phase": "reference_manual_v1",
        "source_schema_version": value.get("source_schema_version"),
        "flow_cards": _compact_value(flow_cards, max_string=900, max_list=20, max_dict=80),
    }


def _limit_rows(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    return [_compact_value(row) for row in rows[:limit] if isinstance(row, dict)]


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


# Mini-class models get a larger analysis budget because their per-token cost is
# far lower. Matched on a delimited "-mini" token rather than the bare substring:
# "mini" appears inside "gemini", which silently gave every Gemini model double
# the budget of its comparators and invalidated cross-model cost comparison.
_MINI_MODEL_RE = re.compile(r"(?:^|[-_/.])mini(?:$|[-_/.])")


def _is_mini_class(key: str) -> bool:
    return bool(_MINI_MODEL_RE.search(key))


def _model_token_budgets(model: str) -> tuple[int, int]:
    key = str(model or "").lower()
    if "5.5" in key:
        return 75_000, 100_000
    if _is_mini_class(key):
        return 420_000, 700_000
    return 420_000, 700_000


def _available_tools_for_budget(
    available_tool_names: List[str],
    *,
    state: AnalysisLoopState,
    total_tokens: int,
    soft_token_budget: int,
) -> List[str]:
    if total_tokens < soft_token_budget:
        return list(available_tool_names)
    contract = state.fresh_probe_contract if isinstance(state.fresh_probe_contract, dict) else {}
    next_required = contract.get("next_required_tool")
    if contract.get("enabled") and not contract.get("satisfied") and next_required in available_tool_names:
        return list(dict.fromkeys(list(available_tool_names) + [str(next_required)]))
    context = state.optimization_state_context if isinstance(state.optimization_state_context, dict) else {}
    summary = context.get("current_node_summary") if isinstance(context.get("current_node_summary"), dict) else {}
    depth = _float(summary.get("depth"), None)
    if depth == 0:
        return list(available_tool_names)
    return [name for name in available_tool_names if not str(name).startswith("vivado_report_")]
