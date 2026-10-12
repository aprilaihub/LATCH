"""Composite QoR scoring for research frontier expansion.

The score is intentionally transparent rather than statistically learned.  It is
stored with a component breakdown so that each state node can be audited and the
weights can be tuned without changing the runner logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .analysis_schema import as_float, extract_metrics_from_context


@dataclass
class QoRWeights:
    fmax_improvement_ratio: float = 100.0
    wns_improvement_ns: float = 20.0
    tns_improvement_ratio: float = 5.0
    # Runtime is retained as an auditable component, but its default QoR
    # weight is zero.  Runtime belongs in efficiency/planner-priority reports,
    # not in the pure QoR leaderboard.
    runtime_minutes: float = 0.0
    invalid_output: float = -100.0
    severe_regression: float = -50.0

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "QoRWeights":
        if not isinstance(data, dict):
            return cls()
        kwargs: Dict[str, Any] = {}
        for field_name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            if field_name in data and data[field_name] is not None:
                kwargs[field_name] = float(data[field_name])
        return cls(**kwargs)

    def to_dict(self) -> Dict[str, float]:
        return {
            "fmax_improvement_ratio": self.fmax_improvement_ratio,
            "wns_improvement_ns": self.wns_improvement_ns,
            "tns_improvement_ratio": self.tns_improvement_ratio,
            "runtime_minutes": self.runtime_minutes,
            "invalid_output": self.invalid_output,
            "severe_regression": self.severe_regression,
        }


@dataclass
class QoRScore:
    total_score: float
    status: str
    components: Dict[str, Any] = field(default_factory=dict)
    weighted_components: Dict[str, float] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_score": self.total_score,
            "status": self.status,
            "components": self.components,
            "weighted_components": self.weighted_components,
            "weights": self.weights,
        }


def merge_run_and_analysis_metrics(run_metrics: Dict[str, Any], analysis_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return common metrics with run summary values taking precedence."""
    merged: Dict[str, Any] = {}
    if analysis_context:
        merged.update(extract_metrics_from_context(analysis_context))
    if run_metrics:
        merged.update(extract_metrics_from_context(run_metrics))
        # Keep raw keys too so downstream summaries do not lose detail.
        merged.update({k: v for k, v in run_metrics.items() if k not in merged or v is not None})
    return merged


def select_final_metrics(node_metrics: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Extract final state metrics from a node's run summary or analysis context."""
    metrics = extract_metrics_from_context(node_metrics)
    final_fmax = as_float(metrics.get("final_fmax_mhz"))
    final_wns = as_float(metrics.get("final_wns_ns"))
    final_tns = as_float(metrics.get("final_tns_ns"))

    # Root analysis contexts usually only have initial_* values; for the root,
    # treat these as the current/final state.
    if final_fmax is None:
        final_fmax = as_float(metrics.get("initial_fmax_mhz"))
    if final_wns is None:
        final_wns = as_float(metrics.get("initial_wns_ns"))
    if final_tns is None:
        final_tns = as_float(metrics.get("initial_tns_ns"))

    return {
        "fmax_mhz": final_fmax,
        "wns_ns": final_wns,
        "tns_ns": final_tns,
        "clock_period_ns": as_float(metrics.get("clock_period_ns")),
    }


def has_qor_baseline(metrics: Dict[str, Any]) -> bool:
    """Return whether metrics contain enough root timing data for QoR deltas."""
    if not isinstance(metrics, dict):
        return False
    selected = select_final_metrics(metrics)
    return any(selected.get(key) is not None for key in ("fmax_mhz", "wns_ns", "tns_ns"))


def infer_root_metrics_from_node_metrics(node_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Infer original/root timing metrics from a run summary's initial values."""
    if not isinstance(node_metrics, dict):
        return {}
    extracted = extract_metrics_from_context(node_metrics)
    inferred: Dict[str, Any] = {}
    for key in (
        "benchmark_name",
        "target_clock",
        "clock_period_ns",
        "initial_fmax_mhz",
        "initial_wns_ns",
        "initial_tns_ns",
        "failing_endpoints",
    ):
        value = extracted.get(key)
        if value is None and key in node_metrics:
            value = node_metrics.get(key)
        if value is not None:
            inferred[key] = value
    return inferred


def fill_missing_qor_baseline(root_metrics: Dict[str, Any], node_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Return root metrics with missing baseline fields filled from node initial metrics.

    Existing root values win.  This is intentionally a repair step for audit and
    scoring inputs; it does not mutate the caller's dictionaries.
    """
    filled: Dict[str, Any] = dict(root_metrics or {})

    extracted_root = extract_metrics_from_context(root_metrics or {})
    for key in ("target_clock", "clock_period_ns", "initial_fmax_mhz", "initial_wns_ns", "initial_tns_ns", "failing_endpoints"):
        if filled.get(key) is None and extracted_root.get(key) is not None:
            filled[key] = extracted_root[key]

    inferred = infer_root_metrics_from_node_metrics(node_metrics or {})
    for key, value in inferred.items():
        if value is not None and filled.get(key) is None:
            filled[key] = value
    return filled


def compute_qor_score(
    root_metrics: Dict[str, Any],
    node_metrics: Dict[str, Any],
    *,
    weights: Optional[QoRWeights] = None,
    valid_output: bool = True,
    path_runtime_seconds: Optional[float] = None,
    severe_regression_threshold_ratio: float = -0.005,
    severe_regression_wns_ns: float = -0.10,
    include_runtime_penalty: bool = False,
) -> QoRScore:
    """Compute pure QoR score relative to the root state.

    Runtime stays in the score breakdown for auditability, but the default
    runtime weight is zero. Runtime and online-analysis costs are reported via
    efficiency metrics and planner-priority components; they must not silently
    change the QoR leaderboard.

    Args:
        root_metrics: Metrics from the original/root DCP analysis.
        node_metrics: Metrics for the current node after execution/analysis.
        weights: Configurable component weights.  Use runtime_minutes=0 for
            pure QoR.
        valid_output: Whether output/execution validation passed.
        path_runtime_seconds: Sum of incremental runtime along the branch. It is
            recorded in components for efficiency reporting only. Runtime stays
            out of QoR unless include_runtime_penalty=True is explicitly
            requested for legacy auditing.
        severe_regression_threshold_ratio: Fmax ratio drop marking severe regression.
        severe_regression_wns_ns: WNS drop marking severe regression.
    """
    w = weights or QoRWeights()
    root_final = select_final_metrics(root_metrics)
    node_final = select_final_metrics(node_metrics)

    root_fmax = root_final["fmax_mhz"]
    node_fmax = node_final["fmax_mhz"]
    root_wns = root_final["wns_ns"]
    node_wns = node_final["wns_ns"]
    root_tns = root_final["tns_ns"]
    node_tns = node_final["tns_ns"]

    fmax_delta = None
    fmax_ratio = 0.0
    if root_fmax is not None and node_fmax is not None:
        fmax_delta = node_fmax - root_fmax
        fmax_ratio = fmax_delta / max(abs(root_fmax), 1.0)

    wns_delta = 0.0
    if root_wns is not None and node_wns is not None:
        wns_delta = node_wns - root_wns

    tns_delta = 0.0
    tns_ratio = 0.0
    if root_tns is not None and node_tns is not None:
        tns_delta = node_tns - root_tns
        tns_ratio = tns_delta / max(abs(root_tns), 1.0)

    runtime_seconds = as_float(path_runtime_seconds)
    if runtime_seconds is None:
        runtime_seconds = as_float(node_metrics.get("total_runtime_seconds"))
    runtime_minutes = 0.0 if runtime_seconds is None else max(0.0, runtime_seconds / 60.0)

    severe_regression = False
    if fmax_delta is not None and fmax_ratio < severe_regression_threshold_ratio:
        severe_regression = True
    if root_wns is not None and node_wns is not None and (node_wns - root_wns) < severe_regression_wns_ns:
        severe_regression = True

    invalid = not bool(valid_output)

    components: Dict[str, Any] = {
        "root_fmax_mhz": root_fmax,
        "node_fmax_mhz": node_fmax,
        "fmax_delta_mhz": fmax_delta,
        "fmax_improvement_ratio": fmax_ratio,
        "root_wns_ns": root_wns,
        "node_wns_ns": node_wns,
        "wns_improvement_ns": wns_delta,
        "root_tns_ns": root_tns,
        "node_tns_ns": node_tns,
        "tns_improvement_ns": tns_delta,
        "tns_improvement_ratio": tns_ratio,
        "runtime_seconds": runtime_seconds,
        "runtime_minutes": runtime_minutes,
        "invalid_output": invalid,
        "severe_regression": severe_regression,
    }

    effective_runtime_weight = w.runtime_minutes if include_runtime_penalty else 0.0
    weighted = {
        "fmax_improvement_ratio": w.fmax_improvement_ratio * fmax_ratio,
        "wns_improvement_ns": w.wns_improvement_ns * wns_delta,
        "tns_improvement_ratio": w.tns_improvement_ratio * tns_ratio,
        "runtime_minutes": effective_runtime_weight * runtime_minutes,
        "invalid_output": w.invalid_output if invalid else 0.0,
        "severe_regression": w.severe_regression if severe_regression else 0.0,
    }
    total = sum(weighted.values())

    if invalid:
        status = "invalid"
    elif severe_regression:
        status = "regressed"
    elif total > 0:
        status = "improved"
    else:
        status = "neutral"

    return QoRScore(
        total_score=round(total, 6),
        status=status,
        components=components,
        weighted_components={k: round(v, 6) for k, v in weighted.items()},
        weights={**w.to_dict(), "runtime_minutes": effective_runtime_weight, "runtime_minutes_configured": w.runtime_minutes},
    )
