from types import SimpleNamespace

from optimizer_pipeline.run.search_runtime import _analysis_seed_for_frontier


def test_analysis_seed_maps_current_node_to_child_frontier_root():
    ctx = SimpleNamespace(
        current_node=SimpleNamespace(node_id="node_003_phys_opt"),
        analysis_priors=[
            {
                "node_id": "node_003_phys_opt",
                "target_node_id": "node_003_phys_opt",
                "flow": "retime",
                "candidate_flow": "retime",
                "source_analysis_session_id": "analysis_001",
            }
        ],
        analysis_sessions=[
            {
                "session_id": "analysis_001",
                "node_id": "node_003_phys_opt",
                "trigger_reason": "custom_post_single_path_deep_analysis",
                "tool_loop_mode": "openai_llm_closed_loop",
                "stop_decision": {"stop_reason": "enough_evidence"},
                "tool_calls": [
                    {
                        "tool_name": "vivado_report_timing_summary",
                        "tool_class": "report_probe",
                        "success": True,
                    }
                ],
                "loop_decisions": [
                    {
                        "action": "call_tool",
                        "next_tool_name": "vivado_report_timing_summary",
                        "confidence": 0.8,
                    }
                ],
                "diagnosis": {
                    "node_id": "node_003_phys_opt",
                    "analysis_type": "deep_llm",
                    "bottleneck_class": "timing",
                },
                "candidate_priors": [
                    {
                        "node_id": "node_003_phys_opt",
                        "target_node_id": "node_003_phys_opt",
                        "flow": "retime",
                        "candidate_flow": "retime",
                        "source_analysis_session_id": "analysis_001",
                    }
                ],
                "fast_skip_suggestions": [{"target_node_id": "node_003_phys_opt", "flow": "net_swap"}],
            }
        ],
        fast_skip_suggestions=[],
        analysis_contexts={"node_003_phys_opt": {"analysis_diagnoses": [{"node_id": "node_003_phys_opt"}]}},
    )

    seed = _analysis_seed_for_frontier(ctx)

    assert seed["analysis_priors"][0]["node_id"] == "node_000_root"
    assert seed["analysis_priors"][0]["target_node_id"] == "node_000_root"
    assert len(seed["analysis_priors"]) == 1
    assert seed["node_diagnoses"][0]["node_id"] == "node_000_root"
    assert seed["analysis_evidence"]["sessions"][0]["node_id"] == "node_000_root"
    assert seed["analysis_evidence"]["tool_calls"][0]["node_id"] == "node_000_root"
    assert seed["analysis_evidence"]["loop_decisions"][0]["node_id"] == "node_000_root"
    assert seed["analysis_evidence"]["fast_skip_suggestions"][0]["target_node_id"] == "node_000_root"
    assert seed["analysis_contexts"]["node_000_root"]["analysis_diagnoses"][0]["node_id"] == "node_000_root"
