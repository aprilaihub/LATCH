"""Run context for DCP-state graph expansion pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from optimizer_pipeline.domain.budget import BudgetLedger
from optimizer_pipeline.domain.transition import DcpTransitionRecord
from optimizer_pipeline.execution import DcpTransitionExecutor
from optimizer_pipeline.models import DcpStateNode, NodeStatus, OutputValidationResult, PipelineState, PipelineStatus, to_jsonable
from optimizer_pipeline.qor import QoRScore
from optimizer_pipeline.run.config import OptimizationConfig
from optimizer_pipeline.run.paths import validate_new_run_paths
from optimizer_pipeline.core.domain import (
    ArtifactReference, AttemptEventKind, AttemptLifecycleEvent, BudgetEvent, BudgetEventKind,
    RunLifecycleEvent, RunStatus, ValidationLevel, ValidationRecord,
    ValidationStatus as CoreValidationStatus,
)
from optimizer_pipeline.run.store_bridge import CanonicalArtifactStore, DatabaseTransitionSink, initialize_run_store, _sha256


def _verify_recorded_dcp(artifact: ArtifactReference, *, role: str) -> None:
    """Check the file against its existing canonical artifact evidence."""
    path = Path(artifact.uri)
    if not path.is_file():
        raise FileNotFoundError(f"recorded {role} DCP is missing or not a file")
    if artifact.sha256 is not None and _sha256(path) != artifact.sha256:
        raise ValueError(f"recorded {role} DCP changed after it was recorded")


@dataclass
class OptimizationRunContext:
    """Shared state for a handwritten optimization run.

    Public business operations are intentionally small:
    ``ctx.analysis`` produces evidence, ``ctx.expand`` produces transitions, and
    ``ctx.finalize`` exports a report from the transition journal projection.
    """

    run_id: str
    artifact_store: Any
    config: OptimizationConfig
    analyzer: Any
    executor: Any
    output_validator: Any
    root_dcp: Path
    current_dcp: Path
    root_metrics: Dict[str, Any]
    parent_metrics: Dict[str, Any]
    current_node: DcpStateNode
    nodes: Dict[str, DcpStateNode]
    state: PipelineState
    transition_executor: DcpTransitionExecutor
    budget: BudgetLedger
    transition_sink: Any
    run_store: Any = None
    mode: str = "handwritten_pipeline"
    stop_on_reject: bool = True
    next_transition_index: int = 0
    analysis_contexts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    analysis_priors: List[Dict[str, Any]] = field(default_factory=list)
    candidate_priorities: List[Dict[str, Any]] = field(default_factory=list)
    analysis_plans: List[Dict[str, Any]] = field(default_factory=list)
    analysis_sessions: List[Any] = field(default_factory=list)
    fast_skip_suggestions: List[Dict[str, Any]] = field(default_factory=list)
    qor_scores: Dict[str, QoRScore] = field(default_factory=dict)
    output_validations: Dict[str, OutputValidationResult] = field(default_factory=dict)
    transition_records: List[DcpTransitionRecord] = field(default_factory=list)
    transition_observers: List[Any] = field(default_factory=list)
    finalized: bool = False
    stopped: bool = False
    analysis: Any = None
    expand: Any = None
    intervention: Any = None

    @property
    def run_dir(self) -> Path:
        return self.artifact_store.run_dir

    @staticmethod
    def safe_label(value: str) -> str:
        return CanonicalArtifactStore.safe_label(str(value))

    def persist_state(self, *, strict_orphans: bool = False) -> None:
        return

    def close(self) -> None:
        """Release the store; finalize remains an explicit application decision."""
        if self.run_store is not None:
            self.run_store.close()

    def notify_transition_record(self, record: DcpTransitionRecord) -> None:
        """Notify run-level observers after a completed transition is journaled."""

        for observer in list(self.transition_observers):
            observer(self, record)

    def record_run_detail(self, kind: str, payload: Dict[str, Any]) -> None:
        if self.run_store is None:
            return
        sequence = len(self.run_store.run_events())
        self.run_store.add_run_event(RunLifecycleEvent(
            f"run-event-{sequence:03d}", self.run_id, sequence, RunStatus.RUNNING,
            detail={"kind": str(kind), "payload": payload},
        ))

    def finalize(
        self,
        *,
        summary_name: str = "run_summary.json",
        extra: Optional[Dict[str, Any]] = None,
        success: bool = True,
        status: PipelineStatus = PipelineStatus.COMPLETED,
    ) -> Path:
        selected = self.run_store.selections()[-1]
        selected_artifact = next(
            artifact for artifact in self.run_store.artifacts()
            if artifact.artifact_id == selected.artifact_id
        )
        if success:
            _verify_recorded_dcp(selected_artifact, role="selected")
        self.finalized = True
        self.state.status = status
        self.persist_state(strict_orphans=True)
        self.state.best_node_id = selected.state_id
        summary = {
            "schema_version": "managed_result.v1",
            "success": bool(success),
            "mode": self.mode,
            "run_id": self.run_id,
            "input_dcp": str(self.root_dcp),
            "final_dcp": selected_artifact.uri,
            "budget": self.budget.to_dict(),
            "run_db": str(self.run_dir / "run.db"),
            "selected_state_id": selected.state_id,
            "attempt_count": len(self.run_store.attempts()),
        }
        if extra:
            for key in ("run_mode", "contest_eligible", "pipeline", "error", "contest_output_dcp"):
                if key in extra:
                    summary[key] = extra[key]
        manifest_path = self.write_result_manifest(summary)
        if self.run_store is not None:
            completed_attempts = {outcome.attempt_id for outcome in self.run_store.outcomes()}
            for attempt in self.run_store.attempts():
                if attempt.attempt_id in completed_attempts:
                    continue
                prior = self.run_store.attempt_events(attempt_id=attempt.attempt_id)
                sequence_index = len(prior)
                self.run_store.add_attempt_event(AttemptLifecycleEvent(
                    f"event:{attempt.attempt_id}:orphaned", attempt.attempt_id,
                    sequence_index, AttemptEventKind.ORPHANED,
                    detail={"reason": "run finalized without outcome"},
                ))
                self.run_store.add_budget_event(BudgetEvent(
                    f"budget:{attempt.attempt_id}:released", self.run_id,
                    attempt.scope_id, attempt.attempt_id, BudgetEventKind.RELEASED,
                ))
            selected_validations = self.run_store.validations(subject_id=selected.state_id)
            levels = {record.level for record in selected_validations}
            if ValidationLevel.CERTIFICATION not in levels:
                self.run_store.add_validation(ValidationRecord(
                    f"certification:{selected.state_id}", self.run_id, selected.scope_id,
                    selected.state_id, ValidationLevel.CERTIFICATION,
                    CoreValidationStatus.NOT_RUN,
                    details={"reason": "application policy did not request certification"},
                ))
            if ValidationLevel.FORMAL not in levels:
                self.run_store.add_validation(ValidationRecord(
                    f"formal:{selected.state_id}", self.run_id, selected.scope_id,
                    selected.state_id, ValidationLevel.FORMAL,
                    CoreValidationStatus.NOT_RUN,
                    details={"provider": "placeholder"},
                ))
            sequence = len(self.run_store.run_events())
            final_status = RunStatus.FINALIZED if success else RunStatus.FAILED
            self.run_store.add_run_event(RunLifecycleEvent(
                f"run-event-{sequence:03d}", self.run_id, sequence, final_status,
                detail={"result_manifest": str(manifest_path)},
            ))
        return manifest_path

    def write_result_manifest(self, payload: Dict[str, Any]) -> Path:
        manifest_path = self.run_dir / "result_manifest.json"
        existing = {}
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.update(to_jsonable(payload))
        manifest_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
        return manifest_path

    def ensure_terminal_event(self, *, success: bool) -> None:
        if self.run_store is None:
            return
        events = self.run_store.run_events()
        if events and events[-1].status in {RunStatus.FINALIZED, RunStatus.FAILED, RunStatus.ABORTED}:
            return
        sequence = len(events)
        self.run_store.add_run_event(RunLifecycleEvent(
            f"run-event-{sequence:03d}", self.run_id, sequence,
            RunStatus.FINALIZED if success else RunStatus.FAILED,
        ))


def _attach_services(ctx: OptimizationRunContext) -> OptimizationRunContext:
    from optimizer_pipeline.capabilities.analysis.runner import AnalysisRunner
    from optimizer_pipeline.capabilities.intervention import InterventionCapability
    from optimizer_pipeline.capabilities.search.controller import ExpansionController
    from optimizer_pipeline.run.search_runtime import ContextSearchRuntime

    ctx.analysis = AnalysisRunner(ctx)
    ctx.expand = ExpansionController(ContextSearchRuntime(ctx))
    ctx.intervention = InterventionCapability(ctx)
    return ctx


async def bootstrap_run(
    *,
    input_dcp: Path,
    run_dir: Path,
    config: OptimizationConfig,
    analyzer: Any,
    executor: Any,
    output_validator: Any,
    run_id: Optional[str] = None,
    mode: str = "handwritten_pipeline",
    max_eda_runs: Optional[int] = None,
    max_parallel_jobs: Optional[int] = None,
    stop_on_reject: bool = True,
) -> OptimizationRunContext:
    validate_new_run_paths(input_dcp=Path(input_dcp), run_dir=Path(run_dir))
    if not Path(input_dcp).is_file():
        raise FileNotFoundError("input DCP must exist before bootstrap_run()")
    artifact_store = CanonicalArtifactStore(run_dir)
    root_context = await analyzer.analyze(Path(input_dcp), [], config)
    root_analysis = dict(root_context or {}) if isinstance(root_context, dict) else {}
    root_metrics = dict(root_analysis)
    root_node = DcpStateNode(
        node_id="node_000_root",
        parent_id=None,
        depth=0,
        input_dcp=str(input_dcp),
        output_dcp=str(input_dcp),
        flow_sequence=[],
        status=NodeStatus.COMPLETED,
        score=0.0,
        metrics=root_metrics,
        artifact_dir=str(artifact_store.node_dir(0, "root")),
    )
    nodes = {root_node.node_id: root_node}
    state = PipelineState(status=PipelineStatus.RUNNING, root_dcp=str(input_dcp), best_node_id=root_node.node_id)
    transition_executor = DcpTransitionExecutor(
        executor=executor,
        output_validator=output_validator,
        analyzer=analyzer,
        pipeline_config=config,
        artifact_store=artifact_store,
        qos_weights=config.qor_weights(),
        persist_diagnostics=False,
    )
    run_id = run_id or artifact_store.run_dir.name
    run_store = initialize_run_store(
        run_dir=artifact_store.run_dir,
        run_id=run_id,
        mode=mode,
        input_dcp=Path(input_dcp),
        root_metrics=root_metrics,
    )
    ctx = OptimizationRunContext(
        run_id=run_id,
        artifact_store=artifact_store,
        config=config,
        analyzer=analyzer,
        executor=executor,
        output_validator=output_validator,
        root_dcp=Path(input_dcp),
        current_dcp=Path(input_dcp),
        root_metrics=root_metrics,
        parent_metrics={},
        current_node=root_node,
        nodes=nodes,
        state=state,
        transition_executor=transition_executor,
        budget=BudgetLedger(
            max_eda_runs=config.max_total_runs if max_eda_runs is None else max_eda_runs,
            max_parallel_jobs=config.max_parallel_jobs if max_parallel_jobs is None else max_parallel_jobs,
            max_analysis_sessions=config.analysis_max_states_per_expansion,
        ),
        transition_sink=DatabaseTransitionSink(run_store),
        run_store=run_store,
        mode=mode,
        stop_on_reject=stop_on_reject,
        analysis_contexts={root_node.node_id: root_analysis},
    )
    _attach_services(ctx)
    ctx.record_run_detail("bootstrap_configuration", {
        "config": config.to_dict(), "max_eda_runs": ctx.budget.max_eda_runs,
        "max_parallel_jobs": ctx.budget.max_parallel_jobs, "stop_on_reject": stop_on_reject,
    })
    artifact_store.write_json(artifact_store.run_dir / "config.json", config.to_dict())
    artifact_store.write_json(artifact_store.run_dir / "root_analysis_context.json", root_analysis)
    artifact_store.write_node_state(root_node)
    ctx.persist_state()
    return ctx


async def resume_run(
    *,
    input_dcp: Path,
    run_dir: Path,
    config: OptimizationConfig,
    analyzer: Any,
    executor: Any,
    output_validator: Any,
    run_id: Optional[str] = None,
    mode: str = "handwritten_pipeline",
    max_eda_runs: Optional[int] = None,
    max_parallel_jobs: Optional[int] = None,
    stop_on_reject: bool = True,
) -> OptimizationRunContext:
    from optimizer_pipeline.core.store import SQLiteRunStore

    run_db = Path(run_dir) / "run.db"
    if not run_db.is_file():
        raise FileNotFoundError("resume_run() requires an existing canonical run.db")
    run_store = SQLiteRunStore(run_db, migrate=False)
    stored_run = run_store.get_run()
    if stored_run is None:
        run_store.close()
        raise ValueError("run.db does not contain a run")
    artifacts = {item.artifact_id: item for item in run_store.artifacts()}
    try:
        root_artifact = artifacts["artifact-root"]
        requested_input = Path(input_dcp).resolve(strict=True)
        if requested_input != Path(root_artifact.uri).resolve(strict=True):
            raise ValueError("resume input does not match the recorded root DCP")
        _verify_recorded_dcp(root_artifact, role="root")
        if run_id is not None and run_id != stored_run.run_id:
            raise ValueError("resume run_id does not match the recorded run")
        saved_configuration = next((
            event.detail.to_dict()["payload"] for event in run_store.run_events()
            if event.detail.to_dict().get("kind") == "bootstrap_configuration"
        ), None)
        if saved_configuration is not None:
            requested = {
                "config": config.to_dict(),
                "max_eda_runs": config.max_total_runs if max_eda_runs is None else max_eda_runs,
                "max_parallel_jobs": config.max_parallel_jobs if max_parallel_jobs is None else max_parallel_jobs,
                "stop_on_reject": stop_on_reject,
            }
            if requested != saved_configuration:
                raise ValueError("resume configuration or execution limits differ from bootstrap")
        stored_states = list(run_store.states())
        selections = list(run_store.selections())
        selected_id = selections[-1].state_id if selections else "node_000_root"
        selected_state = next(fact for fact in stored_states if fact.state_id == selected_id)
        _verify_recorded_dcp(artifacts[selected_state.artifact_id], role="selected")
    except Exception:
        run_store.close()
        raise
    artifact_store = CanonicalArtifactStore(run_dir)
    actions = {item.action_id: item for item in run_store.actions()}
    outcomes = {item.attempt_id: item for item in run_store.outcomes()}
    attempts = {item.attempt_id: item for item in run_store.attempts()}
    orphan_attempt_ids = sorted(set(attempts) - set(outcomes))
    if orphan_attempt_ids:
        run_store.close()
        raise RuntimeError(
            "cannot safely resume canonical run with orphaned attempts: "
            + ", ".join(orphan_attempt_ids)
        )
    validations = {item.subject_id: item for item in run_store.validations()}
    nodes: Dict[str, DcpStateNode] = {}
    for index, fact in enumerate(stored_states):
        artifact = artifacts[fact.artifact_id]
        metrics = fact.metadata.to_dict().get("metrics", {})
        attempt = attempts.get(fact.produced_by_attempt_id) if fact.produced_by_attempt_id else None
        action = actions.get(attempt.action_id) if attempt else None
        parent = nodes.get(fact.parent_state_id) if fact.parent_state_id else None
        validation = validations.get(fact.state_id)
        node = DcpStateNode(
            node_id=fact.state_id,
            parent_id=fact.parent_state_id,
            depth=0 if parent is None else parent.depth + 1,
            input_dcp=str(input_dcp) if parent is None else str(parent.output_dcp),
            output_dcp=artifact.uri,
            flow_sequence=[] if parent is None else list(parent.flow_sequence) + ([action.action_type] if action else []),
            selected_flow=action.action_type if action else None,
            status=NodeStatus.COMPLETED if validation is None or validation.status.value != "INVALID" else NodeStatus.FAILED,
            score=float(metrics.get("qor_score", {}).get("total_score")) if isinstance(metrics.get("qor_score"), dict) and metrics["qor_score"].get("total_score") is not None else None,
            metrics=dict(metrics),
            validation_status=validation.status.value if validation else None,
            artifact_dir=str(artifact_store.node_dir(index, action.action_type if action else "root")),
        )
        nodes[node.node_id] = node
    root_node = nodes["node_000_root"]
    root_metrics = dict(root_node.metrics or {})
    root_analysis = dict(root_metrics)
    current_node = nodes[selected_id]
    state = PipelineState(status=PipelineStatus.RUNNING, root_dcp=str(input_dcp), best_node_id=selected_id)
    state.completed_node_ids = [node.node_id for node in nodes.values() if node.status is NodeStatus.COMPLETED and node.parent_id]
    state.failed_node_ids = [node.node_id for node in nodes.values() if node.status is NodeStatus.FAILED]
    transition_executor = DcpTransitionExecutor(
        executor=executor,
        output_validator=output_validator,
        analyzer=analyzer,
        pipeline_config=config,
        artifact_store=artifact_store,
        qos_weights=config.qor_weights(),
        persist_diagnostics=False,
    )
    ctx = OptimizationRunContext(
        run_id=stored_run.run_id,
        artifact_store=artifact_store,
        config=config,
        analyzer=analyzer,
        executor=executor,
        output_validator=output_validator,
        root_dcp=Path(input_dcp),
        current_dcp=Path(current_node.output_dcp),
        root_metrics=root_metrics,
        parent_metrics=dict(current_node.metrics or {}),
        current_node=current_node,
        nodes=nodes,
        state=state,
        transition_executor=transition_executor,
        budget=BudgetLedger(
            max_eda_runs=config.max_total_runs if max_eda_runs is None else max_eda_runs,
            max_parallel_jobs=config.max_parallel_jobs if max_parallel_jobs is None else max_parallel_jobs,
            max_analysis_sessions=config.analysis_max_states_per_expansion,
        ),
        transition_sink=DatabaseTransitionSink(run_store),
        run_store=run_store,
        mode=mode,
        stop_on_reject=stop_on_reject,
        analysis_contexts={root_node.node_id: root_analysis},
        next_transition_index=len(attempts),
    )
    reserved = sum(event.kind.value == "RESERVED" for event in run_store.budget_events())
    completed = sum(event.kind.value == "COMPLETED" for event in run_store.budget_events())
    failed = sum(event.kind.value == "FAILED" for event in run_store.budget_events())
    ctx.budget.scheduled_eda_runs = reserved
    ctx.budget.completed_eda_runs = completed
    ctx.budget.failed_eda_runs = failed
    _attach_services(ctx)
    return ctx
