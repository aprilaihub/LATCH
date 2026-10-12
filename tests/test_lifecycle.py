import json
import subprocess
import sys
from types import SimpleNamespace
import time
from pathlib import Path

from optimizer_pipeline.capabilities.analysis.memory import AnalysisMemory
from optimizer_pipeline.capabilities.analysis.schemas import CandidatePrior
from optimizer_pipeline.capabilities.analysis.tools.base import ToolContext, execute_tool
from optimizer_pipeline.capabilities.analysis.report_probe_backends import LocalVivadoReportBackend, build_report_probe_backend
from optimizer_pipeline.domain.transition import DcpTransitionRecord
from optimizer_pipeline.domain.transition import annotate_rank_before_after, candidate_priority_audit_fields, records_to_policy_candidate_rows
from optimizer_pipeline.qor import QoRWeights, compute_qor_score
from experiments.tuning.trace_lake import export_trace_lake


class SlowTool:
    name = "slow_tool"
    tool_class = "inspect"
    mutates_design = False
    default_timeout_sec = 0.01

    def run(self, args, context):
        time.sleep(1.0)
        return {"ok": True}


def test_tool_timeout_guard_returns_timeout_without_waiting(tmp_path):
    ctx = ToolContext(run_dir=tmp_path, node=object(), nodes={}, analysis_contexts={})
    t0 = time.time()
    call = execute_tool(SlowTool(), {}, ctx, timeout_sec=0.01)
    elapsed = time.time() - t0
    assert call.timed_out is True
    assert call.success is False
    assert elapsed < 0.5


def test_runtime_weight_requires_explicit_legacy_flag():
    root = {"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5, "initial_tns_ns": -20.0}
    node = {"final_fmax_mhz": 410.0, "final_wns_ns": -0.35, "final_tns_ns": -12.0}
    pure = compute_qor_score(root, node, weights=QoRWeights(runtime_minutes=-0.5), path_runtime_seconds=600.0)
    legacy = compute_qor_score(root, node, weights=QoRWeights(runtime_minutes=-0.5), path_runtime_seconds=600.0, include_runtime_penalty=True)
    assert pure.weights["runtime_minutes"] == 0.0
    assert pure.weighted_components["runtime_minutes"] == 0.0
    assert legacy.weights["runtime_minutes"] == -0.5
    assert legacy.weighted_components["runtime_minutes"] == -5.0


def test_trace_lake_prefers_policy_candidates_with_analysis_fields(tmp_path):
    run = tmp_path / "exp" / "run0"
    run.mkdir(parents=True)
    summary = {
        "policy_type": "retrieval",
        "nodes": [
            {"node_id": "node_000_root", "parent_id": None, "depth": 0, "selected_flow": None, "flow_sequence": [], "status": "COMPLETED", "validation_status": "CLEAN", "pure_qor_score": 0.0, "metrics": {"qor_score": {"status": "neutral", "components": {}}}},
            {"node_id": "node_001_phys_opt", "parent_id": "node_000_root", "depth": 1, "selected_flow": "phys_opt", "flow_sequence": ["phys_opt"], "status": "COMPLETED", "validation_status": "CLEAN", "pure_qor_score": 1.0, "metrics": {"qor_score": {"status": "improved", "components": {"fmax_delta_mhz": 1.0}}}},
        ],
        "root_metrics": {"benchmark_name": "mock"},
        "total_llm_calls": 0,
    }
    (run / "pipeline_summary.json").write_text(json.dumps(summary))
    (run / "policy_events.json").write_text("[]")
    (run / "policy_candidates.jsonl").write_text(json.dumps({
        "candidate_id": "event_0_0",
        "event_index": 0,
        "target_node_id": "node_000_root",
        "candidate_flow": "phys_opt",
        "analysis_prior_applied": True,
        "analysis_priority_delta": 0.1,
        "rank_before_analysis": 2,
        "rank_after_analysis": 0,
        "executed": True,
        "outcome_node_id": "node_001_phys_opt",
    }) + "\n")
    out = tmp_path / "lake"
    manifest = export_trace_lake([tmp_path / "exp"], out, canonical_run_dirs=["run0"], debug_run_dirs=["debug0"])
    rows = [json.loads(line) for line in (out / "candidates.jsonl").read_text().splitlines()]
    assert manifest["num_candidates"] == 1
    assert manifest["generated_at"]
    assert manifest["included_run_dirs"] == [str(run)]
    assert manifest["canonical_run_dirs"] == ["run0"]
    assert manifest["debug_run_dirs"] == ["debug0"]
    assert rows[0]["analysis_prior_applied"] is True
    assert rows[0]["rank_after_analysis"] == 0


def test_analysis_feedback_rank_aliases_from_policy_candidates(tmp_path):
    run = tmp_path / "run"
    memory = AnalysisMemory(run, enabled=True)
    memory.candidate_priors.append(CandidatePrior(
        node_id="node_000_root",
        flow="auto_pblock",
        priority_delta=0.5,
        confidence=0.9,
        source_analysis_session_id="analysis_001",
    ))
    rows = [
        {
            "candidate_id": "event_0_0",
            "event_index": 0,
            "target_node_id": "node_000_root",
            "candidate_flow": "auto_pblock",
            "candidate_rank": 0,
            "priority": 1.0,
            "raw_base_priority": 0.1,
            "raw_analysis_adjusted_priority": 1.3,
            "analysis_priority_delta": 0.5,
            "schedule_stage": "not_executed",
            "executed": False,
        },
        {
            "candidate_id": "event_0_1",
            "event_index": 0,
            "target_node_id": "node_000_root",
            "candidate_flow": "phys_opt",
            "candidate_rank": 1,
            "priority": 0.8,
            "raw_base_priority": 0.9,
            "raw_analysis_adjusted_priority": 0.8,
        },
        {
            "candidate_id": "event_0_2",
            "event_index": 0,
            "target_node_id": "node_000_root",
            "candidate_flow": "retime",
            "candidate_rank": 2,
            "priority": 0.7,
            "raw_base_priority": 0.5,
            "raw_analysis_adjusted_priority": 0.7,
        },
    ]

    memory.rebuild_feedback_from_policy_candidates(rows, run_id="run0")
    record = json.loads((run / "analysis" / "analysis_feedback.jsonl").read_text().splitlines()[0])

    assert record["candidate_rank_before_analysis"] == 2
    assert record["candidate_rank_after_analysis"] == 0
    assert record["rank_before_analysis"] == 2
    assert record["rank_after_analysis"] == 0


def test_analysis_feedback_unmatched_prior_keeps_rank_aliases_null(tmp_path):
    run = tmp_path / "run"
    memory = AnalysisMemory(run, enabled=True)
    memory.candidate_priors.append(CandidatePrior(
        node_id="node_000_root",
        flow="auto_pblock",
        priority_delta=0.5,
        confidence=0.9,
        source_analysis_session_id="analysis_001",
    ))

    memory.rebuild_feedback_from_policy_candidates([], run_id="run0")
    record = json.loads((run / "analysis" / "analysis_feedback.jsonl").read_text().splitlines()[0])

    assert record["candidate_rank_before_analysis"] is None
    assert record["candidate_rank_after_analysis"] is None
    assert record["rank_before_analysis"] is None
    assert record["rank_after_analysis"] is None


def test_candidate_priority_audit_preserves_raw_saturation_ordering():
    fields = candidate_priority_audit_fields(
        {"priority": 1.0, "raw_base_priority": 0.8},
        analysis_priority_delta=0.5,
        analysis_risk_delta=0.0,
    )
    assert fields["raw_base_priority"] == 0.8
    assert fields["raw_analysis_adjusted_priority"] == 1.3
    assert fields["clipped_priority"] == 1.0
    assert fields["priority_clip_applied"] is True

    records = [
        DcpTransitionRecord.scheduled_transition(
            run_id="run0",
            transition_id="boosted",
            source="unit",
            parent_node_id="node_000_root",
            parent_dcp=None,
            optimizer_name="phys_opt",
            target_node_id="node_000_root",
            candidate_rank=0,
            priority=1.0,
            raw_base_priority=0.8,
            raw_analysis_adjusted_priority=1.3,
            clipped_priority=1.0,
        ),
        DcpTransitionRecord.scheduled_transition(
            run_id="run0",
            transition_id="base_high",
            source="unit",
            parent_node_id="node_000_root",
            parent_dcp=None,
            optimizer_name="retime",
            target_node_id="node_000_root",
            candidate_rank=1,
            priority=1.0,
            raw_base_priority=0.9,
            raw_analysis_adjusted_priority=1.1,
            clipped_priority=1.0,
        ),
    ]
    annotate_rank_before_after(records)

    rows = records_to_policy_candidate_rows(records)
    by_id = {row["candidate_id"]: row for row in rows}
    assert by_id["base_high"]["rank_before_analysis"] == 0
    assert by_id["boosted"]["rank_after_analysis"] == 0


def test_partial_trace_lake_exports_pipeline_state_candidates(tmp_path):
    run = tmp_path / "exp" / "root_deep_local_vivado"
    run.mkdir(parents=True)
    (run / "pipeline_state.json").write_text(json.dumps({
        "status": "RUNNING",
        "root_dcp": "logicnets.dcp",
        "active_node_ids": ["node_002_auto_pblock"],
        "completed_node_ids": ["node_000_root", "node_001_phys_opt"],
        "failed_node_ids": [],
        "pruned_node_ids": [],
        "best_node_id": "node_001_phys_opt",
    }))
    (run / "config.json").write_text(json.dumps({
        "policy_type": "retrieval",
        "online_deep_analysis": True,
        "analysis_report_probe_backend": "local_vivado",
        "max_total_runs": 4,
        "scheduler_mode": "async",
    }))
    (run / "state_graph.json").write_text(json.dumps({
        "nodes": [
            {"node_id": "node_000_root", "parent_id": None, "depth": 0, "selected_flow": None, "flow_sequence": [], "status": "COMPLETED", "validation_status": "CLEAN", "pure_qor_score": 0.0, "metrics": {}},
            {"node_id": "node_001_phys_opt", "parent_id": "node_000_root", "depth": 1, "selected_flow": "phys_opt", "flow_sequence": ["phys_opt"], "status": "COMPLETED", "validation_status": "CLEAN", "pure_qor_score": 1.0, "metrics": {}},
            {"node_id": "node_002_auto_pblock", "parent_id": "node_000_root", "depth": 1, "selected_flow": "auto_pblock", "flow_sequence": ["auto_pblock"], "status": "RUNNING", "validation_status": None, "pure_qor_score": None, "metrics": {}},
        ],
        "edges": [],
    }))
    (run / "policy_candidates.jsonl").write_text(json.dumps({
        "target_node_id": "node_000_root",
        "candidate_flow": "phys_opt",
        "executed": True,
        "analysis_prior_applied": True,
    }) + "\n")
    (run / "policy_events.json").write_text("[]")

    out = tmp_path / "lake"
    manifest = export_trace_lake([tmp_path / "exp"], out, include_partial=True)
    rows = [json.loads(line) for line in (out / "candidates.jsonl").read_text().splitlines()]
    runs = json.loads((out / "runs.json").read_text())
    assert manifest["include_partial"] is True
    assert manifest["num_runs"] == 1
    assert rows[0]["analysis_prior_applied"] is True
    assert runs[0]["artifact_kind"] == "partial_run"
    assert runs[0]["best_node_id"] == "node_001_phys_opt"


def test_local_vivado_backend_uses_vivado_exec_fallback(monkeypatch):
    monkeypatch.delenv("VIVADO_BIN", raising=False)
    monkeypatch.setenv("VIVADO_EXEC", "vivado")
    backend = build_report_probe_backend("local_vivado")
    assert isinstance(backend, LocalVivadoReportBackend)
    assert backend.vivado_bin == "vivado"


def test_local_vivado_backend_writes_absolute_checkpoint_and_report_files(tmp_path, monkeypatch):
    dcp = tmp_path / "input.dcp"
    dcp.write_text("checkpoint")
    context = SimpleNamespace(
        run_dir=tmp_path / "run",
        node=SimpleNamespace(node_id="node_000_root", input_dcp=str(dcp), output_dcp=None),
    )

    def fake_run(cmd, *, cwd, timeout, text):
        report_path = Path(cwd) / "timing_summary.rpt"
        report_path.write_text("fresh report")
        return SimpleNamespace(returncode=0, stdout="vivado ok", stderr="")

    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", fake_run)
    backend = LocalVivadoReportBackend(vivado_bin="vivado")
    result = backend.report_timing_summary(args={}, context=context)

    script = Path(result["report_path"]).with_suffix(".tcl")
    script_text = script.read_text()
    assert f'open_checkpoint "{dcp.resolve()}"' in script_text
    assert "report_timing_summary -delay_type max -file" in script_text
    assert "report_timing -delay_type max -max_paths 5 -nworst 1 -append -file" in script_text
    assert result["available"] is True
    assert result["preview"] == "fresh report"
