"""Evidence compression and planning helpers for guarded rescue synthesis."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from optimizer_pipeline.models import NodeStatus, to_jsonable


KNOWN_PUBLIC_PRIOR_PATH = Path(__file__).resolve().parents[3] / "configs" / "rescue_known_public_priors.json"


@dataclass
class RescueAttemptDiagnosis:
    attempt_index: int
    outcome_class: str
    lesson: str
    avoid_primitives: List[str] = field(default_factory=list)
    avoid_flows: List[str] = field(default_factory=list)
    prefer_parent_selector: str = "current_best"
    parent_node_id: Optional[str] = None
    child_node_id: Optional[str] = None
    delta_fmax_mhz: Optional[float] = None
    delta_vs_parent_mhz: Optional[float] = None
    delta_vs_root_mhz: Optional[float] = None
    runtime_seconds: Optional[float] = None
    validation_status: Optional[str] = None
    rejection_reason: Optional[str] = None
    updated_incumbent: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class RescueAttemptPlan:
    parent_selector: str
    objective: str
    prefer_primitives: List[str] = field(default_factory=list)
    avoid_primitives: List[str] = field(default_factory=list)
    prefer_flows: List[str] = field(default_factory=list)
    avoid_flows: List[str] = field(default_factory=list)
    max_recipe_steps: int = 6
    evidence_refs: List[Dict[str, Any]] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


class RescueKnownPublicPriorStore:
    """Load compact public-benchmark priors without using external history."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else KNOWN_PUBLIC_PRIOR_PATH
        self._cache: Optional[Dict[str, Any]] = None

    def prior_for(self, *, benchmark_name: Optional[str], history_scope: str) -> Dict[str, Any]:
        if str(history_scope or "in_run").strip().lower() != "known_public":
            return {}
        key = _normalize_benchmark_name(benchmark_name or "")
        if not key:
            return {}
        data = self._load()
        return dict(data.get(key) or {})

    def _load(self) -> Dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {}
            return self._cache
        try:
            data = json.loads(self.path.read_text())
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._cache = {_normalize_benchmark_name(key): value for key, value in data.items() if isinstance(value, dict)}
        return self._cache


class RescueEvidenceBuilder:
    """Build compact rescue evidence from the current run, not external traces."""

    def __init__(self, ctx: Any, *, prior_store: Optional[RescueKnownPublicPriorStore] = None) -> None:
        self.ctx = ctx
        self.prior_store = prior_store or RescueKnownPublicPriorStore()

    def build(
        self,
        *,
        attempts: Sequence[Dict[str, Any]],
        attempt_diagnoses: Sequence[Dict[str, Any]],
        selected_parent: Any = None,
        history_scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        cfg = getattr(self.ctx, "config", None)
        scope = str(history_scope or getattr(cfg, "rescue_history_scope", "in_run") or "in_run").strip().lower()
        benchmark = _benchmark_name(self.ctx)
        safe_parents = self.safe_parent_candidates(attempts=attempts)
        return {
            "schema_version": "rescue_evidence.v1",
            "benchmark_name": benchmark,
            "history_scope": scope,
            "current_best": self.node_summary(self._best_node()),
            "selected_parent": self.node_summary(selected_parent) if selected_parent is not None else {},
            "safe_parent_candidates": safe_parents,
            "attempt_diagnoses": list(attempt_diagnoses),
            "rescue_attempts": [_compact_attempt(attempt) for attempt in attempts],
            "primitive_effect_history": self.primitive_effect_history(attempts=attempts),
            "flow_effect_history": self.flow_effect_history(),
            "validation_risk_summary": self.validation_risk_summary(attempts=attempts),
            "known_public_prior": self.prior_store.prior_for(benchmark_name=benchmark, history_scope=scope),
            "recommended_information_gaps": self.recommended_information_gaps(attempts=attempts),
        }

    def safe_parent_candidates(self, *, attempts: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
        ordered: List[Tuple[str, str]] = []

        def add(node_id: Optional[str], reason: str) -> None:
            if not node_id or any(existing == str(node_id) for existing, _ in ordered):
                return
            node = self.ctx.nodes.get(str(node_id))
            if node is not None and safe_parent_node(node):
                ordered.append((str(node_id), reason))

        add("node_000_root", "root")
        add(getattr(self._best_node(), "node_id", None), "current_best")
        add(getattr(getattr(self.ctx, "current_node", None), "node_id", None), "current_node")
        for attempt in reversed(list(attempts)):
            if attempt.get("accepted_by_transition_executor"):
                add(attempt.get("child_node_id"), "last_accepted_rescue")
                break
        for attempt in reversed(list(attempts)):
            if attempt.get("updated_incumbent"):
                continue
            add(attempt.get("parent_node_id"), "parent_of_last_failed_or_low_gain")
            break
        return [{**self.node_summary(self.ctx.nodes[node_id]), "candidate_reason": reason} for node_id, reason in ordered]

    def primitive_effect_history(self, *, attempts: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        effects: Dict[str, Dict[str, Any]] = {}
        for attempt in attempts:
            primitives = _attempt_primitives(attempt)
            for primitive in primitives:
                rec = effects.setdefault(primitive, {"support": 0, "invalid": 0, "regressed": 0, "improved": 0, "recent_attempts": []})
                rec["support"] += 1
                delta = _safe_float(attempt.get("delta_fmax_mhz"), None)
                if str(attempt.get("validation_status") or "").upper() == "INVALID":
                    rec["invalid"] += 1
                elif delta is not None and delta < 0:
                    rec["regressed"] += 1
                elif delta is not None and delta > 0:
                    rec["improved"] += 1
                if len(rec["recent_attempts"]) < 5:
                    rec["recent_attempts"].append({
                        "attempt_index": attempt.get("attempt_index"),
                        "delta_fmax_mhz": delta,
                        "validation_status": attempt.get("validation_status"),
                    })
        return effects

    def flow_effect_history(self) -> Dict[str, Dict[str, Any]]:
        effects: Dict[str, Dict[str, Any]] = {}
        for record in list(getattr(self.ctx, "transition_records", []) or []):
            intent = getattr(record, "intent", None)
            outcome = getattr(record, "outcome", None)
            flow = str(getattr(intent, "optimizer_name", "") or "")
            if not flow:
                continue
            rec = effects.setdefault(flow, {"support": 0, "accepted": 0, "invalid": 0, "updated_incumbent": 0, "best_delta_fmax_mhz": None})
            rec["support"] += 1
            if bool(getattr(outcome, "accepted_by_transition_executor", False)):
                rec["accepted"] += 1
            if str(getattr(outcome, "validation_status", "") or "").upper() == "INVALID":
                rec["invalid"] += 1
            handling = getattr(record, "handling", None)
            if bool(getattr(handling, "updated_incumbent", False)):
                rec["updated_incumbent"] += 1
            delta = _record_delta(record, self.ctx.root_metrics)
            if delta is not None:
                rec["best_delta_fmax_mhz"] = _max_optional(rec.get("best_delta_fmax_mhz"), delta)
        return effects

    def validation_risk_summary(self, *, attempts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        invalid_attempts = [attempt for attempt in attempts if str(attempt.get("validation_status") or "").upper() == "INVALID"]
        invalid_nodes = [
            self.node_summary(node)
            for node in self.ctx.nodes.values()
            if str(getattr(node, "validation_status", "") or "").upper() == "INVALID"
        ][:8]
        return {
            "invalid_attempt_count": len(invalid_attempts),
            "invalid_node_count": len(invalid_nodes),
            "recent_invalid_attempts": [_compact_attempt(attempt) for attempt in invalid_attempts[-5:]],
            "invalid_nodes": invalid_nodes,
        }

    def recommended_information_gaps(self, *, attempts: Sequence[Dict[str, Any]]) -> List[str]:
        gaps: List[str] = []
        if not attempts:
            gaps.append("no_rescue_attempts_yet")
        if not any(_attempt_primitives(attempt) for attempt in attempts):
            gaps.append("no_recipe_primitive_history")
        if not any(getattr(record, "outcome", None) for record in getattr(self.ctx, "transition_records", []) or []):
            gaps.append("no_completed_transition_history")
        return gaps

    def node_summary(self, node: Any) -> Dict[str, Any]:
        if node is None:
            return {}
        metrics = dict(getattr(node, "metrics", {}) or {})
        fmax = _fmax_from_metrics(metrics)
        root_fmax = _fmax_from_metrics(getattr(self.ctx, "root_metrics", {}) or {})
        return {
            "node_id": getattr(node, "node_id", None),
            "parent_id": getattr(node, "parent_id", None),
            "depth": getattr(node, "depth", None),
            "flow_sequence": list(getattr(node, "flow_sequence", []) or []),
            "selected_flow": getattr(node, "selected_flow", None),
            "status": _status_value(getattr(node, "status", None)),
            "validation_status": getattr(node, "validation_status", None),
            "output_dcp": getattr(node, "output_dcp", None),
            "fmax_mhz": fmax,
            "delta_fmax_mhz": None if fmax is None or root_fmax is None else fmax - root_fmax,
        }

    def _best_node(self) -> Any:
        best_id = getattr(getattr(self.ctx, "state", None), "best_node_id", None)
        if best_id and best_id in self.ctx.nodes:
            return self.ctx.nodes[best_id]
        return getattr(self.ctx, "current_node", None)


class RescuePlanner:
    """Choose the next guarded rescue experiment intent from compact evidence."""

    def plan(
        self,
        *,
        evidence: Dict[str, Any],
        attempts: Sequence[Dict[str, Any]],
        attempt_diagnoses: Sequence[Dict[str, Any]],
        max_recipe_steps: int = 6,
    ) -> RescueAttemptPlan:
        diagnoses = list(attempt_diagnoses)
        known = evidence.get("known_public_prior") if isinstance(evidence.get("known_public_prior"), dict) else {}
        if not attempts:
            prefer_flows = [str(flow) for flow in known.get("fixed_recipe", [])] if isinstance(known.get("fixed_recipe"), list) else []
            return RescueAttemptPlan(
                parent_selector="current_best",
                objective="Try the most promising guarded rescue recipe from the current best valid parent.",
                prefer_flows=prefer_flows,
                prefer_primitives=_flow_primitives(prefer_flows),
                max_recipe_steps=max_recipe_steps,
                evidence_refs=[{"basis": "current_best"}, {"basis": "known_public_prior"}] if known else [{"basis": "current_best"}],
                rationale="First rescue attempt should use the incumbent best parent unless it is unsafe.",
            )

        last = diagnoses[-1] if diagnoses else diagnose_attempt(attempts[-1]).to_dict()
        outcome = str(last.get("outcome_class") or "")
        avoid_primitives = list(dict.fromkeys([str(x) for diag in diagnoses for x in diag.get("avoid_primitives", [])]))
        avoid_flows = list(dict.fromkeys([str(x) for diag in diagnoses for x in diag.get("avoid_flows", [])]))
        if outcome in {"invalid", "execution_failed"}:
            parent = "parent_of_last_failed_or_low_gain"
            objective = "Open a sibling branch from the failed attempt parent and avoid the invalid motif."
        elif outcome in {"regressed", "no_gain", "low_gain"}:
            parent = "parent_of_last_failed_or_low_gain"
            objective = "Switch parent or primitive family because the previous clean attempt did not produce useful Fmax gain."
        elif outcome == "improved":
            parent = "last_accepted_rescue" if last.get("updated_incumbent") else "current_best"
            objective = "Continue from the improved valid rescue output and compound the gain."
        else:
            parent = "current_best"
            objective = "Try a distinct guarded rescue recipe from the current best valid parent."
        return RescueAttemptPlan(
            parent_selector=parent,
            objective=objective,
            avoid_primitives=avoid_primitives,
            avoid_flows=avoid_flows,
            max_recipe_steps=max_recipe_steps,
            evidence_refs=[{"basis": "last_attempt", "attempt_index": attempts[-1].get("attempt_index")}],
            rationale=str(last.get("lesson") or objective),
        )


def diagnose_attempt(attempt: Dict[str, Any]) -> RescueAttemptDiagnosis:
    delta = _safe_float(attempt.get("meaningful_delta_vs_parent_mhz"), None)
    if delta is None:
        delta = _safe_float(attempt.get("delta_vs_parent_mhz"), _safe_float(attempt.get("delta_fmax_mhz"), None))
    validation = str(attempt.get("validation_status") or "")
    accepted = bool(attempt.get("accepted_by_transition_executor"))
    updated = bool(attempt.get("updated_incumbent"))
    primitives = _attempt_primitives(attempt)
    flows = _attempt_flows(attempt)
    manual_action = attempt.get("manual_action") if isinstance(attempt.get("manual_action"), dict) else {}
    manual_primitive = str(manual_action.get("primitive") or "")
    publish_eligible = bool(attempt.get("publish_eligible"))
    if accepted and not publish_eligible and manual_primitive.startswith("manual."):
        outcome = "structural_progress"
        lesson = "Manual scratch checkpoint advanced the implementation state; continue the episode without comparing its Fmax to routed checkpoints."
        prefer_parent = "last_accepted_rescue"
        avoid_primitives = []
        avoid_flows = []
    elif validation.upper() == "INVALID" or (attempt.get("rejection_reason") and not accepted):
        outcome = "invalid"
        lesson = "Attempt did not produce a publishable DCP; avoid repeating its invalid motif."
        prefer_parent = "parent_of_last_failed_or_low_gain"
        avoid_primitives = primitives
        avoid_flows = flows
    elif updated:
        outcome = "improved"
        lesson = "Attempt produced a valid published improvement; continue from the improved child."
        prefer_parent = "last_accepted_rescue"
        avoid_primitives = []
        avoid_flows = []
    elif delta is not None and delta < 0:
        outcome = "regressed"
        lesson = "Attempt was valid but regressed Fmax; switch recipe family or parent."
        prefer_parent = "parent_of_last_failed_or_low_gain"
        avoid_primitives = primitives
        avoid_flows = flows
    elif delta is not None and delta == 0:
        outcome = "no_gain"
        lesson = "Attempt was valid but made no Fmax progress; avoid repeating the same recipe."
        prefer_parent = "parent_of_last_failed_or_low_gain"
        avoid_primitives = primitives
        avoid_flows = flows
    elif delta is not None and delta > 0:
        outcome = "improved"
        lesson = "Attempt produced a valid improvement; consider continuing from the improved child."
        prefer_parent = "last_accepted_rescue"
        avoid_primitives = []
        avoid_flows = []
    else:
        outcome = "unknown"
        lesson = "Attempt outcome lacks enough timing evidence; gather more evidence or try a conservative distinct recipe."
        prefer_parent = "current_best"
        avoid_primitives = []
        avoid_flows = []
    return RescueAttemptDiagnosis(
        attempt_index=int(attempt.get("attempt_index") or 0),
        outcome_class=outcome,
        lesson=lesson,
        avoid_primitives=avoid_primitives,
        avoid_flows=avoid_flows,
        prefer_parent_selector=prefer_parent,
        parent_node_id=attempt.get("parent_node_id"),
        child_node_id=attempt.get("child_node_id"),
        delta_fmax_mhz=_safe_float(attempt.get("delta_fmax_mhz"), None),
        delta_vs_parent_mhz=_safe_float(attempt.get("delta_vs_parent_mhz"), None),
        delta_vs_root_mhz=_safe_float(attempt.get("delta_vs_root_mhz"), None),
        runtime_seconds=_safe_float(attempt.get("runtime_seconds"), None),
        validation_status=validation or None,
        rejection_reason=attempt.get("rejection_reason"),
        updated_incumbent=updated,
    )


def safe_parent_node(node: Any) -> bool:
    if node is None:
        return False
    if _status_value(getattr(node, "status", None)) != NodeStatus.COMPLETED.value:
        return False
    if str(getattr(node, "validation_status", "") or "").upper() == "INVALID":
        return False
    output_dcp = getattr(node, "output_dcp", None) or getattr(node, "input_dcp", None)
    return bool(output_dcp and Path(output_dcp).exists())


def _compact_attempt(attempt: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "attempt_index": attempt.get("attempt_index"),
        "parent_node_id": attempt.get("parent_node_id"),
        "child_node_id": attempt.get("child_node_id"),
        "status": attempt.get("status"),
        "validation_status": attempt.get("validation_status"),
        "updated_incumbent": attempt.get("updated_incumbent"),
        "publish_eligible": attempt.get("publish_eligible"),
        "accepted_by_transition_executor": attempt.get("accepted_by_transition_executor"),
        "delta_fmax_mhz": attempt.get("delta_fmax_mhz"),
        "runtime_seconds": attempt.get("runtime_seconds"),
        "primitives": _attempt_primitives(attempt),
        "flows": _attempt_flows(attempt),
        "rejection_reason": attempt.get("rejection_reason"),
    }


def _attempt_primitives(attempt: Dict[str, Any]) -> List[str]:
    manual_action = attempt.get("manual_action") if isinstance(attempt.get("manual_action"), dict) else {}
    if manual_action.get("primitive"):
        return [str(manual_action["primitive"])]
    recipe = attempt.get("recipe") if isinstance(attempt.get("recipe"), dict) else {}
    spec = recipe.get("recipe_spec") if isinstance(recipe.get("recipe_spec"), dict) else recipe
    steps = spec.get("steps") if isinstance(spec.get("steps"), list) else []
    return [str(step.get("primitive")) for step in steps if isinstance(step, dict) and step.get("primitive")]


def _attempt_flows(attempt: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    recipe = attempt.get("recipe") if isinstance(attempt.get("recipe"), dict) else {}
    if recipe.get("flow_name"):
        out.append(str(recipe["flow_name"]))
    for primitive in _attempt_primitives(attempt):
        if primitive.startswith("flow."):
            out.append(primitive.split(".", 1)[1])
    return list(dict.fromkeys(out))


def _flow_primitives(flows: Sequence[str]) -> List[str]:
    return [f"flow.{flow}" for flow in flows if flow]


def _record_delta(record: Any, root_metrics: Dict[str, Any]) -> Optional[float]:
    outcome = getattr(record, "outcome", None)
    metrics = dict(getattr(outcome, "metrics", {}) or {})
    fmax = _fmax_from_metrics(metrics)
    root = _fmax_from_metrics(root_metrics)
    return None if fmax is None or root is None else fmax - root


def _benchmark_name(ctx: Any) -> str:
    root = Path(str(getattr(ctx, "root_dcp", "") or ""))
    name = root.name
    for suffix in ("_2025.1.dcp", ".dcp"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return _normalize_benchmark_name(name)


def _normalize_benchmark_name(value: str) -> str:
    text = str(value or "").strip()
    if text.endswith("_2025.1"):
        text = text[:-7]
    text = text.replace("-", "_")
    if text == "amd_mini_isp":
        return "amd_mini_isp"
    return text


def _fmax_from_metrics(metrics: Dict[str, Any]) -> Optional[float]:
    for key in ("final_fmax_mhz", "initial_fmax_mhz", "fmax_mhz", "root_fmax_mhz"):
        parsed = _safe_float(metrics.get(key), None)
        if parsed is not None:
            return parsed
    timing = metrics.get("timing") if isinstance(metrics.get("timing"), dict) else {}
    for key in ("final_fmax_mhz", "initial_fmax_mhz", "fmax_mhz"):
        parsed = _safe_float(timing.get(key), None)
        if parsed is not None:
            return parsed
    return None


def _safe_float(value: Any, default: Optional[float]) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _max_optional(current: Any, value: Optional[float]) -> Optional[float]:
    current_f = _safe_float(current, None)
    if current_f is None:
        return value
    if value is None:
        return current_f
    return max(current_f, value)


def _status_value(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)
