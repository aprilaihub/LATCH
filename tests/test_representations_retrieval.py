import json
from pathlib import Path

from optimizer_pipeline.reliability import ReliabilityTable, read_jsonl, write_jsonl
from optimizer_pipeline.representations import build_representations
from optimizer_pipeline.retrieval import TrajectoryRetrievalIndex, build_retrieval_index


def _transition(flow: str, status: str = "CLEAN", delta: float = 1.0, parent_seq=None):
    parent_seq = parent_seq or []
    return {
        "sample_id": f"run/{flow}_{status}_{delta}",
        "design": "d0",
        "run_id": "run",
        "parent_node_id": "node_000_root",
        "child_node_id": f"node_{flow}",
        "candidate_flow": flow,
        "flow_sequence_before": parent_seq,
        "flow_sequence_after": parent_seq + [flow],
        "state_before": {
            "depth": len(parent_seq),
            "pure_qor_score": 0.0,
            "fmax_mhz": 400.0,
            "wns_ns": -0.5,
            "tns_ns": -10.0,
            "validation_status": "CLEAN",
            "route_delay_ratio": 0.7,
            "dominant_path_type": "routing_dominated",
        },
        "outcome": {
            "validation_status": status,
            "qor_status": "invalid" if status == "INVALID" else "improved",
            "delta_pure_qor": delta,
            "delta_fmax_mhz": 5.0,
            "delta_wns_ns": 0.05,
            "runtime_seconds": 1.0,
            "invalid": status == "INVALID",
            "regressed": False,
        },
        "policy_context": {"planner_priority": 0.9},
    }


def test_unseen_flow_local_observation_is_unknown_not_unsafe():
    table = ReliabilityTable()
    risk = table.risk_for(flow="retime", local_observation={"clean": 0, "warning": 0, "fallback": 0, "invalid": 0})
    assert risk["clean_prob"] == 0.70
    assert risk["invalid_risk"] == 0.10
    assert risk["sources"][0]["name"] == "unseen_flow_prior"


def test_build_representations_and_retrieval_index(tmp_path: Path):
    trace = tmp_path / "trace"
    trace.mkdir()
    rows = [_transition("phys_opt", delta=2.0), _transition("lut_cone", status="INVALID", delta=-10.0)]
    write_jsonl(trace / "transitions.jsonl", rows)
    manifest = build_representations(trace / "transitions.jsonl", tmp_path / "representations")
    assert manifest["num_transitions"] == 2
    vectors = (tmp_path / "representations" / "transition_vectors.csv").read_text()
    assert "label_useful_clean" in vectors
    assert "x__num__route_delay_ratio" in vectors
    idx_manifest = build_retrieval_index(trace, tmp_path / "retrieval_index")
    assert idx_manifest["num_records"] == 2
    index = TrajectoryRetrievalIndex.load(tmp_path / "retrieval_index" / "retrieval_index.pkl")
    neighbours = index.query_transition(rows[0], k=1, exclude_sample_id=rows[0]["sample_id"])
    assert len(neighbours) == 1
