"""Unified record for one outer-run call into the DCP transition executor.

The transition record deliberately keeps this concept small and practical.  A
``DcpTransitionExecutor`` returns facts about one DCP transition.
``DcpTransitionRecord`` joins those facts with the outer run coordination intent
and handling decision so frontier expansion, manual loops, future portfolio/bandit
loops, analysis feedback, policy candidates exports and trace-lake exporters can
share one common record shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from optimizer_pipeline.models import to_jsonable

from optimizer_pipeline.execution.contracts import DcpTransitionResult


@dataclass
class DcpTransitionIntent:
    transition_id: str
    run_id: Optional[str]
    source: str
    parent_node_id: Optional[str]
    parent_dcp: Optional[str]
    optimizer_name: str
    optimizer_params: Dict[str, Any] = field(default_factory=dict)
    target_node_id: Optional[str] = None
    event_index: Optional[int] = None
    candidate_rank: Optional[int] = None
    priority: Optional[float] = None
    raw_base_priority: Optional[float] = None
    raw_analysis_adjusted_priority: Optional[float] = None
    clipped_priority: Optional[float] = None
    priority_clip_applied: bool = False
    rank_before_analysis: Optional[int] = None
    rank_after_analysis: Optional[int] = None
    analysis_prior_applied: bool = False
    analysis_session_id: Optional[str] = None
    analysis_priority_delta: float = 0.0
    analysis_risk_delta: float = 0.0
    analysis_confidence: Any = None
    analysis_reason: Any = None
    risk: Any = None
    confidence: Any = None
    expected_effect: Any = None
    decision_valid: Any = None
    validation_errors: Any = None
    reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class DcpTransitionOutcome:
    executed: bool = False
    success: Optional[bool] = None
    accepted_by_transition_executor: Optional[bool] = None
    output_dcp: Optional[str] = None
    validation_status: Optional[str] = None
    rejection_reason: Optional[str] = None
    pure_qor_score: Optional[float] = None
    qor_status: Optional[str] = None
    runtime_seconds: Optional[float] = None
    child_node_id: Optional[str] = None
    child_status: Optional[str] = None
    transition_record: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_transition_result(cls, result: DcpTransitionResult, *, child_node_id: Optional[str] = None, child_status: Optional[str] = None) -> "DcpTransitionOutcome":
        return cls(
            executed=True,
            success=bool(result.success),
            accepted_by_transition_executor=bool(result.accepted),
            output_dcp=result.output_dcp,
            validation_status=result.validation_status,
            rejection_reason=result.rejection_reason,
            pure_qor_score=result.pure_qor_score,
            qor_status=result.qor_score.status if result.qor_score else None,
            runtime_seconds=result.runtime_seconds,
            child_node_id=child_node_id or (result.transition_record or {}).get("child_node_id"),
            child_status=child_status,
            transition_record=dict(result.transition_record or {}),
            metrics=dict(result.metrics or {}),
        )

    @classmethod
    def from_state_node(cls, child: Any, qos: Any = None) -> Optional["DcpTransitionOutcome"]:
        if child is None:
            return None
        metrics = getattr(child, "metrics", None) if not isinstance(child, dict) else child.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        dcp_inner = metrics.get("dcp_transition") if isinstance(metrics.get("dcp_transition"), dict) else {}
        transition = dcp_inner.get("transition_record") if isinstance(dcp_inner.get("transition_record"), dict) else {}
        output_dcp = getattr(child, "output_dcp", None) if not isinstance(child, dict) else child.get("output_dcp")
        status = _status_value(getattr(child, "status", None) if not isinstance(child, dict) else child.get("status"))
        validation = getattr(child, "validation_status", None) if not isinstance(child, dict) else child.get("validation_status")
        pure_qor_score = getattr(child, "score", None) if not isinstance(child, dict) else child.get("pure_qor_score")
        node_id = getattr(child, "node_id", None) if not isinstance(child, dict) else child.get("node_id")
        return cls(
            executed=status in {"COMPLETED", "FAILED"},
            success=(status == "COMPLETED"),
            accepted_by_transition_executor=bool(dcp_inner.get("accepted")) if dcp_inner else (status == "COMPLETED"),
            output_dcp=output_dcp,
            validation_status=validation,
            rejection_reason=dcp_inner.get("rejection_reason") if dcp_inner else None,
            pure_qor_score=_safe_float(pure_qor_score, None),
            qor_status=getattr(qos, "status", None) if qos else None,
            runtime_seconds=_safe_float(metrics.get("node_runtime_seconds"), None),
            child_node_id=node_id,
            child_status=status,
            transition_record=dict(transition or {}),
            metrics=dict(metrics or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class DcpTransitionHandling:
    scheduled: bool = False
    added_to_graph: bool = False
    updated_incumbent: bool = False
    final_status: str = "proposed"  # proposed | not_selected | scheduled | executed | accepted | rejected
    schedule_stage: str = "not_executed"  # not_executed | scheduled | executed
    outer_rejection_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class DcpTransitionRecord:
    intent: DcpTransitionIntent
    outcome: Optional[DcpTransitionOutcome] = None
    handling: Optional[DcpTransitionHandling] = None

    @classmethod
    def scheduled_transition(
        cls,
        *,
        run_id: Optional[str],
        transition_id: str,
        source: str,
        parent_node_id: Optional[str],
        parent_dcp: Optional[str],
        optimizer_name: str,
        optimizer_params: Optional[Dict[str, Any]] = None,
        target_node_id: Optional[str] = None,
        event_index: Optional[int] = None,
        candidate_rank: Optional[int] = None,
        priority: Optional[float] = None,
        raw_base_priority: Optional[float] = None,
        raw_analysis_adjusted_priority: Optional[float] = None,
        clipped_priority: Optional[float] = None,
        priority_clip_applied: bool = False,
        analysis_prior_applied: bool = False,
        analysis_session_id: Optional[str] = None,
        analysis_priority_delta: float = 0.0,
        analysis_risk_delta: float = 0.0,
        analysis_confidence: Any = None,
        analysis_reason: Any = None,
        risk: Any = None,
        confidence: Any = None,
        expected_effect: Any = None,
        decision_valid: Any = None,
        validation_errors: Any = None,
        reason: Optional[str] = None,
        added_to_graph: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "DcpTransitionRecord":
        intent = DcpTransitionIntent(
            transition_id=transition_id,
            run_id=run_id,
            source=source,
            parent_node_id=parent_node_id,
            parent_dcp=str(parent_dcp) if parent_dcp is not None else None,
            optimizer_name=optimizer_name,
            optimizer_params=dict(optimizer_params or {"flow": optimizer_name}),
            target_node_id=target_node_id or parent_node_id,
            event_index=event_index,
            candidate_rank=candidate_rank,
            priority=priority if priority is not None else clipped_priority,
            raw_base_priority=raw_base_priority,
            raw_analysis_adjusted_priority=raw_analysis_adjusted_priority,
            clipped_priority=clipped_priority if clipped_priority is not None else priority,
            priority_clip_applied=priority_clip_applied,
            analysis_prior_applied=analysis_prior_applied,
            analysis_session_id=analysis_session_id,
            analysis_priority_delta=analysis_priority_delta,
            analysis_risk_delta=analysis_risk_delta,
            analysis_confidence=analysis_confidence,
            analysis_reason=analysis_reason,
            risk=risk,
            confidence=confidence,
            expected_effect=expected_effect,
            decision_valid=decision_valid,
            validation_errors=validation_errors,
            reason=reason,
            metadata=dict(metadata or {}),
        )
        handling = DcpTransitionHandling(
            scheduled=True,
            added_to_graph=bool(added_to_graph),
            updated_incumbent=False,
            final_status="scheduled",
            schedule_stage="scheduled",
            outer_rejection_reason=None,
        )
        return cls(intent=intent, outcome=None, handling=handling)

    @classmethod
    def completed_transition(
        cls,
        *,
        intent: DcpTransitionIntent,
        result: DcpTransitionResult,
        child_node_id: Optional[str] = None,
        child_status: Optional[str] = None,
        added_to_graph: bool = False,
        updated_incumbent: bool = False,
        outer_rejection_reason: Optional[str] = None,
    ) -> "DcpTransitionRecord":
        outcome = DcpTransitionOutcome.from_transition_result(result, child_node_id=child_node_id, child_status=child_status)
        accepted = bool(result.accepted)
        handling = DcpTransitionHandling(
            scheduled=True,
            added_to_graph=bool(added_to_graph),
            updated_incumbent=bool(updated_incumbent),
            final_status="accepted" if accepted else "rejected",
            schedule_stage="executed",
            outer_rejection_reason=outer_rejection_reason if outer_rejection_reason is not None else (None if accepted else result.rejection_reason),
        )
        return cls(intent=intent, outcome=outcome, handling=handling)

    @classmethod
    def from_frontier_candidate_scheduled(
        cls,
        *,
        run_id: Optional[str],
        parent_node: Any,
        child_node: Any,
        candidate: Any,
        event_index: Optional[int] = None,
        candidate_rank: Optional[int] = None,
        request: Any = None,
        decision: Optional[Dict[str, Any]] = None,
        validation: Optional[Dict[str, Any]] = None,
    ) -> "DcpTransitionRecord":
        flow = str(getattr(candidate, "flow", None) or (candidate.get("flow") if isinstance(candidate, dict) else ""))
        cand = candidate.to_dict() if hasattr(candidate, "to_dict") else dict(candidate or {}) if isinstance(candidate, dict) else {}
        target_id = str(cand.get("target_node_id") or _node_id(parent_node) or "")
        prior = matching_analysis_prior(request, target_id, flow)
        analysis_priority_delta = _safe_float(prior.get("priority_delta") if prior else None, 0.0) or 0.0
        analysis_risk_delta = _safe_float(prior.get("risk_delta") if prior else None, 0.0) or 0.0
        audit = candidate_priority_audit_fields(cand, analysis_priority_delta=analysis_priority_delta, analysis_risk_delta=analysis_risk_delta)
        return cls.scheduled_transition(
            run_id=run_id,
            transition_id=str(_node_id(child_node) or f"event_{event_index}_{candidate_rank}"),
            source="frontier_policy",
            parent_node_id=_node_id(parent_node),
            parent_dcp=_node_dcp(parent_node),
            optimizer_name=flow,
            optimizer_params={"flow": flow},
            target_node_id=target_id,
            event_index=event_index,
            candidate_rank=candidate_rank,
            priority=audit["clipped_priority"],
            raw_base_priority=audit["raw_base_priority"],
            raw_analysis_adjusted_priority=audit["raw_analysis_adjusted_priority"],
            clipped_priority=audit["clipped_priority"],
            priority_clip_applied=bool(audit["priority_clip_applied"]),
            analysis_prior_applied=bool(prior),
            analysis_session_id=(prior or {}).get("analysis_session_id") or (prior or {}).get("source_analysis_session_id"),
            analysis_priority_delta=analysis_priority_delta if prior else 0.0,
            analysis_risk_delta=analysis_risk_delta if prior else 0.0,
            analysis_confidence=(prior or {}).get("confidence"),
            analysis_reason=(prior or {}).get("reason"),
            risk=cand.get("risk"),
            confidence=cand.get("confidence"),
            expected_effect=cand.get("expected_effect"),
            decision_valid=(validation or {}).get("valid") if isinstance(validation, dict) else None,
            validation_errors=(validation or {}).get("errors") if isinstance(validation, dict) else None,
            reason=(decision or {}).get("reason") if isinstance(decision, dict) else "frontier policy scheduled candidate",
            added_to_graph=True,
            metadata={"policy_decision_type": (decision or {}).get("decision_type") if isinstance(decision, dict) else None},
        )

    @classmethod
    def from_frontier_candidate_completed(
        cls,
        *,
        scheduled_record: "DcpTransitionRecord",
        result: DcpTransitionResult,
        child_node_id: Optional[str],
        child_status: Optional[str],
        updated_incumbent: bool = False,
    ) -> "DcpTransitionRecord":
        return cls.completed_transition(
            intent=scheduled_record.intent,
            result=result,
            child_node_id=child_node_id,
            child_status=child_status,
            added_to_graph=True,
            updated_incumbent=updated_incumbent,
        )

    @classmethod
    def from_policy_candidate(
        cls,
        *,
        run_id: Optional[str],
        event_index: Optional[int],
        event: Optional[Dict[str, Any]] = None,
        decision: Dict[str, Any],
        validation: Dict[str, Any],
        candidate: Dict[str, Any],
        candidate_rank: int,
        request: Any = None,
        parent_node: Any = None,
        child_node: Any = None,
        qos: Any = None,
        llm_metadata: Optional[Dict[str, Any]] = None,
    ) -> "DcpTransitionRecord":
        target_id = str(candidate.get("target_node_id") or decision.get("target_node_id") or "")
        flow = str(candidate.get("flow") or "")
        prior = matching_analysis_prior(request, target_id, flow)
        analysis_priority_delta = _safe_float(prior.get("priority_delta") if prior else None, 0.0) or 0.0
        analysis_risk_delta = _safe_float(prior.get("risk_delta") if prior else None, 0.0) or 0.0
        audit = candidate_priority_audit_fields(
            candidate,
            analysis_priority_delta=analysis_priority_delta,
            analysis_risk_delta=analysis_risk_delta,
        )
        parent_dcp = _node_dcp(parent_node) if parent_node is not None else None
        intent = DcpTransitionIntent(
            transition_id=_node_id(child_node) or f"event_{event_index}_{candidate_rank}",
            run_id=run_id,
            source="frontier_policy",
            parent_node_id=target_id,
            parent_dcp=parent_dcp,
            optimizer_name=flow,
            optimizer_params={"flow": flow},
            target_node_id=target_id,
            event_index=event_index,
            candidate_rank=candidate_rank,
            priority=audit["clipped_priority"],
            raw_base_priority=audit["raw_base_priority"],
            raw_analysis_adjusted_priority=audit["raw_analysis_adjusted_priority"],
            clipped_priority=audit["clipped_priority"],
            priority_clip_applied=bool(audit["priority_clip_applied"]),
            analysis_prior_applied=bool(prior),
            analysis_session_id=(prior or {}).get("analysis_session_id") or (prior or {}).get("source_analysis_session_id"),
            analysis_priority_delta=analysis_priority_delta if prior else 0.0,
            analysis_risk_delta=analysis_risk_delta if prior else 0.0,
            analysis_confidence=(prior or {}).get("confidence"),
            analysis_reason=(prior or {}).get("reason"),
            risk=candidate.get("risk"),
            confidence=candidate.get("confidence"),
            expected_effect=candidate.get("expected_effect"),
            decision_valid=validation.get("valid"),
            validation_errors=validation.get("errors"),
            reason=decision.get("reason"),
            metadata={
                "policy_decision_type": decision.get("decision_type"),
                "llm_metadata": dict(llm_metadata or {}),
            },
        )
        outcome = DcpTransitionOutcome.from_state_node(child_node, qos=qos) if child_node is not None else None
        if outcome and outcome.executed:
            schedule_stage = "executed"
            final_status = "accepted" if outcome.accepted_by_transition_executor and outcome.child_status == "COMPLETED" else "rejected"
        elif child_node is not None:
            schedule_stage = "scheduled"
            final_status = "scheduled"
        else:
            schedule_stage = "not_executed"
            final_status = "not_selected"
        handling = DcpTransitionHandling(
            scheduled=bool(child_node),
            added_to_graph=bool(child_node),
            updated_incumbent=False,
            final_status=final_status,
            schedule_stage=schedule_stage,
            outer_rejection_reason=None if child_node else "not_selected_or_budget_exhausted",
        )
        return cls(intent=intent, outcome=outcome, handling=handling)

    @classmethod
    def from_fixed_sequence_transition(
        cls,
        *,
        run_id: Optional[str],
        step_index: int,
        input_dcp: Any,
        optimizer_name: str,
        result: DcpTransitionResult,
        flow_sequence_before: Optional[List[str]] = None,
    ) -> "DcpTransitionRecord":
        previous_flow = (flow_sequence_before or [])[-1] if step_index and flow_sequence_before else None
        parent_node_id = f"manual_{step_index - 1:03d}_{_safe_label(previous_flow)}" if previous_flow else (f"manual_{step_index - 1:03d}" if step_index else "root")
        intent = DcpTransitionIntent(
            transition_id=f"manual_{step_index:03d}_{_safe_label(optimizer_name)}",
            run_id=run_id,
            source="manual_plan",
            parent_node_id=parent_node_id,
            parent_dcp=str(input_dcp),
            optimizer_name=optimizer_name,
            optimizer_params=dict(result.optimizer_params or {"flow": optimizer_name}),
            target_node_id=parent_node_id,
            event_index=None,
            candidate_rank=step_index,
            priority=None,
            reason="manual fixed-loop plan",
            metadata={"flow_sequence_before": list(flow_sequence_before or [])},
        )
        outcome = DcpTransitionOutcome.from_transition_result(result, child_node_id=f"manual_{step_index:03d}_{_safe_label(optimizer_name)}")
        handling = DcpTransitionHandling(
            scheduled=True,
            added_to_graph=False,
            updated_incumbent=bool(result.accepted),
            final_status="accepted" if result.accepted else "rejected",
            schedule_stage="executed",
            outer_rejection_reason=None if result.accepted else result.rejection_reason,
        )
        return cls(intent=intent, outcome=outcome, handling=handling)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "transition_record.v1",
            "intent": self.intent.to_dict(),
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "handling": self.handling.to_dict() if self.handling else None,
        }

    def to_policy_candidate_row(self) -> Dict[str, Any]:
        intent = self.intent
        outcome = self.outcome
        handling = self.handling or DcpTransitionHandling()
        row = {
            "transition_record_schema": "transition_record.v1",
            "transition_id": intent.transition_id,
            "source": intent.source,
            "candidate_id": intent.transition_id,
            "run_id": intent.run_id,
            "event_index": intent.event_index,
            "target_node_id": intent.target_node_id or intent.parent_node_id,
            "candidate_flow": intent.optimizer_name,
            "optimizer_name": intent.optimizer_name,
            "optimizer_params": dict(intent.optimizer_params or {}),
            "parent_dcp": intent.parent_dcp,
            "priority": intent.clipped_priority,
            "raw_base_priority": intent.raw_base_priority,
            "raw_analysis_adjusted_priority": intent.raw_analysis_adjusted_priority,
            "clipped_priority": intent.clipped_priority,
            "priority_clip_applied": intent.priority_clip_applied,
            "approximate_base_priority_without_analysis": intent.raw_base_priority,
            "analysis_prior_applied": intent.analysis_prior_applied,
            "analysis_session_id": intent.analysis_session_id,
            "analysis_priority_delta": intent.analysis_priority_delta,
            "analysis_risk_delta": intent.analysis_risk_delta,
            "analysis_confidence": intent.analysis_confidence,
            "analysis_reason": intent.analysis_reason,
            "risk": intent.risk,
            "confidence": intent.confidence,
            "expected_effect": intent.expected_effect,
            "decision_valid": intent.decision_valid,
            "validation_errors": intent.validation_errors,
            "candidate_rank": intent.candidate_rank,
            "rank_before_analysis": intent.rank_before_analysis,
            "rank_after_analysis": intent.rank_after_analysis,
            "schedule_stage": handling.schedule_stage,
            "executed": bool(outcome.executed) if outcome else False,
            "scheduled": bool(handling.scheduled),
            "final_status": handling.final_status,
            "outer_rejection_reason": handling.outer_rejection_reason,
            "parent_pure_qor_score": None,
            "outcome_node_id": outcome.child_node_id if outcome else None,
            "outcome_status": outcome.child_status if outcome else None,
            "outcome_validation_status": outcome.validation_status if outcome else None,
            "outcome_qor_status": outcome.qor_status if outcome else None,
            "outcome_pure_qor_score": outcome.pure_qor_score if outcome else None,
            "trajectory_objective_profile": "pure_qor_v1",
            "trajectory_objective_score": outcome.pure_qor_score if outcome else None,
            "accepted_by_transition_executor": outcome.accepted_by_transition_executor if outcome else None,
            "output_dcp": outcome.output_dcp if outcome else None,
            "runtime_seconds": outcome.runtime_seconds if outcome else None,
            "rejection_reason": outcome.rejection_reason if outcome else None,
            "transition_record": outcome.transition_record if outcome else {},
        }
        row.update(_flat_llm_metadata(intent.metadata.get("llm_metadata") if isinstance(intent.metadata, dict) else None))
        return row


def build_transition_records_from_policy_events(
    *,
    policy_events: Iterable[Dict[str, Any]],
    nodes: Dict[str, Any],
    qor_scores: Dict[str, Any],
    run_id: Optional[str],
    llm_decision_events: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[DcpTransitionRecord]:
    llm_metadata_by_event: Dict[int, Dict[str, Any]] = {}
    executed_index: Dict[Tuple[str, str], Any] = {}
    for node in nodes.values():
        parent_id = getattr(node, "parent_id", None)
        flow = getattr(node, "selected_flow", None)
        if parent_id and flow:
            executed_index[(str(parent_id), str(flow))] = node
    records: List[DcpTransitionRecord] = []
    for event in policy_events:
        decision = event.get("decision") if isinstance(event.get("decision"), dict) else {}
        validation = event.get("validation") if isinstance(event.get("validation"), dict) else {}
        request = event.get("request")
        candidates = decision.get("candidates") if isinstance(decision.get("candidates"), list) else []
        for rank, cand in enumerate(candidates):
            if not isinstance(cand, dict):
                continue
            target_id = str(cand.get("target_node_id") or decision.get("target_node_id") or "")
            flow = str(cand.get("flow") or "")
            child = executed_index.get((target_id, flow))
            parent_node = nodes.get(target_id)
            qos = qor_scores.get(getattr(child, "node_id", "")) if child else None
            record = DcpTransitionRecord.from_policy_candidate(
                run_id=run_id,
                event_index=event.get("event_index"),
                event=event,
                decision=decision,
                validation=validation,
                candidate=cand,
                candidate_rank=rank,
                request=request,
                parent_node=parent_node,
                child_node=child,
                qos=qos,
                llm_metadata=llm_metadata_by_event.get(event.get("event_index")),
            )
            if parent_node is not None:
                try:
                    record.intent.metadata["parent_pure_qor_score"] = getattr(parent_node, "score", None)
                except Exception:
                    pass
            records.append(record)
    annotate_rank_before_after(records)
    return records


def records_to_policy_candidate_rows(records: Iterable[DcpTransitionRecord]) -> List[Dict[str, Any]]:
    rows = [record.to_policy_candidate_row() for record in records]
    # Preserve parent pure-QoR context if it was stored in metadata.
    for row, record in zip(rows, records):
        if row.get("parent_pure_qor_score") is None:
            row["parent_pure_qor_score"] = record.intent.metadata.get("parent_pure_qor_score")
    return rows


def _flat_llm_metadata(metadata: Any) -> Dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "llm_provider": metadata.get("provider"),
        "llm_model": metadata.get("model"),
        "llm_prompt_id": metadata.get("prompt_id"),
        "llm_prompt_hash": metadata.get("prompt_hash"),
        "llm_response_schema_hash": metadata.get("response_schema_hash"),
        "llm_temperature": metadata.get("temperature"),
        "llm_max_tokens": metadata.get("max_tokens"),
        "llm_response_mode": metadata.get("response_mode"),
        "llm_prompt_tokens": metadata.get("prompt_tokens"),
        "llm_completion_tokens": metadata.get("completion_tokens"),
        "llm_total_tokens": metadata.get("total_tokens"),
        "llm_cost_usd": metadata.get("cost_usd"),
        "llm_provider_reported_cost_usd": metadata.get("provider_reported_cost_usd"),
        "llm_estimated_cost_usd": metadata.get("estimated_cost_usd"),
        "llm_cost_source": metadata.get("cost_source"),
        "llm_pricing_profile_id": metadata.get("pricing_profile_id"),
        "llm_latency_sec": metadata.get("latency_sec"),
        "llm_error": metadata.get("error"),
    }


def annotate_rank_before_after(records: List[DcpTransitionRecord]) -> None:
    groups: Dict[Tuple[Any, Any], List[DcpTransitionRecord]] = {}
    for record in records:
        groups.setdefault((record.intent.event_index, record.intent.target_node_id or record.intent.parent_node_id), []).append(record)
    for group in groups.values():
        before = sorted(group, key=lambda r: (_rank_priority_record(r, "raw_base_priority", "priority"), -(int(r.intent.candidate_rank or 9999))), reverse=True)
        after = sorted(group, key=lambda r: (_rank_priority_record(r, "raw_analysis_adjusted_priority", "priority"), -(int(r.intent.candidate_rank or 9999))), reverse=True)
        for rank, record in enumerate(before):
            record.intent.rank_before_analysis = rank
        for rank, record in enumerate(after):
            record.intent.rank_after_analysis = rank


def matching_analysis_prior(request: Any, node_id: str, optimizer_name: str) -> Optional[Dict[str, Any]]:
    if not isinstance(request, dict):
        return None
    priors = request.get("analysis_priors") or request.get("analysis_candidate_priors") or []
    best = None
    best_conf = -1.0
    for prior in priors if isinstance(priors, list) else []:
        if not isinstance(prior, dict):
            continue
        p_node = str(prior.get("target_node_id") or prior.get("node_id") or "")
        p_flow = str(prior.get("candidate_flow") or prior.get("flow") or "")
        if p_node != str(node_id) or p_flow != str(optimizer_name):
            continue
        conf = _safe_float(prior.get("confidence"), 0.0) or 0.0
        if conf >= best_conf:
            best = prior
            best_conf = conf
    return best


def candidate_priority_audit_fields(candidate: Dict[str, Any], *, analysis_priority_delta: float = 0.0, analysis_risk_delta: float = 0.0) -> Dict[str, Any]:
    clipped_priority = _safe_float(candidate.get("priority"), None)
    raw_base_priority = _first_float(candidate, [
        "raw_base_priority",
        "base_priority",
        "approximate_base_priority_without_analysis",
    ])
    raw_analysis_adjusted_priority = _first_float(candidate, [
        "raw_analysis_adjusted_priority",
        "analysis_adjusted_priority",
        "adjusted_priority_with_analysis",
    ])
    risk_penalty = max(0.0, float(analysis_risk_delta or 0.0)) * 0.05
    if raw_base_priority is None and raw_analysis_adjusted_priority is not None:
        raw_base_priority = raw_analysis_adjusted_priority - float(analysis_priority_delta or 0.0) + risk_penalty
    if raw_base_priority is None and clipped_priority is not None:
        raw_base_priority = clipped_priority - float(analysis_priority_delta or 0.0) + risk_penalty
    if raw_analysis_adjusted_priority is None and raw_base_priority is not None:
        raw_analysis_adjusted_priority = raw_base_priority + float(analysis_priority_delta or 0.0) - risk_penalty
    if clipped_priority is None and raw_analysis_adjusted_priority is not None:
        clipped_priority = max(0.0, min(1.0, raw_analysis_adjusted_priority))
    priority_clip_applied = False
    if raw_analysis_adjusted_priority is not None and clipped_priority is not None:
        priority_clip_applied = abs(raw_analysis_adjusted_priority - clipped_priority) > 1e-9
    return {
        "raw_base_priority": raw_base_priority,
        "raw_analysis_adjusted_priority": raw_analysis_adjusted_priority,
        "clipped_priority": clipped_priority,
        "priority_clip_applied": priority_clip_applied,
    }


def _node_id(node: Any) -> Optional[str]:
    if node is None:
        return None
    value = getattr(node, "node_id", None) if not isinstance(node, dict) else node.get("node_id")
    return str(value) if value else None


def _node_dcp(node: Any) -> Optional[str]:
    if node is None:
        return None
    output = getattr(node, "output_dcp", None) if not isinstance(node, dict) else node.get("output_dcp")
    input_dcp = getattr(node, "input_dcp", None) if not isinstance(node, dict) else node.get("input_dcp")
    return str(output or input_dcp) if (output or input_dcp) else None


def _status_value(status: Any) -> Optional[str]:
    if status is None:
        return None
    return status.value if hasattr(status, "value") else str(status)


def _first_float(row: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for key in keys:
        value = _safe_float(row.get(key), None)
        if value is not None:
            return value
    return None


def _rank_priority_record(record: DcpTransitionRecord, preferred: str, fallback: str) -> float:
    value = getattr(record.intent, preferred, None)
    if value is None:
        value = getattr(record.intent, fallback, None)
    return value if value is not None else -1.0


def _safe_float(value: Any, default: Optional[float]) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_label(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value)).strip("_") or "step"
