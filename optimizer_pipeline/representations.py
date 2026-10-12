"""Representation builders for validated FPGA implementation trajectories.

Turns trace-lake records into reusable state/action/outcome vectors.
The representation is deliberately lightweight and transparent: it uses stable
numeric features, categorical one-hot features, flow-history counts, validation
labels, and trajectory n-grams.  This makes the data useful before a large
world-model dataset exists and keeps retrieval/risk priors auditable.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .reliability import read_jsonl, write_jsonl

DEFAULT_NGRAM_MAX = 3

BASE_NUMERIC_FEATURES = [
    "depth",
    "parent_pure_qor_score",
    "parent_fmax_mhz",
    "parent_wns_ns",
    "parent_tns_ns",
    "parent_node_runtime_seconds",
    "parent_path_runtime_seconds",
    "flow_prefix_len",
    "candidate_priority",
    "route_delay_ratio",
    "avg_logic_levels",
    "max_logic_levels",
    "num_paths_analyzed",
    "hardblock_on_path",
    "congestion_report_available",
    "congestion_has_overflow",
    "congestion_max_overflow",
    "congestion_hotspot_count",
    "num_high_fanout_nets",
    "max_fanout",
    "util_luts",
    "util_ffs",
    "util_dsps",
    "util_brams",
]

BASE_CATEGORICAL_FEATURES = [
    "design",
    "candidate_flow",
    "last_flow",
    "parent_validation_status",
    "dominant_path_type",
]

LABEL_COLUMNS = [
    "label_strict_clean",
    "label_legal_clean",
    "label_useful_clean",
    "label_invalid",
    "label_fallback",
    "label_warning",
    "label_regressed",
    "delta_pure_qor",
    "delta_fmax_mhz",
    "delta_wns_ns",
    "runtime_seconds",
]

IDENTITY_COLUMNS = [
    "sample_id",
    "design",
    "run_id",
    "parent_node_id",
    "child_node_id",
    "candidate_flow",
    "flow_sequence_before",
    "flow_sequence_after",
]


def build_representations(
    transitions_path: Path,
    out_dir: Path,
    *,
    candidates_path: Optional[Path] = None,
    ngram_max: int = DEFAULT_NGRAM_MAX,
) -> Dict[str, Any]:
    """Build state, transition and trajectory representations from trace lake.

    Args:
        transitions_path: trace_lake/transitions.jsonl.
        out_dir: directory receiving representation artifacts.
        candidates_path: optional trace_lake/candidates.jsonl for proposal stats.
        ngram_max: maximum flow n-gram length for trajectory motifs.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    transitions = [row for row in read_jsonl(transitions_path) if isinstance(row, dict)]
    candidates = [row for row in read_jsonl(candidates_path)] if candidates_path and candidates_path.exists() else []

    flow_vocab = sorted(_flow_vocab(transitions, candidates))
    categorical_values = _categorical_values(transitions, flow_vocab)
    feature_schema = _feature_schema(flow_vocab, categorical_values, ngram_max=ngram_max)

    state_rows = _state_rows(transitions, feature_schema)
    transition_rows = [_transition_vector_row(row, feature_schema) for row in transitions]
    trajectory_rows = _trajectory_rows(transitions, ngram_max=ngram_max)
    motif_library = _motif_library(transitions, ngram_max=ngram_max)

    _write_csv(out_dir / "state_vectors.csv", state_rows)
    _write_csv(out_dir / "transition_vectors.csv", transition_rows)
    write_jsonl(out_dir / "trajectory_vectors.jsonl", trajectory_rows)
    (out_dir / "motif_library.json").write_text(json.dumps(motif_library, indent=2, sort_keys=True) + "\n")
    (out_dir / "feature_schema.json").write_text(json.dumps(feature_schema, indent=2, sort_keys=True) + "\n")

    manifest = {
        "schema_version": "representation_v1",
        "transitions_path": str(transitions_path),
        "candidates_path": str(candidates_path) if candidates_path else None,
        "num_transitions": len(transitions),
        "num_candidates": len(candidates),
        "num_state_rows": len(state_rows),
        "num_trajectory_rows": len(trajectory_rows),
        "num_motifs": len(motif_library.get("motifs", [])),
        "outputs": {
            "feature_schema": str(out_dir / "feature_schema.json"),
            "state_vectors": str(out_dir / "state_vectors.csv"),
            "transition_vectors": str(out_dir / "transition_vectors.csv"),
            "trajectory_vectors": str(out_dir / "trajectory_vectors.jsonl"),
            "motif_library": str(out_dir / "motif_library.json"),
        },
    }
    (out_dir / "representation_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load_feature_schema(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def transition_feature_dict(row: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, float]:
    """Return numeric/one-hot feature dict aligned with ``feature_schema``."""
    base = _base_features(row)
    features: Dict[str, float] = {}
    for name in schema.get("numeric_features", []):
        features[f"x__num__{name}"] = _float(base.get(name), 0.0)
    for name, values in (schema.get("categorical_values") or {}).items():
        value = str(base.get(name) or "UNKNOWN")
        for option in values:
            features[f"x__cat__{name}={option}"] = 1.0 if value == option else 0.0
    seq = _seq(row.get("flow_sequence_before"))
    counts = Counter(seq)
    for flow in schema.get("flow_vocab", []):
        features[f"x__flow_count__{flow}"] = float(counts.get(flow, 0))
        features[f"x__contains__{flow}"] = 1.0 if flow in counts else 0.0
    for motif in schema.get("motif_vocab", []):
        features[f"x__motif_before__{motif}"] = 1.0 if motif in set(_ngrams(seq, max_n=len(motif.split("->")))) else 0.0
    return features


def candidate_query_row(
    *,
    node: Dict[str, Any],
    analysis_context: Optional[Dict[str, Any]],
    candidate_flow: str,
) -> Dict[str, Any]:
    """Create a transition-like query row for runtime retrieval scoring."""
    analysis_context = analysis_context if isinstance(analysis_context, dict) else {}
    diag = node.get("key_diagnostics") if isinstance(node.get("key_diagnostics"), dict) else {}
    # Prefer compact diagnostics from the frontier node; fall back to analysis context.
    timing = _first_dict(diag.get("timing"), analysis_context.get("timing"))
    critical = _first_dict(diag.get("critical_path_features"), analysis_context.get("critical_path_features"))
    congestion = _first_dict(diag.get("congestion"), analysis_context.get("congestion"))
    fanout = _first_dict(diag.get("fanout"), analysis_context.get("fanout"))
    util = analysis_context.get("utilization_summary") if isinstance(analysis_context.get("utilization_summary"), dict) else {}
    seq = _seq(node.get("flow_sequence"))
    state_before = {
        "depth": node.get("depth"),
        "pure_qor_score": node.get("pure_qor_score") or node.get("trajectory_objective_score"),
        "fmax_mhz": node.get("final_fmax_mhz") or timing.get("final_fmax_mhz") or timing.get("initial_fmax_mhz"),
        "wns_ns": node.get("final_wns_ns") or timing.get("final_wns_ns") or timing.get("initial_wns_ns"),
        "tns_ns": node.get("final_tns_ns") or timing.get("final_tns_ns") or timing.get("initial_tns_ns"),
        "validation_status": node.get("validation_status"),
        "node_runtime_seconds": node.get("node_runtime_seconds"),
        "path_runtime_seconds": node.get("path_runtime_seconds"),
        "failing_endpoints": timing.get("failing_endpoints"),
        "route_delay_ratio": critical.get("route_delay_ratio"),
        "avg_logic_levels": critical.get("avg_logic_levels"),
        "max_logic_levels": critical.get("max_logic_levels"),
        "num_paths_analyzed": critical.get("num_paths_analyzed"),
        "dominant_path_type": critical.get("dominant_path_type"),
        "hardblock_on_path": critical.get("hardblock_on_path"),
        "congestion_report_available": congestion.get("report_available"),
        "congestion_has_overflow": congestion.get("has_overflow"),
        "congestion_max_overflow": congestion.get("max_overflow"),
        "congestion_hotspot_count": congestion.get("hotspot_count"),
        "num_high_fanout_nets": fanout.get("num_high_fanout_nets"),
        "max_fanout": fanout.get("max_fanout"),
        "util_luts": util.get("luts"),
        "util_ffs": util.get("ffs"),
        "util_dsps": util.get("dsps"),
        "util_brams": util.get("brams"),
    }
    return {
        "sample_id": f"query/{node.get('node_id')}/{candidate_flow}",
        "design": _design_from_context(node, analysis_context),
        "run_id": "query",
        "parent_node_id": node.get("node_id"),
        "child_node_id": None,
        "candidate_flow": candidate_flow,
        "flow_sequence_before": seq,
        "flow_sequence_after": seq + [candidate_flow],
        "state_before": state_before,
        "outcome": {},
        "policy_context": {"planner_priority": None},
        "logs": {},
    }


def row_labels(row: Dict[str, Any]) -> Dict[str, Any]:
    outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
    status = str(outcome.get("validation_status") or "UNKNOWN")
    qos_status = str(outcome.get("qor_status") or "")
    invalid = bool(outcome.get("invalid")) or status == "INVALID" or qos_status == "invalid"
    fallback = bool(outcome.get("fallback_used")) or status == "FALLBACK_USED"
    warning = bool(outcome.get("warning")) or status == "WARNING"
    regressed = bool(outcome.get("regressed")) or status == "REGRESSED" or qos_status == "regressed"
    strict_clean = status == "CLEAN" and not invalid
    legal_clean = not invalid
    useful_clean = legal_clean and not regressed
    return {
        "label_strict_clean": int(strict_clean),
        "label_legal_clean": int(legal_clean),
        "label_useful_clean": int(useful_clean),
        "label_invalid": int(invalid),
        "label_fallback": int(fallback),
        "label_warning": int(warning),
        "label_regressed": int(regressed),
        "delta_pure_qor": _float(outcome.get("delta_pure_qor"), 0.0),
        "delta_fmax_mhz": _float(outcome.get("delta_fmax_mhz"), 0.0),
        "delta_wns_ns": _float(outcome.get("delta_wns_ns"), 0.0),
        "runtime_seconds": _float(outcome.get("runtime_seconds"), 0.0),
    }


def _transition_vector_row(row: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "sample_id": row.get("sample_id"),
        "design": row.get("design"),
        "run_id": row.get("run_id"),
        "parent_node_id": row.get("parent_node_id"),
        "child_node_id": row.get("child_node_id"),
        "candidate_flow": row.get("candidate_flow"),
        "flow_sequence_before": " -> ".join(_seq(row.get("flow_sequence_before"))),
        "flow_sequence_after": " -> ".join(_seq(row.get("flow_sequence_after"))),
    }
    out.update(transition_feature_dict(row, schema))
    out.update(row_labels(row))
    return out


def _state_rows(transitions: Sequence[Dict[str, Any]], schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    seen: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in transitions:
        key = (str(row.get("run_id")), str(row.get("parent_node_id")), "before")
        if key in seen:
            continue
        pseudo = dict(row)
        pseudo["candidate_flow"] = "__STATE__"
        features = transition_feature_dict(pseudo, schema)
        state = row.get("state_before") if isinstance(row.get("state_before"), dict) else {}
        state_row = {
            "state_id": f"{row.get('run_id')}/{row.get('parent_node_id')}",
            "design": row.get("design"),
            "run_id": row.get("run_id"),
            "node_id": row.get("parent_node_id"),
            "flow_sequence_before": " -> ".join(_seq(row.get("flow_sequence_before"))),
            "depth": state.get("depth"),
            "parent_pure_qor_score": state.get("pure_qor_score", state.get("qor_score")),
            "parent_validation_status": state.get("validation_status"),
        }
        state_row.update(features)
        seen[key] = state_row
    return list(seen.values())


def _trajectory_rows(transitions: Sequence[Dict[str, Any]], *, ngram_max: int) -> List[Dict[str, Any]]:
    rows = []
    for row in transitions:
        seq = _seq(row.get("flow_sequence_after"))
        labels = row_labels(row)
        rows.append({
            "trajectory_id": f"{row.get('run_id')}/{row.get('child_node_id')}",
            "sample_id": row.get("sample_id"),
            "design": row.get("design"),
            "run_id": row.get("run_id"),
            "child_node_id": row.get("child_node_id"),
            "depth": len(seq),
            "flow_sequence": seq,
            "flow_ngrams": _ngrams(seq, max_n=ngram_max),
            "label_useful_clean": labels["label_useful_clean"],
            "label_invalid": labels["label_invalid"],
            "label_fallback": labels["label_fallback"],
            "label_warning": labels["label_warning"],
            "label_regressed": labels["label_regressed"],
            "delta_pure_qor": labels["delta_pure_qor"],
            "delta_fmax_mhz": labels["delta_fmax_mhz"],
            "delta_wns_ns": labels["delta_wns_ns"],
        })
    return rows


def _motif_library(transitions: Sequence[Dict[str, Any]], *, ngram_max: int) -> Dict[str, Any]:
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "support": 0,
        "useful_clean": 0,
        "invalid": 0,
        "fallback": 0,
        "warning": 0,
        "regressed": 0,
        "delta_pure_qor_total": 0.0,
        "examples": [],
    })
    for row in transitions:
        seq = _seq(row.get("flow_sequence_after"))
        labels = row_labels(row)
        for motif in _ngrams(seq, max_n=ngram_max):
            rec = stats[motif]
            rec["support"] += 1
            rec["useful_clean"] += labels["label_useful_clean"]
            rec["invalid"] += labels["label_invalid"]
            rec["fallback"] += labels["label_fallback"]
            rec["warning"] += labels["label_warning"]
            rec["regressed"] += labels["label_regressed"]
            rec["delta_pure_qor_total"] += labels["delta_pure_qor"]
            if len(rec["examples"]) < 5:
                rec["examples"].append({
                    "sample_id": row.get("sample_id"),
                    "design": row.get("design"),
                    "sequence": seq,
                    "delta_pure_qor": labels["delta_pure_qor"],
                })
    motifs = []
    for motif, rec in stats.items():
        support = max(1, int(rec["support"]))
        motifs.append({
            "motif": motif,
            "flows": motif.split("->"),
            "support": rec["support"],
            "useful_clean_rate": rec["useful_clean"] / support,
            "invalid_rate": rec["invalid"] / support,
            "fallback_rate": rec["fallback"] / support,
            "warning_rate": rec["warning"] / support,
            "regressed_rate": rec["regressed"] / support,
            "avg_delta_pure_qor": rec["delta_pure_qor_total"] / support,
            "examples": rec["examples"],
        })
    motifs.sort(key=lambda item: (item["support"], item["avg_delta_pure_qor"], item["useful_clean_rate"]), reverse=True)
    return {"schema_version": "motif_library_v1", "motifs": motifs}


def _feature_schema(flow_vocab: Sequence[str], categorical_values: Dict[str, List[str]], *, ngram_max: int) -> Dict[str, Any]:
    motif_vocab = sorted({ng for seq in _all_sequences_for_schema(flow_vocab, ngram_max) for ng in _ngrams(seq, max_n=ngram_max)})
    return {
        "schema_version": "feature_schema_v1",
        "numeric_features": list(BASE_NUMERIC_FEATURES),
        "categorical_features": list(BASE_CATEGORICAL_FEATURES),
        "categorical_values": categorical_values,
        "flow_vocab": list(flow_vocab),
        "motif_vocab": motif_vocab,
        "label_columns": list(LABEL_COLUMNS),
        "identity_columns": list(IDENTITY_COLUMNS),
        "feature_prefixes": ["x__num__", "x__cat__", "x__flow_count__", "x__contains__", "x__motif_before__"],
        "notes": " transparent tabular encoding for state/action/outcome retrieval and transfer.",
    }


def _categorical_values(transitions: Sequence[Dict[str, Any]], flow_vocab: Sequence[str]) -> Dict[str, List[str]]:
    values: Dict[str, set[str]] = {name: set() for name in BASE_CATEGORICAL_FEATURES}
    for row in transitions:
        base = _base_features(row)
        for name in BASE_CATEGORICAL_FEATURES:
            values[name].add(str(base.get(name) or "UNKNOWN"))
    values["candidate_flow"].update(str(flow) for flow in flow_vocab)
    values["last_flow"].add("ROOT")
    values["parent_validation_status"].update(["CLEAN", "WARNING", "FALLBACK_USED", "INVALID", "REGRESSED", "UNKNOWN"])
    values["dominant_path_type"].update(["routing_dominated", "logic_dominated", "mixed", "unknown", "UNKNOWN"])
    return {key: sorted(v) for key, v in values.items()}


def _base_features(row: Dict[str, Any]) -> Dict[str, Any]:
    state = row.get("state_before") if isinstance(row.get("state_before"), dict) else {}
    policy = row.get("policy_context") if isinstance(row.get("policy_context"), dict) else {}
    seq = _seq(row.get("flow_sequence_before"))
    last_flow = seq[-1] if seq else "ROOT"
    return {
        "design": row.get("design") or "UNKNOWN",
        "candidate_flow": row.get("candidate_flow") or "UNKNOWN",
        "last_flow": last_flow,
        "parent_validation_status": state.get("validation_status") or "UNKNOWN",
        "dominant_path_type": state.get("dominant_path_type") or "UNKNOWN",
        "depth": state.get("depth"),
        "parent_pure_qor_score": state.get("pure_qor_score", state.get("qor_score")),
        "parent_fmax_mhz": state.get("fmax_mhz"),
        "parent_wns_ns": state.get("wns_ns"),
        "parent_tns_ns": state.get("tns_ns"),
        "parent_node_runtime_seconds": state.get("node_runtime_seconds"),
        "parent_path_runtime_seconds": state.get("path_runtime_seconds"),
        "flow_prefix_len": len(seq),
        "candidate_priority": policy.get("planner_priority"),
        "route_delay_ratio": state.get("route_delay_ratio"),
        "avg_logic_levels": state.get("avg_logic_levels"),
        "max_logic_levels": state.get("max_logic_levels"),
        "num_paths_analyzed": state.get("num_paths_analyzed"),
        "hardblock_on_path": _bool_float(state.get("hardblock_on_path")),
        "congestion_report_available": _bool_float(state.get("congestion_report_available")),
        "congestion_has_overflow": _bool_float(state.get("congestion_has_overflow")),
        "congestion_max_overflow": state.get("congestion_max_overflow"),
        "congestion_hotspot_count": state.get("congestion_hotspot_count"),
        "num_high_fanout_nets": state.get("num_high_fanout_nets"),
        "max_fanout": state.get("max_fanout"),
        "util_luts": state.get("util_luts"),
        "util_ffs": state.get("util_ffs"),
        "util_dsps": state.get("util_dsps"),
        "util_brams": state.get("util_brams"),
    }


def _flow_vocab(transitions: Sequence[Dict[str, Any]], candidates: Sequence[Dict[str, Any]]) -> set[str]:
    flows: set[str] = set()
    for row in list(transitions) + list(candidates):
        flow = row.get("candidate_flow") or row.get("flow")
        if flow:
            flows.add(str(flow))
        for seq_key in ("flow_sequence_before", "flow_sequence_after", "flow_sequence"):
            for item in _seq(row.get(seq_key)):
                flows.add(str(item))
    return {flow for flow in flows if flow and flow != "root"}


def _all_sequences_for_schema(flow_vocab: Sequence[str], ngram_max: int) -> Iterable[List[str]]:
    # Keep schema bounded: include single flow tokens only.  Observed trajectory
    # n-grams are represented in trajectory_vectors/motif_library; transition
    # feature columns use observed motifs generated later by the index if needed.
    for flow in flow_vocab:
        yield [flow]


def _seq(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        if "->" in value:
            return [part.strip() for part in value.split("->") if part.strip()]
        if value.strip():
            return [value.strip()]
    return []


def _ngrams(seq: Sequence[str], *, max_n: int = DEFAULT_NGRAM_MAX) -> List[str]:
    out: List[str] = []
    nmax = max(1, int(max_n))
    for n in range(1, min(nmax, len(seq)) + 1):
        for i in range(0, len(seq) - n + 1):
            out.append("->".join(seq[i : i + n]))
    return out


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_float(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return 1.0 if float(value) != 0.0 else 0.0
    if isinstance(value, str):
        return 1.0 if value.strip().lower() in {"1", "true", "yes", "y"} else 0.0
    return 0.0


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fieldnames: List[str] = list(rows[0].keys())
    for row in rows[1:]:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _first_dict(*values: Any) -> Dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _design_from_context(node: Dict[str, Any], analysis_context: Dict[str, Any]) -> str:
    design = analysis_context.get("benchmark_name")
    if design:
        return str(design)
    diag = node.get("key_diagnostics") if isinstance(node.get("key_diagnostics"), dict) else {}
    timing = diag.get("timing") if isinstance(diag.get("timing"), dict) else {}
    if timing.get("benchmark_name"):
        return str(timing.get("benchmark_name"))
    return "UNKNOWN"
