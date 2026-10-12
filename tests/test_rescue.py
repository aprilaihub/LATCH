import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from optimizer_pipeline.models import DcpStateNode, NodeStatus
from optimizer_pipeline.capabilities.intervention.controller import ContextInterventionExecutor, RESCUE_FLOW_PLACE_RESET_RETIME
from optimizer_pipeline.core.store import load_run_view


REPO_ROOT = Path(__file__).resolve().parents[1]


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


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_rescue_off_does_not_change_default_mock_profile(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "fixed",
        "--plan",
        "phys_opt",
        "--budget-runs",
        "1",
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    assert not (run_dir / "rescue_decision.json").exists()
    view = load_run_view(run_dir)
    assert view["intervention_decision"] is None
    assert view["budget"]["completed"] == 1
    assert view["attempt_count"] == 1


def test_deterministic_rescue_runs_from_root_without_overwriting_better_main_result(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "fixed",
        "--plan",
        "phys_opt",
        "--budget-runs",
        "2",
        "--rescue-mode",
        "deterministic",
        "--rescue-min-main-delta-mhz",
        "999",
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    decision = load_run_view(run_dir)["intervention_decision"]
    assert decision["status"] == "executed"
    assert decision["selected_recipe"]["flow_name"] == RESCUE_FLOW_PLACE_RESET_RETIME
    assert decision["selection_source"] == "deterministic"

    view = load_run_view(run_dir)
    records = view["attempts"]
    assert [record["action_type"] for record in records] == [
        "phys_opt",
        RESCUE_FLOW_PLACE_RESET_RETIME,
    ]
    rescue_record = records[-1]
    assert rescue_record["parent_state_id"] == "node_000_root"
    assert rescue_record["selected_latest"] is False
    assert view["selected_state_id"] == "node_001_phys_opt"


def test_llm_rescue_mock_falls_back_when_analysis_does_not_select_allowlisted_recipe(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "fixed",
        "--plan",
        "phys_opt",
        "--budget-runs",
        "2",
        "--rescue-mode",
        "llm",
        "--rescue-min-main-delta-mhz",
        "999",
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    decision = load_run_view(tmp_path / "run")["intervention_decision"]
    assert decision["status"] == "executed"
    assert decision["analysis_session"] is not None
    assert decision["analysis_session"]["tool_loop_mode"] == "mock_llm_closed_loop"
    assert decision["fallback_used"] is True
    assert decision["selection_source"] == "deterministic_fallback"
    assert decision["selected_recipe"]["flow_name"] == RESCUE_FLOW_PLACE_RESET_RETIME


def test_rescue_recipe_selection_rejects_non_catalog_flows():
    controller = ContextInterventionExecutor(SimpleNamespace())
    session = SimpleNamespace(
        candidate_priorities=[
            SimpleNamespace(to_dict=lambda: {"flow": "run_tcl", "priority_score": 1.0, "confidence": 1.0}),
            SimpleNamespace(to_dict=lambda: {"flow": "bash", "priority_score": 0.9, "confidence": 1.0}),
            SimpleNamespace(
                to_dict=lambda: {
                    "flow": RESCUE_FLOW_PLACE_RESET_RETIME,
                    "priority_score": 0.8,
                    "confidence": 0.7,
                }
            ),
        ],
        candidate_priors=[],
    )

    recipe, detail = controller._recipe_from_analysis_session(session)

    assert recipe is not None
    assert recipe.flow_name == RESCUE_FLOW_PLACE_RESET_RETIME
    assert [row["flow"] for row in detail["rejected_rows"]] == ["run_tcl", "bash"]


def test_rescue_recipe_selection_does_not_treat_candidate_priors_as_llm_choice():
    controller = ContextInterventionExecutor(SimpleNamespace())
    session = SimpleNamespace(
        candidate_priorities=[
            SimpleNamespace(to_dict=lambda: {"flow": "retime", "priority_score": 1.0}),
        ],
        candidate_priors=[
            SimpleNamespace(
                to_dict=lambda: {
                    "flow": RESCUE_FLOW_PLACE_RESET_RETIME,
                    "priority_score": 0.99,
                    "confidence": 0.9,
                }
            ),
        ],
    )

    recipe, detail = controller._recipe_from_analysis_session(session)

    assert recipe is None
    assert detail["reason"] == "no_allowlisted_rescue_flow_in_candidate_priorities"
    assert detail["candidate_prior_rows"][0]["flow"] == RESCUE_FLOW_PLACE_RESET_RETIME


def test_synthesis_loop_honors_rescue_max_runs(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "fixed",
        "--plan",
        "phys_opt",
        "--budget-runs",
        "3",
        "--rescue-mode",
        "synthesis_loop",
        "--rescue-trigger",
        "always",
        "--rescue-max-runs",
        "2",
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    decision = load_run_view(run_dir)["intervention_decision"]
    assert decision["mode"] == "synthesis_loop"
    assert decision["loop_policy"] == "fixed_attempts"
    assert decision["status"] == "executed"
    assert len(decision["attempts"]) == 2
    assert len(decision["attempt_diagnoses"]) == 2
    first_attempt = decision["attempts"][0]
    assert first_attempt["rescue_attempt_plan"]["parent_selector"] == "current_best"
    assert first_attempt["diagnosis"]["outcome_class"] in {"improved", "low_gain", "no_gain", "regressed", "invalid", "unknown"}

    records = load_run_view(run_dir)["attempts"]
    assert [record["action_type"] for record in records] == [
        "phys_opt",
        "synthesized_recipe",
        "synthesized_recipe",
    ]
    assert records[1]["source"] == "rescue_synthesis_loop"
    assert records[2]["policy_metadata"]["rescue_attempt_index"] == 1


def test_synthesis_loop_stop_after_first_improvement_policy(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "linear",
        "--single-path-depth",
        "0",
        "--budget-runs",
        "3",
        "--rescue-mode",
        "synthesis_loop",
        "--rescue-trigger",
        "always",
        "--rescue-max-runs",
        "3",
        "--rescue-loop-policy",
        "stop_after_first_improvement",
        "--rescue-parent-strategy",
        "root",
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    decision = load_run_view(tmp_path / "run")["intervention_decision"]
    assert decision["stop_reason"] == "updated_incumbent"
    assert len(decision["attempts"]) == 1
    assert decision["attempts"][0]["updated_incumbent"] is True


def test_parent_selector_falls_back_from_invalid_best_to_root(tmp_path: Path):
    root_dcp = tmp_path / "root.dcp"
    invalid_dcp = tmp_path / "invalid.dcp"
    root_dcp.write_text("root\n")
    invalid_dcp.write_text("invalid\n")
    root = DcpStateNode(
        node_id="node_000_root",
        parent_id=None,
        depth=0,
        input_dcp=str(root_dcp),
        output_dcp=str(root_dcp),
        flow_sequence=[],
        status=NodeStatus.COMPLETED,
    )
    invalid = DcpStateNode(
        node_id="node_001_bad",
        parent_id="node_000_root",
        depth=1,
        input_dcp=str(root_dcp),
        output_dcp=str(invalid_dcp),
        flow_sequence=["bad"],
        status=NodeStatus.COMPLETED,
        validation_status="INVALID",
    )
    ctx = SimpleNamespace(
        nodes={root.node_id: root, invalid.node_id: invalid},
        state=SimpleNamespace(best_node_id=invalid.node_id),
        current_node=invalid,
        root_metrics={},
    )
    controller = ContextInterventionExecutor(ctx)

    parent, detail = controller._select_loop_parent(
        attempt_index=0,
        strategy="current_best",
        attempts=[],
    )

    assert parent.node_id == "node_000_root"
    assert detail["reason"] == "current_best_fallback_first_safe_parent"
    assert [candidate["node_id"] for candidate in detail["candidate_parents"]] == ["node_000_root"]


def test_parent_selector_creates_sibling_from_failed_node_parent(tmp_path: Path):
    root_dcp = tmp_path / "root.dcp"
    failed_dcp = tmp_path / "failed.dcp"
    root_dcp.write_text("root\n")
    failed_dcp.write_text("failed\n")
    root = DcpStateNode(
        node_id="node_000_root",
        parent_id=None,
        depth=0,
        input_dcp=str(root_dcp),
        output_dcp=str(root_dcp),
        flow_sequence=[],
        status=NodeStatus.COMPLETED,
    )
    failed = DcpStateNode(
        node_id="node_001_failed",
        parent_id="node_000_root",
        depth=1,
        input_dcp=str(root_dcp),
        output_dcp=str(failed_dcp),
        flow_sequence=["bad"],
        selected_flow="bad",
        status=NodeStatus.FAILED,
        validation_status="INVALID",
    )
    ctx = SimpleNamespace(
        nodes={root.node_id: root, failed.node_id: failed},
        state=SimpleNamespace(best_node_id=root.node_id),
        current_node=root,
        root_metrics={},
    )
    controller = ContextInterventionExecutor(ctx)

    parent, detail = controller._select_loop_parent(
        attempt_index=1,
        strategy="current_best",
        attempts=[
            {
                "updated_incumbent": False,
                "accepted_by_transition_executor": False,
                "parent_node_id": "node_000_root",
                "child_node_id": "node_001_failed",
            }
        ],
        parent_sequence=["current_best", "parent_of_last_failed_or_low_gain"],
    )

    assert parent.node_id == "node_000_root"
    assert detail["reason"] == "parent_of_last_failed_or_low_gain"
    assert "node_001_failed" not in [candidate["node_id"] for candidate in detail["candidate_parents"]]
