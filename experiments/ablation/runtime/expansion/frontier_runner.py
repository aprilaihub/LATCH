"""Research frontier-expansion runner for FPGA implementation flow sequences."""

from __future__ import annotations

import asyncio
import csv
import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from experiments.ablation.runtime.analysis_schema import build_analysis_context_v2, sanitize_candidate_flows
from experiments.ablation.runtime.artifacts import ArtifactStore, node_artifact_dict
from experiments.ablation.runtime.expansion.policies.core import (
    BaseExpansionPolicy,
    DeterministicExpansionPolicy,
    SelectorInvalidResponse,
    TransitionCandidate,
    ExpansionDecision,
    validate_expansion_decision,
)
from optimizer_pipeline.models import (
    ExecutionResult,
    NodeStatus,
    OutputValidationResult,
    PipelineResult,
    PipelineState,
    PipelineStatus,
    DcpStateNode,
    StopReason,
    ValidationStatus,
    to_jsonable,
)
from optimizer_pipeline.qor import QoRScore, QoRWeights, compute_qor_score, fill_missing_qor_baseline, has_qor_baseline, merge_run_and_analysis_metrics
from optimizer_pipeline.reliability import ReliabilityTable
from optimizer_pipeline.retrieval import TrajectoryRetrievalIndex
from experiments.ablation.config import OptimizationConfig
from experiments.ablation.runtime.knowledge import DEFAULT_KNOWLEDGE_BASE_PATH, build_knowledge_context
from experiments.ablation.runtime.analysis import AnalysisTriggerDecision, OnlineAnalysisController, NaiveOnlineAnalysisTriggerPolicy, StaticAnalysisBudgetPolicy, TimePressureStopPolicy, StaticAnalysisToolPlanPolicy, build_analysis_decision_policy
from optimizer_pipeline.execution import DcpTransitionExecutor, DcpTransitionRequest, DcpTransitionResult
from optimizer_pipeline.domain.transition import DcpTransitionRecord
from integrations.llm.runtime.decision_events import build_llm_decision_event_rows, write_llm_decision_events
from experiments.ablation.runtime.records import record_from_dict
from optimizer_pipeline.run.store_bridge import initialize_run_store, DatabaseTransitionSink
from optimizer_pipeline.core.domain import RunLifecycleEvent, RunStatus, ValidationRecord, ValidationLevel, ValidationStatus as FactValidationStatus, ResultSelection, SelectionKind
from optimizer_pipeline.domain.transition import build_transition_records_from_policy_events, records_to_policy_candidate_rows


_EXTERNAL_HISTORY_ANALYSIS_TOOLS = {"query_retrieval_memory"}


def _drop_external_history_tools(tools: Sequence[str]) -> List[str]:
    return [str(tool) for tool in tools if str(tool) not in _EXTERNAL_HISTORY_ANALYSIS_TOOLS]


# Expansion policies that never call an LLM. They rank with the deterministic
# heuristic scores that live in the analysis context, so stripping those scores
# does not close a leak -- it just deletes the policy's own signal. LLM-facing
# policies still get the stripped context. Anything not listed here is treated
# as LLM-facing, so a new policy is safe by omission rather than by memory.
NO_LLM_POLICY_TYPES = frozenset({"deterministic", "reliability", "retrieval"})


class FrontierExpansionRunner:
    """Validation-gated frontier expansion over deterministic FPGA optimisation flows."""

    def __init__(
        self,
        *,
        config: OptimizationConfig,
        artifact_store: Optional[ArtifactStore],
        analyzer: Any,
        executor: Any,
        output_validator: Any,
        policy: BaseExpansionPolicy,
        fallback_policy: Optional[BaseExpansionPolicy] = None,
        seed_analysis_priors: Optional[Sequence[Dict[str, Any]]] = None,
        seed_candidate_priorities: Optional[Sequence[Dict[str, Any]]] = None,
        seed_analysis_plans: Optional[Sequence[Dict[str, Any]]] = None,
        seed_node_diagnoses: Optional[Sequence[Dict[str, Any]]] = None,
        seed_analysis_evidence: Optional[Dict[str, Any]] = None,
        seed_analysis_contexts: Optional[Dict[str, Dict[str, Any]]] = None,
        seed_analysis_source: str = "external",
        equivalence_checker: Optional[Any] = None,
        analysis_llm_client: Optional[Any] = None,
    ):
        self.config = config
        self.pipeline_config = config
        self._assert_contest_clean_inputs(
            seed_analysis_priors=seed_analysis_priors,
            seed_candidate_priorities=seed_candidate_priorities,
            seed_analysis_plans=seed_analysis_plans,
            seed_node_diagnoses=seed_node_diagnoses,
            seed_analysis_evidence=seed_analysis_evidence,
            seed_analysis_contexts=seed_analysis_contexts,
            seed_analysis_source=seed_analysis_source,
        )
        self.artifacts = artifact_store
        self.analyzer = analyzer
        self.executor = executor
        self.output_validator = output_validator
        self.equivalence_checker = equivalence_checker
        self.analysis_llm_client = analysis_llm_client
        self.policy = policy
        self.fallback_policy = fallback_policy or DeterministicExpansionPolicy()
        self.nodes: Dict[str, DcpStateNode] = {}
        self.analysis_contexts: Dict[str, Dict[str, Any]] = {}
        self.qor_scores: Dict[str, QoRScore] = {}
        self.output_validations: Dict[str, OutputValidationResult] = {}
        self.policy_events: List[Dict[str, Any]] = []
        self.llm_decision_events: List[Dict[str, Any]] = []
        self.validation_events: List[Dict[str, Any]] = []
        self.execution_events: List[Dict[str, Any]] = []
        self.policy_candidates_events: List[Dict[str, Any]] = []
        self.seed_analysis_priors = [dict(row) for row in (seed_analysis_priors or []) if isinstance(row, dict)]
        self.seed_candidate_priorities = [dict(row) for row in (seed_candidate_priorities or []) if isinstance(row, dict)]
        self.seed_analysis_plans = [dict(row) for row in (seed_analysis_plans or []) if isinstance(row, dict)]
        self.seed_node_diagnoses = [dict(row) for row in (seed_node_diagnoses or []) if isinstance(row, dict)]
        self.seed_analysis_evidence = dict(seed_analysis_evidence or {})
        self.seed_analysis_contexts = dict(seed_analysis_contexts or {})
        self.expanded_node_ids: set[str] = set()
        self.pruned_node_ids: set[str] = set()
        self.run_count = 0
        self.scheduled_run_count = 0
        # Retry-on-hard-execution-failure bookkeeping.
        self.retry_count = 0
        self.retried_decision_points: set[Tuple[str, Any]] = set()
        self.retry_events: List[Dict[str, Any]] = []
        self.failed_execution_seconds = 0.0
        self.productive_execution_seconds = 0.0
        # Machine conditions at run start/end, so timings stay interpretable.
        self.system_conditions_start: Dict[str, Any] = {}
        self.system_conditions_end: Dict[str, Any] = {}
        self.system_load_samples: List[Dict[str, Any]] = []
        # The end-of-run equivalence gate is real Vivado+xsim work, but it is
        # verification rather than optimisation and is deliberately tracked
        # apart from the level's own cost.
        self.verification_seconds = 0.0
        self.repo_revision: Dict[str, Any] = {}
        self.dcp_pruning: Dict[str, Any] = {"enabled": False}
        self._load_sampler_task: Optional[Any] = None
        self._run_start_time: Optional[float] = None
        self.root_metrics: Dict[str, Any] = {}
        self.reliability_table = self._load_reliability_table(config.reliability_stats_path)
        self.retrieval_index = self._load_retrieval_index(config.retrieval_index_path)
        self.knowledge_context = self._build_knowledge_context()
        self.analysis_controller = self._build_analysis_controller()
        self.transition_executor = DcpTransitionExecutor(
            executor=self.executor,
            output_validator=self.output_validator,
            analyzer=self.analyzer,
            pipeline_config=self.pipeline_config,
            artifact_store=self.artifacts,
            qos_weights=self.config.qor_weights(),
        )
        self.transition_record_store = None
        self.run_store = None

    def _assert_contest_clean_inputs(
        self,
        *,
        seed_analysis_priors: Optional[Sequence[Dict[str, Any]]],
        seed_candidate_priorities: Optional[Sequence[Dict[str, Any]]],
        seed_analysis_plans: Optional[Sequence[Dict[str, Any]]],
        seed_node_diagnoses: Optional[Sequence[Dict[str, Any]]],
        seed_analysis_evidence: Optional[Dict[str, Any]],
        seed_analysis_contexts: Optional[Dict[str, Dict[str, Any]]],
        seed_analysis_source: str,
    ) -> None:
        validate = getattr(self.config, "validate_contest_clean", None)
        if callable(validate):
            validate()
        if not getattr(self.config, "contest_clean", False):
            return
        if seed_analysis_source == "current_run":
            return
        violations: List[str] = []
        if seed_analysis_priors:
            violations.append("seed_analysis_priors")
        if seed_candidate_priorities:
            violations.append("seed_candidate_priorities")
        if seed_analysis_plans:
            violations.append("seed_analysis_plans")
        if seed_node_diagnoses:
            violations.append("seed_node_diagnoses")
        if seed_analysis_evidence:
            violations.append("seed_analysis_evidence")
        if seed_analysis_contexts:
            violations.append("seed_analysis_contexts")
        if violations:
            raise ValueError(
                "contest_clean forbids seeded external analysis/history inputs: "
                + ", ".join(violations)
            )

    @staticmethod
    def _load_reliability_table(path: Optional[str]) -> ReliabilityTable:
        if not path:
            return ReliabilityTable()
        p = Path(path)
        try:
            if p.name.endswith(".jsonl"):
                return ReliabilityTable.from_jsonl(p)
            return ReliabilityTable.from_summary_json(p)
        except Exception:
            return ReliabilityTable()

    @staticmethod
    def _load_retrieval_index(path: Optional[str]) -> Optional[TrajectoryRetrievalIndex]:
        if not path:
            return None
        try:
            return TrajectoryRetrievalIndex.load(Path(path))
        except Exception:
            return None

    def _build_knowledge_context(self) -> Optional[Dict[str, Any]]:
        mode = str(getattr(self.config, "knowledge_mode", "off") or "off")
        if mode == "off":
            return None
        if mode != "reference_manual":
            raise ValueError(
                f"knowledge_mode={mode!r} is not supported on dev/llm-experiment; "
                "use knowledge_mode='reference_manual' for static flow cards."
            )
        path = getattr(self.config, "knowledge_base_path", None)
        if mode == "reference_manual" and not path:
            path = str(DEFAULT_KNOWLEDGE_BASE_PATH)
            self.config.knowledge_base_path = path
        return build_knowledge_context(path, mode=mode)

    def _build_analysis_controller(self) -> Optional[OnlineAnalysisController]:
        if self.artifacts is None or not self.config.online_deep_analysis:
            return None
        allow_report = bool(self.config.analysis_allow_report_probes) and self.config.analysis_level in {"report_probe", "act_probe"}
        allow_act = bool(self.config.analysis_allow_act_probes) and self.config.analysis_level == "act_probe"
        trigger = NaiveOnlineAnalysisTriggerPolicy(max_nodes_per_search=self.config.analysis_max_states_per_expansion)
        budget = StaticAnalysisBudgetPolicy(
            root_budget_sec=self.config.analysis_root_budget_sec,
            node_budget_sec=self.config.analysis_state_budget_sec,
            allow_report_probes=allow_report,
            allow_act_probes=allow_act,
            default_tool_timeout_sec=self.config.analysis_tool_timeout_sec,
        )
        stop = TimePressureStopPolicy(
            soft_stop_frac=self.config.analysis_soft_stop_frac,
            strong_stop_frac=self.config.analysis_strong_stop_frac,
        )
        base_tool_plan = StaticAnalysisToolPlanPolicy(name=self.config.analysis_tool_plan_policy)
        tool_plan = StaticAnalysisToolPlanPolicy(
            name=self.config.analysis_tool_plan_policy,
            root_tools=self.config.analysis_root_tools or base_tool_plan.root_tools,
            keynode_tools=self.config.analysis_keynode_tools or base_tool_plan.keynode_tools,
            failure_tools=base_tool_plan.failure_tools,
        )
        if getattr(self.config, "contest_clean", False):
            tool_plan = StaticAnalysisToolPlanPolicy(
                name=f"{tool_plan.name}_contest_clean",
                root_tools=_drop_external_history_tools(tool_plan.root_tools),
                keynode_tools=_drop_external_history_tools(tool_plan.keynode_tools),
                failure_tools=_drop_external_history_tools(tool_plan.failure_tools),
            )
        decision_policy = build_analysis_decision_policy(
            self.config.analysis_tool_loop_mode,
            model=self.config.analysis_decision_model(),
            api_key=None,
            provider=self.config.analysis_loop_provider,
            provider_base_url=self.config.provider_base_url,
            provider_api_key_env=self.config.provider_api_key_env,
            pricing_profile=self.config.pricing_profile,
            timeout_sec=self.config.llm_timeout_sec,
            cost_budget_usd=self.config.analysis_llm_cost_budget_usd,
            knowledge_context=self.knowledge_context,
            reasoning_effort=self.config.analysis_reasoning_effort,
            client=self.analysis_llm_client,
        )
        return OnlineAnalysisController(
            run_dir=self.artifacts.run_dir,
            allowed_flows=list(self.config.allowed_flows),
            retrieval_index=None if getattr(self.config, "contest_clean", False) else self.retrieval_index,
            trigger_policy=trigger,
            budget_policy=budget,
            stop_policy=stop,
            tool_plan_policy=tool_plan,
            report_probe_backend=self.config.analysis_report_probe_backend,
            fresh_probe_contract_mode=self.config.analysis_fresh_probe_contract_mode,
            decision_policy=decision_policy,
            enabled=True,
        )

    LOAD_SAMPLE_INTERVAL_SEC = 120

    async def _sample_load_periodically(self) -> None:
        """Sample machine load every couple of minutes for the whole run.

        Two endpoints cannot characterise a multi-hour run: a 7-minute run was
        observed going from load 17.6 to 41.0 with neither endpoint crossing the
        flag. Reporting only -- nothing here alters scheduling.
        """
        from experiments.ablation.runtime.preflight import system_load_snapshot

        try:
            while True:
                await asyncio.sleep(self.LOAD_SAMPLE_INTERVAL_SEC)
                self.system_load_samples.append(system_load_snapshot("periodic"))
        except asyncio.CancelledError:
            raise

    async def run(self, input_dcp: Path) -> PipelineResult:
        from experiments.ablation.runtime.preflight import system_load_snapshot
        from experiments.ablation.runtime.real_analysis_provider import reset_diagnostic_timing

        from experiments.ablation.runtime.preflight import repo_revision

        reset_diagnostic_timing()
        self.repo_revision = repo_revision()
        self.system_conditions_start = system_load_snapshot("run_start")
        self.system_load_samples = [dict(self.system_conditions_start)]
        self._load_sampler_task = asyncio.create_task(self._sample_load_periodically())
        try:
            result = await self._run_inner(input_dcp)
            if self.run_store is not None:
                self._record_final_facts(result)
            return result
        finally:
            if self.run_store is not None:
                self.run_store.close()
            if self._load_sampler_task is not None:
                self._load_sampler_task.cancel()
                try:
                    await self._load_sampler_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _run_inner(self, input_dcp: Path) -> PipelineResult:
        if self.artifacts is None:
            raise ValueError("ArtifactStore is required for run()")
        self._run_start_time = time.time()
        state = PipelineState(status=PipelineStatus.INITIALIZING, root_dcp=str(input_dcp))
        self._persist_state(state)
        self.artifacts.write_json(self.artifacts.run_dir / "config.json", self.config.to_dict())

        try:
            await self._create_root(Path(input_dcp), state)
            await self._maybe_run_online_analysis([self.nodes["node_000_root"]], state)
            if self.config.analysis_only:
                state.status = PipelineStatus.COMPLETED
                state.stop_reason = StopReason.LLM_REQUESTED_STOP
                final_output = self._finalize_best_output(state)
                gate_result = await self._run_equivalence_gate(Path(input_dcp), final_output, state)
                self._write_policy_candidates_snapshot()
                summary_path = self._write_summary(state, final_output)
                self._write_best_trajectory(state)
                self._write_results_tables()
                self._write_graph_stats()
                self._write_budget_curve()
                self._persist_state(state)
                return PipelineResult(
                    success=gate_result.get("status") != "failed",
                    run_dir=str(self.artifacts.run_dir),
                    final_output=str(final_output) if final_output else None,
                    summary_path=str(summary_path),
                    state=state,
                )
            state.status = PipelineStatus.RUNNING
            self._persist_state(state)

            if self.config.scheduler_mode == "batch":
                await self._run_batch_scheduler(state)
            else:
                await self._run_async_scheduler(state)

            if state.stop_reason is None and self.run_count >= self.config.max_total_runs:
                state.stop_reason = StopReason.MAX_ITERATIONS_REACHED

            state.status = PipelineStatus.COMPLETED
            final_output = self._finalize_best_output(state)
            gate_result = await self._run_equivalence_gate(Path(input_dcp), final_output, state)
            # After the winner is published and verified: optional, opt-in removal
            # of losing-branch design data. Runs before the summary so the record
            # of what was deleted lands in pipeline_summary.json.
            if self.config.prune_dcps_keep_winning_path:
                self.dcp_pruning = self._prune_dcps_keep_winning_path(state)
            summary_path = self._write_summary(state, final_output)
            self._write_best_trajectory(state)
            self._write_results_tables()
            self._write_graph_stats()
            self._write_budget_curve()
            self._persist_state(state)
            return PipelineResult(
                success=gate_result.get("status") != "failed",
                run_dir=str(self.artifacts.run_dir),
                final_output=str(final_output) if final_output else None,
                summary_path=str(summary_path),
                state=state,
            )
        except Exception as exc:
            state.status = PipelineStatus.FAILED
            state.stop_reason = StopReason.EXECUTION_FAILED
            error_path = self.artifacts.write_json(
                self.artifacts.run_dir / "fatal_error.json",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            self._persist_state(state)
            return PipelineResult(
                success=False,
                run_dir=str(self.artifacts.run_dir),
                final_output=None,
                summary_path=str(error_path),
                state=state,
            )

    async def _create_root(self, input_dcp: Path, state: PipelineState) -> DcpStateNode:
        assert self.artifacts is not None
        node_dir = self.artifacts.node_dir(0, "root")
        root = DcpStateNode(
            node_id=node_dir.name,
            parent_id=None,
            depth=0,
            input_dcp=str(input_dcp),
            output_dcp=str(input_dcp),
            flow_sequence=[],
            selected_flow=None,
            status=NodeStatus.ANALYZING,
            validation_status=ValidationStatus.CLEAN.value,
            artifact_dir=str(node_dir),
        )
        self.nodes[root.node_id] = root
        state.active_node_ids = [root.node_id]
        self.artifacts.write_node_state(root)

        analysis = await self.analyzer.analyze(input_dcp, [root], self.pipeline_config)
        if isinstance(analysis, dict) and analysis.get("schema_version") == 2:
            analysis = dict(analysis)
            analysis["node_id"] = root.node_id
            analysis["parent_id"] = None
        else:
            analysis = build_analysis_context_v2(
                input_dcp=input_dcp,
                provider_context=analysis,
                history=[root],
                config=self.pipeline_config,
                node_id=root.node_id,
            )
        seed_context = self.seed_analysis_contexts.get(root.node_id)
        if seed_context:
            analysis = dict(analysis)
            analysis["seeded_analysis_context"] = to_jsonable(seed_context)
            if isinstance(seed_context.get("analysis_diagnoses"), list):
                analysis.setdefault("analysis_diagnoses", seed_context["analysis_diagnoses"])
        self.analysis_contexts[root.node_id] = analysis
        self.artifacts.write_json(node_dir / "analysis_context.json", analysis)
        self.root_metrics = merge_run_and_analysis_metrics({}, analysis)
        self.root_metrics["benchmark_name"] = analysis.get("benchmark_name")
        score = compute_qor_score(
            self.root_metrics,
            self.root_metrics,
            weights=self.config.qor_weights(),
            valid_output=True,
            path_runtime_seconds=0.0,
        )
        self.qor_scores[root.node_id] = score
        root.metrics = {
            **self.root_metrics,
            "path_runtime_seconds": 0.0,
            "node_runtime_seconds": 0.0,
            "qor_score": score.to_dict(),
            "analysis_context_path": str(node_dir / "analysis_context.json"),
            "validation_status": ValidationStatus.CLEAN.value,
        }
        root.score = score.total_score
        root.status = NodeStatus.COMPLETED
        self.run_store = initialize_run_store(
            run_dir=self.artifacts.run_dir,
            run_id=self.artifacts.run_dir.name,
            mode="ablation:" + self.config.policy_type,
            input_dcp=input_dcp,
            root_metrics=self.root_metrics,
        )
        self.transition_record_store = DatabaseTransitionSink(self.run_store)
        state.active_node_ids = []
        state.completed_node_ids = [root.node_id]
        state.best_node_id = root.node_id
        self.artifacts.write_json(node_dir / "qor_score.json", score.to_dict())
        self.artifacts.write_node_state(root)
        self._persist_state(state)
        return root

    def _record_final_facts(self, result: PipelineResult) -> None:
        """Persist study selection and final certification without merging outcomes."""
        store = self.run_store
        run_id = store.get_run().run_id
        state = result.state
        selected = next((item for item in store.states() if item.state_id == state.best_node_id), None)
        if selected is not None:
            gate = getattr(state, "equivalence_gate", None) or {"status": "skipped", "passed": None}
            status = {"passed": FactValidationStatus.PASSED, "failed": FactValidationStatus.FAILED}.get(gate.get("status"), FactValidationStatus.NOT_RUN)
            store.add_validation(ValidationRecord(
                "validation-final", run_id, "scope-root", selected.state_id,
                ValidationLevel.CERTIFICATION, status, details=gate,
            ))
            store.add_selection(ResultSelection(
                "selection-study-final", run_id, "scope-root", selected.state_id,
                selected.artifact_id, SelectionKind.EXPLICIT,
                "study scheduler selection; certification recorded separately",
            ))
        store.add_run_event(RunLifecycleEvent(
            "run-event-terminal", run_id, 1,
            RunStatus.FINALIZED if result.success else RunStatus.FAILED,
            detail={"execution_completed": state.status == PipelineStatus.COMPLETED,
                    "functional_check": getattr(state, "equivalence_gate", None),
                    "migration_source": "a722b1e", "historical_replay": False},
        ))

    def _backfill_root_metrics_from_step(self, step_metrics: Dict[str, Any]) -> None:
        if not isinstance(step_metrics, dict):
            return
        effective = step_metrics.get("qor_root_metrics") if isinstance(step_metrics.get("qor_root_metrics"), dict) else None
        updated = dict(effective) if effective is not None else fill_missing_qor_baseline(self.root_metrics, step_metrics)
        if not has_qor_baseline(updated):
            return
        changed = False
        for key, value in updated.items():
            if value is not None and self.root_metrics.get(key) is None:
                self.root_metrics[key] = value
                changed = True
        if not changed:
            return
        root = self.nodes.get("node_000_root")
        if root is not None:
            root.metrics.update({key: value for key, value in self.root_metrics.items() if value is not None})
            root.metrics["qor_root_metrics_backfilled"] = True
            root_score = compute_qor_score(
                self.root_metrics,
                self.root_metrics,
                weights=self.config.qor_weights(),
                valid_output=True,
                path_runtime_seconds=0.0,
            )
            root.score = root_score.total_score
            root.metrics["qor_score"] = root_score.to_dict()
            self.qor_scores[root.node_id] = root_score
            if self.artifacts is not None:
                node_dir = Path(root.artifact_dir) if root.artifact_dir else self.artifacts.node_dir(0, "root")
                self.artifacts.write_json(node_dir / "qor_score.json", root_score.to_dict())
                self.artifacts.write_node_state(root)

    def _frontier_nodes(self) -> List[DcpStateNode]:
        candidates = []
        max_children = self._max_children_per_node()
        for node in self.nodes.values():
            if node.status != NodeStatus.COMPLETED:
                continue
            if node.depth >= self.config.max_depth:
                continue
            if node.node_id in self.pruned_node_ids:
                continue
            if self.config.scheduler_mode == "batch" and node.node_id in self.expanded_node_ids:
                continue
            if self._child_count(node.node_id) >= max_children:
                continue
            if node.validation_status == ValidationStatus.INVALID.value:
                continue
            if node.validation_status in {ValidationStatus.WARNING.value, ValidationStatus.FALLBACK_USED.value} and not self.config.allow_warning_expansion:
                continue
            candidates.append(node)
        if self.config.policy_type == "analysis_priority":
            candidates.sort(key=lambda node: _node_numeric_id(node.node_id))
            return candidates
        candidates.sort(key=lambda node: (node.score if node.score is not None else float("-inf")), reverse=True)
        return candidates[: self.config.beam_width]

    async def _maybe_run_online_analysis(self, candidate_nodes: Sequence[DcpStateNode], state: PipelineState) -> None:
        if self.analysis_controller is None or not candidate_nodes:
            return
        if self.config.policy_type == "analysis_priority":
            for node in candidate_nodes:
                if self._has_candidate_priority_for_node(node.node_id) or self._analysis_attempted_for_node(node.node_id):
                    continue
                trigger_reason = "analysis_priority_global_transition_ranking"
                profile = "root" if node.depth == 0 else "node"
                trigger = AnalysisTriggerDecision(
                    True,
                    node.node_id,
                    trigger_reason,
                    priority=1.0,
                    budget_profile=profile,
                )
                budget = self.analysis_controller.budget_policy.budget_for(trigger)
                session = await self.analysis_controller.run_session(
                    node=node,
                    trigger_reason=trigger_reason,
                    budget=budget,
                    nodes=self.nodes,
                    analysis_contexts=self.analysis_contexts,
                )
                self.analysis_controller.memory.apply_session(session)
            return
        await self.analysis_controller.maybe_analyze(
            candidate_nodes=list(candidate_nodes),
            state=state,
            nodes=self.nodes,
            analysis_contexts=self.analysis_contexts,
            run_count=self.run_count,
            max_total_runs=self.config.max_total_runs,
        )

    def _has_candidate_priority_for_node(self, node_id: str) -> bool:
        node_ids = {str(node_id)}
        for row in self.seed_candidate_priorities:
            if self._analysis_row_targets_node(row, node_ids) and (row.get("flow") or row.get("candidate_flow")):
                return True
        for plan in self.seed_analysis_plans:
            if not self._analysis_row_targets_node(plan, node_ids):
                continue
            rows = plan.get("candidate_priorities") if isinstance(plan.get("candidate_priorities"), list) else []
            if any(isinstance(row, dict) and (row.get("flow") or row.get("candidate_flow")) for row in rows):
                return True
        if self.analysis_controller is not None:
            memory = self.analysis_controller.memory
            if any(p.node_id == node_id for p in memory.candidate_priorities):
                return True
            for plan in memory.analysis_plans:
                if plan.node_id == node_id and plan.candidate_priorities:
                    return True
        return False

    def _analysis_attempted_for_node(self, node_id: str) -> bool:
        if self.analysis_controller is None:
            return False
        return any(session.node_id == node_id for session in self.analysis_controller.memory.sessions)

    @staticmethod
    def _analysis_row_targets_node(row: Dict[str, Any], node_ids: set[str]) -> bool:
        for key in ("node_id", "target_node_id", "target_node"):
            value = row.get(key)
            if value is not None and str(value) in node_ids:
                return True
        return False

    def _analysis_priors_for_request(self, frontier: Sequence[DcpStateNode]) -> List[Dict[str, Any]]:
        node_ids = {node.node_id for node in frontier}
        rows = [dict(row) for row in self.seed_analysis_priors if self._analysis_row_targets_node(row, node_ids)]
        if self.analysis_controller is not None:
            rows.extend(p.to_dict() for p in self.analysis_controller.memory.candidate_priors if p.node_id in node_ids)
        return rows

    def _candidate_priorities_for_request(self, frontier: Sequence[DcpStateNode]) -> List[Dict[str, Any]]:
        node_ids = {node.node_id for node in frontier}
        rows: List[Dict[str, Any]] = []
        for row in self.seed_candidate_priorities:
            if not self._analysis_row_targets_node(row, node_ids):
                continue
            rows.append(dict(row))
        if self.analysis_controller is not None:
            rows.extend(p.to_dict() for p in self.analysis_controller.memory.candidate_priorities if p.node_id in node_ids)
        return rows

    def _analysis_plans_for_request(self, frontier: Sequence[DcpStateNode]) -> List[Dict[str, Any]]:
        node_ids = {node.node_id for node in frontier}
        rows: List[Dict[str, Any]] = [dict(row) for row in self.seed_analysis_plans if self._analysis_row_targets_node(row, node_ids)]
        if self.analysis_controller is not None:
            rows.extend(plan.to_dict() for plan in self.analysis_controller.memory.analysis_plans if plan.node_id in node_ids)
        return rows

    def _analysis_diagnoses_for_request(self, frontier: Sequence[DcpStateNode]) -> List[Dict[str, Any]]:
        node_ids = {node.node_id for node in frontier}
        rows = [dict(row) for row in self.seed_node_diagnoses if self._analysis_row_targets_node(row, node_ids)]
        if self.analysis_controller is not None:
            for session in self.analysis_controller.memory.sessions:
                if session.node_id not in node_ids or not session.diagnosis:
                    continue
                rows.append(session.diagnosis.to_dict())
        return rows

    def _seed_analysis_evidence_for_request(self, node_ids: set[str]) -> Dict[str, Any]:
        evidence = self.seed_analysis_evidence
        rows_by_key: Dict[str, List[Dict[str, Any]]] = {}
        for key in ("sessions", "tool_calls", "loop_decisions", "fast_skip_suggestions"):
            rows = evidence.get(key) if isinstance(evidence.get(key), list) else []
            rows_by_key[key] = [
                dict(row)
                for row in rows
                if isinstance(row, dict) and self._analysis_row_targets_node(row, node_ids)
            ]
        summary = evidence.get("summary") if isinstance(evidence.get("summary"), dict) else {}
        return {
            "sessions": rows_by_key["sessions"],
            "tool_calls": rows_by_key["tool_calls"][-20:],
            "loop_decisions": rows_by_key["loop_decisions"][-12:],
            "fast_skip_suggestions": rows_by_key["fast_skip_suggestions"][-20:],
            "summary": dict(summary),
        }

    def _analysis_evidence_for_request(self, frontier: Sequence[DcpStateNode]) -> Dict[str, Any]:
        node_ids = {node.node_id for node in frontier}
        seeded = self._seed_analysis_evidence_for_request(node_ids)
        if self.analysis_controller is None:
            return seeded
        memory = self.analysis_controller.memory
        sessions = []
        for session in memory.sessions:
            if session.node_id not in node_ids:
                continue
            sessions.append({
                "session_id": session.session_id,
                "node_id": session.node_id,
                "trigger_reason": session.trigger_reason,
                "tool_loop_mode": session.tool_loop_mode,
                "stop_reason": session.stop_decision.stop_reason if session.stop_decision else None,
                "candidate_prior_count": len(session.candidate_priors),
                "candidate_priority_count": len(session.candidate_priorities),
                "fast_skip_suggestion_count": len(session.fast_skip_suggestions or []),
                "diagnosis": session.diagnosis.to_dict() if session.diagnosis else None,
                "analysis_plan": session.analysis_plan.to_dict() if session.analysis_plan else None,
            })
        tool_calls = [
            {
                "session_id": row.get("session_id"),
                "node_id": row.get("node_id"),
                "tool_name": row.get("tool_name"),
                "tool_class": row.get("tool_class"),
                "success": row.get("success"),
                "timed_out": row.get("timed_out"),
                "error": row.get("error"),
            }
            for row in memory.tool_call_events
            if row.get("node_id") in node_ids
        ]
        loop_decisions = [
            {
                "session_id": row.get("session_id"),
                "node_id": row.get("node_id"),
                "decision_index": row.get("decision_index"),
                "tool_loop_mode": row.get("tool_loop_mode"),
                "action": row.get("action"),
                "next_tool_name": row.get("next_tool_name"),
                "stop_reason": row.get("stop_reason"),
                "confidence": row.get("confidence"),
                "rationale": row.get("rationale"),
            }
            for row in memory.loop_decision_events
            if row.get("node_id") in node_ids
        ]
        fast_skip_suggestions = [
            item
            for item in memory.fast_skip_suggestions
            if item.get("node_id") in node_ids or item.get("target_node_id") in node_ids
        ]
        return {
            "sessions": seeded["sessions"] + sessions,
            "tool_calls": (seeded["tool_calls"] + tool_calls)[-20:],
            "loop_decisions": (seeded["loop_decisions"] + loop_decisions)[-12:],
            "fast_skip_suggestions": (seeded["fast_skip_suggestions"] + fast_skip_suggestions)[-20:],
            "summary": {
                "seeded": seeded.get("summary") or {},
                "online": memory.to_summary(),
            },
        }

    def _build_policy_request(
        self,
        frontier: Sequence[DcpStateNode],
        *,
        state: Optional[PipelineState] = None,
        free_slots: Optional[int] = None,
        remaining_runs: Optional[int] = None,
    ) -> Dict[str, Any]:
        best = self._best_node(include_root=True)
        frontier_for_context = list(frontier) if self.config.policy_type == "analysis_priority" else list(frontier)[: self.config.frontier_context_limit]
        # Explicit: a no-LLM policy keeps its heuristic scores, an LLM policy does not.
        strip_for_policy = self._policy_is_llm_facing()
        context_by_node = {
            node.node_id: self._compact_analysis_context(node.node_id, for_llm=strip_for_policy)
            for node in frontier_for_context
        }
        max_children = self._max_children_per_node()
        effective_remaining = max(
            0,
            int(remaining_runs if remaining_runs is not None else self.config.max_total_runs - self.run_count),
        )
        effective_free_slots = max(0, int(free_slots if free_slots is not None else self.config.batch_size or self.config.branching_factor))
        request_batch_size = min(self.config.batch_size or self.config.branching_factor, effective_remaining, max(1, effective_free_slots))
        active_node_ids = list(state.active_node_ids) if state is not None else [
            node.node_id for node in self.nodes.values() if node.status == NodeStatus.RUNNING
        ]
        return {
            "trajectory_objective_profile": "pure_qor_v1",
            "objective": "Maximise trajectory_objective_score using pure QoR from Fmax, WNS and TNS improvements; runtime, official contest score and analysis cost are reported separately. Invalid routed outputs are rejected; warning/fallback nodes are usable but tracked.",
            "qor_weights": self.config.qor_weights().to_dict(),
            "allowed_flows": list(self.config.allowed_flows),
            "batch_size": request_batch_size,
            "branching_factor": max(1, request_batch_size),
            "beam_width": self.config.beam_width,
            "max_depth": self.config.max_depth,
            "max_parallel_jobs": self.config.max_parallel_jobs,
            "max_total_runs": self.config.max_total_runs,
            "scheduler_mode": self.config.scheduler_mode,
            "runs_completed": self.run_count,
            "scheduled_runs": self.scheduled_run_count,
            "remaining_runs": effective_remaining,
            "free_slots": effective_free_slots,
            "active_node_ids": active_node_ids,
            "max_children_per_node": max_children,
            "node_child_counts": {node.node_id: self._child_count(node.node_id) for node in frontier},
            "llm_context_mode": self.config.llm_context_mode,
            "frontier_node_ids": [node.node_id for node in frontier],
            "frontier": [self._node_summary(node, include_diagnostics=True) for node in frontier_for_context],
            "root": self._node_summary(self.nodes.get("node_000_root"), include_diagnostics=True),
            "best_node": self._node_summary(best, include_diagnostics=True) if best else None,
            "best_trajectory": self._trajectory_summary(best) if best else [],
            "recent_completed": self._recent_completed_nodes(limit=self.config.recent_context_limit),
            "flow_observations": self._flow_observations(),
            "reliability_profile": self._reliability_profile(frontier_for_context),
            "retrieval_profile": self._retrieval_profile(frontier_for_context),
            "risk_budget": self._risk_budget_summary(),
            "analysis_context_by_node": context_by_node,
            "candidate_priorities": self._candidate_priorities_for_request(frontier_for_context),
            "analysis_plans": self._analysis_plans_for_request(frontier_for_context),
            "node_diagnoses": self._analysis_diagnoses_for_request(frontier_for_context),
            "analysis_evidence": self._analysis_evidence_for_request(frontier_for_context),
            "existing_sequences": [node.flow_sequence for node in self.nodes.values()],
            "recent_events": self._recent_policy_event_summaries(limit=5),
        }

    def _max_children_per_node(self) -> int:
        return max(1, int(self.config.batch_size or self.config.branching_factor))

    def _child_count(self, parent_id: str) -> int:
        return sum(1 for node in self.nodes.values() if node.parent_id == parent_id)

    def _get_validated_decision(self, request: Dict[str, Any], frontier: Sequence[DcpStateNode]) -> Tuple[ExpansionDecision, Any]:
        existing_node_ids = [node.node_id for node in frontier]
        target_map = {node.node_id: node for node in frontier}
        max_attempts = 1
        invalid_decisions: List[Dict[str, Any]] = []
        llm_attempts: List[Dict[str, Any]] = []

        for attempt_index in range(1, max_attempts + 1):
            attempt_request = dict(request)
            if invalid_decisions:
                attempt_request["previous_invalid_decisions"] = list(invalid_decisions)
            try:
                decision = self.policy.propose(attempt_request)
            except SelectorInvalidResponse as exc:
                error = f"LLM content parse failure: {type(exc).__name__}: {exc}"
                llm_metadata = self._policy_decision_metadata()
                if llm_metadata:
                    llm_metadata.setdefault("error", error)
                attempt_record = {
                    "attempt_index": attempt_index,
                    "request": self._policy_attempt_request_summary(attempt_request),
                    "decision": None,
                    "validation": {"valid": False, "errors": [error], "rejected_payload": getattr(exc, "raw_content", None)},
                    "exception": error,
                    "llm_metadata": llm_metadata,
                }
                llm_attempts.append(attempt_record)
                invalid_decisions.append({
                    "attempt_index": attempt_index,
                    "decision": None,
                    "validation_errors": [error],
                    "raw_payload": getattr(exc, "raw_content", None),
                    "llm_metadata": llm_metadata,
                })
                continue
            except Exception as exc:
                error = f"policy failure: {type(exc).__name__}: {exc}"
                llm_metadata = self._policy_decision_metadata()
                if llm_metadata:
                    llm_metadata.setdefault("error", error)
                llm_attempts.append({
                    "attempt_index": attempt_index,
                    "request": self._policy_attempt_request_summary(attempt_request),
                    "decision": None,
                    "validation": {"valid": False, "errors": [error], "rejected_payload": None},
                    "exception": error,
                    "llm_metadata": llm_metadata,
                })
                fallback = self.fallback_policy.propose(request)
                fallback.fallback_used = True
                fallback.reason = f"Fallback deterministic policy because LLM policy failed: {type(exc).__name__}: {exc}"
                fallback.errors.append(error)
                return self._validate_fallback_decision(fallback, request, target_map, llm_attempts)

            validation = validate_expansion_decision(
                decision,
                allowed_flows=self.config.allowed_flows,
                existing_node_ids=existing_node_ids,
                branching_factor=max(1, int(request.get("branching_factor") or self.config.branching_factor)),
                target_node=target_map.get(decision.target_node_id or ""),
                target_node_map=target_map,
                existing_sequences=[node.flow_sequence for node in self.nodes.values()],
                prunable_node_ids=existing_node_ids,
                disallow_consecutive_repeat=self.config.disallow_consecutive_repeat,
            )
            attempt_record = {
                "attempt_index": attempt_index,
                "request": self._policy_attempt_request_summary(attempt_request),
                "decision": validation.decision.to_dict(),
                "validation": validation.to_dict(),
                "llm_metadata": self._policy_decision_metadata(),
            }
            llm_attempts.append(attempt_record)
            validation.llm_attempts = list(llm_attempts)
            if validation.valid:
                return validation.decision, validation

            invalid_decisions.append({
                "attempt_index": attempt_index,
                "decision": validation.decision.to_dict(),
                "validation_errors": list(validation.errors),
                "raw_payload": validation.rejected_payload,
                "llm_metadata": attempt_record.get("llm_metadata"),
            })

        fallback = self.fallback_policy.propose(request)
        fallback.fallback_used = True
        final_errors = invalid_decisions[-1]["validation_errors"] if invalid_decisions else ["no valid LLM decision"]
        fallback.reason = "Fallback deterministic policy because proposed decision failed validation: " + "; ".join(final_errors)
        fallback.errors.extend(final_errors)
        return self._validate_fallback_decision(fallback, request, target_map, llm_attempts)

    def _validate_fallback_decision(
        self,
        fallback: ExpansionDecision,
        request: Dict[str, Any],
        target_map: Dict[str, DcpStateNode],
        llm_attempts: Sequence[Dict[str, Any]],
    ) -> Tuple[ExpansionDecision, Any]:
        validation2 = validate_expansion_decision(
            fallback,
            allowed_flows=self.config.allowed_flows,
            existing_node_ids=list(target_map.keys()),
            branching_factor=max(1, int(request.get("branching_factor") or self.config.branching_factor)),
            target_node=target_map.get(fallback.target_node_id or ""),
            target_node_map=target_map,
            existing_sequences=[node.flow_sequence for node in self.nodes.values()],
            prunable_node_ids=list(target_map.keys()),
            disallow_consecutive_repeat=self.config.disallow_consecutive_repeat,
        )
        validation2.rejected_payload = {"llm_attempts": list(llm_attempts)}
        validation2.llm_attempts = list(llm_attempts)
        return validation2.decision, validation2

    @staticmethod
    def _policy_attempt_request_summary(request: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "frontier_node_ids": request.get("frontier_node_ids"),
            "allowed_flows": request.get("allowed_flows"),
            "batch_size": request.get("batch_size"),
            "branching_factor": request.get("branching_factor"),
            "scheduler_mode": request.get("scheduler_mode"),
            "active_node_ids": request.get("active_node_ids"),
            "scheduled_runs": request.get("scheduled_runs"),
            "free_slots": request.get("free_slots"),
            "max_children_per_node": request.get("max_children_per_node"),
            "node_child_counts": request.get("node_child_counts"),
            "existing_sequences": request.get("existing_sequences"),
            "previous_invalid_decisions": request.get("previous_invalid_decisions"),
        }

    @classmethod
    def _policy_event_request_summary(cls, request: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(request, dict):
            return {}
        summary: Dict[str, Any] = {}
        for key, value in request.items():
            if key == "recent_events":
                continue
            summary[str(key)] = to_jsonable(value)
        raw_recent_events = request.get("recent_events")
        if isinstance(raw_recent_events, list):
            summary["recent_events"] = [cls._policy_event_summary(event) for event in raw_recent_events]
        elif "recent_events" in request:
            summary["recent_events"] = []
        return summary

    @classmethod
    def _policy_event_validation_summary(cls, validation: Any) -> Dict[str, Any]:
        if hasattr(validation, "to_dict"):
            summary = validation.to_dict()
        else:
            summary = validation
        if not isinstance(summary, dict):
            converted = to_jsonable(summary)
            return converted if isinstance(converted, dict) else {}
        return cls._validation_dict_summary(summary)

    @classmethod
    def _llm_attempt_summaries(cls, attempts: Any) -> List[Dict[str, Any]]:
        if not isinstance(attempts, list):
            return []
        sanitized: List[Dict[str, Any]] = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            row: Dict[str, Any] = {}
            for key, value in attempt.items():
                if key == "request" and isinstance(value, dict):
                    row["request"] = cls._policy_attempt_request_summary(value)
                elif key == "validation" and isinstance(value, dict):
                    row["validation"] = cls._validation_dict_summary(value)
                else:
                    row[str(key)] = to_jsonable(value)
            sanitized.append(row)
        return sanitized

    @classmethod
    def _policy_event_summary(cls, event: Any) -> Dict[str, Any]:
        if not isinstance(event, dict):
            data = to_jsonable(event)
            if not isinstance(data, dict):
                return {"event": data}
        else:
            data = event
        decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
        validation = data.get("validation") if isinstance(data.get("validation"), dict) else {}
        candidates = decision.get("candidates") if isinstance(decision.get("candidates"), list) else []
        candidate_flows: List[Any] = []
        candidate_targets: List[Any] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_flows.append(candidate.get("flow"))
            candidate_targets.append(candidate.get("target_node_id") or decision.get("target_node_id"))
        errors = validation.get("errors") if isinstance(validation.get("errors"), list) else data.get("errors")
        return to_jsonable({
            "event_index": data.get("event_index"),
            "timestamp": data.get("timestamp"),
            "decision_type": decision.get("decision_type"),
            "target_node_id": decision.get("target_node_id") or data.get("target_node_id"),
            "candidate_flows": candidate_flows,
            "candidate_targets": candidate_targets,
            "fallback_used": decision.get("fallback_used"),
            "stop": decision.get("stop"),
            "reason": decision.get("reason"),
            "validation_valid": validation.get("valid"),
            "validation_errors": errors or [],
            "llm_metadata": cls._llm_metadata_summary(data.get("llm_metadata")),
        })

    @classmethod
    def _validation_dict_summary(cls, validation: Dict[str, Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {}
        for key, value in validation.items():
            if key == "llm_attempts":
                continue
            if key == "rejected_payload":
                summary["rejected_payload"] = cls._rejected_payload_summary(value)
            else:
                summary[str(key)] = to_jsonable(value)
        return summary

    @classmethod
    def _rejected_payload_summary(cls, payload: Any) -> Any:
        if isinstance(payload, dict):
            summary: Dict[str, Any] = {}
            for key, value in payload.items():
                if key == "llm_attempts":
                    summary["llm_attempt_count"] = len(value) if isinstance(value, list) else None
                else:
                    summary[str(key)] = to_jsonable(value)
            return summary
        return to_jsonable(payload)

    @staticmethod
    def _llm_metadata_summary(metadata: Any) -> Dict[str, Any]:
        if not isinstance(metadata, dict):
            return {}
        keys = [
            "provider",
            "model",
            "prompt_id",
            "prompt_hash",
            "response_schema_hash",
            "temperature",
            "response_mode",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cost_usd",
            "provider_reported_cost_usd",
            "latency_sec",
        ]
        return {key: metadata.get(key) for key in keys if metadata.get(key) is not None}

    def _recent_policy_event_summaries(self, limit: int) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        return [self._policy_event_summary(event) for event in self.policy_events[-limit:]]

    async def _run_batch_scheduler(self, state: PipelineState) -> None:
        while self.run_count < self.config.max_total_runs:
            frontier = self._frontier_nodes()
            if not frontier:
                state.stop_reason = StopReason.NO_VALID_CANDIDATE
                break
            await self._maybe_run_online_analysis(frontier, state)
            remaining = self.config.max_total_runs - self.scheduled_run_count
            request = self._build_policy_request(frontier, state=state, free_slots=remaining, remaining_runs=remaining)
            decision, validation = self._get_validated_decision(request, frontier)
            self._record_policy_event(request, decision, validation)

            if decision.stop or not decision.candidates:
                state.stop_reason = StopReason.LLM_REQUESTED_STOP if decision.stop else StopReason.NO_VALID_CANDIDATE
                break

            self._apply_policy_pruning(decision, state)
            plan = self._candidate_plan(decision, frontier, mark_targets_expanded=True)
            if not plan:
                state.stop_reason = StopReason.NO_VALID_CANDIDATE
                break
            plan = plan[:remaining]
            await self._execute_candidate_batch(plan, state)

    async def _run_async_scheduler(self, state: PipelineState) -> None:
        running_tasks: Dict[asyncio.Task[DcpTransitionResult], Tuple[DcpStateNode, DcpStateNode, TransitionCandidate]] = {}

        while self.run_count < self.config.max_total_runs or running_tasks:
            launched = await self._fill_async_slots(running_tasks, state)
            if not running_tasks:
                if self.scheduled_run_count >= self.config.max_total_runs:
                    break
                if not launched:
                    if state.stop_reason is None:
                        state.stop_reason = StopReason.NO_VALID_CANDIDATE
                    break

            if not running_tasks:
                continue

            done, _pending = await asyncio.wait(set(running_tasks.keys()), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                node, parent, candidate = running_tasks.pop(task)
                state.active_node_ids = [item[0].node_id for item in running_tasks.values()]
                try:
                    step = task.result()
                except Exception as exc:
                    step = self._transition_failure_result(node, parent, candidate, exc)
                await self._finalize_child_execution(node, parent, candidate, step, state)
                state.active_node_ids = [item[0].node_id for item in running_tasks.values()]
                self._persist_state(state)

    async def _fill_async_slots(
        self,
        running_tasks: Dict[asyncio.Task[DcpTransitionResult], Tuple[DcpStateNode, DcpStateNode, TransitionCandidate]],
        state: PipelineState,
    ) -> bool:
        if state.stop_reason is not None:
            return False
        launched_any = False
        while len(running_tasks) < self.config.max_parallel_jobs and self.scheduled_run_count < self.config.max_total_runs:
            free_slots = min(
                self.config.max_parallel_jobs - len(running_tasks),
                self.config.max_total_runs - self.scheduled_run_count,
            )
            if free_slots <= 0:
                break
            frontier = self._frontier_nodes()
            if not frontier:
                break
            await self._maybe_run_online_analysis(frontier, state)

            request = self._build_policy_request(
                frontier,
                state=state,
                free_slots=free_slots,
                remaining_runs=self.config.max_total_runs - self.scheduled_run_count,
            )
            decision, validation = self._get_validated_decision(request, frontier)
            self._record_policy_event(request, decision, validation)
            if decision.stop or not decision.candidates:
                if state.stop_reason is None:
                    state.stop_reason = StopReason.LLM_REQUESTED_STOP if decision.stop else StopReason.NO_VALID_CANDIDATE
                break

            self._apply_policy_pruning(decision, state)
            plan = self._candidate_plan(decision, frontier, mark_targets_expanded=False)
            if not plan:
                if not running_tasks and not launched_any:
                    state.stop_reason = StopReason.NO_VALID_CANDIDATE
                break

            launched_this_round = 0
            for parent, candidate in plan[:free_slots]:
                if self.scheduled_run_count >= self.config.max_total_runs:
                    break
                node, parent, candidate = self._create_child_node(parent, candidate, state)
                task = asyncio.create_task(self._execute_child_task(node, parent, candidate))
                running_tasks[task] = (node, parent, candidate)
                self.scheduled_run_count += 1
                launched_this_round += 1
                launched_any = True
            state.active_node_ids = [item[0].node_id for item in running_tasks.values()]
            self._persist_state(state)
            if launched_this_round == 0:
                break
        return launched_any

    async def _execute_child_task(self, node: DcpStateNode, parent: DcpStateNode, candidate: TransitionCandidate) -> DcpTransitionResult:
        """Evaluate one candidate via the reusable DCP transition executor.

        FrontierExpansionRunner remains the outer run coordination layer. It selects the
        parent/candidate and later applies the returned facts to the search
        graph; execution, output validation, post-run analysis and QoR scoring
        live in DcpTransitionExecutor so manual/fixed-loop runners can reuse
        the same inner step.
        """
        request = DcpTransitionRequest(
            input_dcp=Path(node.input_dcp),
            output_dcp=Path(node.output_dcp or ""),
            output_dir=Path(node.artifact_dir or ""),
            optimizer_name=candidate.flow,
            root_metrics=dict(self.root_metrics),
            parent_metrics=dict(parent.metrics or {}),
            optimizer_params={"flow": candidate.flow},
            run_id=self.artifacts.run_dir.name if self.artifacts else None,
            parent_node_id=parent.node_id,
            child_node_id=node.node_id,
            candidate_id=f"{node.node_id}:{candidate.flow}",
            flow_sequence_before=list(parent.flow_sequence or []),
            mode="experiment",
            metadata={
                "policy_type": self.config.policy_type,
                "depth": node.depth,
                "candidate": candidate.to_dict(),
            },
        )
        return await self.transition_executor.run(request, history=list(self.nodes.values()))

    def _transition_failure_result(self, node: DcpStateNode, parent: DcpStateNode, candidate: TransitionCandidate, exc: BaseException) -> DcpTransitionResult:
        execution = ExecutionResult(
            success=False,
            flow=candidate.flow,
            input_dcp=node.input_dcp,
            output_dcp=node.output_dcp or "",
            run_dir=str(Path(node.artifact_dir or "") / "executor_run"),
            metrics={},
            exit_code=1,
            error=f"{type(exc).__name__}: {exc}",
        )
        output_validation = self.output_validator.validate(execution)
        qor = compute_qor_score(
            self.root_metrics,
            {},
            weights=self.config.qor_weights(),
            valid_output=False,
            path_runtime_seconds=float(parent.metrics.get("path_runtime_seconds") or 0.0),
        )
        return DcpTransitionResult(
            success=False,
            accepted=False,
            input_dcp=node.input_dcp,
            output_dcp=node.output_dcp,
            optimizer_name=candidate.flow,
            optimizer_params={"flow": candidate.flow},
            validation_status=output_validation.status,
            rejection_reason=f"transition_executor_exception: {type(exc).__name__}",
            execution=execution,
            output_validation=output_validation,
            analysis_context={},
            metrics={},
            qor_score=qor,
            runtime_seconds=0.0,
            events=[{"event_type": "DCP_TRANSITION_EXCEPTION", "error": f"{type(exc).__name__}: {exc}"}],
            transition_record={
                "transition_id": node.node_id,
                "run_id": self.artifacts.run_dir.name if self.artifacts else None,
                "parent_node_id": parent.node_id,
                "child_node_id": node.node_id,
                "input_dcp": node.input_dcp,
                "output_dcp": node.output_dcp,
                "optimizer_name": candidate.flow,
                "validation_status": output_validation.status,
                "accepted": False,
                "rejection_reason": f"transition_executor_exception: {type(exc).__name__}",
                "pure_qor_score": qor.total_score,
                "qor_status": qor.status,
            },
        )

    def _candidate_plan(
        self,
        decision: ExpansionDecision,
        frontier: Sequence[DcpStateNode],
        *,
        mark_targets_expanded: bool,
    ) -> List[Tuple[DcpStateNode, TransitionCandidate]]:
        frontier_map = {node.node_id: node for node in frontier}
        plan: List[Tuple[DcpStateNode, TransitionCandidate]] = []
        planned_counts: Dict[str, int] = {}
        max_children = self._max_children_per_node()
        for cand in sorted(decision.candidates, key=lambda c: c.priority, reverse=True):
            target_id = cand.target_node_id or decision.target_node_id
            target = frontier_map.get(target_id or "")
            if target is None:
                continue
            current_children = self._child_count(target.node_id) + planned_counts.get(target.node_id, 0)
            if current_children >= max_children:
                continue
            cand.target_node_id = target.node_id
            plan.append((target, cand))
            planned_counts[target.node_id] = planned_counts.get(target.node_id, 0) + 1
        if mark_targets_expanded:
            # Batch mode keeps the original epoch semantics for reproducibility.
            for target, _cand in plan:
                self.expanded_node_ids.add(target.node_id)
        return plan

    MAX_EXECUTION_RETRIES_PER_RUN = 3

    @staticmethod
    def _hard_execution_failure_reason(step: DcpTransitionResult) -> Optional[str]:
        """Why this attempt yielded no evidence about the decision, or None.

        A hard execution failure means the action never happened, so the ranking
        that chose it was never actually tested. That is categorically different
        from a flow that ran and scored badly, or one that ran, produced a
        checkpoint, and failed log-scan validation -- both of those are real
        evidence and are left exactly as they are.

        The missing-artifact check is not redundant with the exit code: the
        executor has been observed reporting success with exit_code 0 while
        writing no output DCP at all.
        """
        execution = step.execution
        if not execution.success:
            return f"executor reported failure (exit_code={execution.exit_code}): {execution.error or 'no error text'}"
        if int(execution.exit_code or 0) != 0:
            return f"non-zero exit code {execution.exit_code}"
        output_dcp = execution.output_dcp or step.output_dcp
        if not output_dcp or not Path(output_dcp).exists():
            return "output DCP missing after a nominally successful execution"
        return None

    def _ranked_alternates(self, parent_id: str, exclude: Sequence[str]) -> Tuple[List[str], str]:
        """Flows for parent_id in the ranking ALREADY computed at this decision point.

        No LLM call and no re-analysis: the design state has not changed, because
        nothing executed successfully. Two sources, matching how each condition
        ranks -- analysis candidate_priorities for the LLM levels, the
        deterministic heuristic ordering for the no-LLM ones.
        """
        blocked = {str(f) for f in exclude if f}
        allowed = [str(f) for f in self.config.allowed_flows]

        # S1-S3: priorities the analysis loop already produced for this node.
        if self.analysis_controller is not None:
            rows = [
                p for p in self.analysis_controller.memory.candidate_priorities
                if getattr(p, "node_id", None) == parent_id and getattr(p, "flow", None) in allowed
            ]
            if rows:
                rows.sort(key=lambda r: float(getattr(r, "priority_score", 0.0) or 0.0), reverse=True)
                ordered = [str(r.flow) for r in rows if str(r.flow) not in blocked]
                if ordered:
                    return ordered, "analysis_candidate_priorities"

        # S0 and the other no-LLM policies: their own score ordering, read from
        # the same context the policy was handed.
        context = self._compact_analysis_context(parent_id, for_llm=self._policy_is_llm_facing())
        ranked = DeterministicExpansionPolicy._ranked_flows_for_node(parent_id, context, allowed)
        ordered = [flow for flow, _score in ranked if flow not in blocked]
        return ordered, "deterministic_flow_ranking"

    async def _retry_failed_executions(
        self,
        failures: Sequence[Tuple[DcpStateNode, DcpStateNode, TransitionCandidate, str]],
        state: PipelineState,
    ) -> None:
        """One substitute attempt per failed decision point, capped per run.

        Deliberately non-recursive: a retry that itself fails is a dead end and
        is handled exactly as a failure is handled today.
        """
        for parent, failed_node, candidate, reason in failures:
            if self.retry_count >= self.MAX_EXECUTION_RETRIES_PER_RUN:
                self._record_retry_skip(parent, candidate, reason, "global retry cap reached")
                continue
            event_index = (self.policy_events[-1] or {}).get("event_index") if self.policy_events else None
            key = (parent.node_id, event_index)
            if key in self.retried_decision_points:
                self._record_retry_skip(parent, candidate, reason, "decision point already retried")
                continue
            if self.scheduled_run_count >= self.config.max_total_runs:
                self._record_retry_skip(parent, candidate, reason, "run budget exhausted")
                continue

            tried = [child.selected_flow for child in self.nodes.values() if child.parent_id == parent.node_id]
            exclude = list(tried) + [candidate.flow]
            if self.config.disallow_consecutive_repeat and parent.flow_sequence:
                exclude.append(parent.flow_sequence[-1])
            alternates, source = self._ranked_alternates(parent.node_id, exclude)
            if not alternates:
                self._record_retry_skip(parent, candidate, reason, "no alternate flow left in the ranking")
                continue

            substitute_flow = alternates[0]
            self.retried_decision_points.add(key)
            self.retry_count += 1

            retry_candidate = TransitionCandidate(
                flow=substitute_flow,
                priority=float(getattr(candidate, "priority", 0.0) or 0.0),
                target_node_id=parent.node_id,
                expected_effect=(
                    f"retry after hard execution failure of '{candidate.flow}' "
                    f"({reason}); next-ranked flow from {source}"
                ),
                risk=getattr(candidate, "risk", "medium"),
                confidence=getattr(candidate, "confidence", "medium"),
            )
            event = {
                "retry_index": self.retry_count,
                "parent_node_id": parent.node_id,
                "failed_node_id": failed_node.node_id,
                "failed_flow": candidate.flow,
                "substituted_flow": substitute_flow,
                "reason": reason,
                "ranking_source": source,
                "rank_index": 0,
                "ranked_alternates": alternates[:5],
                "policy_event_index": event_index,
                "outcome_node_id": None,
                "outcome_status": None,
            }
            self.retry_events.append(event)

            node, node_parent, node_candidate = self._create_child_node(parent, retry_candidate, state)
            node.metrics = dict(node.metrics or {})
            node.metrics["execution_retry"] = dict(event)
            self.scheduled_run_count += 1
            state.active_node_ids = list(state.active_node_ids) + [node.node_id]
            self._persist_state(state)

            step = await self._execute_child_task(node, node_parent, node_candidate)
            await self._finalize_child_execution(node, node_parent, node_candidate, step, state)

            event["outcome_node_id"] = node.node_id
            event["outcome_status"] = node.status.value if hasattr(node.status, "value") else str(node.status)
            event["outcome_validation_status"] = node.validation_status
            if self.artifacts is not None:
                self.artifacts.write_json(self.artifacts.run_dir / "retry_events.json", self.retry_events)

    def _record_retry_skip(self, parent: DcpStateNode, candidate: TransitionCandidate, reason: str, skipped_because: str) -> None:
        self.retry_events.append({
            "retry_index": None,
            "parent_node_id": parent.node_id,
            "failed_flow": candidate.flow,
            "substituted_flow": None,
            "reason": reason,
            "skipped": True,
            "skipped_because": skipped_because,
        })
        if self.artifacts is not None:
            self.artifacts.write_json(self.artifacts.run_dir / "retry_events.json", self.retry_events)

    async def _execute_candidate_batch(self, plan: Sequence[Tuple[DcpStateNode, TransitionCandidate]], state: PipelineState) -> None:
        if not plan:
            return
        children = [self._create_child_node(parent, candidate, state) for parent, candidate in plan]
        self.scheduled_run_count += len(children)
        state.active_node_ids = [node.node_id for node, _parent, _candidate in children]
        self._persist_state(state)

        semaphore = asyncio.Semaphore(self.config.max_parallel_jobs)

        async def run_one(node: DcpStateNode, parent: DcpStateNode, candidate: TransitionCandidate) -> Tuple[DcpStateNode, DcpStateNode, TransitionCandidate, DcpTransitionResult]:
            async with semaphore:
                step = await self._execute_child_task(node, parent, candidate)
                return node, parent, candidate, step

        tasks = [asyncio.create_task(run_one(node, parent, candidate)) for node, parent, candidate in children]
        hard_failures: List[Tuple[DcpStateNode, DcpStateNode, TransitionCandidate, str]] = []
        for task in asyncio.as_completed(tasks):
            node, parent, candidate, step = await task
            await self._finalize_child_execution(node, parent, candidate, step, state)
            reason = self._hard_execution_failure_reason(step)
            if reason:
                hard_failures.append((parent, node, candidate, reason))
        state.active_node_ids = []
        self._persist_state(state)

        if hard_failures:
            # Deterministic order so a batch with several failures retries the
            # same way every time.
            hard_failures.sort(key=lambda item: _node_numeric_id(item[1].node_id))
            await self._retry_failed_executions(hard_failures, state)
            state.active_node_ids = []
            self._persist_state(state)

    def _policy_event_context_for_candidate(
        self,
        parent: DcpStateNode,
        candidate: TransitionCandidate,
    ) -> Tuple[Optional[int], Optional[int], Any, Dict[str, Any], Dict[str, Any]]:
        event = self.policy_events[-1] if self.policy_events else {}
        decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
        validation = event.get("validation") if isinstance(event.get("validation"), dict) else {}
        request = event.get("request")
        event_index = event.get("event_index")
        candidate_rank = None
        candidates = decision.get("candidates") if isinstance(decision.get("candidates"), list) else []
        for idx, item in enumerate(candidates):
            if not isinstance(item, dict):
                continue
            target_id = str(item.get("target_node_id") or decision.get("target_node_id") or "")
            flow = str(item.get("flow") or "")
            if target_id == parent.node_id and flow == candidate.flow:
                candidate_rank = idx
                break
        return event_index, candidate_rank, request, decision, validation

    def _build_scheduled_transition_record(self, parent: DcpStateNode, node: DcpStateNode, candidate: TransitionCandidate) -> DcpTransitionRecord:
        event_index, candidate_rank, request, decision, validation = self._policy_event_context_for_candidate(parent, candidate)
        return DcpTransitionRecord.from_frontier_candidate_scheduled(
            run_id=self.artifacts.run_dir.name if self.artifacts else None,
            parent_node=parent,
            child_node=node,
            candidate=candidate,
            event_index=event_index,
            candidate_rank=candidate_rank,
            request=request,
            decision=decision,
            validation=validation,
        )

    def _create_child_node(self, parent: DcpStateNode, candidate: TransitionCandidate, state: PipelineState) -> Tuple[DcpStateNode, DcpStateNode, TransitionCandidate]:
        assert self.artifacts is not None
        index = len(self.nodes)
        node_dir = self.artifacts.node_dir(index, f"{candidate.flow}")
        output_dcp = node_dir / "output.dcp"
        node = DcpStateNode(
            node_id=node_dir.name,
            parent_id=parent.node_id,
            depth=parent.depth + 1,
            input_dcp=parent.output_dcp or parent.input_dcp,
            output_dcp=str(output_dcp),
            flow_sequence=list(parent.flow_sequence) + [candidate.flow],
            selected_flow=candidate.flow,
            status=NodeStatus.RUNNING,
            artifact_dir=str(node_dir),
        )
        self.nodes[node.node_id] = node
        self.artifacts.write_json(node_dir / "candidate.json", candidate.to_dict())
        scheduled_record = self._build_scheduled_transition_record(parent, node, candidate)
        node.metrics.setdefault("dcp_transition_scheduled_record", scheduled_record.to_dict())
        self.artifacts.write_node_state(node)
        if self.transition_record_store is not None:
            self.transition_record_store.append(scheduled_record)
        self._write_policy_candidates_snapshot()
        return node, parent, candidate

    async def _finalize_child_execution(
        self,
        node: DcpStateNode,
        parent: DcpStateNode,
        candidate: TransitionCandidate,
        step: DcpTransitionResult,
        state: PipelineState,
    ) -> None:
        """Apply DcpTransitionExecutor facts to the frontier-expansion state."""
        assert self.artifacts is not None
        node_dir = Path(node.artifact_dir)
        self.run_count += 1
        if node.node_id in state.active_node_ids:
            state.active_node_ids = [node_id for node_id in state.active_node_ids if node_id != node.node_id]

        execution = step.execution
        output_validation = step.output_validation
        self.execution_events.append({
            "node_id": node.node_id,
            "flow": candidate.flow,
            "success": execution.success,
            "exit_code": execution.exit_code,
            "error": execution.error,
            "transition_executor": "DcpTransitionExecutor",
        })
        self.output_validations[node.node_id] = output_validation
        step_seconds = float(step.runtime_seconds or 0.0)
        if self._hard_execution_failure_reason(step):
            self.failed_execution_seconds += step_seconds
        else:
            self.productive_execution_seconds += step_seconds
        scheduled_record_data = node.metrics.get("dcp_transition_scheduled_record") if isinstance(node.metrics, dict) else None
        updated_incumbent = False

        self.analysis_contexts[node.node_id] = dict(step.analysis_context or {})
        qor = step.qor_score
        self.qor_scores[node.node_id] = qor
        self._backfill_root_metrics_from_step(step.metrics or {})
        metrics = dict(step.metrics or {})
        if scheduled_record_data:
            metrics["dcp_transition_scheduled_record"] = scheduled_record_data
        metrics["candidate"] = candidate.to_dict()
        metrics["dcp_transition"] = {
            "optimizer_name": step.optimizer_name,
            "accepted": step.accepted,
            "rejection_reason": step.rejection_reason,
            "transition_record": step.transition_record,
        }
        if output_validation.valid and qor.status == "regressed":
            metrics["validation_status_effective"] = ValidationStatus.REGRESSED.value

        node.metrics = metrics
        node.score = qor.total_score
        node.validation_status = output_validation.status
        node.validation_warnings = list(output_validation.warnings)
        node.validation_errors = list(output_validation.errors)
        node.fallback_used = bool(output_validation.fallback_used)

        # Persist a node-level copy of the inner-loop result for audit/replay.
        self.artifacts.write_json(node_dir / "dcp_transition_result_full.json", step.to_dict())

        if output_validation.valid:
            node.status = NodeStatus.COMPLETED
            if node.node_id not in state.completed_node_ids:
                state.completed_node_ids.append(node.node_id)
            if self._is_better_than_current_best(node, state):
                state.best_node_id = node.node_id
                updated_incumbent = True
        else:
            node.status = NodeStatus.FAILED
            if node.node_id not in state.failed_node_ids:
                state.failed_node_ids.append(node.node_id)

        if (not output_validation.valid and self.config.prune_invalid) or (qor.status == "regressed" and self.config.prune_severe_regression):
            self.pruned_node_ids.add(node.node_id)
            if node.node_id not in state.pruned_node_ids:
                state.pruned_node_ids.append(node.node_id)
            node.metrics["pruned_by_qor_gate"] = True
            node.metrics["prune_reason"] = "invalid" if not output_validation.valid else "severe_regression"

        self.artifacts.write_node_state(node)
        if self.transition_record_store is not None:
            try:
                scheduled_record = record_from_dict(scheduled_record_data) if isinstance(scheduled_record_data, dict) else self._build_scheduled_transition_record(parent, node, candidate)
            except Exception:
                scheduled_record = self._build_scheduled_transition_record(parent, node, candidate)
            completed_record = DcpTransitionRecord.from_frontier_candidate_completed(
                scheduled_record=scheduled_record,
                result=step,
                child_node_id=node.node_id,
                child_status=node.status.value if hasattr(node.status, "value") else str(node.status),
                updated_incumbent=updated_incumbent,
            )
            self.transition_record_store.append(completed_record)
        self._write_policy_candidates_snapshot()
        self._persist_state(state)

    def _is_better_than_current_best(self, node: DcpStateNode, state: PipelineState) -> bool:
        if node.depth == 0 or node.status != NodeStatus.COMPLETED:
            return False
        if state.best_node_id is None:
            return True
        current = self.nodes.get(state.best_node_id)
        current_pure_qor = current.score if current and current.score is not None else float("-inf")
        node_pure_qor = node.score if node.score is not None else float("-inf")
        return node_pure_qor > current_pure_qor

    def _apply_policy_pruning(self, decision: ExpansionDecision, state: PipelineState) -> None:
        for node_id in decision.prune_node_ids:
            node = self.nodes.get(node_id)
            if node is None:
                continue
            node.metrics.setdefault("policy_prune_suggestions", []).append({
                "reason": decision.reason,
                "mode": self.config.llm_prune_mode,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            if self.config.llm_prune_mode != "hard":
                if self.artifacts:
                    self.artifacts.write_node_state(node)
                continue
            if node.status != NodeStatus.COMPLETED or node_id in state.active_node_ids:
                continue
            if node_id in self.nodes and node_id not in self.pruned_node_ids:
                self.pruned_node_ids.add(node_id)
                if node_id not in state.pruned_node_ids:
                    state.pruned_node_ids.append(node_id)
                node.metrics["pruned_by_policy"] = True
                node.metrics["policy_prune_reason"] = decision.reason
                if self.artifacts:
                    self.artifacts.write_node_state(node)

    def _record_policy_event(self, request: Dict[str, Any], decision: ExpansionDecision, validation: Any) -> None:
        if self.artifacts is None:
            return
        event_dir = self.artifacts.run_dir / "policy_events"
        event_index = len(self.policy_events)
        llm_metadata = self._policy_decision_metadata()
        llm_attempts = self._llm_attempt_summaries(getattr(validation, "llm_attempts", []))
        event = {
            "event_index": event_index,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "request": self._policy_event_request_summary(request),
            "decision": decision.to_dict(),
            "llm_metadata": llm_metadata,
            "validation": self._policy_event_validation_summary(validation),
        }
        self.policy_events.append(event)
        self.llm_decision_events.extend(build_llm_decision_event_rows(
            run_id=self.artifacts.run_dir.name,
            event_index=event_index,
            attempts=llm_attempts,
            final_decision=event["decision"],
        ))
        if not getattr(validation, "valid", False):
            self.validation_events.append({
                "event_index": event_index,
                "errors": getattr(validation, "errors", []),
                "target_node_id": decision.target_node_id,
            })
        self.artifacts.write_json(event_dir / f"policy_event_{event_index:03d}.json", event)
        self.artifacts.write_json(self.artifacts.run_dir / "policy_events.json", self.policy_events)
        write_llm_decision_events(self.artifacts.run_dir, self.llm_decision_events)
        self._write_policy_candidates_snapshot()

    def _policy_decision_metadata(self) -> Dict[str, Any]:
        if hasattr(self.policy, "last_decision_metadata"):
            try:
                metadata = self.policy.last_decision_metadata()
                return to_jsonable(metadata) if isinstance(metadata, dict) else {}
            except Exception:
                return {}
        return {}

    def _best_node(self, include_root: bool = False) -> Optional[DcpStateNode]:
        candidates = []
        for node in self.nodes.values():
            if node.status != NodeStatus.COMPLETED:
                continue
            if not include_root and node.depth == 0:
                continue
            if node.depth > 0 and node.validation_status == ValidationStatus.INVALID.value:
                continue
            if node.node_id in self.pruned_node_ids:
                continue
            if node.output_dcp and not Path(node.output_dcp).exists():
                continue
            candidates.append(node)
        if not candidates:
            return None
        return max(candidates, key=lambda node: node.score if node.score is not None else float("-inf"))

    async def _run_equivalence_gate(
        self, input_dcp: Path, final_output: Optional[Path], state: PipelineState
    ) -> Dict[str, Any]:
        """Mandatory real functional-equivalence gate on a run's single published
        result (RapidWright structural comparison + Vivado/xsim simulation), run
        once per pipeline run against the finalized best-output DCP -- never
        against intermediate candidates explored during search, which keep using
        the existing cheap log-scan OutputValidator. No checker configured (e.g.
        --mock runs, where the DCPs are not real Vivado output) or no final
        output produced (e.g. --analysis-only) both skip cleanly.
        """
        import time as _time

        _gate_started = _time.time()
        if self.equivalence_checker is None or final_output is None:
            result: Dict[str, Any] = {"status": "skipped", "passed": None}
            state.equivalence_gate = result
            return result
        try:
            raw = await self.equivalence_checker(Path(input_dcp), Path(final_output))
            self.verification_seconds += _time.time() - _gate_started
        except Exception as exc:
            # A checker that did not complete is not evidence of a functional
            # mismatch. Preserve failure while making the infrastructure gap
            # explicit to callers; do not rerun the selected candidate.
            raw = {"passed": False, "error": f"{type(exc).__name__}: {exc}",
                   "infrastructure_failure": True, "error_type": type(exc).__name__}
        result = dict(raw)
        result["status"] = "passed" if result.get("passed") else "failed"
        state.equivalence_gate = result
        if self.artifacts is not None:
            self.artifacts.write_json(self.artifacts.run_dir / "equivalence_gate.json", result)
        return result

    def _prune_dcps_keep_winning_path(self, state: PipelineState) -> Dict[str, Any]:
        """Delete DCP artefacts for nodes off the winning path. Opt-in only.

        Refuses to act unless the winner is known, so a run that failed to
        publish a best result never loses data. Metadata, JSON and executor
        logs are untouched for every node, winner or not.
        """
        record: Dict[str, Any] = {
            "enabled": True,
            "kept_node_ids": [],
            "pruned_node_ids": [],
            "bytes_freed": 0,
            "errors": [],
        }
        best_id = getattr(state, "best_node_id", None)
        if not best_id or best_id not in self.nodes:
            record["skipped_reason"] = "winning node unknown; nothing deleted"
            return record

        keep: set[str] = set()
        cursor: Optional[Any] = self.nodes.get(best_id)
        while cursor is not None and cursor.node_id not in keep:
            keep.add(cursor.node_id)
            parent = getattr(cursor, "parent_id", None)
            cursor = self.nodes.get(parent) if parent else None
        record["kept_node_ids"] = sorted(keep)

        for node_id, node in sorted(self.nodes.items()):
            if node_id in keep:
                continue
            artifact_dir = getattr(node, "artifact_dir", None)
            if not artifact_dir:
                continue
            node_dir = Path(artifact_dir)
            pruned_here = False
            for target in (node_dir / "output.dcp", node_dir / "output.dcp.edf"):
                if not target.exists():
                    continue
                try:
                    if target.is_dir():
                        size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
                        shutil.rmtree(target)
                    else:
                        size = target.stat().st_size
                        target.unlink()
                    record["bytes_freed"] += size
                    pruned_here = True
                except OSError as exc:
                    record["errors"].append(f"{target}: {exc}")
            if pruned_here:
                record["pruned_node_ids"].append(node_id)

        record["bytes_freed_mb"] = round(record["bytes_freed"] / (1024 * 1024), 1)
        print(
            "[FRONTIER] DCP pruning: kept %d node(s) on the winning path, "
            "pruned %d, freed %.1f MB"
            % (len(record["kept_node_ids"]), len(record["pruned_node_ids"]),
               record["bytes_freed_mb"])
        )
        return record

    def _finalize_best_output(self, state: PipelineState) -> Optional[Path]:
        best = self._best_node(include_root=False)
        if best is None or not best.output_dcp:
            return None
        state.best_node_id = best.node_id
        source = Path(best.output_dcp)
        if not source.exists():
            return None
        assert self.artifacts is not None
        return self.artifacts.copy_final_output(source)

    def _node_summary(self, node: Optional[DcpStateNode], *, include_diagnostics: bool = False) -> Optional[Dict[str, Any]]:
        if node is None:
            return None
        qos = self.qor_scores.get(node.node_id)
        summary = {
            "node_id": node.node_id,
            "parent_id": node.parent_id,
            "depth": node.depth,
            "flow_sequence": node.flow_sequence,
            "selected_flow": node.selected_flow,
            "status": node.status.value if hasattr(node.status, "value") else str(node.status),
            "validation_status": node.validation_status,
            "validation_warnings": node.validation_warnings[:5],
            "validation_errors": node.validation_errors[:5],
            "fallback_used": node.fallback_used,
            "pure_qor_score": node.score,
            "trajectory_objective_profile": "pure_qor_v1",
            "trajectory_objective_score": node.score,
            "qor_status": qos.status if qos else None,
            "path_runtime_seconds": node.metrics.get("path_runtime_seconds"),
            "node_runtime_seconds": node.metrics.get("node_runtime_seconds"),
            "final_fmax_mhz": node.metrics.get("final_fmax_mhz") or node.metrics.get("fmax_mhz"),
            "final_wns_ns": node.metrics.get("final_wns_ns"),
            "final_tns_ns": node.metrics.get("final_tns_ns"),
            "delta_from_root": self._delta_from_root(node),
        }
        if include_diagnostics:
            summary["key_diagnostics"] = self._diagnostic_summary(node.node_id)
        return summary

    def _delta_from_root(self, node: DcpStateNode) -> Dict[str, Optional[float]]:
        root_fmax = self.root_metrics.get("final_fmax_mhz") or self.root_metrics.get("initial_fmax_mhz")
        root_wns = self.root_metrics.get("final_wns_ns") or self.root_metrics.get("initial_wns_ns")
        node_fmax = node.metrics.get("final_fmax_mhz") or node.metrics.get("fmax_mhz")
        node_wns = node.metrics.get("final_wns_ns")
        def delta(a: Any, b: Any) -> Optional[float]:
            try:
                if a is None or b is None:
                    return None
                return float(a) - float(b)
            except (TypeError, ValueError):
                return None
        return {"fmax_mhz": delta(node_fmax, root_fmax), "wns_ns": delta(node_wns, root_wns)}

    def _policy_is_llm_facing(self) -> bool:
        """Whether the configured expansion policy hands its request to an LLM.

        Only gates the expansion-policy request. The analysis loop's own view is
        stripped unconditionally in analysis/tools/artifact_tools.py, so an
        LLM-backed analysis loop stays clean even under a no-LLM policy.
        """
        return str(self.config.policy_type or "") not in NO_LLM_POLICY_TYPES

    def _compact_analysis_context(self, node_id: str, *, for_llm: Optional[bool] = None) -> Dict[str, Any]:
        ctx = self.analysis_contexts.get(node_id, {})
        if not ctx:
            return {}
        strip = self._policy_is_llm_facing() if for_llm is None else bool(for_llm)
        candidate_flows = ctx.get("candidate_flows")
        return {
            "schema_version": ctx.get("schema_version"),
            "benchmark_name": ctx.get("benchmark_name"),
            "timing": ctx.get("timing"),
            "critical_path_features": ctx.get("critical_path_features"),
            "congestion": ctx.get("congestion"),
            "fanout": ctx.get("fanout"),
            "utilization_summary": ctx.get("utilization_summary"),
            "candidate_flows": sanitize_candidate_flows(candidate_flows) if strip else candidate_flows,
            "previous_node_metrics": ctx.get("previous_node_metrics"),
            "analysis_provider_error": ctx.get("analysis_provider_error"),
        }

    def _diagnostic_summary(self, node_id: str) -> Dict[str, Any]:
        ctx = self.analysis_contexts.get(node_id, {})
        return {
            "timing": ctx.get("timing"),
            "critical_path_features": ctx.get("critical_path_features"),
            "congestion": ctx.get("congestion"),
            "fanout": ctx.get("fanout"),
        }

    def _trajectory_summary(self, node: Optional[DcpStateNode]) -> List[Dict[str, Any]]:
        if not node:
            return []
        path_nodes: List[DcpStateNode] = []
        cursor: Optional[DcpStateNode] = node
        while cursor is not None:
            path_nodes.append(cursor)
            cursor = self.nodes.get(cursor.parent_id) if cursor.parent_id else None
        path_nodes.reverse()
        return [self._node_summary(item, include_diagnostics=False) for item in path_nodes if self._node_summary(item)]

    def _recent_completed_nodes(self, limit: int) -> List[Dict[str, Any]]:
        nodes = [node for node in self.nodes.values() if node.depth > 0 and node.status in {NodeStatus.COMPLETED, NodeStatus.FAILED}]
        nodes.sort(key=lambda node: _node_numeric_id(node.node_id))
        return [self._node_summary(node, include_diagnostics=False) for node in nodes[-limit:]]

    def _flow_observations(self) -> Dict[str, Any]:
        obs: Dict[str, Dict[str, Any]] = {flow: {"clean": 0, "warning": 0, "fallback": 0, "invalid": 0, "regressed": 0, "best_pure_qor_score": None, "recent_messages": []} for flow in self.config.allowed_flows}
        for node in self.nodes.values():
            if not node.selected_flow:
                continue
            item = obs.setdefault(node.selected_flow, {"clean": 0, "warning": 0, "fallback": 0, "invalid": 0, "regressed": 0, "best_pure_qor_score": None, "recent_messages": []})
            status = node.validation_status or ValidationStatus.CLEAN.value
            if status == ValidationStatus.INVALID.value:
                item["invalid"] += 1
            elif status == ValidationStatus.FALLBACK_USED.value:
                item["fallback"] += 1
            elif status == ValidationStatus.WARNING.value:
                item["warning"] += 1
            else:
                item["clean"] += 1
            qos = self.qor_scores.get(node.node_id)
            if qos and qos.status == "regressed":
                item["regressed"] += 1
            if node.score is not None and (item["best_pure_qor_score"] is None or node.score > item["best_pure_qor_score"]):
                item["best_pure_qor_score"] = node.score
            messages = node.validation_errors or node.validation_warnings
            if messages:
                item["recent_messages"] = (item["recent_messages"] + messages)[-3:]
        return obs

    def _reliability_profile(self, frontier: Sequence[DcpStateNode]) -> Dict[str, Any]:
        observations = self._flow_observations()
        profile: Dict[str, Any] = {"flows": {}, "frontier_candidates": []}
        for flow in self.config.allowed_flows:
            global_rec = self.reliability_table.flow_record(flow)
            local_obs = observations.get(flow) if isinstance(observations.get(flow), dict) else None
            profile["flows"][flow] = self.reliability_table.risk_for(flow=flow, local_observation=local_obs)
            if global_rec:
                profile["flows"][flow]["global_stats"] = global_rec
        for node in frontier:
            last_flow = node.flow_sequence[-1] if node.flow_sequence else None
            for flow in self.config.allowed_flows:
                if self.config.disallow_consecutive_repeat and flow == last_flow:
                    continue
                risk = self.reliability_table.risk_for(
                    flow=flow,
                    last_flow=last_flow,
                    design=str(node.metrics.get("benchmark_name") or "") or None,
                    local_observation=observations.get(flow) if isinstance(observations.get(flow), dict) else None,
                )
                profile["frontier_candidates"].append({
                    "target_node_id": node.node_id,
                    "candidate_flow": flow,
                    "clean_prob": risk.get("clean_prob"),
                    "invalid_risk": risk.get("invalid_risk"),
                    "fallback_risk": risk.get("fallback_risk"),
                    "warning_risk": risk.get("warning_risk"),
                    "expected_delta_pure_qor": risk.get("expected_delta_pure_qor"),
                })
        profile["frontier_candidates"] = sorted(
            profile["frontier_candidates"],
            key=lambda item: (item.get("clean_prob") or 0.0, -(item.get("invalid_risk") or 0.0)),
            reverse=True,
        )[: max(12, self.config.frontier_context_limit * 3)]
        return profile

    def _retrieval_profile(self, frontier: Sequence[DcpStateNode]) -> Dict[str, Any]:
        if self.retrieval_index is None:
            return {"enabled": False, "index_path": self.config.retrieval_index_path, "frontier_candidates": []}
        profile: Dict[str, Any] = {"enabled": True, "index_path": self.config.retrieval_index_path, "frontier_candidates": []}
        for node in frontier:
            last_flow = node.flow_sequence[-1] if node.flow_sequence else None
            node_summary = self._node_summary(node, include_diagnostics=True) or {}
            analysis = self._compact_analysis_context(node.node_id)
            for flow in self.config.allowed_flows:
                if self.config.disallow_consecutive_repeat and flow == last_flow:
                    continue
                try:
                    evidence = self.retrieval_index.candidate_evidence(
                        node=node_summary,
                        analysis_context=analysis,
                        candidate_flow=flow,
                        k=self.config.retrieval_k,
                    )
                except Exception as exc:
                    evidence = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
                profile["frontier_candidates"].append({
                    "target_node_id": node.node_id,
                    "candidate_flow": flow,
                    "available": evidence.get("available"),
                    "k": evidence.get("k"),
                    "success_prior": evidence.get("success_prior"),
                    "invalid_risk": evidence.get("invalid_risk"),
                    "fallback_risk": evidence.get("fallback_risk"),
                    "expected_delta_pure_qor": evidence.get("expected_delta_pure_qor"),
                    "motif_bonus": evidence.get("motif_bonus"),
                    "nearest": [
                        {
                            "sample_id": n.get("sample_id"),
                            "design": n.get("design"),
                            "flow": n.get("candidate_flow"),
                            "similarity": n.get("similarity"),
                            "useful_clean": n.get("label_useful_clean"),
                            "invalid": n.get("label_invalid"),
                            "delta_pure_qor": n.get("delta_pure_qor"),
                        }
                        for n in (evidence.get("neighbours") or [])[:3]
                    ],
                })
        profile["frontier_candidates"] = sorted(
            profile["frontier_candidates"],
            key=lambda item: (item.get("success_prior") or 0.0, -(item.get("invalid_risk") or 0.0), item.get("motif_bonus") or 0.0),
            reverse=True,
        )[: max(12, self.config.frontier_context_limit * 3)]
        return profile

    def _risk_budget_summary(self) -> Dict[str, Any]:
        counts = self._validation_counts()
        executed = max(1, self.run_count)
        invalid = counts.get(ValidationStatus.INVALID.value, 0)
        fallback = counts.get(ValidationStatus.FALLBACK_USED.value, 0)
        warning = counts.get(ValidationStatus.WARNING.value, 0)
        return {
            "executed_runs": self.run_count,
            "invalid_rate_so_far": invalid / executed,
            "fallback_rate_so_far": fallback / executed,
            "warning_rate_so_far": warning / executed,
            "policy": "warnings/fallbacks may be expanded; invalid nodes are rejected; LLM prune suggestions are advisory unless configured hard.",
            "llm_prune_mode": self.config.llm_prune_mode,
        }

    def _write_policy_candidates_snapshot(self) -> None:
        """Write policy candidate rows from DCP transition records."""
        if self.artifacts is None:
            return
        records = build_transition_records_from_policy_events(
            policy_events=self.policy_events,
            llm_decision_events=self.llm_decision_events,
            nodes=self.nodes,
            qor_scores=self.qor_scores,
            run_id=self.artifacts.run_dir.name if self.artifacts else None,
        )
        rows = records_to_policy_candidate_rows(records)
        self.policy_candidates_events = rows
        if self.analysis_controller is not None:
            self.analysis_controller.record_policy_candidates_feedback(rows, run_id=self.artifacts.run_dir.name)
        path = self.artifacts.run_dir / "policy_candidates.jsonl"
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(to_jsonable(row), sort_keys=True) + "\n")
        step_path = self.transition_record_store.path if self.transition_record_store is not None else self.artifacts.run_dir / "transition_journal.jsonl"
        journal_summary = self.transition_record_store.progress_summary() if self.transition_record_store is not None else {}
        journal_summary["materialized_transition_records"] = journal_summary.get("scheduled", 0)
        transition_journal_summary = {
            "schema_version": "transition_record.v2.sqlite_export",
            "canonical_store": str(step_path),
            "derived_export_only": True,
            "journal": journal_summary,
            "num_policy_candidate_rows": len(rows),
            "num_executed_candidate_rows": len([r for r in rows if r.get("executed")]),
            "num_not_executed_candidate_rows": len([r for r in rows if r.get("schedule_stage") == "not_executed"]),
        }
        self.artifacts.write_json(self.artifacts.run_dir / "transition_journal_summary.json", transition_journal_summary)
        self.artifacts.write_json(self.artifacts.run_dir / "policy_candidates_summary.json", {
            "schema_version": "transition_record.v2.policy_candidates",
            "num_candidates": len(rows),
            "num_transition_records": journal_summary.get("materialized_transition_records", 0),
            "num_policy_candidate_rows": len(records),
            "num_executed": len([r for r in rows if r.get("executed")]),
            "num_scheduled": len([r for r in rows if r.get("schedule_stage") == "scheduled"]),
            "num_not_executed": len([r for r in rows if r.get("schedule_stage") == "not_executed"]),
            "transition_journal_jsonl": str(step_path),
            "journal": journal_summary,
            "transition_journal_summary_json": str(self.artifacts.run_dir / "transition_journal_summary.json"),
        })

    def _persist_state(self, state: PipelineState) -> None:
        if self.artifacts is None:
            return
        self.artifacts.write_pipeline_state(state)
        self.artifacts.write_state_graph(self.nodes)
        self._write_state_graph_dot()

    def _write_state_graph_dot(self) -> None:
        assert self.artifacts is not None
        lines = ["digraph state_graph {", "  rankdir=LR;"]
        for node in self.nodes.values():
            label = f"{node.node_id}\\n{node.selected_flow or 'root'}\\npure_qor={node.score}\\n{node.validation_status or ''}"
            lines.append(f'  "{node.node_id}" [label="{label}"];')
            if node.parent_id:
                lines.append(f'  "{node.parent_id}" -> "{node.node_id}";')
        lines.append("}")
        path = self.artifacts.run_dir / "state_graph.dot"
        path.write_text("\n".join(lines) + "\n")

    def _phase_runtime_breakdown(self, elapsed: Optional[float]) -> Dict[str, Any]:
        """Wall-clock split, with failed execution kept out of the productive bucket.

        Time spent on attempts that produced nothing would otherwise inflate the
        apparent cost of work that actually moved the design.
        """
        from experiments.ablation.runtime.real_analysis_provider import diagnostic_timing

        analysis_summary = self.analysis_controller.summary() if self.analysis_controller else {}
        analysis_seconds = analysis_summary.get("analysis_wall_time_seconds") if isinstance(analysis_summary, dict) else None
        diagnostic = diagnostic_timing()
        productive = round(self.productive_execution_seconds, 3)
        failed = round(self.failed_execution_seconds, 3)
        total = round(float(elapsed), 3) if elapsed is not None else None
        verification = round(self.verification_seconds, 3)
        accounted = (productive + failed + float(analysis_seconds or 0.0)
                     + float(diagnostic["seconds"]) + verification)
        _concurrent = int(getattr(self.config, "max_parallel_jobs", 1) or 1) > 1
        return {
            "analysis_seconds": round(float(analysis_seconds), 3) if analysis_seconds is not None else None,
            "diagnostic_seconds": diagnostic["seconds"],
            "verification_seconds": verification,
            "diagnostic_calls": diagnostic["calls"],
            "execution_productive_seconds": productive,
            "execution_failed_seconds": failed,
            "total_wall_seconds": total,
            "phases_run_concurrently": _concurrent,
            "partition_valid": not _concurrent,
            "unattributed_seconds": None if _concurrent else round(total - accounted, 3) if total is not None else None,
            "note": (
                "verification_seconds is the end-of-run equivalence gate; it is "
                "reported separately and is not part of the level's optimisation cost. "
                "diagnostic_seconds is the analysis-provider Vivado work (root "
                "diagnostic plus path-feature probe), which is neither a flow "
                "execution nor online-analysis time. "
                "execution_failed_seconds covers attempts classed as hard execution "
                "failures (no output DCP, non-zero exit, or explicit executor error); "
                "they are excluded from execution_productive_seconds."
                + (
                    " Phases ran concurrently (max_parallel_jobs>1), so they overlap "
                    "and do NOT partition wall time: unattributed_seconds is null and "
                    "each phase figure is absolute time-in-phase, not a share of "
                    "total_wall_seconds."
                    if _concurrent else ""
                )
            ),
        }

    def _write_summary(self, state: PipelineState, final_output: Optional[Path]) -> Path:
        from experiments.ablation.runtime.preflight import system_load_snapshot

        # Captured here rather than at the caller so every entry point and every
        # condition records it identically.
        self.system_conditions_end = system_load_snapshot("run_end")
        self.system_load_samples.append(dict(self.system_conditions_end))
        assert self.artifacts is not None
        best = self.nodes.get(state.best_node_id) if state.best_node_id else self._best_node(include_root=False)
        usage = self._usage_summary()
        elapsed = time.time() - self._run_start_time if self._run_start_time else None
        validation_counts = self._validation_counts()
        journal_summary = self.transition_record_store.progress_summary() if self.transition_record_store is not None else {}
        summary = {
            "success": state.status == PipelineStatus.COMPLETED,
            "mode": self.config.policy_type,
            "policy_type": self.config.policy_type,
            "llm_provider": usage.get("provider"),
            "llm_model": usage.get("model"),
            "llm_prompt_id": usage.get("prompt_id"),
            "llm_prompt_hash": usage.get("prompt_hash"),
            "llm_response_schema_hash": usage.get("response_schema_hash"),
            "llm_temperature": usage.get("temperature"),
            "llm_max_tokens": usage.get("max_tokens"),
            "llm_response_mode": usage.get("response_mode"),
            "model": usage.get("model") if usage.get("total_llm_calls", 0) else None,
            "policy_model": getattr(self.policy, "policy_name", None) or usage.get("model"),
            "max_depth": self.config.max_depth,
            "branching_factor": self.config.branching_factor,
            "beam_width": self.config.beam_width,
            "batch_size": self.config.batch_size,
            "max_parallel_jobs": self.config.max_parallel_jobs,
            "scheduler_mode": self.config.scheduler_mode,
            "scheduled_runs": self.scheduled_run_count,
            "max_children_per_node": self._max_children_per_node(),
            "max_total_runs": self.config.max_total_runs,
            "eda_runs": self.run_count,
            "nodes_total": len(self.nodes),
            "nodes_completed": len([n for n in self.nodes.values() if n.status == NodeStatus.COMPLETED and n.depth > 0]),
            "nodes_failed": len([n for n in self.nodes.values() if n.status == NodeStatus.FAILED]),
            "nodes_pruned": len(self.pruned_node_ids),
            # beam_width above is a configured value, not necessarily an enforced one.
            # analysis_priority deliberately does not beam-prune the frontier (see
            # dev_journal/2026_06_29_..._global_analysis_transition_priority_hardcut.md),
            # so report whether the beam actually constrained this run.
            "beam_applies_to_policy": self.config.policy_type != "analysis_priority",
            "validation_counts": validation_counts,
            "fallback_events": [e for e in self.policy_events if e.get("decision", {}).get("fallback_used")],
            "validation_events": self.validation_events,
            "final_output_dcp": str(final_output) if final_output else None,
            "best_node_id": best.node_id if best else None,
            "best_flow_sequence": best.flow_sequence if best else [],
            "best_pure_qor_score": best.score if best else None,
            "trajectory_objective_profile": "pure_qor_v1",
            "best_trajectory_objective_score": best.score if best else None,
            "best_validation_status": best.validation_status if best else None,
            "best_qor": self.qor_scores.get(best.node_id).to_dict() if best and best.node_id in self.qor_scores else None,
            "best_pure_qor": self.qor_scores.get(best.node_id).to_dict() if best and best.node_id in self.qor_scores else None,
            "analysis": self.analysis_controller.summary() if self.analysis_controller else {"enabled": False},
            "transition_journal_jsonl": str(self.artifacts.run_dir / "transition_journal.jsonl"),
            "transition_record_count": journal_summary.get("materialized_transition_records", 0),
            "dcp_transition_journal_record_count": journal_summary.get("journal_records", 0),
            "transition_journal_summary": {
                "schema_version": "transition_record.v2.append_journal",
                "journal": journal_summary,
                "num_policy_candidate_rows": len(self.policy_candidates_events),
                "num_executed_candidate_rows": len([r for r in self.policy_candidates_events if r.get("executed")]),
                "num_not_executed_candidate_rows": len([r for r in self.policy_candidates_events if r.get("schedule_stage") == "not_executed"]),
            },
            "root_metrics": self.root_metrics,
            "repo_revision": self.repo_revision,
            "dcp_pruning": self.dcp_pruning,
            "system_conditions": {
                "start": self.system_conditions_start,
                "end": self.system_conditions_end,
                "sample_interval_sec": self.LOAD_SAMPLE_INTERVAL_SEC,
                "samples": self.system_load_samples,
                "peak_load_per_core": max(
                    (s.get("load_per_core") or 0.0) for s in self.system_load_samples
                ) if self.system_load_samples else None,
                "any_sample_contended": any(s.get("contended") for s in self.system_load_samples),
            },
            "execution_retries": {
                "count": self.retry_count,
                "cap_per_run": self.MAX_EXECUTION_RETRIES_PER_RUN,
                "events": self.retry_events,
            },
            "phase_runtime_breakdown": self._phase_runtime_breakdown(elapsed),
            "total_wall_runtime_seconds": elapsed,
            "total_llm_calls": usage.get("total_llm_calls", 0),
            "total_prompt_tokens": usage.get("total_prompt_tokens"),
            "total_completion_tokens": usage.get("total_completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "total_cost_usd": usage.get("total_cost_usd"),
            "total_provider_reported_cost_usd": usage.get("total_provider_reported_cost_usd"),
            "total_estimated_cost_usd": usage.get("total_estimated_cost_usd"),
            "cost_source": usage.get("cost_source"),
            "pricing_profile_id": usage.get("pricing_profile_id"),
            "total_latency_sec": usage.get("total_latency_sec"),
            "analysis_llm_calls": usage.get("analysis_llm_calls", 0),
            "analysis_tokens": usage.get("analysis_tokens"),
            "analysis_total_cost_usd": usage.get("analysis_total_cost_usd"),
            "analysis_model": usage.get("analysis_model"),
            "analysis_provider": usage.get("analysis_provider"),
            "combined_llm_calls": usage.get("combined_llm_calls", usage.get("total_llm_calls", 0)),
            "combined_tokens": usage.get("combined_tokens", usage.get("total_tokens")),
            "combined_cost_usd": usage.get("combined_cost_usd", usage.get("total_cost_usd")),
            "reliability_profile": self._reliability_profile(self._frontier_nodes()),
            "retrieval_profile": self._retrieval_profile(self._frontier_nodes()),
            "risk_budget": self._risk_budget_summary(),
            "config": self.config.to_dict(),
            "state": state.to_dict(),
            "nodes": [node_artifact_dict(node) for node in self.nodes.values()],
        }
        return self.artifacts.write_json(self.artifacts.run_dir / "pipeline_summary.json", summary)

    def _write_best_trajectory(self, state: PipelineState) -> Path:
        assert self.artifacts is not None
        best = self.nodes.get(state.best_node_id) if state.best_node_id else self._best_node(include_root=False)
        lines = ["# Best optimisation trajectory", ""]
        if not best:
            lines.append("No completed non-root node was found.")
            self.artifacts.write_json(self.artifacts.run_dir / "best_node.json", {})
            md_path = self.artifacts.run_dir / "best_trajectory.md"
            md_path.write_text("\n".join(lines) + "\n")
            return md_path
        for node in self._trajectory_nodes(best):
            qos = self.qor_scores.get(node.node_id)
            lines.append(
                f"- {node.node_id}: flow={node.selected_flow or 'root'}, "
                f"depth={node.depth}, pure_qor={node.score}, qor_status={qos.status if qos else None}, "
                f"validation={node.validation_status}, sequence={' -> '.join(node.flow_sequence) if node.flow_sequence else '(root)'}"
            )
            if node.validation_warnings:
                lines.append(f"  - warnings: {'; '.join(node.validation_warnings[:3])}")
            if node.validation_errors:
                lines.append(f"  - errors: {'; '.join(node.validation_errors[:3])}")
        lines.append("")
        lines.append(f"Best node: `{best.node_id}`")
        lines.append(f"Best sequence: `{' -> '.join(best.flow_sequence)}`")
        lines.append(f"Best pure QoR score: `{best.score}`")
        lines.append(f"Best validation status: `{best.validation_status}`")
        md_path = self.artifacts.run_dir / "best_trajectory.md"
        md_path.write_text("\n".join(lines) + "\n")
        self.artifacts.write_json(self.artifacts.run_dir / "best_node.json", best.to_dict())
        return md_path

    def _trajectory_nodes(self, node: DcpStateNode) -> List[DcpStateNode]:
        path_nodes = []
        cursor: Optional[DcpStateNode] = node
        while cursor is not None:
            path_nodes.append(cursor)
            cursor = self.nodes.get(cursor.parent_id) if cursor.parent_id else None
        return list(reversed(path_nodes))

    def _write_results_tables(self) -> None:
        assert self.artifacts is not None
        rows = []
        for node in self.nodes.values():
            qos = self.qor_scores.get(node.node_id)
            rows.append({
                "node_id": node.node_id,
                "parent_id": node.parent_id,
                "depth": node.depth,
                "flow": node.selected_flow or "root",
                "flow_sequence": " -> ".join(node.flow_sequence),
                "status": node.status.value if hasattr(node.status, "value") else str(node.status),
                "validation_status": node.validation_status,
                "validation_valid": node.metrics.get("validation_valid"),
                "validation_fallback_used": node.metrics.get("validation_fallback_used") or node.fallback_used,
                "validation_warning_count": len(node.validation_warnings),
                "validation_error_count": len(node.validation_errors),
                "validation_warnings": " | ".join(node.validation_warnings[:3]),
                "validation_errors": " | ".join(node.validation_errors[:3]),
                "pure_qor_score": node.score,
                "trajectory_objective_profile": "pure_qor_v1",
                "trajectory_objective_score": node.score,
                "qor_status": qos.status if qos else None,
                "final_fmax_mhz": node.metrics.get("final_fmax_mhz") or node.metrics.get("fmax_mhz"),
                "final_wns_ns": node.metrics.get("final_wns_ns"),
                "final_tns_ns": node.metrics.get("final_tns_ns"),
                "node_runtime_seconds": node.metrics.get("node_runtime_seconds"),
                "path_runtime_seconds": node.metrics.get("path_runtime_seconds"),
                "pruned": node.node_id in self.pruned_node_ids,
                "prune_reason": node.metrics.get("prune_reason") or node.metrics.get("policy_prune_reason"),
                "policy_prune_suggestions": json.dumps(node.metrics.get("policy_prune_suggestions") or []),
            })
        self.artifacts.write_json(self.artifacts.run_dir / "results_summary.json", rows)
        csv_path = self.artifacts.run_dir / "results_summary.csv"
        if rows:
            with open(csv_path, "w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
        self.artifacts.write_json(self.artifacts.run_dir / "model_usage.json", self._usage_summary())
        failures = [row for row in rows if row["status"] == NodeStatus.FAILED.value or row.get("pruned") or row.get("validation_status") in {ValidationStatus.INVALID.value, ValidationStatus.FALLBACK_USED.value, ValidationStatus.WARNING.value}]
        self.artifacts.write_json(self.artifacts.run_dir / "failures.json", failures)

    def _write_graph_stats(self) -> None:
        assert self.artifacts is not None
        by_depth: Dict[str, int] = {}
        by_validation: Dict[str, int] = {}
        by_flow: Dict[str, int] = {}
        for node in self.nodes.values():
            by_depth[str(node.depth)] = by_depth.get(str(node.depth), 0) + 1
            by_validation[str(node.validation_status)] = by_validation.get(str(node.validation_status), 0) + 1
            by_flow[node.selected_flow or "root"] = by_flow.get(node.selected_flow or "root", 0) + 1
        best = self._best_node(include_root=False)
        stats = {
            "nodes_total": len(self.nodes),
            "eda_runs": self.run_count,
            "scheduled_runs": self.scheduled_run_count,
            "scheduler_mode": self.config.scheduler_mode,
            "max_children_per_node": self._max_children_per_node(),
            "max_depth_reached": max((node.depth for node in self.nodes.values()), default=0),
            "best_depth": best.depth if best else None,
            "best_node_id": best.node_id if best else None,
            "best_flow_sequence": best.flow_sequence if best else [],
            "nodes_by_depth": by_depth,
            "nodes_by_validation_status": by_validation,
            "nodes_by_flow": by_flow,
            "pruned_node_ids": sorted(self.pruned_node_ids),
        }
        self.artifacts.write_json(self.artifacts.run_dir / "graph_stats.json", stats)

    def _write_budget_curve(self) -> None:
        assert self.artifacts is not None
        completed = [node for node in self.nodes.values() if node.depth > 0 and node.score is not None]
        completed.sort(key=lambda node: _node_numeric_id(node.node_id))
        rows = []
        best: Optional[DcpStateNode] = None
        for idx, node in enumerate(completed, start=1):
            if node.status == NodeStatus.COMPLETED and (best is None or (node.score or float("-inf")) > (best.score or float("-inf"))):
                best = node
            rows.append({
                "run_index": idx,
                "node_id": node.node_id,
                "flow_sequence": " -> ".join(node.flow_sequence),
                "node_pure_qor_score": node.score,
                "trajectory_objective_profile": "pure_qor_v1",
                "node_trajectory_objective_score": node.score,
                "node_validation_status": node.validation_status,
                "best_node_id": best.node_id if best else None,
                "best_pure_qor_score": best.score if best else None,
                "best_trajectory_objective_score": best.score if best else None,
                "best_flow_sequence": " -> ".join(best.flow_sequence) if best else None,
            })
        self.artifacts.write_json(self.artifacts.run_dir / "budget_curve.json", rows)
        csv_path = self.artifacts.run_dir / "budget_curve.csv"
        fieldnames = ["run_index", "node_id", "flow_sequence", "node_pure_qor_score", "trajectory_objective_profile", "node_trajectory_objective_score", "node_validation_status", "best_node_id", "best_pure_qor_score", "best_trajectory_objective_score", "best_flow_sequence"]
        with open(csv_path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else fieldnames)
            writer.writeheader()
            if rows:
                writer.writerows(rows)

    def _validation_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for node in self.nodes.values():
            key = node.validation_status or "None"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _usage_summary(self) -> Dict[str, Any]:
        usage: Dict[str, Any]
        if hasattr(self.policy, "usage_summary"):
            policy_usage = self.policy.usage_summary()
            usage = dict(policy_usage) if isinstance(policy_usage, dict) else {}
        else:
            usage = {}
        usage.setdefault("provider", None)
        usage.setdefault("model", self.config.model)
        usage.setdefault("prompt_id", None)
        usage.setdefault("prompt_hash", None)
        usage.setdefault("response_schema_hash", None)
        usage.setdefault("temperature", getattr(self.config, "temperature", None))
        usage.setdefault("max_tokens", getattr(self.config, "max_tokens", None))
        usage.setdefault("response_mode", getattr(self.config, "llm_response_mode", None))
        usage.setdefault("total_llm_calls", 0)
        usage.setdefault("total_prompt_tokens", None)
        usage.setdefault("total_completion_tokens", None)
        usage.setdefault("total_tokens", None)
        usage.setdefault("total_cost_usd", None)
        usage.setdefault("total_provider_reported_cost_usd", None)
        usage.setdefault("total_estimated_cost_usd", None)
        usage.setdefault("cost_source", None)
        usage.setdefault("pricing_profile_id", getattr(self.config, "pricing_profile", None))
        usage.setdefault("total_latency_sec", None)

        analysis = self.analysis_controller.summary() if self.analysis_controller else {}
        analysis_calls = int(analysis.get("llm_calls") or 0) if isinstance(analysis, dict) else 0
        analysis_tokens = analysis.get("tokens") if isinstance(analysis, dict) else None
        policy_calls = int(usage.get("total_llm_calls") or 0)
        usage["analysis_llm_calls"] = analysis_calls
        usage["analysis_tokens"] = analysis_tokens
        usage["analysis_model"] = analysis.get("model") if isinstance(analysis, dict) else None
        usage["analysis_provider"] = analysis.get("provider") if isinstance(analysis, dict) else None
        analysis_cost = analysis.get("total_cost_usd") if isinstance(analysis, dict) else None
        usage["analysis_total_cost_usd"] = analysis_cost
        usage["combined_llm_calls"] = policy_calls + analysis_calls
        usage["combined_tokens"] = _sum_optional_int_local(usage.get("total_tokens"), analysis_tokens)
        usage["combined_cost_usd"] = _sum_optional_float_local(usage.get("total_cost_usd"), analysis_cost)
        return usage


class OneShotLLMExpansionConfig(OptimizationConfig):
    def __init__(self, **kwargs: Any):
        super().__init__(max_depth=1, branching_factor=1, beam_width=1, max_total_runs=1, **kwargs)


def _node_numeric_id(node_id: str) -> int:
    try:
        return int(node_id.split("_")[1])
    except Exception:
        return 0


def _sum_optional_int_local(*values: Any) -> Optional[int]:
    total = 0
    seen = False
    for value in values:
        if value is None:
            continue
        try:
            total += int(value)
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen else None


def _sum_optional_float_local(*values: Any) -> Optional[float]:
    total = 0.0
    seen = False
    for value in values:
        if value is None:
            continue
        try:
            total += float(value)
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen else None
