"""Opt-in no-improvement rescue branch for contest/profile runs.

The rescue layer deliberately does not give an LLM direct Tcl or shell access.
Deep analysis may recommend an allowlisted rescue flow; this controller then
executes that flow through the normal transition executor, validator, QoR
scoring and publisher observer path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from optimizer_pipeline.domain.transition import DcpTransitionIntent, DcpTransitionRecord
from optimizer_pipeline.execution import DcpTransitionRequest
from optimizer_pipeline.capabilities.intervention.manual import (
    ManualActionSpec,
    ManualCheckpointState,
    ManualRescuePlan,
    ManualRescueSynthesisController,
    ManualSynthesisResult,
    advance_manual_design_state,
    manual_action_state_errors,
)
from optimizer_pipeline.models import DcpStateNode, NodeStatus, ValidationStatus, to_jsonable
from optimizer_pipeline.qor import fill_missing_qor_baseline, has_qor_baseline
from optimizer_pipeline.capabilities.intervention.recipe import RecipeSpec, RecipeSynthesisController
from optimizer_pipeline.capabilities.intervention.evidence import RescueEvidenceBuilder, RescuePlanner, diagnose_attempt


RESCUE_FLOW_PLACE_RESET_RETIME = "rescue_place_reset_retime"
RESCUE_FLOW_SYNTHESIZED_RECIPE = "synthesized_recipe"


@dataclass(frozen=True)
class RescueRecipe:
    recipe_id: str
    flow_name: str
    trigger: str
    risk_summary: str
    recipe_spec: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class RescueLoopResult:
    status: str
    stop_reason: str
    loop_policy: str
    parent_strategy: str
    attempts: List[Dict[str, Any]]
    attempt_diagnoses: List[Dict[str, Any]]
    best_before_rescue: Dict[str, Any]
    best_after_rescue: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


RESCUE_RECIPES: Tuple[RescueRecipe, ...] = (
    RescueRecipe(
        recipe_id="place_reset_retime_v1",
        flow_name=RESCUE_FLOW_PLACE_RESET_RETIME,
        trigger="no_improvement",
        risk_summary="Large placement-space reset; use only after the main pipeline has not improved Fmax.",
    ),
)


class ContextInterventionExecutor:
    """Execute guarded intervention recipes against the current run adapter."""

    def __init__(self, ctx: Any, recipes: Iterable[RescueRecipe] = RESCUE_RECIPES) -> None:
        self.ctx = ctx
        self.recipes = {recipe.flow_name: recipe for recipe in recipes}
        self.generator = getattr(getattr(ctx, "config", None), "intervention_generator", None)

    async def run(
        self,
        *,
        gate_decision: Optional[Tuple[bool, str]] = None,
        mode: Optional[str] = None,
        trigger: Optional[str] = None,
        max_runs: Optional[int] = None,
        min_main_delta_mhz: Optional[float] = None,
        mock_llm: bool = False,
    ) -> Dict[str, Any]:
        ctx = self.ctx
        cfg = ctx.config
        rescue_mode = str(mode or getattr(cfg, "rescue_mode", "off") or "off").strip().lower()
        rescue_trigger = str(trigger or getattr(cfg, "rescue_trigger", "no_improvement") or "no_improvement").strip().lower()
        run_limit = max(0, int(max_runs if max_runs is not None else getattr(cfg, "rescue_max_runs", 1)))
        threshold = max(0.0, float(min_main_delta_mhz if min_main_delta_mhz is not None else getattr(cfg, "rescue_min_main_delta_mhz", 0.1)))
        synthesis_fallback = str(getattr(cfg, "rescue_synthesis_fallback", "deterministic") or "deterministic").strip().lower()

        decision: Dict[str, Any] = {
            "schema_version": "rescue_decision.v1",
            "mode": rescue_mode,
            "trigger": rescue_trigger,
            "max_runs": run_limit,
            "min_main_delta_mhz": threshold,
            "synthesis_max_steps": max(1, int(getattr(cfg, "rescue_synthesis_max_steps", 8) or 8)),
            "synthesis_fallback": synthesis_fallback,
            "catalog": [recipe.to_dict() for recipe in self.recipes.values()],
            "status": "skipped",
            "reason": None,
            "main_profile": self._main_profile_summary(),
            "selected_recipe": None,
            "selection_source": None,
            "analysis_session": None,
            "fallback_used": False,
            "transition_records": [],
        }

        if rescue_mode == "off":
            decision["reason"] = "rescue_mode_off"
            return self._write_decision(decision)
        if ctx.finalized:
            decision["reason"] = "run_already_finalized"
            return self._write_decision(decision)
        if rescue_trigger not in {"no_improvement", "always"}:
            decision["reason"] = "unsupported_trigger"
            return self._write_decision(decision)
        if run_limit <= 0:
            decision["reason"] = "rescue_max_runs_zero"
            return self._write_decision(decision)

        if gate_decision is None:
            raise ValueError("intervention execution requires a strategy gate decision")
        gate_ok, gate_reason = bool(gate_decision[0]), str(gate_decision[1])
        decision["gate"] = {"passed": gate_ok, "reason": gate_reason}
        if not gate_ok:
            decision["reason"] = gate_reason
            return self._write_decision(decision)
        if ctx.budget.remaining_eda_runs <= 0:
            decision["reason"] = "eda_budget_exhausted"
            return self._write_decision(decision)

        if rescue_mode == "synthesis_loop":
            loop_result = await self.run_synthesis_loop(
                max_runs=run_limit,
                loop_policy=str(getattr(cfg, "rescue_loop_policy", "fixed_attempts") or "fixed_attempts"),
                parent_strategy=str(getattr(cfg, "rescue_parent_strategy", "current_best") or "current_best"),
                mock_llm=mock_llm,
                write_decision=False,
            )
            decision.update(loop_result.to_dict())
            decision["status"] = loop_result.status
            decision["reason"] = loop_result.stop_reason
            return self._write_decision(decision)

        if rescue_mode == "manual_loop":
            loop_result = await self.run_manual_loop(
                max_actions=run_limit,
                max_episodes=max(1, int(getattr(cfg, "rescue_max_episodes", 1) or 1)),
                mock_llm=mock_llm,
                write_decision=False,
            )
            decision.update(loop_result.to_dict())
            decision["status"] = loop_result.status
            decision["reason"] = loop_result.stop_reason
            return self._write_decision(decision)

        recipe, selection_source, analysis_session, selection_detail = await self._select_recipe(
            mode=rescue_mode,
            mock_llm=mock_llm,
        )
        decision["selection_source"] = selection_source
        decision["analysis_session"] = analysis_session
        decision["selection_detail"] = selection_detail
        allow_fallback = rescue_mode != "synthesis" or synthesis_fallback == "deterministic"
        if recipe is None and allow_fallback:
            recipe = self._default_recipe()
            decision["fallback_used"] = True
            selection_source = "deterministic_fallback"
            decision["selection_source"] = selection_source
            decision["selected_recipe"] = recipe.to_dict() if recipe else None
        else:
            decision["selected_recipe"] = recipe.to_dict() if recipe else None
        if recipe is None:
            decision["reason"] = "no_rescue_recipe_available"
            return self._write_decision(decision)

        completed_records = []
        for _ in range(min(run_limit, 1)):
            if ctx.budget.remaining_eda_runs <= 0:
                decision["reason"] = "eda_budget_exhausted_after_selection"
                break
            completed = await self._execute_recipe(recipe, selection_source=selection_source)
            completed_records.append(completed.to_dict())
            decision["transition_records"].append(completed.to_dict())
        decision["status"] = "executed" if completed_records else "skipped"
        decision["reason"] = decision["reason"] or ("executed" if completed_records else "no_transition_executed")
        decision["post_rescue"] = self._main_profile_summary()
        return self._write_decision(decision)

    async def _select_recipe(self, *, mode: str, mock_llm: bool) -> Tuple[Optional[RescueRecipe], str, Optional[Dict[str, Any]], Dict[str, Any]]:
        if mode == "deterministic":
            return self._default_recipe(), "deterministic", None, {"reason": "deterministic_rescue_mode"}
        if mode == "synthesis":
            if str(getattr(self.ctx.config, "rescue_evidence_mode", "compact") or "compact").strip().lower() == "planner":
                builder = RescueEvidenceBuilder(self.ctx)
                evidence = builder.build(attempts=[], attempt_diagnoses=[])
                plan = RescuePlanner().plan(
                    evidence=evidence,
                    attempts=[],
                    attempt_diagnoses=[],
                    max_recipe_steps=max(1, int(getattr(self.ctx.config, "rescue_synthesis_max_steps", 8) or 8)),
                )
                context = {
                    "trigger": str(getattr(self.ctx.config, "rescue_trigger", "no_improvement")),
                    "main_profile": self._main_profile_summary(),
                    "root_dcp": str(self.ctx.root_dcp),
                    "current_best_node": getattr(self._best_node(), "node_id", None),
                    "rescue_evidence": evidence,
                    "rescue_attempt_plan": plan.to_dict(),
                    "attempt_diagnoses": [],
                    "known_public_prior": evidence.get("known_public_prior", {}),
                    "avoid_repeating": [],
                    "information_gaps": evidence.get("recommended_information_gaps", []),
                    "allowed_primitives_note": "Use only primitive_catalog names; no free Tcl or shell.",
                }
                return self._select_synthesized_recipe(mock_llm=mock_llm, context=context)
            return self._select_synthesized_recipe(mock_llm=mock_llm)
        if mode != "llm":
            return None, "unsupported_mode", None, {"reason": f"unsupported rescue mode: {mode}"}

        session = await self._run_llm_analysis(mock_llm=mock_llm)
        session_dict = session.to_dict() if hasattr(session, "to_dict") else session if isinstance(session, dict) else None
        recipe, detail = self._recipe_from_analysis_session(session)
        if recipe is not None:
            return recipe, "analysis_llm_candidate_priority", session_dict, detail
        return None, "analysis_llm_no_valid_recipe", session_dict, detail

    def _select_synthesized_recipe(
        self,
        *,
        mock_llm: bool,
        context: Optional[Dict[str, Any]] = None,
        audit_name: str = "rescue_synthesis_audit.json",
    ) -> Tuple[Optional[RescueRecipe], str, Optional[Dict[str, Any]], Dict[str, Any]]:
        cfg = self.ctx.config
        controller = RecipeSynthesisController(
            config=cfg,
            max_steps=max(1, int(getattr(cfg, "rescue_synthesis_max_steps", 8) or 8)),
            fallback_policy=str(getattr(cfg, "rescue_synthesis_fallback", "deterministic") or "deterministic"),
            generator=self.generator,
        )
        synthesis_context = context or {
            "trigger": str(getattr(cfg, "rescue_trigger", "no_improvement")),
            "main_profile": self._main_profile_summary(),
            "root_dcp": str(self.ctx.root_dcp),
            "current_best_node": getattr(self._best_node(), "node_id", None),
            "allowed_primitives_note": "Use only primitive_catalog names; no free Tcl or shell.",
        }
        result = controller.synthesize_mock(context=synthesis_context) if mock_llm else controller.synthesize(context=synthesis_context)
        audit_path = self.ctx.run_dir / audit_name
        self.ctx.artifact_store.write_json(audit_path, result.to_dict())
        if audit_name != "rescue_synthesis_audit.json":
            self.ctx.artifact_store.write_json(self.ctx.run_dir / "rescue_synthesis_audit.json", result.to_dict())
        detail = {
            "reason": "synthesis_recipe_selected" if result.recipe is not None else "synthesis_recipe_invalid",
            "audit_path": str(audit_path),
            "synthesis_result": result.to_dict(),
        }
        if result.recipe is None:
            return None, "synthesis_no_valid_recipe", None, detail
        recipe = RescueRecipe(
            recipe_id=result.recipe.recipe_id,
            flow_name=RESCUE_FLOW_SYNTHESIZED_RECIPE,
            trigger=str(getattr(cfg, "rescue_trigger", "always") or "always"),
            risk_summary=result.recipe.summary,
            recipe_spec=result.recipe.to_dict(),
        )
        return recipe, "synthesis_recipe", None, detail

    async def run_synthesis_loop(
        self,
        *,
        max_runs: Optional[int] = None,
        loop_policy: Optional[str] = None,
        parent_strategy: Optional[str] = None,
        mock_llm: bool = False,
        parent_sequence: Optional[Sequence[str]] = None,
        write_decision: bool = True,
    ) -> RescueLoopResult:
        """Run multiple guarded synthesis attempts without embedding ROI policy."""
        ctx = self.ctx
        cfg = ctx.config
        run_limit = max(0, int(max_runs if max_runs is not None else getattr(cfg, "rescue_max_runs", 1)))
        policy = str(loop_policy or getattr(cfg, "rescue_loop_policy", "fixed_attempts") or "fixed_attempts").strip().lower()
        if policy not in {"fixed_attempts", "stop_after_first_improvement", "external"}:
            policy = "fixed_attempts"
        strategy = str(parent_strategy or getattr(cfg, "rescue_parent_strategy", "current_best") or "current_best").strip().lower()
        if strategy not in {"current_best", "root", "round_robin_safe_parents"}:
            strategy = "current_best"

        best_before = self._main_profile_summary()
        attempts: List[Dict[str, Any]] = []
        attempt_diagnoses: List[Dict[str, Any]] = []
        started = time.time()
        stop_reason = "max_runs_reached"
        evidence_mode = str(getattr(cfg, "rescue_evidence_mode", "planner") or "planner").strip().lower()
        if evidence_mode not in {"compact", "planner"}:
            evidence_mode = "planner"
        evidence_builder = RescueEvidenceBuilder(ctx)
        planner = RescuePlanner()

        if run_limit <= 0:
            stop_reason = "rescue_max_runs_zero"
        elif policy == "external" and not parent_sequence:
            stop_reason = "external_policy_requires_parent_sequence"

        for index in range(run_limit):
            if stop_reason != "max_runs_reached":
                break
            if ctx.budget.remaining_eda_runs <= 0:
                stop_reason = "eda_budget_exhausted"
                break

            pre_parent_evidence = evidence_builder.build(attempts=attempts, attempt_diagnoses=attempt_diagnoses)
            plan = None
            parent_selector = None
            if evidence_mode == "planner":
                plan = planner.plan(
                    evidence=pre_parent_evidence,
                    attempts=attempts,
                    attempt_diagnoses=attempt_diagnoses,
                    max_recipe_steps=max(1, int(getattr(cfg, "rescue_synthesis_max_steps", 8) or 8)),
                )
                parent_selector = plan.parent_selector
            parent, parent_detail = self._select_loop_parent(
                attempt_index=index,
                strategy=strategy,
                attempts=attempts,
                parent_sequence=parent_sequence,
                parent_selector=parent_selector,
            )
            if parent is None:
                stop_reason = "no_safe_parent_available"
                break

            evidence = evidence_builder.build(
                attempts=attempts,
                attempt_diagnoses=attempt_diagnoses,
                selected_parent=parent,
            )
            evidence_path = self.ctx.run_dir / f"rescue_evidence_{index:03d}.json"
            self.ctx.artifact_store.write_json(evidence_path, evidence)
            plan_path = None
            if plan is not None:
                plan_path = self.ctx.run_dir / f"rescue_attempt_plan_{index:03d}.json"
                self.ctx.artifact_store.write_json(plan_path, plan.to_dict())
            self._publish_rescue_analysis_context(
                attempts=attempts,
                attempt_diagnoses=attempt_diagnoses,
                evidence=evidence,
                plan=plan.to_dict() if plan else None,
            )
            analysis_session = await self._run_loop_analysis(parent, index=index, mock_llm=mock_llm)
            synthesis_context = self._synthesis_loop_context(
                loop_index=index,
                parent=parent,
                attempts=attempts,
                attempt_diagnoses=attempt_diagnoses,
                analysis_session=analysis_session,
                started=started,
                parent_detail=parent_detail,
                evidence=evidence,
                plan=plan.to_dict() if plan else None,
            )
            recipe, selection_source, _analysis_session_unused, selection_detail = self._select_synthesized_recipe(
                mock_llm=mock_llm,
                context=synthesis_context,
                audit_name=f"rescue_synthesis_audit_{index:03d}.json",
            )
            fallback_used = False
            if recipe is None and str(getattr(cfg, "rescue_synthesis_fallback", "deterministic") or "deterministic").strip().lower() == "deterministic":
                recipe = self._default_recipe()
                selection_source = "deterministic_fallback"
                fallback_used = True

            attempt: Dict[str, Any] = {
                "schema_version": "rescue_attempt.v1",
                "attempt_index": index,
                "loop_policy": policy,
                "parent_strategy": strategy,
                "evidence_mode": evidence_mode,
                "rescue_evidence_path": str(evidence_path),
                "rescue_attempt_plan_path": str(plan_path) if plan_path else None,
                "rescue_attempt_plan": plan.to_dict() if plan else None,
                "parent_selection": parent_detail,
                "selected_parent": self._node_summary(parent),
                "selection_source": selection_source,
                "selection_detail": selection_detail,
                "fallback_used": fallback_used,
                "analysis_session": self._session_to_dict(analysis_session),
                "recipe": recipe.to_dict() if recipe else None,
                "status": "selected" if recipe else "no_recipe",
            }

            if recipe is None:
                attempt["stop_reason"] = "no_rescue_recipe_available"
                diagnosis = diagnose_attempt(attempt).to_dict()
                attempt["diagnosis"] = diagnosis
                attempt_diagnoses.append(diagnosis)
                diagnosis_path = self.ctx.run_dir / f"rescue_attempt_diagnosis_{index:03d}.json"
                attempt["rescue_attempt_diagnosis_path"] = str(diagnosis_path)
                self.ctx.artifact_store.write_json(diagnosis_path, diagnosis)
                attempts.append(attempt)
                self.ctx.artifact_store.write_json(self.ctx.run_dir / f"rescue_attempt_{index:03d}.json", attempt)
                stop_reason = "no_rescue_recipe_available"
                break

            completed = await self._execute_recipe(
                recipe,
                selection_source=selection_source,
                parent_node=parent,
                source="rescue_synthesis_loop",
                attempt_index=index,
            )
            attempt.update(self._attempt_result_from_record(completed, parent=parent))
            diagnosis = diagnose_attempt(attempt).to_dict()
            attempt["diagnosis"] = diagnosis
            attempt_diagnoses.append(diagnosis)
            diagnosis_path = self.ctx.run_dir / f"rescue_attempt_diagnosis_{index:03d}.json"
            attempt["rescue_attempt_diagnosis_path"] = str(diagnosis_path)
            self.ctx.artifact_store.write_json(diagnosis_path, diagnosis)
            attempts.append(attempt)
            self.ctx.artifact_store.write_json(self.ctx.run_dir / f"rescue_attempt_{index:03d}.json", attempt)

            if policy == "stop_after_first_improvement" and attempt.get("updated_incumbent"):
                stop_reason = "updated_incumbent"
                break
            if policy == "external" and parent_sequence and index + 1 >= len(parent_sequence):
                stop_reason = "external_parent_sequence_exhausted"
                break

        status = "executed" if any(attempt.get("transition_record") for attempt in attempts) else "skipped"
        result = RescueLoopResult(
            status=status,
            stop_reason=stop_reason,
            loop_policy=policy,
            parent_strategy=strategy,
            attempts=attempts,
            attempt_diagnoses=attempt_diagnoses,
            best_before_rescue=best_before,
            best_after_rescue=self._main_profile_summary(),
        )
        if write_decision:
            self._write_decision({
                "schema_version": "rescue_decision.v1",
                "mode": "synthesis_loop",
                **result.to_dict(),
            })
        return result

    async def run_manual_loop(
        self,
        *,
        max_actions: Optional[int] = None,
        max_episodes: int = 1,
        mock_llm: bool = False,
        write_decision: bool = True,
    ) -> RescueLoopResult:
        """Run stepwise manual rescue actions without exposing optimizer flows."""
        cfg = self.ctx.config
        action_limit = max(0, int(max_actions if max_actions is not None else getattr(cfg, "rescue_max_runs", 1)))
        episode_limit = max(1, int(max_episodes))
        controller = ManualRescueSynthesisController(
            config=cfg,
            initial_cost_usd=self._existing_llm_cost(),
            generator=self.generator,
        )
        evidence_builder = RescueEvidenceBuilder(self.ctx)
        started = time.time()
        best_before = self._main_profile_summary()
        attempts: List[Dict[str, Any]] = []
        diagnoses: List[Dict[str, Any]] = []
        stop_reason = "max_actions_reached"
        action_count = 0
        previous_plan: Optional[ManualRescuePlan] = None

        for episode_index in range(episode_limit):
            if action_count >= action_limit:
                break
            episode_attempts: List[Dict[str, Any]] = []
            while action_count < action_limit:
                parent, parent_detail = self._select_manual_parent(attempts)
                if parent is None:
                    stop_reason = "no_manual_parent_available"
                    break
                evidence = evidence_builder.build(attempts=attempts, attempt_diagnoses=diagnoses, selected_parent=parent)
                manual_state = self._manual_design_state(parent)
                plan_context = self._manual_loop_context(
                    episode_index=episode_index,
                    action_index=action_count,
                    parent=parent,
                    attempts=attempts,
                    attempt_diagnoses=diagnoses,
                    evidence=evidence,
                    parent_detail=parent_detail,
                    plan=previous_plan,
                    manual_design_state=manual_state,
                    started=started,
                )
                plan_result = None
                plan_path = self._manual_plan_path(episode_index=episode_index, action_index=action_count)
                plan_state_errors: List[str] = []
                for plan_retry in range(3):
                    retry_context = dict(plan_context)
                    if plan_state_errors:
                        retry_context["plan_state_feedback"] = {
                            "retry_index": plan_retry,
                            "errors": list(plan_state_errors),
                            "instruction": "Revise the first action for the current manual_design_state; do not repeat the rejected action.",
                        }
                    plan_result = controller.synthesize_plan_mock(context=retry_context) if mock_llm else controller.synthesize_plan(context=retry_context)
                    plan_path = self._manual_plan_path(
                        episode_index=episode_index,
                        action_index=action_count,
                        retry_index=plan_retry,
                    )
                    self.ctx.artifact_store.write_json(plan_path, plan_result.to_dict())
                    if plan_result.plan is None:
                        break
                    first_action = plan_result.plan.steps[0]
                    plan_state_errors = manual_action_state_errors(first_action, manual_state)
                    if not plan_state_errors:
                        break
                assert plan_result is not None
                if plan_result.plan is None:
                    stop_reason = "llm_cost_budget_reached" if plan_result.audit.get("error") == "llm_cost_budget_reached" else "no_valid_manual_plan"
                    attempts.append(
                        {
                            "schema_version": "manual_action_attempt.v1",
                            "episode_index": episode_index,
                            "attempt_index": action_count,
                            "action_index": action_count,
                            "status": "no_plan",
                            "manual_rescue_plan_path": str(plan_path),
                            "plan_validation": plan_result.validation.to_dict(),
                        }
                    )
                    break
                if plan_state_errors:
                    stop_reason = "manual_plan_state_precondition_rejected"
                    attempts.append(
                        {
                            "schema_version": "manual_action_attempt.v1",
                            "episode_index": episode_index,
                            "attempt_index": action_count,
                            "action_index": action_count,
                            "status": "plan_state_precondition_rejected",
                            "manual_rescue_plan_path": str(plan_path),
                            "manual_design_state": manual_state,
                            "manual_plan": plan_result.plan.to_dict(),
                            "plan_state_precondition_errors": plan_state_errors,
                        }
                    )
                    break
                previous_plan = plan_result.plan
                selected_action = previous_plan.steps[0]
                action_validation = controller.validator.validate_action(selected_action.to_dict(), source="manual_plan_first_step")
                action_result = ManualSynthesisResult(
                    action=action_validation.action,
                    validation=action_validation,
                    audit={
                        "schema_version": "manual_rescue_action_selection.v1",
                        "mode": "plan_first_step",
                        "source_plan_path": str(plan_path),
                        "usage": controller.usage_summary(),
                    },
                )
                action_path = self.ctx.run_dir / f"manual_action_{episode_index:03d}_{action_count:03d}.json"
                self.ctx.artifact_store.write_json(action_path, action_result.to_dict())
                attempt: Dict[str, Any] = {
                    "schema_version": "manual_action_attempt.v1",
                    "episode_index": episode_index,
                    "attempt_index": action_count,
                    "action_index": action_count,
                    "step_index": 0,
                    "manual_rescue_plan_path": str(plan_path),
                    "manual_action_path": str(action_path),
                    "selected_parent": self._node_summary(parent),
                    "parent_selection": parent_detail,
                    "manual_plan": previous_plan.to_dict(),
                    "manual_design_state": manual_state,
                    "manual_action": action_result.action.to_dict() if action_result.action else None,
                    "status": "selected" if action_result.action else "no_action",
                    "selection_detail": action_result.to_dict(),
                }
                if action_result.action is None:
                    attempt["stop_reason"] = "no_valid_manual_action"
                    diagnosis = diagnose_attempt(attempt).to_dict()
                    attempt["diagnosis"] = diagnosis
                    diagnoses.append(diagnosis)
                    self.ctx.artifact_store.write_json(self.ctx.run_dir / f"manual_action_diagnosis_{episode_index:03d}_{action_count:03d}.json", diagnosis)
                    self.ctx.artifact_store.write_json(self.ctx.run_dir / f"manual_action_result_{episode_index:03d}_{action_count:03d}.json", attempt)
                    attempts.append(attempt)
                    stop_reason = "no_valid_manual_action"
                    break

                state_errors = manual_action_state_errors(action_result.action, manual_state)
                if state_errors:
                    attempt["status"] = "state_precondition_rejected"
                    attempt["state_precondition_errors"] = state_errors
                    diagnosis = diagnose_attempt(attempt).to_dict()
                    attempt["diagnosis"] = diagnosis
                    diagnoses.append(diagnosis)
                    self.ctx.artifact_store.write_json(self.ctx.run_dir / f"manual_action_diagnosis_{episode_index:03d}_{action_count:03d}.json", diagnosis)
                    self.ctx.artifact_store.write_json(self.ctx.run_dir / f"manual_action_result_{episode_index:03d}_{action_count:03d}.json", attempt)
                    attempts.append(attempt)
                    episode_attempts.append(attempt)
                    stop_reason = "state_precondition_rejected"
                    break

                completed = await self._execute_manual_action(
                    action_result.action,
                    parent_node=parent,
                    parent_manual_state=manual_state,
                    episode_index=episode_index,
                    action_index=action_count,
                )
                attempt.update(self._attempt_result_from_record(completed, parent=parent))
                attempt["status"] = "executed"
                attempt["non_publishable_scratch"] = not bool(attempt.get("publish_eligible"))
                diagnosis = diagnose_attempt(attempt).to_dict()
                attempt["diagnosis"] = diagnosis
                diagnoses.append(diagnosis)
                self.ctx.artifact_store.write_json(self.ctx.run_dir / f"manual_action_diagnosis_{episode_index:03d}_{action_count:03d}.json", diagnosis)
                self.ctx.artifact_store.write_json(self.ctx.run_dir / f"manual_action_result_{episode_index:03d}_{action_count:03d}.json", attempt)
                attempts.append(attempt)
                episode_attempts.append(attempt)
                action_count += 1

            episode_path = self.ctx.run_dir / f"manual_episode_{episode_index:03d}.json"
            self.ctx.artifact_store.write_json(
                episode_path,
                {
                    "schema_version": "manual_rescue_episode.v1",
                    "episode_index": episode_index,
                    "last_plan": previous_plan.to_dict() if previous_plan else None,
                    "attempts": episode_attempts,
                },
            )
            if stop_reason in {
                "no_manual_parent_available",
                "no_valid_manual_action",
                "no_valid_manual_plan",
                "state_precondition_rejected",
                "manual_plan_state_precondition_rejected",
            }:
                break

        status = "executed" if any(attempt.get("transition_record") for attempt in attempts) else "skipped"
        self._record_manual_usage(controller)
        result = RescueLoopResult(
            status=status,
            stop_reason=stop_reason,
            loop_policy="manual_loop",
            parent_strategy="manual_scratch_then_best",
            attempts=attempts,
            attempt_diagnoses=diagnoses,
            best_before_rescue=best_before,
            best_after_rescue=self._main_profile_summary(),
        )
        if write_decision:
            self._write_decision({
                "schema_version": "rescue_decision.v1",
                "mode": "manual_loop",
                **result.to_dict(),
            })
        self.ctx.artifact_store.write_json(
            self.ctx.run_dir / "manual_rescue_decision.json",
            {"schema_version": "manual_rescue_decision.v1", "mode": "manual_loop", **result.to_dict()},
        )
        return result

    async def _run_llm_analysis(self, *, mock_llm: bool) -> Any:
        ctx = self.ctx
        root = ctx.nodes.get("node_000_root") or ctx.current_node
        self._ensure_rescue_flow_allowed()
        mode = "mock_llm_closed_loop" if mock_llm else "openai_llm_closed_loop"
        return await ctx.analysis.run_once(
            target_node=root,
            mode=mode,
            trigger_reason="rescue_no_improvement_recipe_selection",
            budget_profile="root",
            force_enable=True,
        )

    async def _run_loop_analysis(self, parent: Any, *, index: int, mock_llm: bool) -> Any:
        ctx = self.ctx
        if getattr(ctx, "analysis", None) is None:
            return None
        self._ensure_rescue_flow_allowed()
        mode = "mock_llm_closed_loop" if mock_llm else "openai_llm_closed_loop"
        try:
            if bool(getattr(ctx.config, "rescue_enable_act_probes", False)) and getattr(ctx.analysis, "controller", None) is not None:
                # Rebuild the controller so act-probe flags/budgets set by rescue config are reflected.
                ctx.analysis.controller = None
            return await ctx.analysis.run_once(
                target_node=parent,
                mode=mode,
                trigger_reason=f"rescue_synthesis_loop_attempt_{index:03d}",
                budget_profile="root" if getattr(parent, "depth", 0) == 0 else "node",
                force_enable=True,
            )
        except Exception as exc:
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "target_node_id": getattr(parent, "node_id", None),
            }

    def _recipe_from_analysis_session(self, session: Any) -> Tuple[Optional[RescueRecipe], Dict[str, Any]]:
        if session is None:
            return None, {"reason": "analysis_session_missing"}
        priority_rows: List[Dict[str, Any]] = []
        prior_rows: List[Dict[str, Any]] = []
        for attr, bucket in (("candidate_priorities", priority_rows), ("candidate_priors", prior_rows)):
            for item in getattr(session, attr, []) or []:
                row = item.to_dict() if hasattr(item, "to_dict") else dict(item) if isinstance(item, dict) else {}
                if row:
                    row["_source"] = attr
                    bucket.append(row)
        priority_rows.sort(key=lambda row: (_safe_float(row.get("priority_score"), _safe_float(row.get("confidence"), 0.0)), -_safe_float(row.get("risk_score"), 0.0)), reverse=True)
        rejected: List[Dict[str, Any]] = []
        for row in priority_rows:
            flow = str(row.get("flow") or row.get("candidate_flow") or "").strip()
            recipe = self.recipes.get(flow)
            if recipe is not None:
                return recipe, {
                    "selected_row": row,
                    "rejected_rows": rejected,
                    "candidate_prior_rows": prior_rows,
                }
            if flow:
                rejected.append({"flow": flow, "source": row.get("_source"), "reason": "not_in_rescue_catalog"})
        return None, {
            "reason": "no_allowlisted_rescue_flow_in_candidate_priorities",
            "rejected_rows": rejected,
            "candidate_prior_rows": prior_rows,
        }

    def _select_manual_parent(self, attempts: Sequence[Dict[str, Any]]) -> Tuple[Optional[Any], Dict[str, Any]]:
        allow_invalid = bool(getattr(self.ctx.config, "rescue_allow_invalid_scratch", True))
        selector = str(getattr(self.ctx.config, "rescue_manual_parent_selector", "last_scratch") or "last_scratch").strip().lower()
        if selector == "last_scratch":
            if attempts and allow_invalid:
                child_id = _attempt_node_id(attempts[-1], "child_node_id")
                child = self.ctx.nodes.get(child_id) if child_id else None
                validation = str(getattr(child, "validation_status", "") or "").upper() if child is not None else ""
                output_dcp = getattr(child, "output_dcp", None) if child is not None else None
                if validation == "INVALID" and output_dcp and Path(output_dcp).exists():
                    return child, {
                        "strategy": "last_invalid_scratch_checkpoint",
                        "reason": "continue_from_invalid_scratch",
                        "parent_use_mode": "EXPLICIT_INTERVENTION",
                        "selected_node_id": getattr(child, "node_id", None),
                        "allow_invalid_scratch": True,
                    }
            if attempts:
                last = attempts[-1]
                diagnosis = last.get("diagnosis") if isinstance(last.get("diagnosis"), dict) else {}
                preferred = str(diagnosis.get("prefer_parent_selector") or "")
                if preferred == "parent_of_last_failed_or_low_gain":
                    parent_id = _attempt_node_id(last, "parent_node_id")
                    parent = self.ctx.nodes.get(parent_id) if parent_id else None
                    if parent is not None and self._manual_parent_usable(parent, allow_invalid=allow_invalid):
                        return parent, {
                            "strategy": "diagnosis_parent",
                            "reason": preferred,
                            "selected_node_id": getattr(parent, "node_id", None),
                            "allow_invalid_scratch": allow_invalid,
                        }
            for attempt in reversed(list(attempts)):
                child_id = _attempt_node_id(attempt, "child_node_id")
                if not child_id:
                    continue
                node = self.ctx.nodes.get(child_id)
                if node is None:
                    continue
                output_dcp = getattr(node, "output_dcp", None)
                if output_dcp and Path(output_dcp).exists() and self._manual_parent_usable(node, allow_invalid=allow_invalid):
                    validation = str(getattr(node, "validation_status", "") or "").upper()
                    return node, {
                        "strategy": "last_scratch_checkpoint",
                        "reason": "continue_from_last_scratch" if validation != "INVALID" else "continue_from_invalid_scratch",
                        "selected_node_id": getattr(node, "node_id", None),
                        "allow_invalid_scratch": allow_invalid,
                    }
        if selector == "root":
            root = self.ctx.nodes.get("node_000_root")
            if root is not None and self._manual_parent_usable(root, allow_invalid=allow_invalid):
                return root, {"strategy": "root", "reason": "manual_selector_root", "selected_node_id": root.node_id, "allow_invalid_scratch": allow_invalid}
        parent = self._best_node() or self.ctx.nodes.get("node_000_root") or self.ctx.current_node
        if parent is not None and self._manual_parent_usable(parent, allow_invalid=allow_invalid):
            return parent, {
                "strategy": "current_best",
                "reason": "start_from_current_best",
                "selected_node_id": getattr(parent, "node_id", None),
                "allow_invalid_scratch": allow_invalid,
            }
        root = self.ctx.nodes.get("node_000_root")
        if root is not None and self._manual_parent_usable(root, allow_invalid=allow_invalid):
            return root, {
                "strategy": "root",
                "reason": "fallback_root",
                "selected_node_id": getattr(root, "node_id", None),
                "allow_invalid_scratch": allow_invalid,
            }
        return None, {
            "strategy": "manual_parent",
            "reason": "no_usable_checkpoint",
            "allow_invalid_scratch": allow_invalid,
        }

    def _manual_parent_usable(self, node: Any, *, allow_invalid: bool) -> bool:
        output_dcp = getattr(node, "output_dcp", None) or getattr(node, "input_dcp", None)
        if not output_dcp or not Path(output_dcp).exists():
            return False
        validation = str(getattr(node, "validation_status", "") or "").upper()
        return validation != "INVALID" or allow_invalid

    def _manual_loop_context(
        self,
        *,
        episode_index: int,
        action_index: int,
        parent: Any,
        attempts: Sequence[Dict[str, Any]],
        attempt_diagnoses: Sequence[Dict[str, Any]],
        evidence: Dict[str, Any],
        parent_detail: Dict[str, Any],
        plan: Optional[ManualRescuePlan],
        manual_design_state: Dict[str, Any],
        started: float,
    ) -> Dict[str, Any]:
        return {
            "trigger": str(getattr(self.ctx.config, "rescue_trigger", "no_improvement")),
            "mode": "manual_loop",
            "episode_index": episode_index,
            "action_index": action_index,
            "main_profile": self._main_profile_summary(),
            "root_dcp": str(self.ctx.root_dcp),
            "selected_parent": self._node_summary(parent),
            "parent_selection": parent_detail,
            "current_best": self._node_summary(self._best_node()),
            "previous_manual_actions": self._compact_manual_history(attempts),
            "attempt_diagnoses": list(attempt_diagnoses)[-4:],
            "rescue_evidence": evidence,
            "manual_plan": plan.to_dict() if plan else None,
            "manual_design_state": manual_design_state,
            "remaining_eda_runs": self.ctx.budget.remaining_eda_runs,
            "elapsed_rescue_wall_s": max(0.0, time.time() - started),
            "allowed_primitives_note": "Use only manual.* primitive_catalog names; flow.* and raw Tcl/shell/path are forbidden.",
        }

    def _manual_plan_path(self, *, episode_index: int, action_index: int, retry_index: int = 0) -> Path:
        retry_suffix = "" if retry_index == 0 else f"_retry_{retry_index:02d}"
        if action_index == 0:
            return self.ctx.run_dir / f"manual_rescue_plan_{episode_index:03d}{retry_suffix}.json"
        return self.ctx.run_dir / f"manual_rescue_plan_{episode_index:03d}_{action_index:03d}{retry_suffix}.json"

    @staticmethod
    def _compact_manual_history(attempts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = list(attempts)
        compact: List[Dict[str, Any]] = []
        for attempt in rows[:-2]:
            action = attempt.get("manual_action") if isinstance(attempt.get("manual_action"), dict) else {}
            compact.append({
                "attempt_index": attempt.get("attempt_index"),
                "parent_node_id": attempt.get("parent_node_id"),
                "child_node_id": attempt.get("child_node_id"),
                "primitive": action.get("primitive"),
                "args": action.get("args") if isinstance(action.get("args"), dict) else {},
                "outcome_class": (attempt.get("diagnosis") or {}).get("outcome_class") if isinstance(attempt.get("diagnosis"), dict) else None,
                "publish_eligible": attempt.get("publish_eligible"),
                "delta_vs_root_mhz": attempt.get("delta_vs_root_mhz"),
            })
        compact.extend(rows[-2:])
        return compact

    def _manual_design_state(self, parent: Any) -> Dict[str, Any]:
        """Resolve implementation state from the selected checkpoint's own metadata."""
        states = getattr(self.ctx, "manual_checkpoint_states", None)
        if not isinstance(states, dict):
            states = {}
            self.ctx.manual_checkpoint_states = states
        node_id = str(getattr(parent, "node_id", "") or "")
        if isinstance(states.get(node_id), dict):
            return ManualCheckpointState.from_dict(states[node_id], parent_node_id=node_id).to_dict()
        metrics = dict(getattr(parent, "metrics", {}) or {})
        stored = metrics.get("manual_checkpoint_state") if isinstance(metrics.get("manual_checkpoint_state"), dict) else None
        state = ManualCheckpointState.from_dict(stored, parent_node_id=node_id).to_dict()
        if node_id:
            states[node_id] = state
        return state

    async def _execute_manual_action(
        self,
        action: ManualActionSpec,
        *,
        parent_node: Any,
        parent_manual_state: Dict[str, Any],
        episode_index: int,
        action_index: int,
    ) -> DcpTransitionRecord:
        ctx = self.ctx
        if not ctx.budget.reserve_eda_run():
            raise RuntimeError("EDA transition budget exhausted before manual rescue action")

        parent = parent_node
        parent_dcp = Path(getattr(parent, "output_dcp", None) or getattr(parent, "input_dcp", None) or ctx.root_dcp)
        step_index = ctx.next_transition_index
        optimizer_name = "manual_action"
        transition_id = f"node_{step_index + 1:03d}_{ctx.safe_label(action.primitive)}"
        step_dir = ctx.artifact_store.run_dir / f"transition_{step_index:03d}_{ctx.safe_label(action.primitive)}"
        output_dcp = step_dir / "output.dcp"
        child = DcpStateNode(
            node_id=transition_id,
            parent_id=parent.node_id,
            depth=parent.depth + 1,
            input_dcp=str(parent_dcp),
            output_dcp=str(output_dcp),
            flow_sequence=list(parent.flow_sequence or []) + [action.primitive],
            selected_flow=action.primitive,
            status=NodeStatus.RUNNING,
            artifact_dir=str(step_dir),
        )
        ctx.nodes[child.node_id] = child
        ctx.artifact_store.write_node_state(child)

        optimizer_params = {
            "flow": optimizer_name,
            "manual_action_spec": action.to_dict(),
            "rescue_episode_index": episode_index,
            "rescue_action_index": action_index,
        }
        intent = DcpTransitionIntent(
            transition_id=transition_id,
            run_id=ctx.run_id,
            source="rescue_manual_loop",
            parent_node_id=parent.node_id,
            parent_dcp=str(parent_dcp),
            optimizer_name=optimizer_name,
            optimizer_params=optimizer_params,
            target_node_id=parent.node_id,
            candidate_rank=step_index,
            reason="Manual rescue loop selected one allowlisted manual action.",
            metadata={
                "flow_sequence_before": list(parent.flow_sequence or []),
                "rescue_episode_index": episode_index,
                "rescue_action_index": action_index,
                "manual_primitive": action.primitive,
            },
        )
        scheduled = DcpTransitionRecord.scheduled_transition(
            run_id=ctx.run_id,
            transition_id=intent.transition_id,
            source=intent.source,
            parent_node_id=intent.parent_node_id,
            parent_dcp=intent.parent_dcp,
            optimizer_name=intent.optimizer_name,
            optimizer_params=intent.optimizer_params,
            target_node_id=intent.target_node_id,
            candidate_rank=intent.candidate_rank,
            reason=intent.reason,
            added_to_graph=True,
            metadata=intent.metadata,
        )
        ctx.transition_sink.append(scheduled)

        req = DcpTransitionRequest(
            input_dcp=parent_dcp,
            output_dcp=output_dcp,
            output_dir=step_dir,
            optimizer_name=optimizer_name,
            root_metrics=dict(ctx.root_metrics),
            parent_metrics=dict(getattr(parent, "metrics", {}) or ctx.root_metrics or {}),
            optimizer_params=optimizer_params,
            run_id=ctx.run_id,
            parent_node_id=parent.node_id,
            child_node_id=child.node_id,
            candidate_id=f"{child.node_id}:{action.primitive}",
            flow_sequence_before=list(parent.flow_sequence or []),
            mode=ctx.mode,
            metadata={"expansion_source": "rescue_manual_loop", **dict(intent.metadata or {})},
        )
        result = await ctx.transition_executor.run(req, history=list(ctx.nodes.values()))
        self._backfill_root_metrics(result.metrics or {})

        child.metrics = dict(result.metrics or {})
        next_manual_state = advance_manual_design_state(parent_manual_state, action)
        next_manual_state.update(
            {
                "source": "manual_action_transition",
                "parent_node_id": parent.node_id,
                "transition_history": list(parent_manual_state.get("transition_history") or [])[-5:]
                + [{"primitive": action.primitive, "placement": next_manual_state.get("placement", ""), "routing": next_manual_state.get("routing", "")}],
            }
        )
        implementation_complete = bool(
            next_manual_state.get("placement") == "placed"
            and next_manual_state.get("routing") == "routed"
        )
        if not implementation_complete:
            reason = "manual_checkpoint_not_fully_implemented"
            result.accepted = False
            result.rejection_reason = reason
            result.validation_status = ValidationStatus.INVALID.value
            result.output_validation.valid = False
            result.output_validation.status = ValidationStatus.INVALID.value
            if reason not in result.output_validation.errors:
                result.output_validation.errors.append(reason)
            result.metrics["validation_valid"] = False
            result.metrics["validation_status"] = ValidationStatus.INVALID.value
            result.metrics["validation_errors"] = list(result.output_validation.errors)
        ctx.budget.complete_eda_run(success=bool(result.accepted))
        result.metrics["manual_checkpoint_state"] = dict(next_manual_state)
        result.metrics["publish_eligible"] = bool(
            result.accepted
            and implementation_complete
        )
        child.metrics["manual_checkpoint_state"] = next_manual_state
        child.metrics["publish_eligible"] = result.metrics["publish_eligible"]
        states = getattr(ctx, "manual_checkpoint_states", None)
        if not isinstance(states, dict):
            states = {}
            ctx.manual_checkpoint_states = states
        states[child.node_id] = dict(next_manual_state)
        child.metrics["dcp_transition"] = {
            "optimizer_name": result.optimizer_name,
            "manual_action": action.to_dict(),
            "accepted": result.accepted,
            "rejection_reason": result.rejection_reason,
            "transition_record": result.transition_record,
            "non_publishable_scratch": not bool(result.metrics["publish_eligible"]),
        }
        child.score = result.pure_qor_score
        child.validation_status = result.validation_status
        child.validation_warnings = list(result.output_validation.warnings)
        child.validation_errors = list(result.output_validation.errors)
        child.fallback_used = bool(result.output_validation.fallback_used)
        child.status = NodeStatus.COMPLETED if result.accepted else NodeStatus.FAILED
        ctx.artifact_store.write_node_state(child)

        updated_incumbent = bool(self._manual_result_publishable(result, manual_state=next_manual_state))
        completed = DcpTransitionRecord.completed_transition(
            intent=scheduled.intent,
            result=result,
            child_node_id=child.node_id,
            child_status=child.status.value if hasattr(child.status, "value") else str(child.status),
            added_to_graph=True,
            updated_incumbent=updated_incumbent,
            outer_rejection_reason=(
                None
                if updated_incumbent or not result.accepted
                else "manual_checkpoint_not_publish_eligible"
                if not result.metrics["publish_eligible"]
                else "not_better_than_current_best"
            ),
        )
        ctx.transition_sink.append(completed)
        ctx.transition_records.append(completed)
        ctx.notify_transition_record(completed)
        ctx.analysis_contexts[child.node_id] = dict(result.analysis_context or {})
        ctx.qor_scores[child.node_id] = result.qor_score
        ctx.output_validations[child.node_id] = result.output_validation
        ctx.next_transition_index += 1

        if result.accepted:
            if child.node_id not in ctx.state.completed_node_ids:
                ctx.state.completed_node_ids.append(child.node_id)
            if updated_incumbent:
                ctx.current_dcp = Path(result.output_dcp)
                ctx.parent_metrics = dict(result.metrics or {})
                ctx.current_node = child
                ctx.state.best_node_id = child.node_id
        else:
            if child.node_id not in ctx.state.failed_node_ids:
                ctx.state.failed_node_ids.append(child.node_id)
        ctx.persist_state(strict_orphans=True)
        return completed

    async def _execute_recipe(
        self,
        recipe: RescueRecipe,
        *,
        selection_source: str,
        parent_node: Optional[Any] = None,
        source: str = "rescue_no_improvement",
        attempt_index: Optional[int] = None,
    ) -> DcpTransitionRecord:
        ctx = self.ctx
        if not ctx.budget.reserve_eda_run():
            raise RuntimeError("EDA transition budget exhausted before rescue execution")

        parent = parent_node or ctx.nodes.get("node_000_root") or ctx.current_node
        parent_dcp = Path(getattr(parent, "output_dcp", None) or ctx.root_dcp)
        step_index = ctx.next_transition_index
        transition_id = f"node_{step_index + 1:03d}_{ctx.safe_label(recipe.flow_name)}"
        step_dir = ctx.artifact_store.run_dir / f"transition_{step_index:03d}_{ctx.safe_label(recipe.flow_name)}"
        output_dcp = step_dir / "output.dcp"
        child = DcpStateNode(
            node_id=transition_id,
            parent_id=parent.node_id,
            depth=parent.depth + 1,
            input_dcp=str(parent_dcp),
            output_dcp=str(output_dcp),
            flow_sequence=list(parent.flow_sequence or []) + [recipe.flow_name],
            selected_flow=recipe.flow_name,
            status=NodeStatus.RUNNING,
            artifact_dir=str(step_dir),
        )
        ctx.nodes[child.node_id] = child
        ctx.artifact_store.write_node_state(child)

        intent = DcpTransitionIntent(
            transition_id=transition_id,
            run_id=ctx.run_id,
            source=source,
            parent_node_id=parent.node_id,
            parent_dcp=str(parent_dcp),
            optimizer_name=recipe.flow_name,
            optimizer_params={
                "flow": recipe.flow_name,
                "recipe_id": recipe.recipe_id,
                **({"recipe_spec": dict(recipe.recipe_spec)} if recipe.recipe_spec else {}),
            },
            target_node_id=parent.node_id,
            candidate_rank=step_index,
            reason="No-improvement rescue branch selected an allowlisted recipe.",
            metadata={
                "flow_sequence_before": list(parent.flow_sequence or []),
                "rescue_recipe_id": recipe.recipe_id,
                "rescue_selection_source": selection_source,
                **({"rescue_attempt_index": attempt_index} if attempt_index is not None else {}),
            },
        )
        scheduled = DcpTransitionRecord.scheduled_transition(
            run_id=ctx.run_id,
            transition_id=intent.transition_id,
            source=intent.source,
            parent_node_id=intent.parent_node_id,
            parent_dcp=intent.parent_dcp,
            optimizer_name=intent.optimizer_name,
            optimizer_params=intent.optimizer_params,
            target_node_id=intent.target_node_id,
            candidate_rank=intent.candidate_rank,
            reason=intent.reason,
            added_to_graph=True,
            metadata=intent.metadata,
        )
        ctx.transition_sink.append(scheduled)

        req = DcpTransitionRequest(
            input_dcp=parent_dcp,
            output_dcp=output_dcp,
            output_dir=step_dir,
            optimizer_name=recipe.flow_name,
            root_metrics=dict(ctx.root_metrics),
            parent_metrics=dict(parent.metrics or ctx.root_metrics or {}),
            optimizer_params=dict(intent.optimizer_params or {}),
            run_id=ctx.run_id,
            parent_node_id=parent.node_id,
            child_node_id=child.node_id,
            candidate_id=f"{child.node_id}:{recipe.flow_name}",
            flow_sequence_before=list(parent.flow_sequence or []),
            mode=ctx.mode,
            metadata={"expansion_source": source, **dict(intent.metadata or {})},
        )
        result = await ctx.transition_executor.run(req, history=list(ctx.nodes.values()))
        ctx.budget.complete_eda_run(success=bool(result.success))
        self._backfill_root_metrics(result.metrics or {})

        child.metrics = dict(result.metrics or {})
        child.metrics["dcp_transition"] = {
            "optimizer_name": result.optimizer_name,
            "accepted": result.accepted,
            "rejection_reason": result.rejection_reason,
            "transition_record": result.transition_record,
        }
        child.score = result.pure_qor_score
        child.validation_status = result.validation_status
        child.validation_warnings = list(result.output_validation.warnings)
        child.validation_errors = list(result.output_validation.errors)
        child.fallback_used = bool(result.output_validation.fallback_used)
        child.status = NodeStatus.COMPLETED if result.accepted else NodeStatus.FAILED
        ctx.artifact_store.write_node_state(child)

        updated_incumbent = bool(result.accepted and result.output_dcp and self._result_beats_current_best(result.metrics or {}, result.pure_qor_score))
        completed = DcpTransitionRecord.completed_transition(
            intent=scheduled.intent,
            result=result,
            child_node_id=child.node_id,
            child_status=child.status.value if hasattr(child.status, "value") else str(child.status),
            added_to_graph=True,
            updated_incumbent=updated_incumbent,
            outer_rejection_reason=None if updated_incumbent or not result.accepted else "not_better_than_current_best",
        )
        ctx.transition_sink.append(completed)
        ctx.transition_records.append(completed)
        ctx.notify_transition_record(completed)
        ctx.analysis_contexts[child.node_id] = dict(result.analysis_context or {})
        ctx.qor_scores[child.node_id] = result.qor_score
        ctx.output_validations[child.node_id] = result.output_validation
        ctx.next_transition_index += 1

        if result.accepted:
            if child.node_id not in ctx.state.completed_node_ids:
                ctx.state.completed_node_ids.append(child.node_id)
            if updated_incumbent:
                ctx.current_dcp = Path(result.output_dcp)
                ctx.parent_metrics = dict(result.metrics or {})
                ctx.current_node = child
                ctx.state.best_node_id = child.node_id
        else:
            if child.node_id not in ctx.state.failed_node_ids:
                ctx.state.failed_node_ids.append(child.node_id)
        ctx.persist_state(strict_orphans=True)
        return completed

    def _select_loop_parent(
        self,
        *,
        attempt_index: int,
        strategy: str,
        attempts: Sequence[Dict[str, Any]],
        parent_sequence: Optional[Sequence[str]] = None,
        parent_selector: Optional[str] = None,
    ) -> Tuple[Optional[Any], Dict[str, Any]]:
        candidates = self._safe_parent_candidates(attempts)
        by_id = {str(item["node_id"]): item for item in candidates}
        reason = strategy
        selected_id: Optional[str] = None
        if parent_selector:
            selected_id, reason = self._resolve_parent_selector(parent_selector, attempts)
        elif parent_sequence and attempt_index < len(parent_sequence):
            selected_id, reason = self._resolve_parent_selector(parent_sequence[attempt_index], attempts)
        elif strategy == "root":
            selected_id, reason = self._resolve_parent_selector("root", attempts)
        elif strategy == "round_robin_safe_parents":
            if candidates:
                selected_id = str(candidates[attempt_index % len(candidates)]["node_id"])
        else:
            selected_id, reason = self._resolve_parent_selector("current_best", attempts)

        if selected_id not in by_id and candidates:
            selected_id = str(candidates[0]["node_id"])
            reason = f"{reason}_fallback_first_safe_parent"
        node = self.ctx.nodes.get(selected_id) if selected_id else None
        if node is not None and not self._safe_parent(node):
            node = None
        return node, {
            "strategy": strategy,
            "reason": reason,
            "planner_parent_selector": parent_selector,
            "selected_node_id": getattr(node, "node_id", None),
            "candidate_parents": candidates,
        }

    def _resolve_parent_selector(self, selector: str, attempts: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], str]:
        key = str(selector or "").strip()
        normalized = key.lower()
        if normalized == "root":
            return "node_000_root", "root"
        if normalized == "current_best":
            return getattr(self._best_node(), "node_id", None), "current_best"
        if normalized == "current_node":
            return getattr(self.ctx.current_node, "node_id", None), "current_node"
        if normalized == "last_accepted_rescue":
            for attempt in reversed(list(attempts)):
                if attempt.get("accepted_by_transition_executor"):
                    return _attempt_node_id(attempt, "child_node_id"), "last_accepted_rescue"
            return None, "last_accepted_rescue_unavailable"
        if normalized == "parent_of_last_failed_or_low_gain":
            for attempt in reversed(list(attempts)):
                if attempt.get("updated_incumbent"):
                    continue
                parent_id = _attempt_node_id(attempt, "parent_node_id")
                if parent_id:
                    return parent_id, "parent_of_last_failed_or_low_gain"
                selected_parent = attempt.get("selected_parent") if isinstance(attempt.get("selected_parent"), dict) else {}
                if selected_parent.get("node_id"):
                    return str(selected_parent["node_id"]), "parent_of_last_failed_or_low_gain"
            return None, "parent_of_last_failed_or_low_gain_unavailable"
        return key, "external_node_id"

    def _safe_parent_candidates(self, attempts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ordered: List[Tuple[str, str]] = []

        def add(node_id: Optional[str], reason: str) -> None:
            if not node_id:
                return
            if any(existing == node_id for existing, _reason in ordered):
                return
            node = self.ctx.nodes.get(str(node_id))
            if node is not None and self._safe_parent(node):
                ordered.append((str(node_id), reason))

        add("node_000_root", "root")
        add(getattr(self._best_node(), "node_id", None), "current_best")
        add(getattr(self.ctx.current_node, "node_id", None), "current_node")
        for attempt in reversed(list(attempts)):
            if attempt.get("accepted_by_transition_executor"):
                add(attempt.get("child_node_id"), "last_accepted_rescue")
                break
        for attempt in reversed(list(attempts)):
            if attempt.get("updated_incumbent"):
                continue
            add(attempt.get("parent_node_id"), "parent_of_last_failed_or_low_gain")
            break
        return [
            {
                **self._node_summary(self.ctx.nodes[node_id]),
                "candidate_reason": reason,
            }
            for node_id, reason in ordered
        ]

    def _safe_parent(self, node: Any) -> bool:
        status = getattr(node, "status", None)
        status_value = status.value if hasattr(status, "value") else str(status)
        if status_value != "COMPLETED":
            return False
        validation = str(getattr(node, "validation_status", "") or "")
        if validation.upper() == "INVALID":
            return False
        output_dcp = getattr(node, "output_dcp", None) or getattr(node, "input_dcp", None)
        return bool(output_dcp and Path(output_dcp).exists())

    def _synthesis_loop_context(
        self,
        *,
        loop_index: int,
        parent: Any,
        attempts: Sequence[Dict[str, Any]],
        attempt_diagnoses: Sequence[Dict[str, Any]],
        analysis_session: Any,
        started: float,
        parent_detail: Dict[str, Any],
        evidence: Dict[str, Any],
        plan: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        avoid_repeating = []
        for diagnosis in attempt_diagnoses:
            avoid_repeating.extend(str(x) for x in diagnosis.get("avoid_primitives", []) or [])
            avoid_repeating.extend(f"flow.{x}" for x in diagnosis.get("avoid_flows", []) or [])
        return {
            "trigger": str(getattr(self.ctx.config, "rescue_trigger", "no_improvement")),
            "loop_index": loop_index,
            "main_profile": self._main_profile_summary(),
            "root_dcp": str(self.ctx.root_dcp),
            "selected_parent": self._node_summary(parent),
            "parent_selection": parent_detail,
            "current_best": self._node_summary(self._best_node()),
            "previous_attempts": list(attempts),
            "attempt_diagnoses": list(attempt_diagnoses),
            "rescue_evidence": evidence,
            "rescue_attempt_plan": plan or {},
            "known_public_prior": evidence.get("known_public_prior", {}),
            "avoid_repeating": list(dict.fromkeys(avoid_repeating)),
            "information_gaps": evidence.get("recommended_information_gaps", []),
            "remaining_eda_runs": self.ctx.budget.remaining_eda_runs,
            "elapsed_rescue_wall_s": max(0.0, time.time() - started),
            "analysis_session": self._session_to_dict(analysis_session),
            "allowed_primitives_note": "Use only primitive_catalog names; no free Tcl or shell.",
        }

    def _publish_rescue_analysis_context(
        self,
        *,
        attempts: Sequence[Dict[str, Any]],
        attempt_diagnoses: Sequence[Dict[str, Any]],
        evidence: Dict[str, Any],
        plan: Optional[Dict[str, Any]],
    ) -> None:
        payload = {
            "attempts": list(attempts),
            "attempt_diagnoses": list(attempt_diagnoses),
            "evidence": evidence,
            "plan": plan or {},
        }
        self.ctx.analysis_contexts["__rescue__"] = payload
        try:
            setattr(self.ctx.config, "rescue_attempts", list(attempts))
            setattr(self.ctx.config, "rescue_attempt_diagnoses", list(attempt_diagnoses))
            setattr(self.ctx.config, "root_dcp", str(self.ctx.root_dcp))
            setattr(self.ctx.config, "transition_records", list(self.ctx.transition_records))
        except Exception:
            return

    def _attempt_result_from_record(self, completed: DcpTransitionRecord, *, parent: Any) -> Dict[str, Any]:
        outcome = completed.outcome
        handling = completed.handling
        metrics = dict(getattr(outcome, "metrics", {}) or {})
        final_fmax = _fmax_from_metrics(metrics)
        root_fmax = _fmax_from_metrics(self.ctx.root_metrics)
        parent_fmax = _fmax_from_metrics(dict(getattr(parent, "metrics", {}) or self.ctx.root_metrics or {}))
        delta_root = None if final_fmax is None or root_fmax is None else final_fmax - root_fmax
        delta_parent = None if final_fmax is None or parent_fmax is None else final_fmax - parent_fmax
        epsilon = max(0.0, float(getattr(self.ctx.config, "rescue_manual_min_publish_delta_mhz", 0.01) or 0.0))
        manual_state = dict(metrics.get("manual_checkpoint_state") or {})
        parent_state = self._manual_design_state(parent)
        comparable_delta = delta_parent
        if manual_state.get("routing") == "routed" and parent_state.get("routing") != "routed":
            comparable_delta = delta_root
        meaningful_parent_delta = 0.0 if comparable_delta is not None and abs(comparable_delta) < epsilon else comparable_delta
        return {
            "status": "executed",
            "parent_node_id": getattr(parent, "node_id", None),
            "child_node_id": getattr(outcome, "child_node_id", None),
            "transition_record": completed.to_dict(),
            "accepted_by_transition_executor": bool(getattr(outcome, "accepted_by_transition_executor", False)),
            "updated_incumbent": bool(getattr(handling, "updated_incumbent", False)),
            "publish_eligible": bool(metrics.get("publish_eligible", False)),
            "manual_checkpoint_state": manual_state,
            "validation_status": getattr(outcome, "validation_status", None),
            "rejection_reason": getattr(outcome, "rejection_reason", None),
            "output_dcp": getattr(outcome, "output_dcp", None),
            "final_fmax_mhz": final_fmax,
            "fmax_before_mhz": parent_fmax,
            "delta_fmax_mhz": delta_root,
            "delta_vs_root_mhz": delta_root,
            "delta_vs_parent_mhz": delta_parent,
            "meaningful_delta_vs_parent_mhz": meaningful_parent_delta,
            "delta_epsilon_mhz": epsilon,
            "runtime_seconds": getattr(outcome, "runtime_seconds", None),
        }

    def _node_summary(self, node: Any) -> Dict[str, Any]:
        if node is None:
            return {}
        metrics = dict(getattr(node, "metrics", {}) or {})
        fmax = _fmax_from_metrics(metrics)
        root_fmax = _fmax_from_metrics(self.ctx.root_metrics)
        return {
            "node_id": getattr(node, "node_id", None),
            "parent_id": getattr(node, "parent_id", None),
            "depth": getattr(node, "depth", None),
            "flow_sequence": list(getattr(node, "flow_sequence", []) or []),
            "selected_flow": getattr(node, "selected_flow", None),
            "status": getattr(getattr(node, "status", None), "value", str(getattr(node, "status", ""))),
            "validation_status": getattr(node, "validation_status", None),
            "output_dcp": getattr(node, "output_dcp", None),
            "fmax_mhz": fmax,
            "delta_fmax_mhz": None if fmax is None or root_fmax is None else fmax - root_fmax,
        }

    @staticmethod
    def _session_to_dict(session: Any) -> Optional[Dict[str, Any]]:
        if session is None:
            return None
        if hasattr(session, "to_dict"):
            return session.to_dict()
        if isinstance(session, dict):
            return dict(session)
        return to_jsonable(session)

    def _main_profile_summary(self) -> Dict[str, Any]:
        root_fmax = _fmax_from_metrics(self.ctx.root_metrics)
        best = self._best_node()
        best_metrics = dict(getattr(best, "metrics", {}) or {})
        best_fmax = _fmax_from_metrics(best_metrics)
        delta = None if root_fmax is None or best_fmax is None else best_fmax - root_fmax
        return {
            "root_node_id": "node_000_root",
            "best_node_id": getattr(best, "node_id", None),
            "root_fmax_mhz": root_fmax,
            "best_fmax_mhz": best_fmax,
            "best_delta_fmax_mhz": delta,
            "remaining_eda_runs": self.ctx.budget.remaining_eda_runs,
        }

    def _best_node(self) -> Any:
        best_id = getattr(self.ctx.state, "best_node_id", None)
        if best_id and best_id in self.ctx.nodes:
            return self.ctx.nodes[best_id]
        return self.ctx.current_node

    def _result_beats_current_best(self, metrics: Dict[str, Any], score: Optional[float]) -> bool:
        best = self._best_node()
        best_fmax = _fmax_from_metrics(dict(getattr(best, "metrics", {}) or {}))
        result_fmax = _fmax_from_metrics(metrics)
        if best_fmax is not None and result_fmax is not None:
            return result_fmax > best_fmax + 1e-9
        best_score = _safe_float(getattr(best, "score", None), None)
        result_score = _safe_float(score, None)
        if best_score is not None and result_score is not None:
            return result_score > best_score + 1e-9
        return True

    def _manual_result_publishable(self, result: Any, *, manual_state: Optional[Dict[str, Any]] = None) -> bool:
        if not bool(getattr(result, "accepted", False)) or not getattr(result, "output_dcp", None):
            return False
        resolved_state = manual_state if isinstance(manual_state, dict) else dict((getattr(result, "metrics", {}) or {}).get("manual_checkpoint_state") or {})
        if resolved_state.get("placement") != "placed" or resolved_state.get("routing") != "routed":
            return False
        validation_status = str(getattr(result, "validation_status", "") or "").upper()
        if validation_status != ValidationStatus.CLEAN.value and not bool(getattr(self.ctx.config, "rescue_manual_allow_nonclean_publish", False)):
            return False
        best = self._best_node()
        best_fmax = _fmax_from_metrics(dict(getattr(best, "metrics", {}) or {}))
        result_fmax = _fmax_from_metrics(dict(getattr(result, "metrics", {}) or {}))
        min_delta = max(0.0, float(getattr(self.ctx.config, "rescue_manual_min_publish_delta_mhz", 0.01) or 0.0))
        if best_fmax is not None and result_fmax is not None:
            return result_fmax >= best_fmax + min_delta
        return self._result_beats_current_best(dict(getattr(result, "metrics", {}) or {}), getattr(result, "pure_qor_score", None))

    def _existing_llm_cost(self) -> Optional[float]:
        controller = getattr(self.ctx, "analysis_controller", None)
        if controller is None and getattr(self.ctx, "analysis", None) is not None:
            controller = getattr(self.ctx.analysis, "controller", None)
        if controller is None or not hasattr(controller, "summary"):
            return None
        try:
            summary = controller.summary()
            return _safe_float(summary.get("total_cost_usd") if isinstance(summary, dict) else None, None)
        except Exception:
            return None

    def _record_manual_usage(self, controller: ManualRescueSynthesisController) -> None:
        usage = controller.usage_summary()
        self.ctx.manual_rescue_usage = usage
        try:
            setattr(self.ctx.config, "manual_rescue_usage", usage)
        except Exception:
            pass

    def _ensure_rescue_flow_allowed(self) -> None:
        flows = list(getattr(self.ctx.config, "allowed_flows", []) or [])
        for flow in self.recipes:
            if flow not in flows:
                flows.append(flow)
        if RESCUE_FLOW_SYNTHESIZED_RECIPE not in flows:
            flows.append(RESCUE_FLOW_SYNTHESIZED_RECIPE)
        self.ctx.config.allowed_flows = flows

    def _default_recipe(self) -> Optional[RescueRecipe]:
        return self.recipes.get(RESCUE_FLOW_PLACE_RESET_RETIME)

    def _backfill_root_metrics(self, transition_metrics: Dict[str, Any]) -> None:
        ctx = self.ctx
        if not isinstance(transition_metrics, dict):
            return
        effective = transition_metrics.get("qor_root_metrics") if isinstance(transition_metrics.get("qor_root_metrics"), dict) else None
        updated = dict(effective) if effective is not None else fill_missing_qor_baseline(ctx.root_metrics, transition_metrics)
        if not has_qor_baseline(updated):
            return
        changed = False
        for key, value in updated.items():
            if value is not None and ctx.root_metrics.get(key) is None:
                ctx.root_metrics[key] = value
                changed = True
        if changed:
            root = ctx.nodes.get("node_000_root")
            if root is not None:
                root.metrics = dict(root.metrics or {})
                root.metrics.update({key: value for key, value in ctx.root_metrics.items() if value is not None})
                ctx.artifact_store.write_node_state(root)

    def _write_decision(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        self.ctx.record_run_detail("intervention_decision", decision)
        return decision


def _fmax_from_metrics(metrics: Dict[str, Any]) -> Optional[float]:
    for key in ("final_fmax_mhz", "initial_fmax_mhz", "fmax_mhz", "root_fmax_mhz"):
        value = metrics.get(key)
        parsed = _safe_float(value, None)
        if parsed is not None:
            return parsed
    timing = metrics.get("timing") if isinstance(metrics.get("timing"), dict) else {}
    for key in ("final_fmax_mhz", "initial_fmax_mhz", "fmax_mhz"):
        parsed = _safe_float(timing.get(key), None)
        if parsed is not None:
            return parsed
    return None


def _attempt_node_id(attempt: Dict[str, Any], key: str) -> Optional[str]:
    value = attempt.get(key)
    if value:
        return str(value)
    record = attempt.get("transition_record") if isinstance(attempt.get("transition_record"), dict) else {}
    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
    value = outcome.get(key)
    return str(value) if value else None


def _safe_float(value: Any, default: Optional[float]) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
