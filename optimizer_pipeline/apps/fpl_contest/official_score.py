"""Official contest trace export for contest-compatible runs."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from optimizer_pipeline.scoring.official_contest import compute_fpl26_official_contest_score
from optimizer_pipeline.scoring import file_sha256, score_fmax_delta


TRAJECTORY_OBJECTIVE_PROFILE = "pure_qor_v1"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass
class OfficialContestTracer:
    """Append official contest root and transition rows to JSONL."""

    run_dir: Path
    run_mode: str
    contest_eligible: bool
    llm_provider: str
    llm_model: Optional[str] = None
    noncontest_reason: Optional[str] = None
    epsilon_fmax_mhz: float = 0.0

    def __post_init__(self) -> None:
        self.run_dir = Path(self.run_dir)
        self.rows = []
        self.epsilon_fmax_mhz = float(self.epsilon_fmax_mhz)
        if self.epsilon_fmax_mhz < 0:
            raise ValueError("epsilon_fmax_mhz_must_be_nonnegative")

    def append_root(self, *, input_dcp: Path, root_metrics: Dict[str, Any], published_output: Path) -> Dict[str, Any]:
        fmax = _first_float(root_metrics, "final_fmax_mhz", "fmax_mhz", "initial_fmax_mhz")
        row = self._base_row(
            node_id="node_000_root",
            transition_id="baseline",
            parent_node_id=None,
            optimizer_name="baseline",
            flow_sequence=[],
            input_dcp=str(input_dcp),
            output_dcp=str(published_output),
            fmax_input_mhz=fmax,
            fmax_output_mhz=fmax,
            wns_ns=_first_float(root_metrics, "final_wns_ns", "wns_ns", "initial_wns_ns"),
            tns_ns=_first_float(root_metrics, "final_tns_ns", "tns_ns", "initial_tns_ns"),
            runtime_seconds=0.0,
            openrouter_cost_usd=0.0,
            valid_for_scoring=True,
            produced_output_dcp=Path(published_output).exists(),
            pure_qor_score=0.0,
            pure_qor_components={"status": "baseline"},
            rejection_reason=None,
        )
        row["event_type"] = "baseline"
        row["publication_selection"]["selected_as_published_best"] = True
        return self.append(row)

    def row_from_transition(
        self,
        record: Any,
        *,
        baseline_fmax_mhz: Optional[float],
        openrouter_cost_usd: Optional[float] = None,
    ) -> Dict[str, Any]:
        intent = getattr(record, "intent", None)
        outcome = getattr(record, "outcome", None)
        handling = getattr(record, "handling", None)
        transition_record = dict(getattr(outcome, "transition_record", None) or {})
        metrics = dict(getattr(outcome, "metrics", None) or {})
        output_dcp = getattr(outcome, "output_dcp", None) or transition_record.get("output_dcp")
        fmax = _first_float(metrics, "final_fmax_mhz", "fmax_mhz", "initial_fmax_mhz")
        if fmax is None:
            fmax = _first_float(transition_record, "final_fmax_mhz", "fmax_mhz")
        effective_baseline_fmax = baseline_fmax_mhz
        if effective_baseline_fmax is None:
            effective_baseline_fmax = _first_float(metrics, "root_fmax_mhz")
        if effective_baseline_fmax is None and isinstance(metrics.get("qor_root_metrics"), dict):
            effective_baseline_fmax = _first_float(metrics["qor_root_metrics"], "initial_fmax_mhz", "final_fmax_mhz", "fmax_mhz")
        validation_valid = bool(getattr(outcome, "accepted_by_transition_executor", False))
        routing_errors = _routing_errors(metrics)
        fully_routed = routing_errors == 0 if routing_errors is not None else validation_valid
        manual_state = metrics.get("manual_checkpoint_state") if isinstance(metrics.get("manual_checkpoint_state"), dict) else {}
        if manual_state:
            fully_routed = bool(manual_state.get("placement") == "placed" and manual_state.get("routing") == "routed")
        produced_output_dcp = bool(output_dcp and Path(output_dcp).exists())
        valid_for_scoring = bool(validation_valid and produced_output_dcp and fully_routed)
        if metrics.get("publish_eligible") is False:
            valid_for_scoring = False
        if routing_errors is not None and routing_errors > 0:
            valid_for_scoring = False
        runtime_seconds = _first_float(metrics, "node_runtime_seconds", "total_runtime_seconds", "duration_seconds")
        pure_qor_components = metrics.get("qor_score") if isinstance(metrics.get("qor_score"), dict) else {}
        row = self._base_row(
            node_id=getattr(outcome, "child_node_id", None),
            transition_id=getattr(intent, "transition_id", None),
            parent_node_id=getattr(intent, "parent_node_id", None),
            optimizer_name=getattr(intent, "optimizer_name", None),
            flow_sequence=transition_record.get("flow_sequence_after")
            or list((getattr(intent, "metadata", None) or {}).get("flow_sequence_before") or [])
            + [getattr(intent, "optimizer_name", None)],
            input_dcp=getattr(intent, "parent_dcp", None),
            output_dcp=output_dcp,
            fmax_input_mhz=effective_baseline_fmax,
            fmax_output_mhz=fmax,
            wns_ns=_first_float(metrics, "final_wns_ns", "wns_ns"),
            tns_ns=_first_float(metrics, "final_tns_ns", "tns_ns"),
            runtime_seconds=runtime_seconds,
            openrouter_cost_usd=_float_or(openrouter_cost_usd, 0.0) if self.llm_provider == "openrouter" else 0.0,
            valid_for_scoring=valid_for_scoring,
            produced_output_dcp=produced_output_dcp,
            pure_qor_score=getattr(outcome, "pure_qor_score", None),
            pure_qor_components=pure_qor_components,
            rejection_reason=getattr(outcome, "rejection_reason", None) or getattr(handling, "outer_rejection_reason", None),
        )
        row.update(
            {
                "event_type": "transition",
                "fully_routed": fully_routed,
                "routing_errors": routing_errors,
                "hold_met": _first_bool(metrics, "hold_met", "hold_timing_met"),
                "pulse_width_met": _first_bool(metrics, "pulse_width_met", "pulse_width_timing_met"),
                "functional_equivalence_status": metrics.get("functional_equivalence_status") or "unknown",
                "baseline_fmax_mhz": effective_baseline_fmax,
                "baseline_wns_ns": _first_float(metrics, "root_wns_ns"),
                "baseline_tns_ns": _first_float(metrics, "root_tns_ns"),
                "validation_status": getattr(outcome, "validation_status", None),
                "schedule_stage": getattr(handling, "schedule_stage", None),
                "manual_checkpoint_state": manual_state or None,
                "publish_eligible": bool(valid_for_scoring),
            }
        )
        if not self.contest_eligible:
            row["reason"] = self.noncontest_reason
        return row

    def append(self, row: Dict[str, Any]) -> Dict[str, Any]:
        self.rows.append(row)
        return row

    def update_baseline_row(
        self,
        *,
        fmax_mhz: Optional[float],
        wns_ns: Optional[float] = None,
        tns_ns: Optional[float] = None,
    ) -> None:
        """Backfill the baseline trace row once resolved root QoR is known."""

        if fmax_mhz is None:
            return
        source_rows = self.rows
        rows = []
        changed = False
        for original in source_rows:
            row = dict(original)
            if row.get("event_type") == "baseline" and row.get("fmax_output_mhz") is None:
                row["fmax_input_mhz"] = fmax_mhz
                row["fmax_output_mhz"] = fmax_mhz
                row["delta_fmax_mhz"] = 0.0
                score = self._official_score_fields(
                    fmax_input_mhz=fmax_mhz,
                    fmax_output_mhz=fmax_mhz,
                    runtime_seconds=row.get("runtime_seconds"),
                    openrouter_cost_usd=row.get("openrouter_cost_usd"),
                    valid_for_scoring=bool(row.get("valid_for_scoring")),
                    produced_output_dcp=bool(row.get("output_dcp")),
                )
                row["official_contest_score"] = score["official_contest_score"]
                row["official_contest_score_components"] = score["components"]
                row["publication_selection"]["fmax_output_mhz"] = fmax_mhz
                row["publication_selection"]["delta_fmax_mhz"] = 0.0
                if wns_ns is not None:
                    row["wns_ns"] = wns_ns
                if tns_ns is not None:
                    row["tns_ns"] = tns_ns
                changed = True
            rows.append(row)
        self.rows = rows

    def _base_row(
        self,
        *,
        node_id: Optional[str],
        transition_id: Optional[str],
        parent_node_id: Optional[str],
        optimizer_name: Optional[str],
        flow_sequence: Any,
        input_dcp: Optional[str],
        output_dcp: Optional[str],
        fmax_input_mhz: Optional[float],
        fmax_output_mhz: Optional[float],
        wns_ns: Optional[float],
        tns_ns: Optional[float],
        runtime_seconds: Optional[float],
        openrouter_cost_usd: float,
        valid_for_scoring: bool,
        produced_output_dcp: bool,
        pure_qor_score: Optional[float],
        pure_qor_components: Dict[str, Any],
        rejection_reason: Optional[str],
    ) -> Dict[str, Any]:
        official = self._official_score_fields(
            fmax_input_mhz=fmax_input_mhz,
            fmax_output_mhz=fmax_output_mhz,
            runtime_seconds=runtime_seconds,
            openrouter_cost_usd=openrouter_cost_usd,
            valid_for_scoring=valid_for_scoring,
            produced_output_dcp=produced_output_dcp,
        )
        delta_evidence = score_fmax_delta(
            root_fmax_mhz=fmax_input_mhz,
            child_fmax_mhz=fmax_output_mhz,
            root_sha256=file_sha256(input_dcp),
            child_sha256=file_sha256(output_dcp),
            epsilon_fmax=self.epsilon_fmax_mhz,
        )
        delta_fmax = delta_evidence["delta_fmax_mhz"]
        publication_selection = {
            "eligible": bool(valid_for_scoring),
            "selection_metric": "fmax_output_mhz",
            "fmax_output_mhz": fmax_output_mhz,
            "delta_fmax_mhz": delta_fmax,
            "measurement_delta_fmax_mhz": delta_evidence["measurement_delta_fmax_mhz"],
            "input_dcp_sha256": delta_evidence["root_sha256"],
            "output_dcp_sha256": delta_evidence["child_sha256"],
            "byte_identical_child": delta_evidence["byte_identical_child"],
            "epsilon_fmax": delta_evidence["epsilon_fmax"],
            "selected_as_published_best": False,
        }
        return {
            "schema_version": "official_contest_trace.v1",
            "generated_at": now_iso(),
            "run_mode": self.run_mode,
            "contest_eligible": self.contest_eligible,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "node_id": node_id,
            "transition_id": transition_id,
            "parent_node_id": parent_node_id,
            "optimizer_name": optimizer_name,
            "flow_sequence": [item for item in list(flow_sequence or []) if item],
            "input_dcp": input_dcp,
            "output_dcp": output_dcp,
            "fully_routed": bool(valid_for_scoring),
            "routing_errors": 0 if valid_for_scoring else None,
            "hold_met": None,
            "pulse_width_met": None,
            "functional_equivalence_status": "unknown",
            "fmax_input_mhz": fmax_input_mhz,
            "fmax_output_mhz": fmax_output_mhz,
            "delta_fmax_mhz": delta_fmax,
            "measurement_delta_fmax_mhz": delta_evidence["measurement_delta_fmax_mhz"],
            "input_dcp_sha256": delta_evidence["root_sha256"],
            "output_dcp_sha256": delta_evidence["child_sha256"],
            "byte_identical_child": delta_evidence["byte_identical_child"],
            "epsilon_fmax": delta_evidence["epsilon_fmax"],
            "wns_ns": wns_ns,
            "tns_ns": tns_ns,
            "runtime_seconds": runtime_seconds,
            "openrouter_cost_usd": openrouter_cost_usd,
            "noncontest_llm_cost_usd": 0.0,
            "official_contest_score": official["official_contest_score"],
            "official_contest_score_components": official["components"],
            "pure_qor_score": pure_qor_score,
            "pure_qor_components": pure_qor_components,
            "trajectory_objective_profile": TRAJECTORY_OBJECTIVE_PROFILE,
            "trajectory_objective_score": pure_qor_score,
            "valid_for_scoring": bool(valid_for_scoring),
            "publication_selection": publication_selection,
            "rejection_reason": rejection_reason,
        }

    def _official_score_fields(
        self,
        *,
        fmax_input_mhz: Optional[float],
        fmax_output_mhz: Optional[float],
        runtime_seconds: Optional[float],
        openrouter_cost_usd: Any,
        valid_for_scoring: bool,
        produced_output_dcp: bool,
    ) -> Dict[str, Any]:
        score = compute_fpl26_official_contest_score(
            fmax_input_mhz=fmax_input_mhz,
            fmax_output_mhz=fmax_output_mhz,
            openrouter_cost_usd=_float_or(openrouter_cost_usd, 0.0),
            runtime_seconds=_float_or(runtime_seconds, 0.0),
            valid_for_scoring=valid_for_scoring,
            produced_output_dcp=produced_output_dcp,
        )
        components = score.to_dict()
        components["formula_score"] = components.pop("score")
        components["contest_eligible"] = self.contest_eligible
        if not self.contest_eligible:
            components["official_contest_score_if_eligible"] = score.score
            components["noncontest_reason"] = self.noncontest_reason
            return {"official_contest_score": None, "components": components}
        return {"official_contest_score": score.score, "components": components}


def _first_float(data: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _float_or(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_bool(data: Dict[str, Any], *keys: str) -> Optional[bool]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "met", "pass", "passed"}:
                return True
            if lowered in {"false", "no", "failed", "fail"}:
                return False
    return None


def _routing_errors(metrics: Dict[str, Any]) -> Optional[int]:
    counts = metrics.get("routing_counts")
    if isinstance(counts, dict) and counts.get("routing_errors") is not None:
        try:
            return int(counts["routing_errors"])
        except Exception:
            return None
    evidence = metrics.get("validation_evidence")
    if isinstance(evidence, dict):
        routing = evidence.get("routing_counts")
        if isinstance(routing, dict) and routing.get("routing_errors") is not None:
            try:
                return int(routing["routing_errors"])
            except Exception:
                return None
    if metrics.get("routing_errors") is not None:
        try:
            return int(metrics["routing_errors"])
        except Exception:
            return None
    return None
