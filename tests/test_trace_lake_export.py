import json
from pathlib import Path

from experiments.tuning.trace_lake import export_trace_lake
from optimizer_pipeline.reliability import read_jsonl
from experiments.llm.runtime.decision_events import write_llm_decision_events


def test_export_trace_lake_from_minimal_pipeline_summary(tmp_path: Path):
    run = tmp_path / "exp" / "logicnets__deterministic__none"
    node0 = {
        "node_id": "node_000_root", "parent_id": None, "depth": 0, "selected_flow": None,
        "flow_sequence": [], "status": "COMPLETED", "validation_status": "CLEAN", "pure_qor_score": 0.0,
        "metrics": {"benchmark_name": "logicnets", "final_fmax_mhz": 400.0, "final_wns_ns": -1.0},
    }
    node1 = {
        "node_id": "node_001_phys_opt", "parent_id": "node_000_root", "depth": 1, "selected_flow": "phys_opt",
        "flow_sequence": ["phys_opt"], "status": "COMPLETED", "validation_status": "CLEAN", "pure_qor_score": 2.0,
        "metrics": {
            "benchmark_name": "logicnets", "final_fmax_mhz": 410.0, "final_wns_ns": -0.9,
            "node_runtime_seconds": 12.0,
            "candidate": {"target_node_id": "node_000_root", "flow": "phys_opt", "priority": 0.9},
            "qor_score": {"status": "improved", "components": {"fmax_delta_mhz": 10.0, "wns_improvement_ns": 0.1}},
        },
    }
    run.mkdir(parents=True)
    (run / "pipeline_summary.json").write_text(json.dumps({
        "policy_type": "deterministic",
        "model": None,
        "llm_provider": "mock",
        "llm_model": "mock_good",
        "llm_prompt_id": "frontier_expansion.baseline",
        "llm_prompt_hash": "a" * 64,
        "llm_response_schema_hash": "b" * 64,
        "llm_temperature": 0.0,
        "llm_response_mode": "json_object",
        "total_llm_calls": 1,
        "total_prompt_tokens": 3,
        "total_completion_tokens": 4,
        "total_tokens": 7,
        "total_cost_usd": None,
        "total_latency_sec": 0.0,
        "root_metrics": {"benchmark_name": "logicnets"},
        "nodes": [node0, node1],
        "state": {"pruned_node_ids": []},
    }))
    (run / "policy_events.json").write_text(json.dumps([
        {
            "event_index": 0,
            "decision": {"target_node_id": "node_000_root", "candidates": [{"target_node_id": "node_000_root", "flow": "phys_opt", "priority": 0.9}]},
            "validation": {"errors": []},
        }
    ]))
    write_llm_decision_events(run, [
        {
            "schema_version": "llm_decision_attempt.v1",
            "task": "frontier_expansion",
            "run_id": run.name,
            "event_index": 0,
            "attempt_index": 1,
            "provider": "mock",
            "model": "mock_good",
            "prompt_id": "frontier_expansion.baseline",
            "prompt_hash": "a" * 64,
            "response_schema_hash": "b" * 64,
            "temperature": 0.0,
            "max_tokens": 100,
            "request_context_hash": "c" * 64,
            "frontier_node_count": 1,
            "allowed_flow_count": 1,
            "json_parse_success": True,
            "domain_validation_success": True,
            "validation_errors": [],
            "fallback_triggered_after_attempt": False,
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "cost_usd": None,
            "cost_source": "unknown",
            "latency_sec": 0.0,
        }
    ])
    out = tmp_path / "trace"
    manifest = export_trace_lake([tmp_path / "exp"], out)
    assert manifest["num_transitions"] == 1
    assert manifest["num_llm_decision_events"] == 1
    assert "llm_decision_events_jsonl" in manifest["outputs"]
    transitions = read_jsonl(out / "transitions.jsonl")
    assert transitions[0]["candidate_flow"] == "phys_opt"
    assert transitions[0]["llm_provider"] == "mock"
    assert transitions[0]["llm_prompt_id"] == "frontier_expansion.baseline"
    assert transitions[0]["state_before"]["pure_qor_score"] == 0.0
    assert transitions[0]["outcome"]["delta_pure_qor"] == 2.0
    assert "delta_qor" not in transitions[0]["outcome"]
    candidates = read_jsonl(out / "candidates.jsonl")
    assert candidates[0]["executed"] is True
    assert candidates[0]["outcome_pure_qor_score"] == 2.0
    assert candidates[0]["llm_model"] == "mock_good"
    assert candidates[0]["llm_total_tokens"] == 7
    assert "outcome_score" not in candidates[0]
    attempts = read_jsonl(out / "llm_decision_events.jsonl")
    assert attempts[0]["schema_version"] == "llm_decision_attempt.v1"
    assert attempts[0]["run_id"] == run.name
    assert attempts[0]["event_index"] == 0
    assert attempts[0]["attempt_index"] == 1
    runs = json.loads((out / "runs.json").read_text())
    assert runs[0]["llm_prompt_hash"] == "a" * 64
    assert runs[0]["total_prompt_tokens"] == 3
