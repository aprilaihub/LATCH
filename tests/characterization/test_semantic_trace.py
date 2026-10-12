from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from optimizer_pipeline.models import DcpStateNode, NodeStatus
from optimizer_pipeline.capabilities.intervention.controller import ContextInterventionExecutor
from optimizer_pipeline.capabilities.intervention.evidence import safe_parent_node


REPO = Path(__file__).resolve().parents[2]


def run_case(tmp_path: Path, case_id: str, *args: str, expected_returncode: int = 0) -> dict:
    input_dcp = tmp_path / "input.dcp"
    input_dcp.parent.mkdir(parents=True, exist_ok=True)
    input_dcp.write_text("mock input\n")
    run_dir = tmp_path / "run"
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENROUTER_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            "dcp_optimizer.py",
            str(input_dcp),
            "--mock",
            "--no-dotenv",
            "--run-dir",
            str(run_dir),
            *args,
        ],
        cwd=REPO,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == expected_returncode, result.stderr + result.stdout
    trace = subprocess.run(
        [sys.executable, "tools/capture_semantic_trace.py", str(run_dir), "--case-id", case_id],
        cwd=REPO,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return json.loads(trace.stdout)


def test_fixed_and_explicit_single_action_semantics(tmp_path: Path):
    single = run_case(
        tmp_path / "single",
        "explicit-single-action",
        "--pipeline",
        "fixed",
        "--plan",
        "phys_opt",
        "--budget-runs",
        "1",
    )
    assert single["action_sequence"] == ["phys_opt"]
    assert single["parent_lineage"] == [
        {"attempt_index": 0, "parent_state": "state_000_root", "output_state": "state_001"}
    ]

    fixed = run_case(
        tmp_path / "fixed",
        "fixed-parent-chain",
        "--pipeline",
        "fixed",
        "--plan",
        "pblock,retime",
        "--budget-runs",
        "2",
    )
    assert fixed["action_sequence"] == ["pblock", "retime"]
    assert [row["parent_state"] for row in fixed["parent_lineage"]] == ["state_000_root", "state_001"]
    assert fixed["validation_classes"] == ["CLEAN", "CLEAN"]
    assert fixed["selection"]["state"] == "state_002"
    assert fixed["selection"]["valid_for_publication"] is True


def test_linear_frontier_and_custom_branching_semantics(tmp_path: Path):
    linear = run_case(
        tmp_path / "linear",
        "linear-depth-three",
        "--pipeline",
        "linear",
        "--single-path-depth",
        "3",
        "--budget-runs",
        "3",
    )
    assert linear["attempt_count"] == 3
    assert [row["parent_state"] for row in linear["parent_lineage"]] == [
        "state_000_root",
        "state_001",
        "state_002",
    ]

    frontier = run_case(
        tmp_path / "frontier",
        "frontier-siblings",
        "--pipeline",
        "frontier",
        "--budget-runs",
        "2",
        "--parallelism",
        "2",
    )
    assert frontier["attempt_count"] == 2
    assert [row["parent_state"] for row in frontier["parent_lineage"]] == ["state_000_root", "state_000_root"]
    assert frontier["budget_summary"]["scheduled_eda_runs"] == 2

    custom = run_case(
        tmp_path / "custom",
        "custom-fixed-frontier",
        "--pipeline",
        "custom",
        "--custom-pipeline",
        "tests.contest_pipeline_fixtures:pblock_retime_then_frontier_demo",
        "--budget-runs",
        "3",
    )
    assert custom["run"]["pipeline"] == "custom"
    assert custom["action_sequence"][:2] == ["pblock", "retime"]
    assert custom["attempt_count"] == 3


def test_intervention_recipe_and_manual_scratch_semantics(tmp_path: Path):
    recipe = run_case(
        tmp_path / "recipe",
        "deterministic-intervention-recipe",
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
    assert recipe["action_sequence"] == ["phys_opt", "rescue_place_reset_retime"]
    assert [row["parent_state"] for row in recipe["parent_lineage"]] == ["state_000_root", "state_000_root"]
    assert recipe["selection"]["state"] == "state_001"

    typed_recipe = run_case(
        tmp_path / "typed-recipe",
        "typed-primitive-intervention-recipe",
        "--pipeline",
        "linear",
        "--single-path-depth",
        "0",
        "--budget-runs",
        "2",
        "--rescue-mode",
        "synthesis_loop",
        "--rescue-trigger",
        "always",
        "--rescue-max-runs",
        "2",
    )
    assert typed_recipe["action_sequence"] == ["synthesized_recipe", "synthesized_recipe"]
    assert typed_recipe["attempt_count"] == 2
    assert all(row["source"] == "rescue_synthesis_loop" for row in typed_recipe["attempts"])

    manual = run_case(
        tmp_path / "manual",
        "manual-multi-step-scratch",
        "--pipeline",
        "linear",
        "--single-path-depth",
        "0",
        "--budget-runs",
        "2",
        "--rescue-mode",
        "manual_loop",
        "--rescue-trigger",
        "always",
        "--rescue-max-runs",
        "2",
    )
    assert manual["action_sequence"] == ["manual_action", "manual_action"]
    assert [row["parent_state"] for row in manual["parent_lineage"]] == ["state_000_root", "state_001"]
    assert all(row["selection_eligible"] is False for row in manual["attempts"])
    assert manual["selection"]["baseline_fallback"] is True


def test_invalid_and_orphaned_attempts_keep_baseline_safe(tmp_path: Path):
    invalid = run_case(
        tmp_path / "invalid",
        "invalid-attempt-baseline-fallback",
        "--pipeline",
        "custom",
        "--custom-pipeline",
        "tests.contest_pipeline_fixtures:invalid_transition",
        "--budget-runs",
        "1",
    )
    assert invalid["attempts"][0]["lifecycle"] == "rejected"
    assert invalid["attempts"][0]["selection_eligible"] is False
    assert invalid["selection"]["baseline_fallback"] is True
    assert invalid["selected_output_sha256"] == invalid["input_sha256"]

    orphan = run_case(
        tmp_path / "orphan",
        "orphaned-attempt-baseline-fallback",
        "--pipeline",
        "custom",
        "--custom-pipeline",
        "tests.contest_pipeline_fixtures:crash_after_scheduled",
        "--budget-runs",
        "1",
        expected_returncode=1,
    )
    assert orphan["attempts"][0]["lifecycle"] == "orphaned_scheduled"
    assert orphan["validation_classes"] == ["NOT_RUN"]
    assert orphan["budget_summary"]["scheduled_eda_runs"] == 1
    assert orphan["selection"]["baseline_fallback"] is True
    assert orphan["selected_output_sha256"] == orphan["input_sha256"]


@pytest.mark.parametrize("allow_invalid", [True, False])
def test_invalid_scratch_requires_explicit_intervention_policy(tmp_path: Path, allow_invalid: bool):
    root_dcp = tmp_path / "root.dcp"
    invalid_dcp = tmp_path / "invalid.dcp"
    root_dcp.write_text("root")
    invalid_dcp.write_text("invalid")
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
        node_id="node_001_invalid",
        parent_id=root.node_id,
        depth=1,
        input_dcp=str(root_dcp),
        output_dcp=str(invalid_dcp),
        flow_sequence=["manual.unroute_all"],
        status=NodeStatus.COMPLETED,
        validation_status="INVALID",
    )
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            rescue_allow_invalid_scratch=allow_invalid,
            rescue_manual_parent_selector="last_scratch",
        ),
        nodes={root.node_id: root, invalid.node_id: invalid},
        state=SimpleNamespace(best_node_id=root.node_id),
        current_node=root,
    )
    parent, detail = ContextInterventionExecutor(ctx)._select_manual_parent(
        [{"child_node_id": invalid.node_id, "diagnosis": {}}]
    )
    assert safe_parent_node(invalid) is False
    assert parent is (invalid if allow_invalid else root)
    assert detail["reason"] == ("continue_from_invalid_scratch" if allow_invalid else "start_from_current_best")
