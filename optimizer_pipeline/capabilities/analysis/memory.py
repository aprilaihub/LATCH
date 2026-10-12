"""Persistent analysis-agent memory and artifact writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .schemas import AnalysisFeedbackRecord, AnalysisSessionResult, CandidatePrior, CandidatePriority, NodeAnalysisPlan, NodeDiagnosis


class AnalysisMemory:
    def __init__(self, run_dir: Path, *, enabled: bool = False, persist_artifacts: bool = True) -> None:
        self.run_dir = Path(run_dir)
        self.enabled = enabled
        self.persist_artifacts = bool(persist_artifacts)
        self.analysis_dir = self.run_dir / "analysis"
        self.sessions: List[AnalysisSessionResult] = []
        self.candidate_priors: List[CandidatePrior] = []
        self.candidate_priorities: List[CandidatePriority] = []
        self.analysis_plans: List[NodeAnalysisPlan] = []
        self.node_diagnoses: List[NodeDiagnosis] = []
        self.tool_call_events: List[Dict[str, Any]] = []
        self.loop_decision_events: List[Dict[str, Any]] = []
        self.feedback_events: Dict[str, AnalysisFeedbackRecord] = {}
        self.fast_skip_suggestions: List[Dict[str, Any]] = []
        if enabled and self.persist_artifacts:
            self.analysis_dir.mkdir(parents=True, exist_ok=True)

    def apply_session(self, session: AnalysisSessionResult) -> None:
        if not self.enabled:
            return
        self.sessions.append(session)
        self.candidate_priors.extend(session.candidate_priors)
        self.candidate_priorities.extend(session.candidate_priorities)
        if session.analysis_plan:
            self.analysis_plans.append(session.analysis_plan)
        self.fast_skip_suggestions.extend(getattr(session, "fast_skip_suggestions", []) or [])
        if session.diagnosis:
            self.node_diagnoses.append(session.diagnosis)
        for call in session.tool_calls:
            row = call.to_dict()
            row["session_id"] = session.session_id
            row["node_id"] = session.node_id
            self.tool_call_events.append(row)
        for idx, decision in enumerate(getattr(session, "loop_decisions", []) or []):
            row = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
            row["session_id"] = session.session_id
            row["node_id"] = session.node_id
            row["decision_index"] = idx
            row["tool_loop_mode"] = session.tool_loop_mode
            self.loop_decision_events.append(row)
        self.persist()

    def priors_for_node(self, node_id: str) -> List[CandidatePrior]:
        return [p for p in self.candidate_priors if p.node_id == node_id]

    def priorities_for_node(self, node_id: str) -> List[CandidatePriority]:
        return [p for p in self.candidate_priorities if p.node_id == node_id]

    def priorities_for_candidate(self, node_id: str, flow: str) -> List[CandidatePriority]:
        return [p for p in self.candidate_priorities if p.node_id == node_id and p.flow == flow]

    def priors_for_candidate(self, node_id: str, flow: str) -> List[CandidatePrior]:
        return [p for p in self.candidate_priors if p.node_id == node_id and p.flow == flow]

    def best_prior_for(self, node_id: str, flow: str) -> Optional[CandidatePrior]:
        matches = self.priors_for_candidate(node_id, flow)
        if not matches:
            return None
        return max(matches, key=lambda p: float(p.confidence or 0.0))

    def rebuild_feedback_from_policy_candidates(self, rows: List[Dict[str, Any]], *, run_id: Optional[str] = None) -> None:
        """Create selected and unselected feedback records from current policy candidates.

        This method is idempotent for a run snapshot: feedback IDs are stable and
        existing rows are overwritten with newer outcome fields when execution
        completes.
        """
        if not self.enabled:
            return
        rows_by_node_flow: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for row in rows:
            rows_by_node_flow.setdefault((str(row.get("target_node_id") or ""), str(row.get("candidate_flow") or "")), []).append(row)
        # Compute approximate rank-before-analysis for every event/target group.
        for row in rows:
            base = _approx_base_priority(row)
            if base is not None:
                row["approximate_base_priority_without_analysis"] = base
        for (event_index, target_id), group in _groups(rows, ["event_index", "target_node_id"]).items():
            base_sorted = sorted(group, key=lambda r: (_rank_priority(r, "raw_base_priority", "approximate_base_priority_without_analysis"), -int(r.get("candidate_rank") or 9999)), reverse=True)
            after_sorted = sorted(group, key=lambda r: (_rank_priority(r, "raw_analysis_adjusted_priority", "priority"), -int(r.get("candidate_rank") or 9999)), reverse=True)
            for rank, row in enumerate(base_sorted):
                row["rank_before_analysis"] = rank
            for rank, row in enumerate(after_sorted):
                row["rank_after_analysis"] = rank
        seen_prior_keys = set()
        for prior in self.candidate_priors:
            key = (prior.node_id, prior.flow)
            candidate_rows = rows_by_node_flow.get(key, [])
            if not candidate_rows:
                rec = self._feedback_record_from_prior(prior, row=None, run_id=run_id)
                self.feedback_events[rec.feedback_id] = rec
                seen_prior_keys.add((prior.source_analysis_session_id, prior.node_id, prior.flow))
                continue
            for row in candidate_rows:
                rec = self._feedback_record_from_prior(prior, row=row, run_id=run_id)
                self.feedback_events[rec.feedback_id] = rec
                seen_prior_keys.add((prior.source_analysis_session_id, prior.node_id, prior.flow))
        self.persist()

    def _feedback_record_from_prior(self, prior: CandidatePrior, *, row: Optional[Dict[str, Any]], run_id: Optional[str]) -> AnalysisFeedbackRecord:
        if row is None:
            feedback_id = f"{prior.source_analysis_session_id}/{prior.node_id}/{prior.flow}/unselected"
            return AnalysisFeedbackRecord(
                feedback_id=feedback_id,
                analysis_session_id=prior.source_analysis_session_id,
                node_id=prior.node_id,
                candidate_flow=prior.flow,
                run_id=run_id,
                priority_delta=prior.priority_delta,
                risk_delta=prior.risk_delta,
                confidence=prior.confidence,
                reason=prior.reason,
                feedback_kind="unselected_prior",
                was_emitted=False,
                was_selected=False,
                was_executed=False,
            )
        executed = bool(row.get("executed"))
        stage = str(row.get("schedule_stage") or ("executed" if executed else "emitted_not_executed"))
        delta = _delta(row.get("outcome_pure_qor_score"), row.get("parent_pure_qor_score"))
        validated = _recommendation_validated(prior, row, delta)
        feedback_id = f"{prior.source_analysis_session_id}/{row.get('candidate_id') or prior.node_id + ':' + prior.flow}"
        return AnalysisFeedbackRecord(
            feedback_id=feedback_id,
            analysis_session_id=prior.source_analysis_session_id,
            node_id=prior.node_id,
            candidate_flow=prior.flow,
            run_id=run_id or row.get("run_id"),
            event_index=_safe_int(row.get("event_index"), None),
            candidate_id=row.get("candidate_id"),
            was_emitted=True,
            was_selected=stage != "not_executed",
            was_executed=executed,
            schedule_stage=stage,
            candidate_rank_after_analysis=_safe_int(row.get("rank_after_analysis", row.get("candidate_rank")), None),
            candidate_rank_before_analysis=_safe_int(row.get("rank_before_analysis"), None),
            selected_priority=_safe_float(row.get("priority"), None),
            adjusted_priority_with_analysis=_safe_float(row.get("priority"), None),
            approximate_base_priority_without_analysis=_safe_float(row.get("approximate_base_priority_without_analysis"), None),
            priority_delta=prior.priority_delta,
            risk_delta=prior.risk_delta,
            confidence=prior.confidence,
            reason=prior.reason,
            outcome_node_id=row.get("outcome_node_id"),
            validation_status=row.get("outcome_validation_status"),
            qor_status=row.get("outcome_qor_status"),
            pure_qor_score=_safe_float(row.get("outcome_pure_qor_score"), None),
            parent_pure_qor_score=_safe_float(row.get("parent_pure_qor_score"), None),
            delta_pure_qor_from_parent=delta,
            recommendation_validated=validated,
            feedback_kind="selected_prior" if executed else "emitted_not_executed",
        )

    def to_summary(self) -> Dict[str, Any]:
        report_probe_seconds = sum(
            float(call.get("elapsed_sec") or 0.0)
            for call in self.tool_call_events
            if call.get("tool_class") == "report_probe"
        )
        feedback = list(self.feedback_events.values())
        exact_changed = len([f for f in feedback if f.candidate_rank_before_analysis is not None and f.candidate_rank_after_analysis is not None and f.candidate_rank_before_analysis != f.candidate_rank_after_analysis])
        final_contracts: Dict[str, Dict[str, Any]] = {}
        forced_overrides = 0
        for row in self.loop_decision_events:
            contract = row.get("fresh_probe_contract") if isinstance(row.get("fresh_probe_contract"), dict) else {}
            if contract.get("enabled"):
                final_contracts[str(row.get("session_id") or "")] = contract
            if row.get("fresh_probe_override"):
                forced_overrides += 1
        requiring_fresh = [contract for contract in final_contracts.values() if contract.get("enabled")]
        return {
            "enabled": self.enabled,
            "sessions": len(self.sessions),
            "tool_calls": len(self.tool_call_events),
            "loop_decisions": len(self.loop_decision_events),
            "llm_calls": sum(int(getattr(session, "llm_calls", 0) or 0) for session in self.sessions),
            "tokens": _sum_optional_int(getattr(session, "tokens", None) for session in self.sessions),
            "total_cost_usd": _sum_optional_float(getattr(session, "cost_usd", None) for session in self.sessions),
            "model": _first_present(getattr(session, "analysis_model", None) for session in self.sessions),
            "provider": _first_present(getattr(session, "analysis_provider", None) for session in self.sessions),
            "analysis_wall_time_seconds": round(sum(s.elapsed_sec for s in self.sessions), 6),
            "report_probe_seconds": round(report_probe_seconds, 6),
            "fresh_probe_sessions_requiring": len(requiring_fresh),
            "fresh_probe_sessions_satisfied": len([c for c in requiring_fresh if c.get("satisfied")]),
            "fresh_probe_sessions_incomplete": len([c for c in requiring_fresh if c.get("incomplete") or not c.get("satisfied")]),
            "fresh_probe_forced_overrides": forced_overrides,
            "act_probe_count": len([c for c in self.tool_call_events if c.get("tool_class") == "act_probe"]),
            "candidate_prior_count": len(self.candidate_priors),
            "candidate_priority_count": len(self.candidate_priorities),
            "analysis_plan_count": len(self.analysis_plans),
            "diagnosis_count": len(self.node_diagnoses),
            "feedback_count": len(feedback),
            "selected_feedback_count": len([f for f in feedback if f.was_selected]),
            "unselected_feedback_count": len([f for f in feedback if not f.was_selected]),
            "validated_feedback_count": len([f for f in feedback if f.recommendation_validated is True]),
            "analysis_changed_ranking_count": exact_changed,
            "fast_skip_suggestion_count": len(self.fast_skip_suggestions),
            "output_dir": str(self.analysis_dir) if self.enabled and self.persist_artifacts else None,
        }

    def persist(self) -> None:
        if not self.enabled or not self.persist_artifacts:
            return
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        self._write_jsonl("analysis_events.jsonl", [s.to_dict() for s in self.sessions])
        self._write_jsonl("analysis_runs.jsonl", [s.analysis_run for s in self.sessions if getattr(s, "analysis_run", None)])
        self._write_jsonl("tool_call_events.jsonl", self.tool_call_events)
        self._write_jsonl("loop_decisions.jsonl", self.loop_decision_events)
        self._write_jsonl("candidate_priors.jsonl", [p.to_dict() for p in self.candidate_priors])
        self._write_jsonl("candidate_priorities.jsonl", [p.to_dict() for p in self.candidate_priorities])
        self._write_jsonl("analysis_plans.jsonl", [p.to_dict() for p in self.analysis_plans])
        self._write_jsonl("node_diagnoses.jsonl", [d.to_dict() for d in self.node_diagnoses])
        self._write_jsonl("analysis_feedback.jsonl", [f.to_dict() for f in self.feedback_events.values()])
        self._write_jsonl("fast_skip_suggestions.jsonl", list(self.fast_skip_suggestions))
        (self.analysis_dir / "analysis_cost_summary.json").write_text(json.dumps(self.to_summary(), indent=2) + "\n")

    def _write_jsonl(self, filename: str, rows: List[Dict[str, Any]]) -> None:
        path = self.analysis_dir / filename
        with open(path, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")


def _analysis_run_row(session: AnalysisSessionResult) -> Dict[str, Any]:
    return {
        "analysis_run_id": session.session_id,
        "target_node_id": session.node_id,
        "analysis_type": session.trigger_reason,
        "status": "COMPLETED" if session.stop_decision else "UNKNOWN",
        "elapsed_sec": session.elapsed_sec,
        "tool_loop_mode": session.tool_loop_mode,
        "num_tool_calls": len(session.tool_calls),
        "num_loop_decisions": len(getattr(session, "loop_decisions", []) or []),
        "num_candidate_priors": len(session.candidate_priors),
        "num_candidate_priorities": len(session.candidate_priorities),
        "num_fast_skip_suggestions": len(getattr(session, "fast_skip_suggestions", []) or []),
        "stop_reason": session.stop_decision.stop_reason if session.stop_decision else None,
    }


def _groups(rows: List[Dict[str, Any]], keys: List[str]) -> Dict[Tuple[Any, ...], List[Dict[str, Any]]]:
    out: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(tuple(row.get(k) for k in keys), []).append(row)
    return out


def _approx_base_priority(row: Dict[str, Any]) -> Optional[float]:
    explicit = _safe_float(row.get("raw_base_priority"), None)
    if explicit is not None:
        return explicit
    priority = _safe_float(row.get("priority"), None)
    if priority is None:
        return None
    delta = _safe_float(row.get("analysis_priority_delta"), 0.0) or 0.0
    risk = _safe_float(row.get("analysis_risk_delta"), 0.0) or 0.0
    return priority - delta + max(0.0, risk) * 0.05


def _rank_priority(row: Dict[str, Any], preferred: str, fallback: str) -> float:
    value = _safe_float(row.get(preferred), None)
    if value is None:
        value = _safe_float(row.get(fallback), None)
    return value if value is not None else -1.0


def _sum_optional_int(values: Any) -> Optional[int]:
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


def _sum_optional_float(values: Any) -> Optional[float]:
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


def _first_present(values: Any) -> Optional[Any]:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _delta(value: Any, parent: Any) -> Optional[float]:
    a = _safe_float(value, None)
    b = _safe_float(parent, None)
    if a is None or b is None:
        return None
    return a - b


def _recommendation_validated(prior: CandidatePrior, row: Dict[str, Any], delta: Optional[float]) -> Optional[bool]:
    if not row.get("executed"):
        return None
    validation = row.get("outcome_validation_status")
    if prior.priority_delta > 0:
        return validation not in {"INVALID", None, ""} and (delta is None or delta >= 0.0)
    if prior.risk_delta >= 0.25:
        return validation in {"INVALID", "WARNING", "FALLBACK_USED"}
    return None


def _safe_float(value: Any, default: Optional[float]) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: Optional[int]) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default
