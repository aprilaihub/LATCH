"""Online budgeted deep-analysis controller.

It supports two execution modes:

* ``static_plan``: execute a configured safe tool plan.
* closed-loop decision policy: repeatedly ask a bounded decision policy
  (mock/local or optional LLM JSON policy) which read-only/report-probe tool to
  call next, until the policy says enough evidence is available or the hard
  budget stops it.

The controller is the safety authority: it validates tool names, tool classes,
mutability, timeouts and budgets.  Analysis never mutates a DCP and never enters
QoR scoring.
"""

from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dynamic_loop import AnalysisDecisionPolicy, AnalysisLoopState, StaticPlanDecisionPolicy
from .memory import AnalysisMemory
from .policies import (
    AnalysisBudgetPolicy,
    AnalysisStopPolicy,
    AnalysisTriggerPolicy,
    DiagnosisSynthesizer,
    HeuristicDiagnosisSynthesizer,
    NaiveOnlineAnalysisTriggerPolicy,
    StaticAnalysisBudgetPolicy,
    TimePressureStopPolicy,
)
from .report_probe_backends import ReportProbeBackend, build_report_probe_backend
from .schemas import (
    AnalysisRunRecord,
    AnalysisSessionResult,
    AnalysisStopDecision,
    AnalysisUpdate,
    CandidatePrior,
    CandidatePriority,
    NodeAnalysisPlan,
    now_iso,
)
from .tool_plan_policies import AnalysisToolPlanPolicy, StaticAnalysisToolPlanPolicy
from .tool_registry import AnalysisToolRegistry, default_tool_registry
from .tools.artifact_tools import build_optimization_state_context
from .tools.base import ToolContext, execute_tool


_REPORT_PROBE_TOOLS = {
    "vivado_report_timing_summary",
    "vivado_report_route_status",
    "vivado_report_utilization",
}
_FRESH_PROBE_OPTIONAL_TOOLS = ["vivado_report_utilization"]
_FRESH_PROBE_OPTIONAL_AFTER_QUORUM_MAX = 1
_FRESH_PROBE_INCOMPLETE_CONFIDENCE_CAP = 0.45


class OnlineAnalysisController:
    """Runs bounded root/key-node analysis and persists evidence traces."""

    def __init__(
        self,
        *,
        run_dir: Path,
        allowed_flows: List[str],
        retrieval_index: Any = None,
        trigger_policy: Optional[AnalysisTriggerPolicy] = None,
        budget_policy: Optional[AnalysisBudgetPolicy] = None,
        stop_policy: Optional[AnalysisStopPolicy] = None,
        synthesizer: Optional[DiagnosisSynthesizer] = None,
        tool_registry: Optional[AnalysisToolRegistry] = None,
        tool_plan_policy: Optional[AnalysisToolPlanPolicy] = None,
        report_probe_backend: Optional[str | ReportProbeBackend] = None,
        fresh_probe_contract_mode: str = "auto",
        decision_policy: Optional[AnalysisDecisionPolicy] = None,
        config: Any = None,
        enabled: bool = False,
        persist_artifacts: bool = True,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.allowed_flows = list(allowed_flows)
        self.retrieval_index = retrieval_index
        self.enabled = bool(enabled)
        self.trigger_policy = trigger_policy or NaiveOnlineAnalysisTriggerPolicy()
        self.budget_policy = budget_policy or StaticAnalysisBudgetPolicy()
        self.stop_policy = stop_policy or TimePressureStopPolicy()
        self.synthesizer = synthesizer or HeuristicDiagnosisSynthesizer()
        self.tool_registry = tool_registry or default_tool_registry()
        self.tool_plan_policy = tool_plan_policy or StaticAnalysisToolPlanPolicy()
        self.report_probe_backend = build_report_probe_backend(report_probe_backend)
        self.fresh_probe_contract_mode = str(fresh_probe_contract_mode or "auto").strip().lower()
        if self.fresh_probe_contract_mode not in {"auto", "off", "required"}:
            self.fresh_probe_contract_mode = "auto"
        if self.fresh_probe_contract_mode == "required" and not bool(getattr(self.report_probe_backend, "fresh_vivado_report", False)):
            raise ValueError("analysis_fresh_probe_contract_mode=required needs a fresh report-probe backend")
        self.decision_policy = decision_policy or StaticPlanDecisionPolicy()
        self.config = config
        self.persist_artifacts = bool(persist_artifacts)
        self.memory = AnalysisMemory(
            self.run_dir,
            enabled=self.enabled,
            persist_artifacts=self.persist_artifacts,
        )
        self._session_counter = 0
        if self.enabled and self.persist_artifacts:
            self.memory.analysis_dir.mkdir(parents=True, exist_ok=True)
            (self.memory.analysis_dir / "config.json").write_text(json.dumps(self._config_summary(), indent=2) + "\n")

    @classmethod
    def disabled(cls, run_dir: Path) -> "OnlineAnalysisController":
        return cls(run_dir=run_dir, allowed_flows=[], enabled=False)

    def summary(self) -> Dict[str, Any]:
        return self.memory.to_summary() if self.enabled else {"enabled": False}

    async def maybe_analyze(
        self,
        *,
        candidate_nodes: List[Any],
        state: Any,
        nodes: Dict[str, Any],
        analysis_contexts: Dict[str, Dict[str, Any]],
        run_count: int,
        max_total_runs: int,
    ) -> AnalysisUpdate:
        update = AnalysisUpdate()
        if not self.enabled:
            return update
        runner_view = {
            "analysis_sessions_done": len(self.memory.sessions),
            "analyzed_node_ids": [s.node_id for s in self.memory.sessions],
            "run_count": run_count,
            "max_total_runs": max_total_runs,
            "no_improvement_window_hit": self._no_improvement_window_hit(nodes),
            "frontier_disagreement_high": self._frontier_disagreement_high(candidate_nodes),
        }
        for node in candidate_nodes:
            trigger = self.trigger_policy.should_analyze(node=node, state=state, runner_view=runner_view)
            if not trigger.should_analyze:
                continue
            session = await self._run_session(
                node=node,
                trigger_reason=trigger.trigger_reason,
                budget=self.budget_policy.budget_for(trigger),
                nodes=nodes,
                analysis_contexts=analysis_contexts,
            )
            self.memory.apply_session(session)
            update.sessions.append(session)
            update.candidate_priors.extend(session.candidate_priors)
            update.candidate_priorities.extend(session.candidate_priorities)
            if session.analysis_plan:
                update.analysis_plans.append(session.analysis_plan)
            if session.diagnosis:
                update.node_diagnoses.append(session.diagnosis)
            break
        return update

    async def run_session(
        self,
        *,
        node: Any,
        trigger_reason: str,
        budget: AnalysisBudget,
        nodes: Dict[str, Any],
        analysis_contexts: Dict[str, Dict[str, Any]],
    ) -> AnalysisSessionResult:
        """Run one deep-analysis session for an existing DCP state node.

        Analysis sessions are evidence producers; they do not create DCP-state
        graph nodes or execute transitions.
        """
        return await self._run_session(
            node=node,
            trigger_reason=trigger_reason,
            budget=budget,
            nodes=nodes,
            analysis_contexts=analysis_contexts,
        )

    async def _run_session(
        self,
        *,
        node: Any,
        trigger_reason: str,
        budget: Any,
        nodes: Dict[str, Any],
        analysis_contexts: Dict[str, Dict[str, Any]],
    ) -> AnalysisSessionResult:
        self._session_counter += 1
        node_id = str(getattr(node, "node_id", "node"))
        session_id = f"analysis_{self._session_counter:03d}_{node_id}"
        started_at = now_iso()
        t0 = time.time()
        tool_context = ToolContext(
            run_dir=self.run_dir,
            node=node,
            nodes=nodes,
            analysis_contexts=analysis_contexts,
            retrieval_index=self.retrieval_index,
            config=self.config,
            allowed_flows=list(self.allowed_flows),
            report_probe_backend=self.report_probe_backend,
        )
        tools = self._tool_plan_policy_tools(node=node, trigger_reason=trigger_reason, budget=budget)
        llm_calls_before = int(getattr(self.decision_policy, "total_llm_calls", 0) or 0)
        tokens_before = getattr(self.decision_policy, "total_tokens", None)
        usage_before = getattr(self.decision_policy, "usage_summary", lambda: {})()
        cost_before = usage_before.get("total_cost_usd") if isinstance(usage_before, dict) else None
        if getattr(self.decision_policy, "name", "static_plan") == "static_plan":
            tool_calls, stop, loop_decisions, dynamic_priors, dynamic_priorities, fast_skip_suggestions = self._run_static_plan(
                tools=tools,
                node=node,
                budget=budget,
                analysis_contexts=analysis_contexts,
                tool_context=tool_context,
                start_time=t0,
            )
        else:
            tool_calls, stop, loop_decisions, dynamic_priors, dynamic_priorities, fast_skip_suggestions = self._run_closed_loop_plan(
                session_id=session_id,
                tools=tools,
                node=node,
                trigger_reason=trigger_reason,
                budget=budget,
                analysis_contexts=analysis_contexts,
                tool_context=tool_context,
                start_time=t0,
            )
        elapsed = time.time() - t0
        llm_calls_after = int(getattr(self.decision_policy, "total_llm_calls", llm_calls_before) or 0)
        tokens_after = getattr(self.decision_policy, "total_tokens", tokens_before)
        session_llm_calls = max(0, llm_calls_after - llm_calls_before)
        session_tokens = None
        if tokens_after is not None:
            session_tokens = int(tokens_after) - int(tokens_before or 0)
        usage_after = getattr(self.decision_policy, "usage_summary", lambda: {})()
        session_cost = None
        if isinstance(usage_after, dict):
            cost_after = usage_after.get("total_cost_usd")
            if cost_after is not None:
                session_cost = float(cost_after) - float(cost_before or 0.0)
        diagnosis = self.synthesizer.synthesize(node=node, tool_calls=tool_calls, allowed_flows=self.allowed_flows, session_id=session_id)
        diagnosis.analysis_type = trigger_reason
        if dynamic_priorities:
            diagnosis.candidate_priorities = dynamic_priorities
            diagnosis.recommended_action = "apply_closed_loop_candidate_priorities"
        elif dynamic_priors:
            diagnosis.candidate_priors = dynamic_priors
            diagnosis.candidate_priorities = [CandidatePriority.from_prior(prior) for prior in dynamic_priors]
            diagnosis.recommended_action = "apply_closed_loop_candidate_priors"
        elif not diagnosis.candidate_priorities:
            diagnosis.candidate_priorities = [CandidatePriority.from_prior(prior) for prior in diagnosis.candidate_priors]
        if fast_skip_suggestions:
            diagnosis.risks.append({"type": "advisory_fast_skip_suggestions", "suggestions": fast_skip_suggestions})
        if loop_decisions:
            diagnosis.evidence.append({
                "tool": "closed_loop_decision_policy",
                "summary": {
                    "decision_policy": getattr(self.decision_policy, "name", type(self.decision_policy).__name__),
                    "num_loop_decisions": len(loop_decisions),
                    "final_decision": loop_decisions[-1].to_dict() if hasattr(loop_decisions[-1], "to_dict") else loop_decisions[-1],
                    "fast_skip_suggestions": fast_skip_suggestions,
                },
            })
        analysis_run = AnalysisRunRecord(
            analysis_run_id=f"analysis_run_{session_id}",
            target_node_id=node_id,
            analysis_type=trigger_reason,
            status="COMPLETED" if stop.stop_reason not in {"hard_wall_time_budget_reached", "tool_call_budget_reached", "llm_call_budget_reached"} else "TIMEOUT",
            session_id=session_id,
            tool_loop_mode=getattr(self.decision_policy, "name", "static_plan"),
            loop_decisions=list(loop_decisions),
            fast_skip_suggestions=list(fast_skip_suggestions),
            budget_profile="root" if getattr(node, "depth", 0) == 0 else "node",
            stop_reason=stop.stop_reason,
            candidate_prior_count=len(diagnosis.candidate_priors),
            tool_call_count=len(tool_calls),
        )
        analysis_plan = _analysis_plan_from_diagnosis(
            diagnosis,
            session_id=session_id,
            trigger_reason=trigger_reason,
        )
        report_path = self._write_report(session_id, node_id, trigger_reason, diagnosis, tool_calls, stop, elapsed, loop_decisions=loop_decisions, fast_skip_suggestions=fast_skip_suggestions, analysis_run=analysis_run)
        return AnalysisSessionResult(
            session_id=session_id,
            node_id=node_id,
            trigger_reason=trigger_reason,
            budget=budget,
            elapsed_sec=round(elapsed, 6),
            stop_decision=stop,
            tool_calls=tool_calls,
            diagnosis=diagnosis,
            candidate_priors=list(diagnosis.candidate_priors),
            candidate_priorities=list(diagnosis.candidate_priorities),
            analysis_plan=analysis_plan,
            fast_skip_suggestions=list(fast_skip_suggestions),
            report_path=str(report_path) if report_path else None,
            started_at=started_at,
            tool_loop_mode=getattr(self.decision_policy, "name", "static_plan"),
            loop_decisions=loop_decisions,
            analysis_run=analysis_run.to_dict(),
            llm_calls=session_llm_calls,
            tokens=session_tokens,
            cost_usd=session_cost,
            analysis_model=getattr(self.decision_policy, "model", None),
            analysis_provider=_analysis_provider_name(self.decision_policy),
        )

    def _run_static_plan(
        self,
        *,
        tools: List[Any],
        node: Any,
        budget: Any,
        analysis_contexts: Dict[str, Dict[str, Any]],
        tool_context: ToolContext,
        start_time: float,
    ) -> tuple[List[Any], AnalysisStopDecision, List[Any], List[CandidatePrior], List[CandidatePriority], List[Dict[str, Any]]]:
        tool_calls: List[Any] = []
        stop = AnalysisStopDecision(True, "initial")
        for tool in tools:
            elapsed = time.time() - start_time
            stop = self.stop_policy.should_continue(elapsed_sec=elapsed, budget=budget, tool_calls=tool_calls, next_tool=getattr(tool, "name", None))
            if not stop.continue_analysis:
                break
            if not self._tool_allowed_by_budget(tool, budget, tool_calls=tool_calls):
                continue
            call = execute_tool(tool, self._tool_args(tool, node, analysis_contexts), tool_context, timeout_sec=getattr(budget, "default_tool_timeout_sec", None))
            tool_calls.append(call)
        elapsed = time.time() - start_time
        if stop.continue_analysis:
            stop = self.stop_policy.should_continue(elapsed_sec=elapsed, budget=budget, tool_calls=tool_calls, next_tool=None)
            if stop.continue_analysis:
                stop = AnalysisStopDecision(False, "tool_plan_exhausted", confidence=0.70)
        return tool_calls, stop, [], [], [], []

    def _run_closed_loop_plan(
        self,
        *,
        session_id: str,
        tools: List[Any],
        node: Any,
        trigger_reason: str,
        budget: Any,
        analysis_contexts: Dict[str, Dict[str, Any]],
        tool_context: ToolContext,
        start_time: float,
    ) -> tuple[List[Any], AnalysisStopDecision, List[Any], List[CandidatePrior], List[CandidatePriority], List[Dict[str, Any]]]:
        tool_calls: List[Any] = []
        loop_decisions: List[Any] = []
        dynamic_priors: List[CandidatePrior] = []
        dynamic_priorities: List[CandidatePriority] = []
        fast_skip_suggestions: List[Dict[str, Any]] = []
        stop = AnalysisStopDecision(True, "initial_closed_loop")
        allowed_by_name = {getattr(tool, "name", ""): tool for tool in tools}
        while True:
            elapsed = time.time() - start_time
            state = AnalysisLoopState(
                session_id=session_id,
                node_id=str(getattr(node, "node_id", "node")),
                trigger_reason=trigger_reason,
                elapsed_sec=round(elapsed, 6),
                remaining_budget_sec=max(0.0, float(getattr(budget, "max_wall_time_sec", 0.0)) - elapsed),
                allowed_flows=list(self.allowed_flows),
                tool_calls=list(tool_calls),
                candidate_priors=[p.to_dict() for p in dynamic_priors],
                candidate_priorities=[p.to_dict() for p in dynamic_priorities],
                evidence_summary=self._summarize_loop_evidence(tool_calls),
                optimization_state_context=build_optimization_state_context(
                    node=node,
                    nodes=tool_context.nodes if tool_context is not None else {str(getattr(node, "node_id", "node")): node},
                    analysis_contexts=analysis_contexts,
                    allowed_flows=list(self.allowed_flows),
                ),
                required_tools=list(allowed_by_name.keys()),
                max_tool_calls=int(getattr(budget, "max_tool_calls", 0) or 0),
                fresh_probe_contract=self._fresh_probe_contract(
                    node=node,
                    trigger_reason=trigger_reason,
                    budget=budget,
                    tool_calls=tool_calls,
                    allowed_by_name=allowed_by_name,
                ),
            )
            hard = self.stop_policy.should_continue(elapsed_sec=elapsed, budget=budget, tool_calls=tool_calls, next_tool="closed_loop")
            if not hard.continue_analysis and not self._allow_final_fresh_probe_decision(
                hard,
                state.fresh_probe_contract,
                loop_decisions,
            ):
                stop = hard
                break
            max_llm_calls = int(getattr(budget, "max_llm_calls", 0) or 0)
            if max_llm_calls > 0 and len(loop_decisions) >= max_llm_calls:
                stop = self._fresh_probe_budget_stop(
                    "llm_call_budget_reached",
                    state.fresh_probe_contract,
                )
                break
            decision = self.decision_policy.decide_next_tool(state=state, available_tool_names=list(allowed_by_name.keys()))
            decision.fresh_probe_contract = dict(state.fresh_probe_contract or {})
            forced_tool = self._fresh_probe_forced_tool(
                decision=decision,
                contract=state.fresh_probe_contract,
                allowed_by_name=allowed_by_name,
                budget=budget,
                tool_calls=tool_calls,
            )
            if forced_tool:
                decision.fresh_probe_override = True
                decision.action = "call_tool"
                decision.continue_analysis = True
                decision.next_tool_name = forced_tool
                decision.next_tool_args = {"node_id": str(getattr(node, "node_id", "node"))}
                decision.stop_reason = "fresh_probe_contract_forced_required_probe"
                decision.rationale = "Controller forced the next required fresh report-probe before accepting analysis priorities."
                decision.candidate_priors = []
                decision.candidate_priorities = []
            loop_decisions.append(decision)
            if getattr(decision, "candidate_priorities", None):
                missing_diagnostics = _missing_diagnostics_from_state_context(state.optimization_state_context)
                dynamic_priorities = _candidate_priorities_from_payload(
                    decision.candidate_priorities,
                    node_id=str(getattr(node, "node_id", "node")),
                    session_id=session_id,
                    default_uncertainty=list(getattr(decision, "remaining_uncertainty", []) or []),
                    missing_diagnostics=missing_diagnostics,
                    fresh_probe_incomplete=bool(state.fresh_probe_contract.get("incomplete")),
                )
            if getattr(decision, "candidate_priors", None):
                dynamic_priors = _candidate_priors_from_payload(
                    decision.candidate_priors,
                    node_id=str(getattr(node, "node_id", "node")),
                    session_id=session_id,
                )
            if getattr(decision, "fast_skip_suggestions", None):
                fast_skip_suggestions = _fast_skip_suggestions_from_payload(
                    decision.fast_skip_suggestions,
                    node_id=str(getattr(node, "node_id", "node")),
                    session_id=session_id,
                )
            if not decision.continue_analysis or getattr(decision, "action", "stop") in {"stop", "emit_prior"}:
                if getattr(decision, "candidate_priorities", None):
                    missing_diagnostics = _missing_diagnostics_from_state_context(state.optimization_state_context)
                    dynamic_priorities = _candidate_priorities_from_payload(
                        decision.candidate_priorities,
                        node_id=str(getattr(node, "node_id", "node")),
                        session_id=session_id,
                        default_uncertainty=list(getattr(decision, "remaining_uncertainty", []) or []),
                        missing_diagnostics=missing_diagnostics,
                        fresh_probe_incomplete=bool(state.fresh_probe_contract.get("incomplete")),
                    )
                if getattr(decision, "candidate_priors", None):
                    dynamic_priors = _candidate_priors_from_payload(decision.candidate_priors, node_id=str(getattr(node, "node_id", "node")), session_id=session_id)
                if getattr(decision, "fast_skip_suggestions", None):
                    fast_skip_suggestions = _fast_skip_suggestions_from_payload(decision.fast_skip_suggestions, node_id=str(getattr(node, "node_id", "node")), session_id=session_id)
                stop = decision.to_stop_decision()
                if not stop.stop_reason:
                    stop.stop_reason = "closed_loop_policy_stopped"
                break
            tool_name = decision.next_tool_name
            if not tool_name or tool_name not in allowed_by_name:
                stop = AnalysisStopDecision(False, "closed_loop_requested_disallowed_tool", confidence=1.0, reason=str(tool_name))
                break
            tool = allowed_by_name[tool_name]
            if not self._tool_allowed_by_budget(tool, budget, tool_calls=tool_calls):
                stop = AnalysisStopDecision(False, "closed_loop_tool_not_allowed_by_budget", confidence=1.0, reason=str(tool_name))
                break
            if self._fresh_probe_optional_limit_reached(tool_name, state.fresh_probe_contract):
                stop = AnalysisStopDecision(False, "fresh_probe_optional_probe_limit_reached", confidence=1.0, reason=str(tool_name))
                break
            args = self._tool_args(tool, node, analysis_contexts)
            if decision.next_tool_args:
                for key, value in decision.next_tool_args.items():
                    if key in {"tcl", "cmd", "command", "shell", "script"}:
                        continue
                    args[key] = value
            call = execute_tool(tool, args, tool_context, timeout_sec=getattr(budget, "default_tool_timeout_sec", None))
            tool_calls.append(call)
        return tool_calls, stop, loop_decisions, dynamic_priors, dynamic_priorities, fast_skip_suggestions

    @staticmethod
    def _tool_allowed_by_budget(tool: Any, budget: Any, tool_calls: Optional[List[Any]] = None) -> bool:
        tool_class = getattr(tool, "tool_class", "inspect")
        if tool_class == "report_probe" and not getattr(budget, "allow_report_probes", False):
            return False
        if tool_class == "act_probe" and not getattr(budget, "allow_act_probes", False):
            return False
        if bool(getattr(tool, "mutates_design", False)) and not getattr(budget, "allow_act_probes", False):
            return False
        if tool_class == "report_probe":
            stats = _report_probe_stats(tool_calls or [])
            max_calls = int(getattr(budget, "max_report_probe_calls", 0) or 0)
            max_seconds = float(getattr(budget, "max_report_probe_time_sec", 0.0) or 0.0)
            if max_calls > 0 and stats["calls"] >= max_calls:
                return False
            if max_seconds > 0.0 and stats["seconds"] >= max_seconds:
                return False
        if tool_class == "act_probe":
            stats = _act_probe_stats(tool_calls or [])
            max_calls = int(getattr(budget, "max_act_probe_calls", 0) or 0)
            max_seconds = float(getattr(budget, "max_act_probe_time_sec", 0.0) or 0.0)
            if max_calls <= 0:
                return False
            if stats["calls"] >= max_calls:
                return False
            if max_seconds > 0.0 and stats["seconds"] >= max_seconds:
                return False
        return True

    def _fresh_probe_contract(
        self,
        *,
        node: Any,
        trigger_reason: str,
        budget: Any,
        tool_calls: List[Any],
        allowed_by_name: Dict[str, Any],
    ) -> Dict[str, Any]:
        mode = self.fresh_probe_contract_mode
        backend_fresh = bool(getattr(self.report_probe_backend, "fresh_vivado_report", False))
        closed_loop = getattr(self.decision_policy, "name", "static_plan") != "static_plan"
        enabled = (
            mode != "off"
            and closed_loop
            and bool(getattr(budget, "allow_report_probes", False))
            and (backend_fresh or mode == "required")
        )
        required = _fresh_probe_required_tools(node=node, trigger_reason=trigger_reason)
        optional = [tool for tool in _FRESH_PROBE_OPTIONAL_TOOLS if tool in allowed_by_name]
        attempted = {
            name: _fresh_probe_attempted(name, tool_calls)
            for name in required
        }
        satisfied_tools = [
            name for name in required
            if _fresh_probe_satisfied(name, tool_calls)
        ]
        missing = [name for name in required if name not in satisfied_tools]
        stats = _report_probe_stats(tool_calls)
        max_report_calls = int(getattr(budget, "max_report_probe_calls", 0) or 0)
        max_report_seconds = float(getattr(budget, "max_report_probe_time_sec", 0.0) or 0.0)
        max_tool_calls = int(getattr(budget, "max_tool_calls", 0) or 0)
        budget_allows_probe = (
            (max_report_calls <= 0 or stats["calls"] < max_report_calls)
            and (max_report_seconds <= 0.0 or stats["seconds"] < max_report_seconds)
            and (max_tool_calls <= 0 or len(tool_calls) < max_tool_calls)
        )
        next_required = None
        for name in missing:
            if name in allowed_by_name and not attempted.get(name) and budget_allows_probe:
                next_required = name
                break
        satisfied = bool(enabled) and not missing
        incomplete = bool(enabled) and not satisfied and next_required is None
        stop_allowed = not enabled or satisfied or incomplete
        optional_after_quorum_used = _fresh_optional_after_quorum_used(tool_calls, required)
        unavailable = [name for name in missing if name not in allowed_by_name]
        failed = [name for name in missing if attempted.get(name)]
        incomplete_reason = ""
        if incomplete:
            if unavailable:
                incomplete_reason = "required_fresh_probe_tool_unavailable"
            elif failed:
                incomplete_reason = "required_fresh_probe_failed_or_timed_out"
            elif not budget_allows_probe:
                incomplete_reason = "fresh_probe_budget_exhausted"
            elif not backend_fresh:
                incomplete_reason = "backend_not_fresh"
            else:
                incomplete_reason = "fresh_probe_incomplete"
        return {
            "enabled": bool(enabled),
            "mode": mode,
            "backend": getattr(self.report_probe_backend, "name", type(self.report_probe_backend).__name__),
            "backend_fresh_vivado_report": backend_fresh,
            "required_tools": required,
            "optional_tools": optional,
            "satisfied": satisfied,
            "stop_allowed": stop_allowed,
            "incomplete": incomplete,
            "incomplete_reason": incomplete_reason,
            "next_required_tool": next_required,
            "missing_required_tools": missing,
            "satisfied_required_tools": satisfied_tools,
            "attempted_required_tools": attempted,
            "fresh_probe_calls_used": stats["calls"],
            "fresh_probe_calls_max": max_report_calls,
            "fresh_probe_seconds_used": round(float(stats["seconds"]), 6),
            "fresh_probe_seconds_max": max_report_seconds,
            "optional_after_quorum_used": optional_after_quorum_used,
            "optional_after_quorum_max": _FRESH_PROBE_OPTIONAL_AFTER_QUORUM_MAX,
        }

    def _fresh_probe_forced_tool(
        self,
        *,
        decision: Any,
        contract: Dict[str, Any],
        allowed_by_name: Dict[str, Any],
        budget: Any,
        tool_calls: List[Any],
    ) -> Optional[str]:
        if not contract.get("enabled") or contract.get("stop_allowed"):
            return None
        tool_name = contract.get("next_required_tool")
        if not tool_name or tool_name not in allowed_by_name:
            return None
        if not self._tool_allowed_by_budget(allowed_by_name[tool_name], budget, tool_calls=tool_calls):
            return None
        if decision.continue_analysis and decision.next_tool_name == tool_name:
            return None
        if not decision.continue_analysis or getattr(decision, "action", "stop") in {"stop", "emit_prior"}:
            return str(tool_name)
        return str(tool_name)

    @staticmethod
    def _fresh_probe_budget_stop(reason: str, contract: Dict[str, Any]) -> AnalysisStopDecision:
        stop = AnalysisStopDecision(False, reason, confidence=1.0)
        if contract.get("enabled") and not contract.get("satisfied"):
            stop.reason = "fresh_probe_incomplete"
        return stop

    @staticmethod
    def _fresh_probe_optional_limit_reached(tool_name: str, contract: Dict[str, Any]) -> bool:
        if not contract.get("enabled") or not contract.get("satisfied"):
            return False
        if tool_name not in _REPORT_PROBE_TOOLS:
            return False
        if tool_name in set(contract.get("required_tools") or []):
            return False
        return int(contract.get("optional_after_quorum_used") or 0) >= int(contract.get("optional_after_quorum_max") or 0)

    @staticmethod
    def _allow_final_fresh_probe_decision(
        stop: AnalysisStopDecision,
        contract: Dict[str, Any],
        loop_decisions: List[Any],
    ) -> bool:
        if stop.stop_reason == "hard_wall_time_budget_reached":
            return False
        if not contract.get("enabled") or not contract.get("satisfied") or contract.get("incomplete"):
            return False
        for decision in loop_decisions:
            prior = getattr(decision, "fresh_probe_contract", {}) or {}
            if prior.get("enabled") and prior.get("satisfied"):
                return False
        return True

    @staticmethod
    def _summarize_loop_evidence(tool_calls: List[Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"called_tools": [c.tool_name for c in tool_calls]}
        retrieval_by_flow: Dict[str, Any] = {}
        for call in tool_calls:
            output = call.output if isinstance(call.output, dict) else {}
            if call.tool_name == "query_retrieval_memory":
                flows = output.get("flows") if isinstance(output.get("flows"), dict) else {}
                retrieval_by_flow.update(flows)
            elif call.tool_name == "vivado_report_timing_summary":
                summary["timing"] = {**(output.get("timing") if isinstance(output.get("timing"), dict) else {}), **(output.get("parsed") if isinstance(output.get("parsed"), dict) else {})}
            elif call.tool_name == "vivado_report_route_status":
                summary["route_status"] = {**(output.get("route_status") if isinstance(output.get("route_status"), dict) else {}), **(output.get("parsed") if isinstance(output.get("parsed"), dict) else {})}
            elif call.tool_name == "vivado_report_utilization":
                summary["utilization"] = {**(output.get("utilization_summary") if isinstance(output.get("utilization_summary"), dict) else {}), **(output.get("parsed") if isinstance(output.get("parsed"), dict) else {})}
        if retrieval_by_flow:
            summary["retrieval_by_flow"] = retrieval_by_flow
        return summary

    def record_policy_candidates_feedback(self, rows: List[Dict[str, Any]], *, run_id: Optional[str] = None) -> None:
        if self.enabled:
            self.memory.rebuild_feedback_from_policy_candidates(rows, run_id=run_id)

    def _tool_plan_policy_tools(self, *, node: Any, trigger_reason: str, budget: Any) -> List[Any]:
        allowed_tools = list(self.tool_registry.allowed_tools(_ToolPolicyView(budget)))
        all_tools = {getattr(tool, "name", ""): tool for tool in allowed_tools}
        names = self.tool_plan_policy.plan_tools(node=node, trigger_reason=trigger_reason, budget=budget, available_tool_names=all_tools.keys())
        if self._fresh_probe_contract_tools_enabled(budget):
            for name in _fresh_probe_required_tools(node=node, trigger_reason=trigger_reason):
                if name in all_tools and name not in names:
                    names.append(name)
        return [all_tools[name] for name in names if name in all_tools]

    def _fresh_probe_contract_tools_enabled(self, budget: Any) -> bool:
        if self.fresh_probe_contract_mode == "off":
            return False
        if getattr(self.decision_policy, "name", "static_plan") == "static_plan":
            return False
        if not bool(getattr(budget, "allow_report_probes", False)):
            return False
        return bool(getattr(self.report_probe_backend, "fresh_vivado_report", False)) or self.fresh_probe_contract_mode == "required"

    def _tool_args(self, tool: Any, node: Any, analysis_contexts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        node_id = str(getattr(node, "node_id", ""))
        ctx = analysis_contexts.get(node_id, {})
        flows = _sanitize_candidate_flows(_flow_candidates_from_context(ctx), self.allowed_flows)
        return {"node_id": node_id, "flows": flows[:8], "k": 5}

    def _write_report(
        self,
        session_id: str,
        node_id: str,
        trigger_reason: str,
        diagnosis: Any,
        tool_calls: List[Any],
        stop: AnalysisStopDecision,
        elapsed: float,
        *,
        loop_decisions: Optional[List[Any]] = None,
        fast_skip_suggestions: Optional[List[Dict[str, Any]]] = None,
        analysis_run: Optional[AnalysisRunRecord] = None,
    ) -> Optional[Path]:
        if not self.enabled:
            return None
        report_dir = self.memory.analysis_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"{_safe_report_label(session_id)}.md"
        provenance = _evidence_provenance(tool_calls)
        lines = [
            f"# Online deep analysis: {session_id}",
            "",
            f"Node: `{node_id}`",
            f"Trigger: `{trigger_reason}`",
            f"Elapsed seconds: `{elapsed:.3f}`",
            f"Stop reason: `{stop.stop_reason}`",
            f"Tool loop mode: `{getattr(self.decision_policy, 'name', 'static_plan')}`",
            "",
            "## Evidence provenance",
        ]
        if provenance:
            for item in provenance:
                lines.append(f"- `{item.get('tool_name')}`: backend=`{item.get('probe_backend')}`, fresh_vivado_report=`{item.get('fresh_vivado_report')}`, mutates_design=`{item.get('mutates_design')}`, timed_out=`{item.get('timed_out')}`")
        else:
            lines.append("- No tool evidence recorded.")
        if loop_decisions:
            lines += ["", "## Closed-loop decisions"]
            for idx, decision in enumerate(loop_decisions):
                data = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
                lines.append(f"- #{idx}: action=`{data.get('action')}`, continue=`{data.get('continue_analysis')}`, next_tool=`{data.get('next_tool_name')}`, confidence=`{data.get('confidence')}`, stop_reason=`{data.get('stop_reason')}`")
            final_contract = loop_decisions[-1].fresh_probe_contract if hasattr(loop_decisions[-1], "fresh_probe_contract") else {}
            if final_contract:
                lines += [
                    "",
                    "## Fresh probe contract",
                    f"- Enabled: `{final_contract.get('enabled')}`",
                    f"- Satisfied: `{final_contract.get('satisfied')}`",
                    f"- Incomplete: `{final_contract.get('incomplete')}`",
                    f"- Required tools: `{', '.join(final_contract.get('required_tools') or [])}`",
                    f"- Forced overrides: `{len([d for d in loop_decisions if getattr(d, 'fresh_probe_override', False)])}`",
                ]
        if fast_skip_suggestions:
            lines += ["", "## Advisory fast-skip suggestions"]
            for item in fast_skip_suggestions:
                lines.append(f"- `{item.get('candidate_flow') or item.get('flow')}`: would_skip=`{item.get('would_skip')}`, confidence=`{item.get('confidence')}`; {item.get('reason')}")
        lines += ["", "## Diagnosis", f"- Bottleneck: `{diagnosis.bottleneck_class}`", f"- Confidence: `{diagnosis.bottleneck_confidence}`", f"- Recommended action: `{diagnosis.recommended_action}`", "", "## Candidate priors"]
        if diagnosis.candidate_priors:
            for prior in diagnosis.candidate_priors:
                lines.append(f"- `{prior.flow}`: priority_delta={prior.priority_delta}, risk_delta={prior.risk_delta}, confidence={prior.confidence}; {prior.reason}")
        else:
            lines.append("- No candidate priors generated.")
        lines += ["", "## Candidate priorities"]
        if getattr(diagnosis, "candidate_priorities", None):
            for priority in diagnosis.candidate_priorities:
                lines.append(f"- `{priority.flow}`: priority_score={priority.priority_score}, risk_score={priority.risk_score}, confidence={priority.confidence}, evidence_basis=`{getattr(priority, 'evidence_basis', '')}`, gain=`{getattr(priority, 'expected_gain_class', '')}`; {priority.reason}")
        else:
            lines.append("- No candidate priorities generated.")
        if analysis_run is not None:
            lines += ["", "## Analysis run", f"- ID: `{analysis_run.analysis_run_id}`", f"- Status: `{analysis_run.status}`", f"- Tool calls: `{analysis_run.tool_call_count}`", f"- Candidate priors: `{analysis_run.candidate_prior_count}`"]
        lines += ["", "## Tool calls"]
        for call in tool_calls:
            lines.append(f"- `{call.tool_name}` [{call.tool_class}] success={call.success} elapsed={call.elapsed_sec}s timeout={call.timeout_sec} timed_out={call.timed_out}")
            if call.error:
                lines.append(f"  - error: {call.error}")
        path.write_text("\n".join(lines) + "\n")
        return path

    def _no_improvement_window_hit(self, nodes: Dict[str, Any]) -> bool:
        completed = [n for n in nodes.values() if str(getattr(getattr(n, "status", None), "value", getattr(n, "status", ""))) == "COMPLETED" and getattr(n, "depth", 0) > 0]
        if len(completed) < 5:
            return False
        completed.sort(key=lambda n: _node_numeric_id(str(getattr(n, "node_id", ""))))
        recent = completed[-5:]
        best_before = max((getattr(n, "score", None) for n in completed[:-5] if getattr(n, "score", None) is not None), default=None)
        if best_before is None:
            return False
        return all((getattr(n, "score", float("-inf")) or float("-inf")) <= best_before for n in recent)

    def _frontier_disagreement_high(self, candidate_nodes: List[Any]) -> bool:
        return False

    def _config_summary(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "allowed_flows": self.allowed_flows,
            "trigger_policy": type(self.trigger_policy).__name__,
            "budget_policy": type(self.budget_policy).__name__,
            "stop_policy": type(self.stop_policy).__name__,
            "synthesizer": type(self.synthesizer).__name__,
            "tool_plan_policy": self.tool_plan_policy.to_dict() if hasattr(self.tool_plan_policy, "to_dict") else type(self.tool_plan_policy).__name__,
            "decision_policy": getattr(self.decision_policy, "name", type(self.decision_policy).__name__),
            "report_probe_backend": getattr(self.report_probe_backend, "name", type(self.report_probe_backend).__name__),
            "fresh_probe_contract_mode": self.fresh_probe_contract_mode,
            "tools": list(self.tool_registry.names()),
            "retrieval_index_enabled": self.retrieval_index is not None,
        }


class _ToolPolicyView:
    def __init__(self, budget: Any) -> None:
        self.allow_report_probes = bool(getattr(budget, "allow_report_probes", False))
        self.allow_act_probes = bool(getattr(budget, "allow_act_probes", False))


def _candidate_priors_from_payload(rows: List[Dict[str, Any]], *, node_id: str, session_id: str) -> List[CandidatePrior]:
    priors: List[CandidatePrior] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        flow = row.get("flow") or row.get("candidate_flow")
        if not flow:
            continue
        priors.append(CandidatePrior(
            node_id=node_id,
            flow=str(flow),
            priority_delta=_clamp_float(row.get("priority_delta"), -0.5, 0.5, 0.0),
            risk_delta=_clamp_float(row.get("risk_delta"), -0.5, 1.0, 0.0),
            confidence=_clamp_float(row.get("confidence"), 0.0, 1.0, 0.5),
            reason=str(row.get("reason") or "closed-loop analysis prior"),
            source_analysis_session_id=session_id,
        ))
    return priors


def _candidate_priorities_from_payload(
    rows: List[Dict[str, Any]],
    *,
    node_id: str,
    session_id: str,
    default_uncertainty: Optional[List[str]] = None,
    missing_diagnostics: Optional[List[str]] = None,
    fresh_probe_incomplete: bool = False,
) -> List[CandidatePriority]:
    priorities: List[CandidatePriority] = []
    missing = [str(item) for item in (missing_diagnostics or []) if item]
    default_remaining = _dedupe_strings(
        list(default_uncertainty or [])
        + [f"missing {item}" for item in missing]
        + (["fresh_probe_incomplete"] if fresh_probe_incomplete else [])
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        flow = row.get("flow") or row.get("candidate_flow")
        if not flow:
            continue
        evidence_basis = _evidence_basis(row.get("evidence_basis"))
        row_uncertainty = [str(item) for item in row.get("remaining_uncertainty", [])] if isinstance(row.get("remaining_uncertainty"), list) else []
        remaining_uncertainty = _dedupe_strings(row_uncertainty + default_remaining)
        confidence = _clamp_float(row.get("confidence"), 0.0, 1.0, 0.5)
        if fresh_probe_incomplete:
            confidence = min(confidence, _FRESH_PROBE_INCOMPLETE_CONFIDENCE_CAP)
        priorities.append(CandidatePriority(
            node_id=str(node_id),
            flow=str(flow),
            priority_score=_clamp_float(row.get("priority_score") if row.get("priority_score") is not None else row.get("candidate_priority_score"), 0.0, 1.0, 0.5),
            risk_score=_clamp_float(row.get("risk_score") if row.get("risk_score") is not None else row.get("risk_delta"), 0.0, 1.0, 0.0),
            confidence=confidence,
            expected_effect=str(row.get("expected_effect") or row.get("effect") or row.get("reason") or "closed-loop analysis priority"),
            evidence_refs=_normalize_evidence_refs(row.get("evidence_refs"), default_basis=evidence_basis, node_id=str(node_id), flow=str(flow), missing_diagnostics=missing),
            evidence_basis=evidence_basis,
            expected_gain_class=_expected_gain_class(row.get("expected_gain_class")),
            novelty_score=_optional_clamp_float(row.get("novelty_score"), 0.0, 1.0),
            contraindications=[str(item) for item in row.get("contraindications", [])] if isinstance(row.get("contraindications"), list) else [],
            why_now=str(row.get("why_now") or ""),
            do_not_repeat_if=str(row.get("do_not_repeat_if") or ""),
            reason=str(row.get("reason") or row.get("rationale") or "closed-loop analysis priority"),
            remaining_uncertainty=remaining_uncertainty,
            source_analysis_session_id=session_id,
        ))
    priorities.sort(key=lambda item: (item.priority_score, item.confidence, -item.risk_score), reverse=True)
    return priorities


def _fresh_probe_required_tools(*, node: Any, trigger_reason: str) -> List[str]:
    depth = int(getattr(node, "depth", 0) or 0)
    validation_status = str(getattr(node, "validation_status", "") or "").upper()
    trigger = str(trigger_reason or "").lower()
    needs_route = (
        depth == 0
        or validation_status in {"INVALID", "WARNING", "FALLBACK_USED", "REGRESSED"}
        or any(token in trigger for token in ("validation", "failure", "boundary"))
    )
    tools = ["vivado_report_timing_summary"]
    if needs_route:
        tools.append("vivado_report_route_status")
    return tools


def _fresh_probe_attempted(tool_name: str, tool_calls: List[Any]) -> bool:
    return any(call.tool_name == tool_name for call in tool_calls)


def _fresh_probe_satisfied(tool_name: str, tool_calls: List[Any]) -> bool:
    for call in tool_calls:
        if call.tool_name != tool_name:
            continue
        output = call.output if isinstance(call.output, dict) else {}
        if not call.success or call.timed_out:
            continue
        if not bool(output.get("fresh_vivado_report")):
            continue
        if output.get("available") is False:
            continue
        return True
    return False


def _fresh_optional_after_quorum_used(tool_calls: List[Any], required_tools: List[str]) -> int:
    required = set(required_tools)
    satisfied: set[str] = set()
    count = 0
    for call in tool_calls:
        if call.tool_name in required and _fresh_probe_satisfied(call.tool_name, [call]):
            satisfied.add(call.tool_name)
            continue
        if required.issubset(satisfied) and call.tool_name in _REPORT_PROBE_TOOLS and call.tool_name not in required:
            count += 1
    return count


def _report_probe_stats(tool_calls: List[Any]) -> Dict[str, Any]:
    calls = [call for call in tool_calls if getattr(call, "tool_class", "") == "report_probe" or getattr(call, "tool_name", "") in _REPORT_PROBE_TOOLS]
    return {
        "calls": len(calls),
        "seconds": sum(float(getattr(call, "elapsed_sec", 0.0) or 0.0) for call in calls),
    }


def _act_probe_stats(tool_calls: List[Any]) -> Dict[str, Any]:
    calls = [call for call in tool_calls if getattr(call, "tool_class", "") == "act_probe"]
    return {
        "calls": len(calls),
        "seconds": sum(float(getattr(call, "elapsed_sec", 0.0) or 0.0) for call in calls),
    }


def _normalize_evidence_refs(raw: Any, *, default_basis: str, node_id: str, flow: str, missing_diagnostics: List[str]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    values = raw if isinstance(raw, list) else []
    for item in values:
        parsed = item
        source = "llm"
        if isinstance(item, str):
            text = item.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    parsed = ast.literal_eval(text)
                    source = "legacy_unparsed" if not isinstance(parsed, dict) else "legacy_stringified_dict"
                except (SyntaxError, ValueError):
                    parsed = text
                    source = "legacy_unparsed"
            else:
                parsed = text
                source = "legacy"
        if isinstance(parsed, dict):
            basis = _evidence_basis(parsed.get("basis") or parsed.get("evidence_basis") or default_basis)
            ref = str(parsed.get("ref") or parsed.get("id") or parsed.get("observation") or parsed.get("motif") or parsed.get("hypothesis") or "").strip()
            if not ref:
                ref = "evidence.unspecified"
            out = {
                "basis": basis,
                "ref": ref,
                "source": str(parsed.get("source") or source),
            }
            if parsed.get("node_id") is not None:
                out["node_id"] = str(parsed.get("node_id"))
            elif node_id:
                out["node_id"] = node_id
            if parsed.get("flow") is not None:
                out["flow"] = str(parsed.get("flow"))
            elif flow:
                out["flow"] = flow
            for key in ("metric", "value"):
                if parsed.get(key) is not None:
                    out[key] = parsed.get(key)
            refs.append(out)
        elif parsed:
            refs.append({
                "basis": default_basis,
                "ref": str(parsed),
                "node_id": node_id,
                "flow": flow,
                "source": source,
            })
    for diagnostic in missing_diagnostics:
        refs.append({
            "basis": "missing_evidence",
            "ref": f"missing.{diagnostic}",
            "node_id": node_id,
            "flow": flow,
            "source": "analysis_context",
        })
    return _dedupe_evidence_refs(refs)


def _dedupe_evidence_refs(refs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for ref in refs:
        key = (ref.get("basis"), ref.get("ref"), ref.get("node_id"), ref.get("flow"), ref.get("source"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _missing_diagnostics_from_state_context(context: Dict[str, Any]) -> List[str]:
    summary = context.get("current_node_summary") if isinstance(context, dict) else {}
    missing = summary.get("missing_diagnostic_features") if isinstance(summary, dict) else []
    return [str(item) for item in missing] if isinstance(missing, list) else []


def _dedupe_strings(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _analysis_plan_from_diagnosis(
    diagnosis: Any,
    *,
    session_id: str,
    trigger_reason: str,
) -> NodeAnalysisPlan:
    return NodeAnalysisPlan(
        node_id=str(getattr(diagnosis, "node_id", "")),
        analysis_type=str(getattr(diagnosis, "analysis_type", trigger_reason) or trigger_reason),
        bottleneck_class=str(getattr(diagnosis, "bottleneck_class", "unknown") or "unknown"),
        bottleneck_confidence=_clamp_float(getattr(diagnosis, "bottleneck_confidence", 0.0), 0.0, 1.0, 0.0),
        evidence_summary=list(getattr(diagnosis, "evidence", []) or []),
        risks=list(getattr(diagnosis, "risks", []) or []),
        candidate_priorities=list(getattr(diagnosis, "candidate_priorities", []) or []),
        recommended_evidence_to_collect_next=[],
        remaining_uncertainty=list(getattr(diagnosis, "remaining_uncertainty", []) or []),
        source_analysis_session_id=session_id,
    )


def _fast_skip_suggestions_from_payload(rows: List[Dict[str, Any]], *, node_id: str, session_id: str) -> List[Dict[str, Any]]:
    suggestions: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        flow = row.get("flow") or row.get("candidate_flow")
        if not flow:
            continue
        suggestions.append({
            "node_id": node_id,
            "target_node_id": node_id,
            "candidate_flow": str(flow),
            "flow": str(flow),
            "would_skip": bool(row.get("would_skip") or row.get("skip")),
            "confidence": _clamp_float(row.get("confidence"), 0.0, 1.0, 0.0),
            "reason": str(row.get("reason") or "closed-loop advisory fast-skip suggestion"),
            "analysis_session_id": session_id,
            "mode": "advisory",
        })
    return suggestions


def _flow_candidates_from_context(ctx: Dict[str, Any]) -> List[str]:
    flows: List[str] = []
    candidate = ctx.get("candidate_flows") if isinstance(ctx.get("candidate_flows"), dict) else {}
    scores = candidate.get("deterministic_scores") or ctx.get("flow_scores") or []
    if isinstance(scores, list):
        for item in scores:
            if isinstance(item, dict) and item.get("flow"):
                flows.append(str(item["flow"]))
    return flows


def _sanitize_candidate_flows(context_flows: List[str], allowed_flows: List[str]) -> List[str]:
    allowed = [str(flow) for flow in allowed_flows if flow]
    allowed_set = set(allowed)
    seen = set()
    out: List[str] = []
    for flow in context_flows:
        flow = str(flow)
        if flow in allowed_set and flow not in seen:
            out.append(flow)
            seen.add(flow)
    for flow in allowed:
        if flow not in seen:
            out.append(flow)
            seen.add(flow)
    return out


def _analysis_provider_name(policy: Any) -> Optional[str]:
    provider = getattr(policy, "provider", None)
    if provider:
        return str(provider)
    name = str(getattr(policy, "name", "") or "").lower()
    if name.startswith("openai") or "openai" in name:
        return "openai"
    if name in {"static_plan", "mock_llm_closed_loop", "dynamic_llm_reserved"}:
        return None
    return name or None


def _evidence_provenance(tool_calls: List[Any]) -> List[Dict[str, Any]]:
    rows = []
    for call in tool_calls:
        output = call.output if isinstance(call.output, dict) else {}
        prov = output.get("evidence_provenance") if isinstance(output.get("evidence_provenance"), dict) else {}
        rows.append({
            "tool_name": call.tool_name,
            "tool_class": call.tool_class,
            "probe_backend": prov.get("probe_backend") or output.get("probe_backend"),
            "fresh_vivado_report": prov.get("fresh_vivado_report") if "fresh_vivado_report" in prov else output.get("fresh_vivado_report"),
            "mutates_design": prov.get("mutates_design") if "mutates_design" in prov else output.get("mutates_design", call.mutates_design),
            "timed_out": call.timed_out,
        })
    return rows


def _safe_report_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label).strip())
    return cleaned.strip("_") or "analysis"


def _clamp_float(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        val = float(value)
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


def _optional_clamp_float(value: Any, lo: float, hi: float) -> Optional[float]:
    if value is None or value == "":
        return None
    return _clamp_float(value, lo, hi, 0.0)


def _evidence_basis(value: Any) -> str:
    basis = str(value or "heuristic_prior").strip()
    allowed = {"current_observation", "trajectory_observation", "retrieved_motif", "heuristic_prior", "missing_evidence"}
    return basis if basis in allowed else "heuristic_prior"


def _expected_gain_class(value: Any) -> str:
    gain = str(value or "unknown").strip()
    allowed = {"low", "medium", "high", "unknown"}
    return gain if gain in allowed else "unknown"


def _node_numeric_id(node_id: str) -> int:
    try:
        return int(str(node_id).split("_")[1])
    except Exception:
        return 0
