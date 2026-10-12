import json
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

from optimizer_pipeline.capabilities.intervention.manual import (
    ManualActionSpec,
    ManualActionValidator,
    ManualPrimitiveCatalog,
    ManualRescuePlan,
    ManualRescueSynthesisController,
    deterministic_manual_plan,
)
from experiments.llm import ProviderInterventionGenerator
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


def test_manual_catalog_rejects_flow_and_raw_actions():
    validator = ManualActionValidator(ManualPrimitiveCatalog.default())

    assert validator.validate_action({"primitive": "manual.phys_opt_design", "args": {"directive": "AddRetime"}}).valid
    assert not validator.validate_action({"primitive": "flow.retime"}).valid
    assert not validator.validate_action({"primitive": "retime"}).valid
    assert not validator.validate_action({"primitive": "run_tcl", "args": {"command": "place_design"}}).valid
    assert not validator.validate_action({"primitive": "manual.route_design", "args": {"directive": "AggressiveExplore"}}).valid


def test_manual_plan_mock_is_guidance_not_flow_recipe():
    controller = ManualRescueSynthesisController(config=object())

    result = controller.synthesize_plan_mock(context={"case": "mock"})

    assert result.plan is not None
    assert result.validation.valid
    assert all(step.primitive.startswith("manual.") for step in result.plan.steps)
    assert all(not step.primitive.startswith("flow.") for step in result.plan.steps)


def test_manual_plan_state_validation_requires_reset_before_reimplementation():
    validator = ManualActionValidator(ManualPrimitiveCatalog.default())
    bad_plan = ManualRescuePlan(
        plan_id="bad_route_first",
        hypothesis="",
        target_bottleneck="",
        steps=(ManualActionSpec("action_000", "manual.route_design", {"directive": "Explore"}),),
    )
    reset_plan = deterministic_manual_plan()

    assert validator.validate_plan_state(bad_plan, {"placement": "placed", "routing": "routed"}) == [
        "step_0_manual_route_requires_placed_unrouted_design"
    ]
    assert validator.validate_plan_state(reset_plan, {"placement": "placed", "routing": "routed"}) == []


def test_manual_action_mock_selects_one_step_only():
    controller = ManualRescueSynthesisController(config=object())
    plan = deterministic_manual_plan()

    result = controller.synthesize_action_mock(
        context={"manual_design_state": {"placement": "unplaced", "routing": "unrouted"}},
        plan=plan,
        action_index=2,
    )

    assert result.action is not None
    assert result.action.primitive == "manual.place_design"
    assert result.action.args == {"directive": "Explore"}


def test_manual_loop_runs_stepwise_mock_actions(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "linear",
        "--single-path-depth",
        "0",
        "--budget-runs",
        "3",
        "--rescue-mode",
        "manual_loop",
        "--rescue-trigger",
        "always",
        "--rescue-max-runs",
        "2",
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    view = load_run_view(run_dir)
    decision = view["intervention_decision"]
    assert decision["mode"] == "manual_loop"
    assert decision["loop_policy"] == "manual_loop"
    assert decision["status"] == "executed"
    assert len(decision["attempts"]) == 2

    actions = [attempt["manual_action"]["primitive"] for attempt in decision["attempts"]]
    assert actions == ["manual.unroute_all", "manual.unplace_all"]
    assert all(attempt["publish_eligible"] is False for attempt in decision["attempts"])
    assert all(attempt["non_publishable_scratch"] is True for attempt in decision["attempts"])
    assert [attempt["diagnosis"]["outcome_class"] for attempt in decision["attempts"]] == [
        "invalid",
        "invalid",
    ]
    assert decision["best_after_rescue"]["best_node_id"] == "node_000_root"
    assert view["selected_state_id"] == "node_000_root"
    records = view["attempts"]
    assert [record["action_type"] for record in records] == ["manual_action", "manual_action"]
    assert records[0]["source"] == "rescue_manual_loop"
    assert records[0]["parameters"]["manual_action_spec"]["primitive"] == "manual.unroute_all"
    assert [record["validation_status"] for record in records] == ["INVALID", "INVALID"]
    assert records[1]["parent_state_id"] == records[0]["output_state_id"]


def test_manual_loop_replans_until_the_action_budget_is_exhausted(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "linear",
        "--single-path-depth",
        "0",
        "--budget-runs",
        "6",
        "--rescue-mode",
        "manual_loop",
        "--rescue-trigger",
        "always",
        "--rescue-max-runs",
        "6",
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    view = load_run_view(run_dir)
    decision = view["intervention_decision"]
    assert decision["stop_reason"] == "max_actions_reached"
    assert len(decision["attempts"]) == 6
    assert [attempt["manual_action"]["primitive"] for attempt in decision["attempts"]] == [
        "manual.unroute_all",
        "manual.unplace_all",
        "manual.place_design",
        "manual.phys_opt_design",
        "manual.route_design",
        "manual.phys_opt_design",
    ]
    assert [attempt["publish_eligible"] for attempt in decision["attempts"]] == [
        False,
        False,
        False,
        False,
        True,
        True,
    ]
    assert decision["attempts"][2]["diagnosis"]["outcome_class"] == "invalid"
    assert view["selected_state_id"] == decision["attempts"][4]["child_node_id"]
    assert decision["attempts"][-1]["updated_incumbent"] is False
    second_attempt = decision["attempts"][1]
    assert second_attempt["delta_vs_parent_mhz"] == 0.0
    assert second_attempt["delta_vs_root_mhz"] == 1.0
    assert second_attempt["meaningful_delta_vs_parent_mhz"] == 0.0


def test_manual_plan_usage_respects_the_shared_cost_budget():
    class FakeClient:
        def complete_json(self, _request):
            return SimpleNamespace(
                content="{}",
                parsed_json=deterministic_manual_plan().to_dict(),
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cost_usd=0.20,
                provider_reported_cost_usd=None,
                estimated_cost_usd=0.20,
                cost_source="estimated",
                pricing_profile_id="test",
                latency_sec=0.01,
            )

    config = SimpleNamespace(provider="openai", model="gpt-5.4-mini", llm_cost_budget_usd=0.20, llm_timeout_sec=None)
    generator = ProviderInterventionGenerator(config, client=FakeClient())
    controller = ManualRescueSynthesisController(
        config=config,
        generator=generator,
    )
    first = controller.synthesize_plan(context={})
    second = controller.synthesize_plan(context={})

    assert first.plan is not None
    assert controller.usage_summary()["total_llm_calls"] == 1
    assert second.plan is None
    assert second.audit["error"] == "llm_cost_budget_reached"
