"""Reliability statistics and risk scoring for FPGA flow-sequence search.

This module intentionally uses transparent count-based estimators rather than a
learned model.  It is the bridge between the current validation-gated search
system and future legality/QoR world models: every executed transition can be
converted into reliability priors, and planners can use those priors before a
candidate is sent to Vivado.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


RELIABILITY_STATUSES = {"CLEAN", "WARNING", "FALLBACK_USED", "INVALID", "REGRESSED"}
NON_CLEAN_STATUSES = {"WARNING", "FALLBACK_USED", "INVALID", "REGRESSED"}
DEFAULT_CLEAN_PRIOR = 0.70
DEFAULT_INVALID_PRIOR = 0.10
DEFAULT_FALLBACK_PRIOR = 0.10
DEFAULT_WARNING_PRIOR = 0.10
DEFAULT_REGRESSION_PRIOR = 0.10



@dataclass
class FlowReliabilityRecord:
    flow: str
    executed: int = 0
    clean: int = 0
    legal_clean: int = 0
    useful_clean: int = 0
    warning: int = 0
    fallback: int = 0
    invalid: int = 0
    regressed: int = 0
    runtime_seconds_total: float = 0.0
    delta_pure_qor_total: float = 0.0
    delta_fmax_total: float = 0.0
    delta_wns_total: float = 0.0

    def update(self, transition: Dict[str, Any]) -> None:
        self.executed += 1
        outcome = transition.get("outcome") if isinstance(transition.get("outcome"), dict) else transition
        status = str(outcome.get("validation_status") or "CLEAN")
        qos = str(outcome.get("qor_status") or "")
        invalid_output = status == "INVALID" or qos == "invalid" or bool(outcome.get("invalid"))
        regressed_output = status == "REGRESSED" or qos == "regressed" or bool(outcome.get("regressed"))
        if status == "INVALID":
            self.invalid += 1
        elif status == "FALLBACK_USED":
            self.fallback += 1
        elif status == "WARNING":
            self.warning += 1
        else:
            self.clean += 1
        if not invalid_output:
            self.legal_clean += 1
        if not invalid_output and not regressed_output:
            self.useful_clean += 1
        if regressed_output:
            self.regressed += 1
        self.runtime_seconds_total += _safe_float(outcome.get("runtime_seconds"), 0.0)
        self.delta_pure_qor_total += _safe_float(outcome.get("delta_pure_qor"), 0.0)
        self.delta_fmax_total += _safe_float(outcome.get("delta_fmax_mhz"), 0.0)
        self.delta_wns_total += _safe_float(outcome.get("delta_wns_ns"), 0.0)

    def to_dict(self) -> Dict[str, Any]:
        n = max(self.executed, 1)
        return {
            "flow": self.flow,
            "executed": self.executed,
            "clean": self.clean,
            "legal_clean": self.legal_clean,
            "useful_clean": self.useful_clean,
            "warning": self.warning,
            "fallback": self.fallback,
            "invalid": self.invalid,
            "regressed": self.regressed,
            "clean_rate": self.clean / n,
            "legal_clean_rate": self.legal_clean / n,
            "useful_clean_rate": self.useful_clean / n,
            "warning_rate": self.warning / n,
            "fallback_rate": self.fallback / n,
            "invalid_rate": self.invalid / n,
            "regressed_rate": self.regressed / n,
            "non_clean_rate": (self.warning + self.fallback + self.invalid + self.regressed) / n,
            "avg_runtime_seconds": self.runtime_seconds_total / n,
            "avg_delta_pure_qor": self.delta_pure_qor_total / n,
            "avg_delta_fmax_mhz": self.delta_fmax_total / n,
            "avg_delta_wns_ns": self.delta_wns_total / n,
        }


@dataclass
class ReliabilityTable:
    flow_stats: Dict[str, FlowReliabilityRecord] = field(default_factory=dict)
    sequence_pair_stats: Dict[str, FlowReliabilityRecord] = field(default_factory=dict)
    design_stats: Dict[str, FlowReliabilityRecord] = field(default_factory=dict)

    @classmethod
    def from_transitions(cls, transitions: Iterable[Dict[str, Any]]) -> "ReliabilityTable":
        table = cls()
        for transition in transitions:
            flow = str(transition.get("candidate_flow") or transition.get("flow") or "unknown")
            design = str(transition.get("design") or "unknown")
            before_seq = transition.get("flow_sequence_before") or []
            last_flow = before_seq[-1] if isinstance(before_seq, list) and before_seq else "ROOT"
            pair = f"{last_flow}->{flow}"
            table.flow_stats.setdefault(flow, FlowReliabilityRecord(flow)).update(transition)
            table.sequence_pair_stats.setdefault(pair, FlowReliabilityRecord(pair)).update(transition)
            table.design_stats.setdefault(design, FlowReliabilityRecord(design)).update(transition)
        return table

    @classmethod
    def from_jsonl(cls, path: Path) -> "ReliabilityTable":
        return cls.from_transitions(read_jsonl(path))

    @classmethod
    def from_summary_json(cls, path: Path) -> "ReliabilityTable":
        data = json.loads(path.read_text())
        table = cls()
        for item in data.get("flow_reliability", []):
            rec = FlowReliabilityRecord(str(item.get("flow") or item.get("name") or "unknown"))
            _load_record_counts(rec, item)
            table.flow_stats[rec.flow] = rec
        for item in data.get("sequence_reliability", []):
            rec = FlowReliabilityRecord(str(item.get("sequence_pair") or item.get("flow") or "unknown"))
            _load_record_counts(rec, item)
            table.sequence_pair_stats[rec.flow] = rec
        for item in data.get("design_reliability", []):
            rec = FlowReliabilityRecord(str(item.get("design") or item.get("flow") or "unknown"))
            _load_record_counts(rec, item)
            table.design_stats[rec.flow] = rec
        return table

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flow_reliability": [rec.to_dict() for rec in sorted(self.flow_stats.values(), key=lambda r: r.flow)],
            "sequence_reliability": [
                {"sequence_pair": key, **rec.to_dict()} for key, rec in sorted(self.sequence_pair_stats.items())
            ],
            "design_reliability": [
                {"design": key, **rec.to_dict()} for key, rec in sorted(self.design_stats.items())
            ],
        }

    def flow_record(self, flow: str) -> Optional[Dict[str, Any]]:
        rec = self.flow_stats.get(flow)
        return rec.to_dict() if rec else None

    def risk_for(
        self,
        *,
        flow: str,
        last_flow: Optional[str] = None,
        design: Optional[str] = None,
        local_observation: Optional[Dict[str, Any]] = None,
        prior_strength: float = 0.5,
    ) -> Dict[str, Any]:
        """Return a smoothed risk estimate for a candidate expansion.

        The estimate combines global flow priors, optional sequence-pair priors,
        optional design-level priors, and online observations from the current
        tree.  The planner uses this only for ordering; Vivado validation remains
        the source of truth.
        """
        sources: List[Tuple[str, Dict[str, Any], float]] = []
        flow_rec = self.flow_stats.get(flow)
        if flow_rec:
            sources.append(("global_flow", flow_rec.to_dict(), float(flow_rec.executed)))
        if last_flow:
            pair_rec = self.sequence_pair_stats.get(f"{last_flow}->{flow}")
            if pair_rec:
                sources.append(("sequence_pair", pair_rec.to_dict(), float(pair_rec.executed)))
        if design:
            design_rec = self.design_stats.get(design)
            if design_rec:
                sources.append(("design", design_rec.to_dict(), min(float(design_rec.executed), prior_strength)))
        if local_observation:
            local = _local_obs_to_record(flow, local_observation)
            # : an allowed but never-executed flow is unknown, not unsafe.
            # Do not let an all-zero local observation contribute clean_rate=0.0.
            if local.executed > 0:
                sources.append(("current_tree", local.to_dict(), float(local.executed)))

        prior_record = _default_prior_record(flow)
        # Beta-prior style smoothing keeps tiny samples from turning into hard bans.
        # With no evidence this is exactly the unseen-flow prior; with evidence it
        # becomes a weak regularizer whose weight is controlled by prior_strength.
        sources.insert(0, ("unseen_flow_prior", prior_record, max(0.0, float(prior_strength))))

        total_weight = sum(max(0.001, weight) for _name, _rec, weight in sources)
        def wavg(key: str, default: float = 0.0) -> float:
            return sum(max(0.001, weight) * _safe_float(rec.get(key), default) for _name, rec, weight in sources) / total_weight

        return {
            "flow": flow,
            "clean_prob": wavg("useful_clean_rate", DEFAULT_CLEAN_PRIOR),
            "legal_clean_prob": wavg("legal_clean_rate", DEFAULT_CLEAN_PRIOR),
            "strict_clean_prob": wavg("clean_rate", DEFAULT_CLEAN_PRIOR),
            "invalid_risk": wavg("invalid_rate", DEFAULT_INVALID_PRIOR),
            "fallback_risk": wavg("fallback_rate", DEFAULT_FALLBACK_PRIOR),
            "warning_risk": wavg("warning_rate", DEFAULT_WARNING_PRIOR),
            "regression_risk": wavg("regressed_rate", DEFAULT_REGRESSION_PRIOR),
            "expected_delta_pure_qor": wavg("avg_delta_pure_qor", 0.0),
            "expected_runtime_seconds": wavg("avg_runtime_seconds", 0.0),
            "sources": [{"name": name, "weight": weight, "stats": rec} for name, rec, weight in sources],
        }


def write_reliability_outputs(table: ReliabilityTable, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = table.to_dict()
    (out_dir / "reliability_summary.json").write_text(json.dumps(data, indent=2) + "\n")
    _write_csv(out_dir / "flow_reliability.csv", data["flow_reliability"])
    _write_csv(out_dir / "sequence_reliability.csv", data["sequence_reliability"])
    _write_csv(out_dir / "design_reliability.csv", data["design_reliability"])


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with open(path, "r") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    # Preserve first-row order, then append any keys found later.
    fieldnames: List[str] = list(rows[0].keys())
    for row in rows[1:]:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _load_record_counts(rec: FlowReliabilityRecord, item: Dict[str, Any]) -> None:
    rec.executed = _safe_int(item.get("executed"))
    rec.clean = _safe_int(item.get("clean"))
    rec.legal_clean = _safe_int(item.get("legal_clean"), rec.clean + _safe_int(item.get("warning")) + _safe_int(item.get("fallback")))
    rec.useful_clean = _safe_int(item.get("useful_clean"), max(0, rec.legal_clean - _safe_int(item.get("regressed"))))
    rec.warning = _safe_int(item.get("warning"))
    rec.fallback = _safe_int(item.get("fallback"))
    rec.invalid = _safe_int(item.get("invalid"))
    rec.regressed = _safe_int(item.get("regressed"))
    rec.runtime_seconds_total = _safe_float(item.get("avg_runtime_seconds")) * max(rec.executed, 1)
    rec.delta_pure_qor_total = _safe_float(item.get("avg_delta_pure_qor")) * max(rec.executed, 1)
    rec.delta_fmax_total = _safe_float(item.get("avg_delta_fmax_mhz")) * max(rec.executed, 1)
    rec.delta_wns_total = _safe_float(item.get("avg_delta_wns_ns")) * max(rec.executed, 1)


def _local_obs_to_record(flow: str, obs: Dict[str, Any]) -> FlowReliabilityRecord:
    rec = FlowReliabilityRecord(flow=flow)
    rec.clean = _safe_int(obs.get("clean"))
    rec.warning = _safe_int(obs.get("warning"))
    rec.fallback = _safe_int(obs.get("fallback"))
    rec.invalid = _safe_int(obs.get("invalid"))
    rec.regressed = _safe_int(obs.get("regressed"))
    rec.executed = rec.clean + rec.warning + rec.fallback + rec.invalid
    rec.legal_clean = _safe_int(obs.get("legal_clean"), rec.clean + rec.warning + rec.fallback)
    rec.useful_clean = _safe_int(obs.get("useful_clean"), max(0, rec.legal_clean - rec.regressed))
    # QoR/runtime online observations are not always present; leave at zero.
    return rec


def _default_prior_record(flow: str) -> Dict[str, Any]:
    return {
        "flow": flow,
        "executed": 0,
        "clean": 0,
        "legal_clean": 0,
        "useful_clean": 0,
        "warning": 0,
        "fallback": 0,
        "invalid": 0,
        "regressed": 0,
        "clean_rate": DEFAULT_CLEAN_PRIOR,
        "legal_clean_rate": DEFAULT_CLEAN_PRIOR,
        "useful_clean_rate": DEFAULT_CLEAN_PRIOR,
        "warning_rate": DEFAULT_WARNING_PRIOR,
        "fallback_rate": DEFAULT_FALLBACK_PRIOR,
        "invalid_rate": DEFAULT_INVALID_PRIOR,
        "regressed_rate": DEFAULT_REGRESSION_PRIOR,
        "non_clean_rate": 1.0 - DEFAULT_CLEAN_PRIOR,
        "avg_runtime_seconds": 0.0,
        "avg_delta_pure_qor": 0.0,
        "avg_delta_fmax_mhz": 0.0,
        "avg_delta_wns_ns": 0.0,
    }
