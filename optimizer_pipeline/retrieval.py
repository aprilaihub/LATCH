"""Trajectory-memory retrieval for FPGA implementation-flow search.

The retrieval index is intentionally lightweight: it stores transparent tabular
transition vectors and performs nearest-neighbour search with z-normalised
Euclidean distance.  It is not a document RAG layer; neighbours are validated
state/action/outcome cases from previous Vivado/RapidWright runs.
"""

from __future__ import annotations

import csv
import json
import math
import pickle
import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .representations import (
    build_representations,
    candidate_query_row,
    load_feature_schema,
    row_labels,
    transition_feature_dict,
)
from .reliability import read_jsonl


class TrajectoryRetrievalIndex:
    """In-memory nearest-neighbour index over transition vectors."""

    def __init__(
        self,
        *,
        feature_schema: Dict[str, Any],
        feature_columns: Sequence[str],
        means: Dict[str, float],
        stds: Dict[str, float],
        vectors: Sequence[Sequence[float]],
        records: Sequence[Dict[str, Any]],
        motif_library: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.feature_schema = feature_schema
        self.feature_columns = list(feature_columns)
        self.means = dict(means)
        self.stds = dict(stds)
        self.vectors = [list(vec) for vec in vectors]
        self.records = [dict(rec) for rec in records]
        self.motif_library = motif_library or {"motifs": []}
        self.metadata = metadata or {}
        self._motif_stats = {str(item.get("motif")): item for item in self.motif_library.get("motifs", []) if isinstance(item, dict)}

    @classmethod
    def from_representations(cls, representations_dir: Path) -> "TrajectoryRetrievalIndex":
        schema = load_feature_schema(representations_dir / "feature_schema.json")
        rows = _read_csv(representations_dir / "transition_vectors.csv")
        motif_path = representations_dir / "motif_library.json"
        motif_library = json.loads(motif_path.read_text()) if motif_path.exists() else {"motifs": []}
        return cls.build_from_rows(rows, schema, motif_library=motif_library)

    @classmethod
    def build_from_trace_lake(cls, trace_lake_dir: Path, representations_dir: Optional[Path] = None) -> "TrajectoryRetrievalIndex":
        representations_dir = representations_dir or (trace_lake_dir / "representations")
        transitions = trace_lake_dir / "transitions.jsonl"
        candidates = trace_lake_dir / "candidates.jsonl"
        build_representations(transitions, representations_dir, candidates_path=candidates if candidates.exists() else None)
        return cls.from_representations(representations_dir)

    @classmethod
    def build_from_rows(
        cls,
        rows: Sequence[Dict[str, Any]],
        feature_schema: Dict[str, Any],
        *,
        motif_library: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "TrajectoryRetrievalIndex":
        feature_columns = sorted({key for row in rows for key in row if key.startswith("x__")})
        means: Dict[str, float] = {}
        stds: Dict[str, float] = {}
        for col in feature_columns:
            vals = [_float(row.get(col), 0.0) for row in rows]
            if vals:
                mean = sum(vals) / len(vals)
                var = sum((v - mean) ** 2 for v in vals) / max(1, len(vals))
                std = math.sqrt(var) if var > 1e-12 else 1.0
            else:
                mean, std = 0.0, 1.0
            means[col] = mean
            stds[col] = std
        vectors = [_normalised_vector(row, feature_columns, means, stds) for row in rows]
        records = [_record_from_vector_row(row) for row in rows]
        return cls(
            feature_schema=feature_schema,
            feature_columns=feature_columns,
            means=means,
            stds=stds,
            vectors=vectors,
            records=records,
            motif_library=motif_library,
            metadata=metadata or {"num_records": len(rows)},
        )

    @classmethod
    def load(cls, path: Path) -> "TrajectoryRetrievalIndex":
        with open(path, "rb") as handle:
            data = pickle.load(handle)
        if isinstance(data, cls):
            return data
        return cls(**data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            pickle.dump({
                "feature_schema": self.feature_schema,
                "feature_columns": self.feature_columns,
                "means": self.means,
                "stds": self.stds,
                "vectors": self.vectors,
                "records": self.records,
                "motif_library": self.motif_library,
                "metadata": self.metadata,
            }, handle)

    def query_transition(
        self,
        row: Dict[str, Any],
        *,
        k: int = 5,
        same_flow_bonus: bool = True,
        exclude_sample_id: Optional[str] = None,
        exclude_design: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self.vectors:
            return []
        features = transition_feature_dict(row, self.feature_schema)
        query_vec = _normalised_vector(features, self.feature_columns, self.means, self.stds)
        candidate_flow = str(row.get("candidate_flow") or "")
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for vec, rec in zip(self.vectors, self.records):
            if exclude_sample_id and str(rec.get("sample_id")) == str(exclude_sample_id):
                continue
            if exclude_design and str(rec.get("design")) == str(exclude_design):
                continue
            dist = _euclidean(query_vec, vec)
            if same_flow_bonus and candidate_flow and rec.get("candidate_flow") == candidate_flow:
                dist *= 0.75
            scored.append((dist, rec))
        scored.sort(key=lambda item: item[0])
        out = []
        for dist, rec in scored[: max(1, int(k))]:
            item = dict(rec)
            item["distance"] = round(float(dist), 6)
            item["similarity"] = round(1.0 / (1.0 + float(dist)), 6)
            out.append(item)
        return out

    def candidate_evidence(
        self,
        *,
        node: Dict[str, Any],
        analysis_context: Optional[Dict[str, Any]],
        candidate_flow: str,
        k: int = 5,
    ) -> Dict[str, Any]:
        query = candidate_query_row(node=node, analysis_context=analysis_context, candidate_flow=candidate_flow)
        neighbours = self.query_transition(query, k=k)
        return self.aggregate_evidence(query, neighbours)

    def aggregate_evidence(self, query_row: Dict[str, Any], neighbours: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        if not neighbours:
            return {
                "available": False,
                "k": 0,
                "success_prior": 0.0,
                "legal_clean_prior": 0.0,
                "invalid_risk": 0.0,
                "fallback_risk": 0.0,
                "warning_risk": 0.0,
                "regression_risk": 0.0,
                "expected_delta_pure_qor": 0.0,
                "motif_bonus": 0.0,
                "neighbours": [],
            }
        weights = [max(1e-6, float(n.get("similarity") or 0.0)) for n in neighbours]
        denom = sum(weights) or 1.0

        def avg(key: str) -> float:
            return sum(w * _float(n.get(key), 0.0) for w, n in zip(weights, neighbours)) / denom

        motif_bonus, motif_records = self._motif_bonus(query_row)
        return {
            "available": True,
            "k": len(neighbours),
            "success_prior": avg("label_useful_clean"),
            "legal_clean_prior": avg("label_legal_clean"),
            "invalid_risk": avg("label_invalid"),
            "fallback_risk": avg("label_fallback"),
            "warning_risk": avg("label_warning"),
            "regression_risk": avg("label_regressed"),
            "expected_delta_pure_qor": avg("delta_pure_qor"),
            "expected_delta_fmax_mhz": avg("delta_fmax_mhz"),
            "expected_delta_wns_ns": avg("delta_wns_ns"),
            "motif_bonus": motif_bonus,
            "motif_records": motif_records,
            "neighbours": list(neighbours),
        }

    def _motif_bonus(self, query_row: Dict[str, Any]) -> Tuple[float, List[Dict[str, Any]]]:
        seq = query_row.get("flow_sequence_after") if isinstance(query_row.get("flow_sequence_after"), list) else []
        if not seq:
            return 0.0, []
        motifs = []
        # Focus on suffix motifs because they are what the planner is about to create.
        for n in range(1, min(3, len(seq)) + 1):
            motif = "->".join(seq[-n:])
            rec = self._motif_stats.get(motif)
            if isinstance(rec, dict):
                motifs.append(rec)
        if not motifs:
            return 0.0, []
        bonus = 0.0
        for rec in motifs:
            support = max(1.0, _float(rec.get("support"), 1.0))
            quality = _float(rec.get("useful_clean_rate"), 0.0) - _float(rec.get("invalid_rate"), 0.0) - 0.5 * _float(rec.get("fallback_rate"), 0.0)
            bonus += min(0.2, math.log1p(support) / 20.0) * quality + 0.01 * _float(rec.get("avg_delta_pure_qor"), 0.0)
        return max(-0.25, min(0.25, bonus)), motifs[:5]


def build_retrieval_index(
    trace_lake_dir: Path,
    out_dir: Path,
    *,
    representations_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    representations_dir = representations_dir or (out_dir / "representations")
    index = TrajectoryRetrievalIndex.build_from_trace_lake(trace_lake_dir, representations_dir=representations_dir)
    index_path = out_dir / "retrieval_index.pkl"
    index.save(index_path)
    checksums = {
        "retrieval_index_pkl_sha256": _sha256(index_path),
        "transitions_jsonl_sha256": _sha256(Path(trace_lake_dir) / "transitions.jsonl"),
        "candidates_jsonl_sha256": _sha256(Path(trace_lake_dir) / "candidates.jsonl"),
        "transition_vectors_csv_sha256": _sha256(Path(representations_dir) / "transition_vectors.csv"),
        "feature_schema_json_sha256": _sha256(Path(representations_dir) / "feature_schema.json"),
    }
    manifest = {
        "schema_version": "retrieval_index_v1",
        "trace_lake_dir": str(trace_lake_dir),
        "representations_dir": str(representations_dir),
        "index_path": str(index_path),
        "requires_rebuild_if_index_missing": True,
        "num_records": len(index.records),
        "num_feature_columns": len(index.feature_columns),
        "num_motifs": len(index.motif_library.get("motifs", [])),
        "checksums": checksums,
    }
    (out_dir / "retrieval_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    # Human-readable copy for quick inspection.
    (out_dir / "feature_columns.json").write_text(json.dumps(index.feature_columns, indent=2) + "\n")
    return manifest


def _sha256(path: Path) -> Optional[str]:
    try:
        if not path.exists() or not path.is_file():
            return None
        h = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def evaluate_retrieval(
    transitions_path: Path,
    *,
    k: int = 5,
    exclude_same_design: bool = False,
) -> Dict[str, Any]:
    rows = [r for r in read_jsonl(transitions_path) if isinstance(r, dict)]
    if len(rows) < 2:
        return {"num_rows": len(rows), "error": "need at least two transitions"}
    # Build leave-one-query index from the full schema but exclude sample/self at query time.
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        rep_dir = Path(tmp) / "representations"
        # Build schema/vectors through temporary trace layout.
        tmp_trace = Path(tmp) / "trace"
        tmp_trace.mkdir(parents=True)
        from .reliability import write_jsonl
        write_jsonl(tmp_trace / "transitions.jsonl", rows)
        build_representations(tmp_trace / "transitions.jsonl", rep_dir)
        index = TrajectoryRetrievalIndex.from_representations(rep_dir)
        predictions: List[Dict[str, Any]] = []
        for row in rows:
            neighbours = index.query_transition(
                row,
                k=k,
                exclude_sample_id=str(row.get("sample_id")),
                exclude_design=str(row.get("design")) if exclude_same_design else None,
            )
            evidence = index.aggregate_evidence(row, neighbours)
            labels = row_labels(row)
            pred_useful = 1 if evidence.get("success_prior", 0.0) >= 0.5 else 0
            pred_invalid = 1 if evidence.get("invalid_risk", 0.0) >= 0.5 else 0
            predictions.append({
                "sample_id": row.get("sample_id"),
                "design": row.get("design"),
                "candidate_flow": row.get("candidate_flow"),
                "actual_useful_clean": labels["label_useful_clean"],
                "pred_useful_clean": pred_useful,
                "pred_success_prior": evidence.get("success_prior"),
                "actual_invalid": labels["label_invalid"],
                "pred_invalid": pred_invalid,
                "pred_invalid_risk": evidence.get("invalid_risk"),
                "actual_delta_pure_qor": labels["delta_pure_qor"],
                "pred_delta_pure_qor": evidence.get("expected_delta_pure_qor"),
                "num_neighbours": evidence.get("k"),
            })
    return _retrieval_metrics(predictions, k=k, exclude_same_design=exclude_same_design)


def _retrieval_metrics(predictions: Sequence[Dict[str, Any]], *, k: int, exclude_same_design: bool) -> Dict[str, Any]:
    n = len(predictions)
    useful_correct = sum(1 for p in predictions if int(p["actual_useful_clean"]) == int(p["pred_useful_clean"]))
    invalid_correct = sum(1 for p in predictions if int(p["actual_invalid"]) == int(p["pred_invalid"]))
    mae = sum(abs(_float(p["actual_delta_pure_qor"]) - _float(p["pred_delta_pure_qor"])) for p in predictions) / max(1, n)
    invalid_tp = sum(1 for p in predictions if p["actual_invalid"] == 1 and p["pred_invalid"] == 1)
    invalid_fp = sum(1 for p in predictions if p["actual_invalid"] == 0 and p["pred_invalid"] == 1)
    invalid_fn = sum(1 for p in predictions if p["actual_invalid"] == 1 and p["pred_invalid"] == 0)
    precision = invalid_tp / max(1, invalid_tp + invalid_fp)
    recall = invalid_tp / max(1, invalid_tp + invalid_fn)
    f1 = 2 * precision * recall / max(1e-9, precision + recall)
    return {
        "schema_version": "retrieval_eval_v1",
        "k": k,
        "exclude_same_design": exclude_same_design,
        "num_predictions": n,
        "useful_clean_accuracy": useful_correct / max(1, n),
        "invalid_accuracy": invalid_correct / max(1, n),
        "invalid_precision": precision,
        "invalid_recall": recall,
        "invalid_f1": f1,
        "delta_pure_qor_mae": mae,
        "predictions": list(predictions),
    }


def _read_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, "r", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _normalised_vector(row: Dict[str, Any], columns: Sequence[str], means: Dict[str, float], stds: Dict[str, float]) -> List[float]:
    return [(_float(row.get(col), 0.0) - means.get(col, 0.0)) / max(1e-9, stds.get(col, 1.0)) for col in columns]


def _record_from_vector_row(row: Dict[str, Any]) -> Dict[str, Any]:
    keep = [
        "sample_id",
        "design",
        "run_id",
        "parent_node_id",
        "child_node_id",
        "candidate_flow",
        "flow_sequence_before",
        "flow_sequence_after",
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
    return {key: _maybe_number(row.get(key)) for key in keep if key in row}


def _euclidean(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _maybe_number(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            if value.strip() == "":
                return value
            v = float(value)
            if abs(v - int(v)) < 1e-12:
                return int(v)
            return v
        except Exception:
            return value
    return value
