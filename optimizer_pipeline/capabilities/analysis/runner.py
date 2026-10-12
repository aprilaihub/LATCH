"""Public deep-analysis runner for handwritten optimization pipelines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from optimizer_pipeline.capabilities.analysis.controller import OnlineAnalysisController
from optimizer_pipeline.capabilities.analysis.dynamic_loop import build_analysis_decision_policy
from optimizer_pipeline.capabilities.analysis.policies import (
    NaiveOnlineAnalysisTriggerPolicy,
    StaticAnalysisBudgetPolicy,
    TimePressureStopPolicy,
)
from optimizer_pipeline.capabilities.analysis.schemas import AnalysisSessionResult, AnalysisTriggerDecision
from optimizer_pipeline.capabilities.analysis.tool_plan_policies import build_analysis_tool_plan_policy

if TYPE_CHECKING:  # pragma: no cover
    from optimizer_pipeline.run.context import OptimizationRunContext


class AnalysisRunner:
    """Run diagnostic analysis for an existing DCP state node.

    The analysis layer is evidence-only: it may produce candidate priors,
    diagnoses and fast-skip suggestions, but it never creates child DCP states.
    """

    def __init__(self, ctx: "OptimizationRunContext") -> None:
        self.ctx = ctx
        self.controller: Optional[OnlineAnalysisController] = None
        self.decision_policy_factory: Any = None

    async def run_once(
        self,
        *,
        target_node: Any = None,
        mode: Optional[str] = None,
        trigger_reason: str = "manual_deep_analysis",
        budget_profile: Optional[str] = None,
        force_enable: bool = True,
    ) -> Optional[AnalysisSessionResult]:
        ctx = self.ctx
        if ctx.finalized:
            raise RuntimeError("Cannot run analysis after context.finalize()")
        node = target_node or ctx.current_node
        if not force_enable and not ctx.config.online_deep_analysis:
            return None
        controller = self._controller(mode=mode)
        profile = budget_profile or ("root" if getattr(node, "depth", 0) == 0 else "node")
        trigger = AnalysisTriggerDecision(True, getattr(node, "node_id", None), trigger_reason, priority=1.0, budget_profile=profile)
        budget = controller.budget_policy.budget_for(trigger)
        session = await controller.run_session(
            node=node,
            trigger_reason=trigger_reason,
            budget=budget,
            nodes=ctx.nodes,
            analysis_contexts=ctx.analysis_contexts,
        )
        controller.memory.apply_session(session)
        ctx.record_run_detail(
            "analysis_session",
            session.to_dict() if hasattr(session, "to_dict") else dict(session),
        )
        ctx.analysis_sessions.append(session)
        ctx.analysis_priors.extend([p.to_dict() if hasattr(p, "to_dict") else p for p in session.candidate_priors])
        ctx.candidate_priorities.extend([p.to_dict() if hasattr(p, "to_dict") else p for p in session.candidate_priorities])
        if session.analysis_plan:
            ctx.analysis_plans.append(session.analysis_plan.to_dict())
        ctx.fast_skip_suggestions.extend(list(session.fast_skip_suggestions or []))
        if session.diagnosis:
            ctx.analysis_contexts.setdefault(session.node_id, {}).setdefault("analysis_diagnoses", []).append(session.diagnosis.to_dict())
        ctx.budget.analysis_sessions += 1
        ctx.artifact_store.write_json(ctx.run_dir / "analysis_summary.json", {
            "sessions": [s.to_dict() if hasattr(s, "to_dict") else s for s in ctx.analysis_sessions],
            "analysis_priors": ctx.analysis_priors,
            "candidate_priorities": ctx.candidate_priorities,
            "analysis_plans": ctx.analysis_plans,
            "fast_skip_suggestions": ctx.fast_skip_suggestions,
        })
        ctx.persist_state()
        return session

    async def llm_once(self, **kwargs: Any) -> Optional[AnalysisSessionResult]:
        kwargs.setdefault("mode", "openai_llm_closed_loop")
        kwargs.setdefault("trigger_reason", "pipeline_llm_deep_analysis")
        return await self.run_once(**kwargs)

    def _controller(self, *, mode: Optional[str] = None) -> OnlineAnalysisController:
        if self.controller is not None and (mode is None or getattr(self.controller.decision_policy, "name", None) == mode):
            return self.controller
        ctx = self.ctx
        config = ctx.config
        allow_report = bool(config.analysis_allow_report_probes) and config.analysis_level in {"report_probe", "act_probe"}
        allow_act = bool(config.analysis_allow_act_probes) and config.analysis_level == "act_probe"
        tool_plan = build_analysis_tool_plan_policy(
            name=config.analysis_tool_plan_policy,
            root_tools=config.analysis_root_tools,
            keynode_tools=config.analysis_keynode_tools,
        )
        self.controller = OnlineAnalysisController(
            run_dir=ctx.run_dir,
            allowed_flows=list(config.allowed_flows),
            retrieval_index=None,
            trigger_policy=NaiveOnlineAnalysisTriggerPolicy(max_nodes_per_search=config.analysis_max_states_per_expansion),
            budget_policy=StaticAnalysisBudgetPolicy(
                root_budget_sec=config.analysis_root_budget_sec,
                node_budget_sec=config.analysis_state_budget_sec,
                allow_report_probes=allow_report,
                allow_act_probes=allow_act,
                max_act_probe_calls=int(getattr(config, "rescue_max_act_probes", 0) or 0) if allow_act else 0,
                max_act_probe_time_sec=float(getattr(config, "rescue_act_probe_wall_sec", 0.0) or 0.0) if allow_act else 0.0,
                default_tool_timeout_sec=config.analysis_tool_timeout_sec,
            ),
            stop_policy=TimePressureStopPolicy(
                soft_stop_frac=config.analysis_soft_stop_frac,
                strong_stop_frac=config.analysis_strong_stop_frac,
            ),
            tool_plan_policy=tool_plan,
            report_probe_backend=config.analysis_report_probe_backend,
            fresh_probe_contract_mode=config.analysis_fresh_probe_contract_mode,
            decision_policy=(
                self.decision_policy_factory(mode or config.analysis_tool_loop_mode, config)
                if self.decision_policy_factory is not None
                else build_analysis_decision_policy(mode or config.analysis_tool_loop_mode)
            ),
            config=config,
            enabled=True,
            persist_artifacts=ctx.run_store is None,
        )
        return self.controller
