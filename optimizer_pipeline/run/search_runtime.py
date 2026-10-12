"""Adapter from the legacy run context to the search runtime contract."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from optimizer_pipeline.models import PipelineResult
from optimizer_pipeline.domain.transition import DcpTransitionIntent, DcpTransitionRecord
from optimizer_pipeline.models import DcpStateNode, NodeStatus
from optimizer_pipeline.qor import fill_missing_qor_baseline, has_qor_baseline
from optimizer_pipeline.capabilities.search.policies.registry import PolicyResources, build_policy_registry
from optimizer_pipeline.capabilities.search.policies.core import validate_expansion_decision
from optimizer_pipeline.execution import DcpTransitionRequest

if TYPE_CHECKING:  # pragma: no cover
    from optimizer_pipeline.run.context import OptimizationRunContext


_POLICY_ALIASES = {
    "analysis_priority": "analysis_priority",
    "deterministic": "deterministic",
    "deterministic_frontier": "deterministic",
    "reliability": "reliability",
    "retrieval": "retrieval",
}

_FRONTIER_POLICY_NAMES = {
    "analysis_priority": "analysis_priority",
    "deterministic": "deterministic",
    "reliability": "reliability",
    "retrieval": "retrieval",
}

_ANALYSIS_NODE_REFERENCE_KEYS = {"node_id", "target_node_id", "target_node"}


def _plain_analysis_value(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return copy.deepcopy(value)


def _rewrite_analysis_node_references(value: Any, *, old_node_id: str, new_node_id: str) -> Any:
    if isinstance(value, dict):
        rewritten: Dict[str, Any] = {}
        for key, child in value.items():
            if key in _ANALYSIS_NODE_REFERENCE_KEYS and child is not None and str(child) == old_node_id:
                rewritten[key] = new_node_id
            else:
                rewritten[key] = _rewrite_analysis_node_references(child, old_node_id=old_node_id, new_node_id=new_node_id)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_analysis_node_references(item, old_node_id=old_node_id, new_node_id=new_node_id) for item in value]
    return value


def _analysis_row_targets_node(row: Dict[str, Any], node_id: str) -> bool:
    return any(row.get(key) is not None and str(row.get(key)) == node_id for key in _ANALYSIS_NODE_REFERENCE_KEYS)


def _dedupe_analysis_priors(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique: List[Dict[str, Any]] = []
    for row in rows:
        key = (
            row.get("node_id"),
            row.get("target_node_id"),
            row.get("flow") or row.get("candidate_flow"),
            row.get("source_analysis_session_id") or row.get("analysis_session_id"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _dedupe_candidate_priorities(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique: List[Dict[str, Any]] = []
    for row in rows:
        key = (
            row.get("node_id"),
            row.get("target_node_id"),
            row.get("flow") or row.get("candidate_flow"),
            row.get("source_analysis_session_id") or row.get("analysis_session_id"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _analysis_seed_for_frontier(ctx: Any) -> Dict[str, Any]:
    """Map run-context analysis evidence onto the child frontier root node."""

    old_node_id = str(getattr(ctx.current_node, "node_id", "node_000_root") or "node_000_root")
    new_node_id = "node_000_root"
    sessions: List[Dict[str, Any]] = []
    priors: List[Dict[str, Any]] = []
    priorities: List[Dict[str, Any]] = []
    analysis_plans: List[Dict[str, Any]] = []
    node_diagnoses: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []
    loop_decisions: List[Dict[str, Any]] = []
    fast_skip_suggestions: List[Dict[str, Any]] = []

    for row in (_plain_analysis_value(item) for item in (getattr(ctx, "analysis_priors", []) or [])):
        if isinstance(row, dict) and _analysis_row_targets_node(row, old_node_id):
            priors.append(_rewrite_analysis_node_references(row, old_node_id=old_node_id, new_node_id=new_node_id))

    for row in (_plain_analysis_value(item) for item in (getattr(ctx, "candidate_priorities", []) or [])):
        if isinstance(row, dict) and _analysis_row_targets_node(row, old_node_id):
            priorities.append(_rewrite_analysis_node_references(row, old_node_id=old_node_id, new_node_id=new_node_id))

    for row in (_plain_analysis_value(item) for item in (getattr(ctx, "analysis_plans", []) or [])):
        if isinstance(row, dict) and _analysis_row_targets_node(row, old_node_id):
            analysis_plans.append(_rewrite_analysis_node_references(row, old_node_id=old_node_id, new_node_id=new_node_id))

    for session in getattr(ctx, "analysis_sessions", []) or []:
        row = _plain_analysis_value(session)
        if not isinstance(row, dict) or not _analysis_row_targets_node(row, old_node_id):
            continue
        row = _rewrite_analysis_node_references(row, old_node_id=old_node_id, new_node_id=new_node_id)
        session_id = row.get("session_id")
        diagnosis = row.get("diagnosis") if isinstance(row.get("diagnosis"), dict) else None
        candidate_priors = row.get("candidate_priors") if isinstance(row.get("candidate_priors"), list) else []
        candidate_priorities = row.get("candidate_priorities") if isinstance(row.get("candidate_priorities"), list) else []
        analysis_plan = row.get("analysis_plan") if isinstance(row.get("analysis_plan"), dict) else None
        fast_skips = row.get("fast_skip_suggestions") if isinstance(row.get("fast_skip_suggestions"), list) else []
        stop_decision = row.get("stop_decision") if isinstance(row.get("stop_decision"), dict) else {}

        sessions.append({
            "session_id": session_id,
            "node_id": new_node_id,
            "trigger_reason": row.get("trigger_reason"),
            "tool_loop_mode": row.get("tool_loop_mode"),
            "stop_reason": stop_decision.get("stop_reason"),
            "candidate_prior_count": len(candidate_priors),
            "candidate_priority_count": len(candidate_priorities),
            "fast_skip_suggestion_count": len(fast_skips),
            "diagnosis": diagnosis,
            "analysis_plan": analysis_plan,
        })
        if diagnosis:
            node_diagnoses.append(diagnosis)
        if analysis_plan:
            analysis_plans.append(analysis_plan)
        priors.extend(item for item in candidate_priors if isinstance(item, dict))
        priorities.extend(item for item in candidate_priorities if isinstance(item, dict))
        fast_skip_suggestions.extend(item for item in fast_skips if isinstance(item, dict))

        for idx, call in enumerate(row.get("tool_calls") if isinstance(row.get("tool_calls"), list) else []):
            if not isinstance(call, dict):
                continue
            tool_calls.append({
                "session_id": session_id,
                "node_id": new_node_id,
                "tool_name": call.get("tool_name"),
                "tool_class": call.get("tool_class"),
                "success": call.get("success"),
                "timed_out": call.get("timed_out"),
                "error": call.get("error"),
                "call_index": idx,
            })
        for idx, decision in enumerate(row.get("loop_decisions") if isinstance(row.get("loop_decisions"), list) else []):
            if not isinstance(decision, dict):
                continue
            loop_decisions.append({
                "session_id": session_id,
                "node_id": new_node_id,
                "decision_index": idx,
                "tool_loop_mode": row.get("tool_loop_mode"),
                "action": decision.get("action"),
                "next_tool_name": decision.get("next_tool_name"),
                "stop_reason": decision.get("stop_reason"),
                "confidence": decision.get("confidence"),
                "rationale": decision.get("rationale"),
            })

    for row in (_plain_analysis_value(item) for item in (getattr(ctx, "fast_skip_suggestions", []) or [])):
        if isinstance(row, dict) and _analysis_row_targets_node(row, old_node_id):
            fast_skip_suggestions.append(_rewrite_analysis_node_references(row, old_node_id=old_node_id, new_node_id=new_node_id))

    seed_contexts: Dict[str, Dict[str, Any]] = {}
    current_context = getattr(ctx, "analysis_contexts", {}).get(old_node_id) if isinstance(getattr(ctx, "analysis_contexts", None), dict) else None
    if current_context and (sessions or priors or priorities or analysis_plans or node_diagnoses or fast_skip_suggestions):
        seed_contexts[new_node_id] = _rewrite_analysis_node_references(
            _plain_analysis_value(current_context),
            old_node_id=old_node_id,
            new_node_id=new_node_id,
        )

    priors = _dedupe_analysis_priors([row for row in priors if isinstance(row, dict)])
    priorities = _dedupe_candidate_priorities([row for row in priorities if isinstance(row, dict)])
    return {
        "analysis_priors": priors,
        "candidate_priorities": priorities,
        "analysis_plans": analysis_plans,
        "node_diagnoses": node_diagnoses,
        "analysis_evidence": {
            "sessions": sessions,
            "tool_calls": tool_calls[-20:],
            "loop_decisions": loop_decisions[-12:],
            "fast_skip_suggestions": fast_skip_suggestions[-20:],
            "summary": {
                "source": "run_context_analysis_seed",
                "source_node_id": old_node_id,
                "frontier_node_id": new_node_id,
                "sessions": len(sessions),
                "tool_calls": len(tool_calls),
                "loop_decisions": len(loop_decisions),
                "candidate_prior_count": len(priors),
                "candidate_priority_count": len(priorities),
                "analysis_plan_count": len(analysis_plans),
                "diagnosis_count": len(node_diagnoses),
                "fast_skip_suggestion_count": len(fast_skip_suggestions),
            },
        },
        "analysis_contexts": seed_contexts,
    }


class ContextSearchRuntime:
    """Execute search primitives against ``OptimizationRunContext`` state."""

    def __init__(self, ctx: "OptimizationRunContext") -> None:
        self.ctx = ctx
        self.frontier_index = 0

    @property
    def remaining_eda_runs(self) -> int:
        return self.ctx.budget.remaining_eda_runs

    @property
    def stopped(self) -> bool:
        return bool(self.ctx.stopped)

    async def execute_transition(
        self,
        optimizer_name: str,
        *,
        source: str = "manual_transition",
        reason: str = "explicit graph expansion transition",
        stop_on_reject: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DcpTransitionRecord:
        ctx = self.ctx
        if ctx.finalized:
            raise RuntimeError("Cannot expand after context.finalize()")
        if not ctx.budget.reserve_eda_run():
            raise RuntimeError("EDA transition budget exhausted")

        step_index = ctx.next_transition_index
        transition_id = f"node_{step_index + 1:03d}_{ctx.safe_label(optimizer_name)}"
        step_dir = ctx.artifact_store.run_dir / f"transition_{step_index:03d}_{ctx.safe_label(optimizer_name)}"
        output_dcp = step_dir / "output.dcp"
        child = DcpStateNode(
            node_id=transition_id,
            parent_id=ctx.current_node.node_id,
            depth=ctx.current_node.depth + 1,
            input_dcp=str(ctx.current_dcp),
            output_dcp=str(output_dcp),
            flow_sequence=list(ctx.current_node.flow_sequence) + [optimizer_name],
            selected_flow=optimizer_name,
            status=NodeStatus.RUNNING,
            artifact_dir=str(step_dir),
        )
        ctx.nodes[child.node_id] = child
        ctx.artifact_store.write_node_state(child)

        intent = DcpTransitionIntent(
            transition_id=transition_id,
            run_id=ctx.run_id,
            source=source,
            parent_node_id=ctx.current_node.node_id,
            parent_dcp=str(ctx.current_dcp),
            optimizer_name=optimizer_name,
            optimizer_params={"flow": optimizer_name},
            target_node_id=ctx.current_node.node_id,
            candidate_rank=step_index,
            reason=reason,
            metadata={"flow_sequence_before": list(ctx.current_node.flow_sequence), **dict(metadata or {})},
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
            event_index=intent.event_index,
            candidate_rank=intent.candidate_rank,
            priority=intent.priority,
            reason=intent.reason,
            metadata=intent.metadata,
        )
        ctx.transition_sink.append(scheduled)

        req = DcpTransitionRequest(
            input_dcp=Path(ctx.current_dcp),
            output_dcp=output_dcp,
            output_dir=step_dir,
            optimizer_name=optimizer_name,
            root_metrics=dict(ctx.root_metrics),
            parent_metrics=dict(ctx.parent_metrics or {}),
            optimizer_params=dict(intent.optimizer_params or {}),
            run_id=ctx.run_id,
            parent_node_id=ctx.current_node.node_id,
            child_node_id=child.node_id,
            candidate_id=f"{child.node_id}:{optimizer_name}",
            flow_sequence_before=list(ctx.current_node.flow_sequence),
            mode=ctx.mode,
            metadata={"expansion_source": source, **dict(intent.metadata or {})},
        )
        result = await ctx.transition_executor.run(req, history=list(ctx.nodes.values()))
        # Budget completion preserves the historical managed-run meaning:
        # an executed transition that fails attempt validation consumes a
        # failed run, while execution outcome and validation remain separate.
        ctx.budget.complete_eda_run(success=bool(result.accepted))
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

        completed = DcpTransitionRecord.completed_transition(
            intent=scheduled.intent,
            result=result,
            child_node_id=child.node_id,
            child_status=child.status.value if hasattr(child.status, "value") else str(child.status),
            added_to_graph=True,
            updated_incumbent=bool(result.accepted),
        )
        ctx.transition_sink.append(completed)
        ctx.transition_records.append(completed)
        ctx.notify_transition_record(completed)
        ctx.analysis_contexts[child.node_id] = dict(result.analysis_context or {})
        ctx.qor_scores[child.node_id] = result.qor_score
        ctx.output_validations[child.node_id] = result.output_validation
        ctx.next_transition_index += 1

        if result.accepted and result.output_dcp:
            ctx.current_dcp = Path(result.output_dcp)
            ctx.parent_metrics = dict(result.metrics or {})
            ctx.current_node = child
            ctx.state.best_node_id = child.node_id
            if child.node_id not in ctx.state.completed_node_ids:
                ctx.state.completed_node_ids.append(child.node_id)
        else:
            if child.node_id not in ctx.state.failed_node_ids:
                ctx.state.failed_node_ids.append(child.node_id)
            if stop_on_reject if stop_on_reject is not None else ctx.stop_on_reject:
                ctx.stopped = True
        ctx.persist_state()
        return completed

    async def execute_frontier(
        self,
        *,
        policy: Any = "deterministic",
        max_transitions: Optional[int] = None,
        parallelism: Optional[int] = None,
        heuristic: bool = True,
        scheduler_mode: Optional[str] = None,
        run_dir_name: Optional[str] = None,
        source: str = "frontier_expansion",
        branching_factor: Optional[int] = None,
        beam_width: Optional[int] = None,
        batch_size: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> Optional[PipelineResult]:
        ctx = self.ctx
        if ctx.finalized:
            raise RuntimeError("Cannot expand after context.finalize()")
        remaining = ctx.budget.remaining_eda_runs
        requested = remaining if max_transitions is None else min(max(0, int(max_transitions)), remaining)
        if bool(getattr(ctx.config, "online_deep_analysis", False)):
            await ctx.analysis.run_once(
                mode=getattr(ctx.config, "analysis_tool_loop_mode", None),
                trigger_reason="frontier_root_analysis",
                force_enable=False,
            )
        if requested <= 0:
            if bool(getattr(ctx.config, "analysis_only", False)):
                return PipelineResult(
                    success=True,
                    run_dir=str(ctx.run_dir),
                    final_output=str(ctx.current_dcp),
                    summary_path=str(ctx.run_dir / "run.db"),
                    state=ctx.state,
                )
            return None

        policy_key = self._policy_key(policy)
        policy_impl = policy
        if not hasattr(policy_impl, "propose"):
            policy_impl = build_policy_registry().create(
                policy_key,
                resources=PolicyResources(config=ctx.config, api_key=None),
            )
        scope_index = self.frontier_index
        self.frontier_index += 1
        scope_id = f"scope-frontier-{scope_index:03d}"
        linear_shape = all(value == 1 for value in (branching_factor, beam_width, batch_size, parallelism) if value is not None)
        if ctx.run_store is not None:
            from optimizer_pipeline.core.domain import ExecutionScope, ScopeKind

            ctx.run_store.add_scope(ExecutionScope(
                scope_id, ctx.run_id, ScopeKind.LINEAR if linear_shape else ScopeKind.FRONTIER,
                source, "scope-root",
                metadata={"policy": policy_key, "requested": requested, "parallelism": parallelism or ctx.budget.max_parallel_jobs},
            ))
        previous_scope = getattr(ctx.transition_sink, "scope_id", "scope-root")
        ctx.transition_sink.scope_id = scope_id
        allowed = list(dict.fromkeys(getattr(ctx.config, "allowed_flows", []) or []))
        base_parent = ctx.current_node
        base_dcp = Path(ctx.current_dcp)
        request = {
            "frontier": [base_parent.to_dict()],
            "frontier_node_ids": [base_parent.node_id],
            "allowed_flows": allowed,
            "batch_size": requested,
            "branching_factor": requested,
            "beam_width": beam_width or getattr(ctx.config, "beam_width", 1),
            "max_parallel_jobs": parallelism or ctx.budget.max_parallel_jobs,
            "remaining_runs": requested,
            "free_slots": requested,
            "analysis_context_by_node": {
                base_parent.node_id: dict(ctx.analysis_contexts.get(base_parent.node_id, {}) or {}),
            },
            "candidate_priorities": list(ctx.candidate_priorities or []),
            "analysis_plans": list(ctx.analysis_plans or []),
            "existing_sequences": [list(node.flow_sequence or []) for node in ctx.nodes.values()],
        }
        validation = validate_expansion_decision(
            policy_impl.propose(request),
            allowed_flows=allowed,
            existing_node_ids=[base_parent.node_id],
            branching_factor=requested,
            target_node=base_parent,
            target_node_map={base_parent.node_id: base_parent},
            existing_sequences=request["existing_sequences"],
            prunable_node_ids=[base_parent.node_id],
            disallow_consecutive_repeat=bool(getattr(ctx.config, "disallow_consecutive_repeat", True)),
        )
        if not validation.valid:
            raise ValueError("frontier policy produced no legal candidates: " + "; ".join(validation.errors))
        flows = [candidate.flow for candidate in validation.decision.candidates]
        if not flows:
            flows = [str(getattr(ctx.config, "fallback_flow", "phys_opt"))]
        records = []
        try:
            for index in range(requested):
                if not linear_shape:
                    ctx.current_node = base_parent
                    ctx.current_dcp = base_dcp
                    ctx.parent_metrics = dict(base_parent.metrics or {})
                flow = flows[index % len(flows)]
                record = await self.execute_transition(
                    flow,
                    source=f"{source}:{policy_key}",
                    reason="canonical frontier scope candidate",
                    stop_on_reject=False,
                    metadata={"scope_id": scope_id, "candidate_rank": index},
                )
                records.append(record)
            candidates = [ctx.nodes.get(record.outcome.child_node_id) for record in records if record.outcome is not None]
            valid = [node for node in candidates if node is not None and str(getattr(node, "status", "")).endswith("COMPLETED")]
            if valid:
                best = max(valid, key=lambda node: float(node.score if node.score is not None else float("-inf")))
                ctx.current_node = best
                ctx.current_dcp = Path(best.output_dcp)
                ctx.parent_metrics = dict(best.metrics or {})
                ctx.state.best_node_id = best.node_id
        finally:
            ctx.transition_sink.scope_id = previous_scope
        ctx.persist_state(strict_orphans=True)
        return PipelineResult(
            success=bool(records),
            run_dir=str(ctx.run_dir),
            final_output=str(ctx.current_dcp),
            summary_path=str(ctx.run_dir / "run.db"),
            state=ctx.state,
        )

    def _policy_key(self, policy: Any) -> str:
        if hasattr(policy, "propose"):
            return getattr(policy, "policy_name", None) or getattr(policy, "name", None) or "custom"
        raw = str(policy)
        key = _POLICY_ALIASES.get(raw, raw)
        if key not in _FRONTIER_POLICY_NAMES and key != "custom":
            raise ValueError(f"Unknown expansion policy: {raw}")
        return key

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
        if not changed:
            return
        root = ctx.nodes.get("node_000_root")
        if root is not None:
            root.metrics.update({key: value for key, value in ctx.root_metrics.items() if value is not None})
            root.metrics["qor_root_metrics_backfilled"] = True
            ctx.artifact_store.write_node_state(root)
