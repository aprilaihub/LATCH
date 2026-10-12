from optimizer_pipeline.capabilities.search.policies.core import ReliabilityAwareExpansionPolicy
from optimizer_pipeline.reliability import ReliabilityTable


def test_reliability_policy_penalizes_known_invalid_flow():
    transitions = [
        {
            "design": "d0",
            "candidate_flow": "lut_cone",
            "flow_sequence_before": ["phys_opt"],
            "outcome": {"validation_status": "INVALID", "qor_status": "invalid", "delta_pure_qor": -10.0, "runtime_seconds": 1.0},
        },
        {
            "design": "d0",
            "candidate_flow": "phys_opt",
            "flow_sequence_before": ["auto_pblock"],
            "outcome": {"validation_status": "CLEAN", "qor_status": "improved", "delta_pure_qor": 4.0, "runtime_seconds": 1.0},
        },
    ]
    table = ReliabilityTable.from_transitions(transitions)
    policy = ReliabilityAwareExpansionPolicy(reliability_table=table, invalid_penalty=0.8)
    request = {
        "allowed_flows": ["lut_cone", "phys_opt"],
        "batch_size": 2,
        "existing_sequences": [],
        "frontier": [{"node_id": "node_000_root", "pure_qor_score": 0.0, "flow_sequence": []}],
        "analysis_context_by_node": {
            "node_000_root": {
                "candidate_flows": {"deterministic_scores": [
                    {"flow": "lut_cone", "flow_heuristic_score": 100.0},
                    {"flow": "phys_opt", "flow_heuristic_score": 1.0},
                ]}
            }
        },
        "flow_observations": {},
    }
    decision = policy.propose(request)
    assert decision.candidates
    assert decision.candidates[0].flow == "phys_opt"
    assert any(c.flow == "lut_cone" for c in decision.candidates)


def test_reliability_table_summarizes_rates():
    table = ReliabilityTable.from_transitions([
        {"candidate_flow": "a", "outcome": {"validation_status": "CLEAN", "delta_pure_qor": 1.0}},
        {"candidate_flow": "a", "outcome": {"validation_status": "INVALID", "delta_pure_qor": -1.0}},
    ])
    rec = table.flow_record("a")
    assert rec is not None
    assert rec["executed"] == 2
    assert rec["clean_rate"] == 0.5
    assert rec["invalid_rate"] == 0.5
