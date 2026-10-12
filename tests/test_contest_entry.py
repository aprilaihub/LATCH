import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from optimizer_pipeline.apps.fpl_contest.analysis_profiles import AnalysisProfileError, resolve_analysis_profile
from optimizer_pipeline.apps.fpl_contest.args import REPO_ROOT as APP_REPO_ROOT, build_contest_parser
from optimizer_pipeline.apps.fpl_contest.entry import _apply_cli_api_key
from optimizer_pipeline.apps.fpl_contest.official_score import OfficialContestTracer
from optimizer_pipeline.apps.fpl_contest.output_publisher import BestDcpPublisher
from optimizer_pipeline.apps.fpl_contest.network_policy import NetworkPolicyError, RunModeDecision, enforce_network_policy
from optimizer_pipeline.core.store import load_run_view


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_contest_app_resolves_repository_owned_defaults():
    args = build_contest_parser().parse_args(["input.dcp"])
    assert APP_REPO_ROOT == REPO_ROOT
    assert args.config == REPO_ROOT / "configs" / "frontier_expansion_default.json"
    assert {
        "run_mode": args.run_mode,
        "pipeline": args.pipeline,
        "policy": args.policy,
        "analysis": args.analysis,
        "llm_provider": args.llm_provider,
        "budget_runs": args.budget_runs,
        "single_path_depth": args.single_path_depth,
        "parallelism": args.parallelism,
        "rescue_mode": args.rescue_mode,
    } == {
        "run_mode": "contest-strict",
        "pipeline": "contest_default",
        "policy": "deterministic",
        "analysis": "none",
        "llm_provider": "none",
        "budget_runs": 6,
        "single_path_depth": 3,
        "parallelism": 1,
        "rescue_mode": "off",
    }


def run_optimizer(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("mock input\n")
    run_dir = tmp_path / "run"
    cmd = [
        sys.executable,
        "dcp_optimizer.py",
        str(input_dcp),
        "--mock",
        "--no-dotenv",
        "--run-dir",
        str(run_dir),
        *args,
    ]
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENROUTER_API_KEY", None)
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env, text=True, capture_output=True)


def run_optimizer_real_entry(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("real entry guard input\n")
    run_dir = tmp_path / "run"
    cmd = [
        sys.executable,
        "dcp_optimizer.py",
        str(input_dcp),
        "--no-dotenv",
        "--run-dir",
        str(run_dir),
        *args,
    ]
    merged = os.environ.copy()
    merged.pop("OPENAI_API_KEY", None)
    merged.pop("OPENROUTER_API_KEY", None)
    if env:
        merged.update(env)
    return subprocess.run(cmd, cwd=REPO_ROOT, env=merged, text=True, capture_output=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _policy(run_mode: str, provider: str, api_key_source: str | None = None) -> RunModeDecision:
    return RunModeDecision(
        run_mode=run_mode,
        llm_provider=provider,
        contest_eligible=run_mode == "contest-strict",
        reason=None,
        api_key_source=api_key_source,
        cost_source="research" if run_mode == "research-dev" else "none",
    )


def test_analysis_profile_resolver_hard_cut_modes():
    none = resolve_analysis_profile(Namespace(analysis="none", mock=False), _policy("contest-strict", "none"))
    assert none.enabled is False

    static = resolve_analysis_profile(Namespace(analysis="static", mock=False), _policy("contest-strict", "none"))
    assert static.enabled is True
    assert static.analysis_level == "read_only"
    assert static.loop_mode == "static_plan"
    assert static.uses_llm is False

    fast_mock = resolve_analysis_profile(Namespace(analysis="fast_llm", mock=True), _policy("contest-strict", "none"))
    assert fast_mock.analysis_level == "read_only"
    assert fast_mock.loop_mode == "mock_llm_closed_loop"
    assert fast_mock.tool_plan_policy == "minimal_read_only_v1"

    deep_real = resolve_analysis_profile(Namespace(analysis="deep_llm", mock=False), _policy("research-dev", "openai", "cli"))
    assert deep_real.analysis_level == "report_probe"
    assert deep_real.loop_mode == "openai_llm_closed_loop"
    assert deep_real.uses_llm is True

    contest_fast = resolve_analysis_profile(
        Namespace(analysis="fast_llm", mock=False),
        _policy("contest-strict", "openrouter", "OPENROUTER_API_KEY"),
    )
    assert contest_fast.analysis_level == "read_only"
    assert contest_fast.loop_mode == "openai_llm_closed_loop"
    assert contest_fast.tool_plan_policy == "minimal_read_only_v1"
    assert contest_fast.uses_llm is True

    contest_deep = resolve_analysis_profile(
        Namespace(analysis="deep_llm", mock=False),
        _policy("contest-strict", "openrouter", "OPENROUTER_API_KEY"),
    )
    assert contest_deep.analysis_level == "report_probe"
    assert contest_deep.uses_llm is True


def test_analysis_profile_resolver_rejects_invalid_real_llm_modes():
    with pytest.raises(AnalysisProfileError):
        resolve_analysis_profile(Namespace(analysis="fast_llm", mock=False), _policy("contest-strict", "none"))
    with pytest.raises(AnalysisProfileError):
        resolve_analysis_profile(Namespace(analysis="deep_llm", mock=False), _policy("research-dev", "openai", "missing"))
    with pytest.raises(AnalysisProfileError):
        resolve_analysis_profile(Namespace(analysis="deep_llm", mock=False), _policy("contest-strict", "openrouter", "cli"))


def test_contest_strict_rejects_config_with_external_history(tmp_path: Path):
    config = tmp_path / "external_history_config.json"
    config.write_text(json.dumps({"retrieval_index_path": "experiments/old/retrieval_index.pkl"}))

    proc = run_optimizer(
        tmp_path,
        "--config",
        str(config),
        "--pipeline",
        "fixed",
        "--plan",
        "phys_opt",
        "--budget-runs",
        "1",
    )

    assert proc.returncode != 0
    assert "contest_clean forbids external history" in proc.stderr


def test_contest_entry_fixed_mock_artifact_contract(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "fixed",
        "--plan",
        "memory_path_opt,phys_opt",
        "--budget-runs",
        "2",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    optimized = sorted(tmp_path.glob("input_optimized-*.dcp"))
    assert optimized and optimized[-1].is_file()
    assert (run_dir / "run.db").is_file()
    assert (run_dir / "result_manifest.json").is_file()
    for old_name in (
        "transition_journal.jsonl", "state_graph.json", "pipeline_state.json",
        "pipeline_summary.json", "policy_candidates.jsonl", "published_best.json",
    ):
        assert not (run_dir / old_name).exists()
    view = load_run_view(run_dir)
    assert view["attempt_count"] == 2
    assert view["states"] == ["node_000_root", "node_001_memory_path_opt", "node_002_phys_opt"]
    assert view["integrity_errors"] == []


def test_contest_entry_linear_frontier_and_custom_mock_profiles(tmp_path: Path):
    linear = run_optimizer(tmp_path / "linear", "--pipeline", "linear", "--single-path-depth", "3", "--budget-runs", "3")
    assert linear.returncode == 0, linear.stderr + linear.stdout
    linear_state = load_run_view(tmp_path / "linear" / "run")
    assert linear_state["budget"]["completed"] == 3
    assert any(
        record["source"].startswith("single_path_frontier_profile:")
        for record in linear_state["attempts"]
    )

    frontier = run_optimizer(tmp_path / "frontier", "--pipeline", "frontier", "--budget-runs", "4", "--parallelism", "2")
    assert frontier.returncode == 0, frontier.stderr + frontier.stdout
    frontier_summary = load_run_view(tmp_path / "frontier" / "run")
    assert frontier_summary["budget"]["reserved"] <= 4
    assert frontier_summary["budget"]["completed"] <= 4
    frontier_manifest = read_json(tmp_path / "frontier" / "run" / "result_manifest.json")
    assert frontier_manifest["selected_state_id"] == frontier_summary["selected_state_id"]
    assert frontier_manifest["final_dcp"] == frontier_summary["selected_output_uri"]
    assert Path(frontier_manifest["contest_output_dcp"]).read_bytes() == Path(frontier_manifest["final_dcp"]).read_bytes()

    custom = run_optimizer(
        tmp_path / "custom",
        "--pipeline",
        "custom",
        "--budget-runs",
        "3",
        "--custom-pipeline",
        "tests.contest_pipeline_fixtures:analysis_then_single_path_then_frontier",
    )
    assert custom.returncode == 0, custom.stderr + custom.stdout
    custom_run = tmp_path / "custom" / "run"
    assert load_run_view(custom_run)["analysis_sessions"]
    assert read_json(custom_run / "result_manifest.json")["pipeline"] == "custom"


def test_fixed_pblock_retime_sequence_uses_graph_transitions(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "fixed",
        "--plan",
        "pblock,retime",
        "--budget-runs",
        "2",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    attempts = load_run_view(run_dir)["attempts"]
    assert attempts[0]["policy_metadata"]["flow_sequence_before"] == []
    assert attempts[1]["policy_metadata"]["flow_sequence_before"] == ["pblock"]


def test_custom_pblock_retime_frontier_demo_mock_profile(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "custom",
        "--budget-runs",
        "3",
        "--custom-pipeline",
        "tests.contest_pipeline_fixtures:pblock_retime_then_frontier_demo",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    summary = read_json(run_dir / "result_manifest.json")
    view = load_run_view(run_dir)
    assert view["analysis_sessions"]
    assert summary["pipeline"] == "custom"
    assert view["budget"]["completed"] == 3
    assert view["attempts"][1]["policy_metadata"]["flow_sequence_before"] == ["pblock"]


def test_analysis_single_path_frontier_handles_prefixed_frontier_node_reports(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "analysis_single_path_frontier",
        "--analysis",
        "static",
        "--policy",
        "deterministic",
        "--single-path-depth",
        "2",
        "--budget-runs",
        "5",
        "--parallelism",
        "2",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    reports = sorted((run_dir / "analysis" / "reports").glob("*.md"))
    assert reports
    assert all("/" not in report.name for report in reports)
    summary = read_json(run_dir / "result_manifest.json")
    assert summary["success"] is True
    assert summary["pipeline"] == "analysis_single_path_frontier"
    analysis_run = load_run_view(run_dir)["analysis_sessions"][0]
    assert analysis_run["tool_loop_mode"] == "static_plan"


def test_fast_llm_mock_uses_minimal_read_only_tool_plan(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "analysis_single_path",
        "--analysis",
        "fast_llm",
        "--policy",
        "deterministic",
        "--llm-provider",
        "none",
        "--single-path-depth",
        "1",
        "--budget-runs",
        "1",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    analysis_run = load_run_view(run_dir)["analysis_sessions"][0]
    assert analysis_run["tool_loop_mode"] == "mock_llm_closed_loop"
    tool_calls = analysis_run["tool_calls"]
    tool_names = {call["tool_name"] for call in tool_calls}
    assert tool_names
    assert tool_names <= {"load_node_summary", "load_analysis_context", "load_trajectory_outcomes", "query_retrieval_memory"}
    assert all(not name.startswith("vivado_report_") for name in tool_names)


def test_real_llm_analysis_requires_openai_key_before_run_dir(tmp_path: Path):
    proc = run_optimizer_real_entry(
        tmp_path,
        "--run-mode",
        "research-dev",
        "--pipeline",
        "analysis_single_path",
        "--analysis",
        "fast_llm",
        "--llm-provider",
        "openai",
    )
    assert proc.returncode == 2
    assert "requires OPENAI_API_KEY or --api-key" in proc.stderr
    assert not (tmp_path / "run").exists()


def test_old_analysis_flag_values_are_rejected(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "analysis_single_path",
        "--analysis",
        "fast",
    )
    assert proc.returncode == 2
    assert "invalid choice" in proc.stderr


def test_run_mode_network_policy_guards(tmp_path: Path):
    rejected = run_optimizer(
        tmp_path / "reject",
        "--run-mode",
        "contest-strict",
        "--pipeline",
        "analysis_single_path",
        "--analysis",
        "deep_llm",
        "--llm-provider",
        "openai",
    )
    assert rejected.returncode == 2
    assert "does not allow llm_provider=openai" in rejected.stderr
    assert not (tmp_path / "reject" / "run").exists()

    accepted = run_optimizer(
        tmp_path / "research",
        "--run-mode",
        "research-dev",
        "--pipeline",
        "linear",
        "--llm-provider",
        "openai",
        "--model",
        "gpt-summary-test",
        "--budget-runs",
        "1",
        "--single-path-depth",
        "1",
    )
    assert accepted.returncode == 0, accepted.stderr + accepted.stdout
    summary = read_json(tmp_path / "research" / "run" / "result_manifest.json")
    assert summary["contest_eligible"] is False
    assert summary["run_mode"] == "research-dev"
    assert "llm_provider" not in summary

    missing = run_optimizer(
        tmp_path / "openrouter_missing",
        "--run-mode",
        "contest-strict",
        "--pipeline",
        "analysis_single_path",
        "--analysis",
        "deep_llm",
        "--llm-provider",
        "openrouter",
    )
    assert missing.returncode == 2
    assert "requires OPENROUTER_API_KEY" in missing.stderr


def test_contest_strict_openrouter_requires_env_key_and_rejects_cli_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(NetworkPolicyError, match="requires OPENROUTER_API_KEY"):
        enforce_network_policy(Namespace(run_mode="contest-strict", llm_provider="openrouter", api_key=None))

    with pytest.raises(NetworkPolicyError, match="forbids --api-key"):
        enforce_network_policy(Namespace(run_mode="contest-strict", llm_provider="openrouter", api_key="sk-cli"))

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter")
    policy = enforce_network_policy(Namespace(run_mode="contest-strict", llm_provider="openrouter", api_key=None))
    assert policy.contest_eligible is True
    assert policy.api_key_source == "OPENROUTER_API_KEY"
    assert policy.cost_source == "contest"


def test_cli_api_key_override_is_exposed_to_policy_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-existing-openai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-existing-openrouter")

    _apply_cli_api_key(
        Namespace(api_key="sk-cli-openai"),
        RunModeDecision(
            run_mode="research-dev",
            llm_provider="openai",
            contest_eligible=False,
            reason="test",
            api_key_source="cli",
            cost_source="research",
        ),
    )
    assert os.environ["OPENAI_API_KEY"] == "sk-cli-openai"

    _apply_cli_api_key(
        Namespace(api_key="sk-cli-openrouter"),
        RunModeDecision(
            run_mode="research-dev",
            llm_provider="openrouter",
            contest_eligible=False,
            reason="test",
            api_key_source="cli",
            cost_source="research",
        ),
    )
    assert os.environ["OPENROUTER_API_KEY"] == "sk-cli-openrouter"


def test_best_dcp_publisher_selects_only_valid_improvements(tmp_path: Path):
    baseline = tmp_path / "baseline.dcp"
    baseline.write_text("baseline\n")
    worse = tmp_path / "worse.dcp"
    worse.write_text("worse\n")
    better = tmp_path / "better.dcp"
    better.write_text("better\n")
    output = tmp_path / "input_optimized-test.dcp"
    publisher = BestDcpPublisher(
        output_path=output,
        run_dir=tmp_path / "run",
        run_mode="contest-strict",
        contest_eligible=True,
    )
    publisher.publish_baseline(baseline, root_fmax_mhz=400.0)
    assert output.read_text() == "baseline\n"

    assert publisher.consider_transition({"valid_for_scoring": False, "output_dcp": str(better), "fmax_output_mhz": 410.0}) is None
    assert output.read_text() == "baseline\n"
    assert publisher.consider_transition({"valid_for_scoring": True, "output_dcp": str(worse), "fmax_output_mhz": 399.0}) is None
    assert output.read_text() == "baseline\n"
    event = publisher.consider_transition(
        {
            "valid_for_scoring": True,
            "output_dcp": str(better),
            "fmax_output_mhz": 410.0,
            "delta_fmax_mhz": 10.0,
            "node_id": "node_001_phys_opt",
            "transition_id": "node_001_phys_opt",
            "optimizer_name": "phys_opt",
        }
    )
    assert event is not None
    assert output.read_text() == "better\n"
    published = publisher.snapshot()
    assert published["best_node_id"] == "node_001_phys_opt"
    assert published["selection_metric"] == "fmax_output_mhz"
    assert published["published_best_fmax_mhz"] == 410.0
    assert published["published_best_delta_fmax_mhz"] == 10.0
    assert "official_qor_score" not in published


def test_official_contest_trace_can_backfill_baseline_metrics(tmp_path: Path):
    tracer = OfficialContestTracer(
        run_dir=tmp_path,
        run_mode="contest-strict",
        contest_eligible=True,
        llm_provider="none",
    )
    input_dcp = tmp_path / "input.dcp"
    output_dcp = tmp_path / "input_optimized.dcp"
    input_dcp.write_text("dcp\n")
    output_dcp.write_text("dcp\n")
    tracer.append_root(input_dcp=input_dcp, root_metrics={}, published_output=output_dcp)
    tracer.update_baseline_row(fmax_mhz=400.0, wns_ns=-0.5, tns_ns=-20.0)
    row = tracer.rows[0]
    assert row["event_type"] == "baseline"
    assert row["fmax_output_mhz"] == 400.0
    assert row["delta_fmax_mhz"] == 0.0
    assert row["wns_ns"] == -0.5
    assert row["official_contest_score"] == 0.0
    assert row["official_contest_score_components"]["zero_reason"] == "no_fmax_improvement"


def test_crash_after_scheduled_reports_orphan_and_keeps_baseline_output(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "custom",
        "--budget-runs",
        "1",
        "--custom-pipeline",
        "tests.contest_pipeline_fixtures:crash_after_scheduled",
    )
    assert proc.returncode == 1
    optimized = sorted(tmp_path.glob("input_optimized-*.dcp"))
    assert optimized and optimized[-1].read_text() == "mock input\n"
    run_dir = tmp_path / "run"
    view = load_run_view(run_dir)
    summary = read_json(run_dir / "result_manifest.json")
    assert summary["success"] is False
    assert len(view["orphan_attempt_ids"]) == 1
    assert view["budget"]["reserved"] == 1


def test_invalid_executor_result_is_rejected_and_not_published(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "custom",
        "--budget-runs",
        "1",
        "--custom-pipeline",
        "tests.contest_pipeline_fixtures:invalid_transition",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    view = load_run_view(run_dir)
    assert view["attempts"][0]["validation_status"] == "INVALID"
    assert view["attempts"][0]["selection_eligible"] is False
    assert view["selected_state_id"] == "node_000_root"
    assert view["budget"] == {"reserved": 1, "completed": 0, "failed": 1}


def test_early_custom_failure_records_failed_summary_and_baseline(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "custom",
        "--budget-runs",
        "1",
        "--custom-pipeline",
        "tests.contest_pipeline_fixtures:raise_before_transition",
    )
    assert proc.returncode == 1
    optimized = sorted(tmp_path.glob("input_optimized-*.dcp"))
    assert optimized and optimized[-1].read_text() == "mock input\n"
    run_dir = tmp_path / "run"
    summary = read_json(run_dir / "result_manifest.json")
    assert summary["success"] is False
    assert "intentional early stop before transition" in summary["error"]
    assert load_run_view(run_dir)["budget"]["reserved"] == 0


def test_frontier_budget_reservation_with_parallelism_above_budget(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "frontier",
        "--budget-runs",
        "3",
        "--parallelism",
        "4",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    budget = load_run_view(tmp_path / "run")["budget"]
    assert budget["reserved"] <= 3
    assert budget["completed"] + budget["failed"] <= 3
