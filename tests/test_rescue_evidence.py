from pathlib import Path
from types import SimpleNamespace

from optimizer_pipeline.capabilities.analysis.controller import OnlineAnalysisController
from optimizer_pipeline.capabilities.analysis.schemas import AnalysisBudget
from optimizer_pipeline.capabilities.analysis.tool_plan_policies import StaticAnalysisToolPlanPolicy
from optimizer_pipeline.capabilities.analysis.tool_registry import default_tool_registry
from optimizer_pipeline.capabilities.analysis.tools.act_probe_tools import ProbePlaceDirectiveTool
from optimizer_pipeline.capabilities.analysis.tools.base import ToolContext
from optimizer_pipeline.models import DcpStateNode, NodeStatus
from optimizer_pipeline.capabilities.intervention.evidence import RescueEvidenceBuilder, RescuePlanner, diagnose_attempt


def _ctx(tmp_path: Path, *, dcp_name: str = "logicnets_jscl_2025.1.dcp") -> SimpleNamespace:
    root_dcp = tmp_path / dcp_name
    root_dcp.write_text("root\n")
    root = DcpStateNode(
        node_id="node_000_root",
        parent_id=None,
        depth=0,
        input_dcp=str(root_dcp),
        output_dcp=str(root_dcp),
        flow_sequence=[],
        status=NodeStatus.COMPLETED,
        metrics={"final_fmax_mhz": 403.55},
    )
    return SimpleNamespace(
        root_dcp=root_dcp,
        root_metrics={"final_fmax_mhz": 403.55},
        nodes={root.node_id: root},
        state=SimpleNamespace(best_node_id=root.node_id),
        current_node=root,
        transition_records=[],
        config=SimpleNamespace(rescue_history_scope="in_run"),
    )


def test_evidence_builder_injects_known_public_prior_only_when_enabled(tmp_path: Path):
    ctx = _ctx(tmp_path)
    builder = RescueEvidenceBuilder(ctx)

    in_run = builder.build(attempts=[], attempt_diagnoses=[])
    known_public = builder.build(attempts=[], attempt_diagnoses=[], history_scope="known_public")

    assert in_run["known_public_prior"] == {}
    assert known_public["known_public_prior"]["fixed_recipe"] == ["pblock", "lut_cone", "memory_path_opt"]
    assert known_public["safe_parent_candidates"][0]["node_id"] == "node_000_root"


def test_planner_uses_public_prior_on_first_attempt(tmp_path: Path):
    ctx = _ctx(tmp_path)
    evidence = RescueEvidenceBuilder(ctx).build(attempts=[], attempt_diagnoses=[], history_scope="known_public")

    plan = RescuePlanner().plan(evidence=evidence, attempts=[], attempt_diagnoses=[], max_recipe_steps=5)

    assert plan.parent_selector == "current_best"
    assert plan.prefer_flows == ["pblock", "lut_cone", "memory_path_opt"]
    assert plan.prefer_primitives == ["flow.pblock", "flow.lut_cone", "flow.memory_path_opt"]
    assert plan.max_recipe_steps == 5


def test_planner_switches_to_failed_parent_and_avoids_invalid_motif(tmp_path: Path):
    ctx = _ctx(tmp_path)
    attempt = {
        "attempt_index": 0,
        "parent_node_id": "node_000_root",
        "child_node_id": "node_001_bad",
        "accepted_by_transition_executor": False,
        "updated_incumbent": False,
        "validation_status": "INVALID",
        "rejection_reason": "invalid_output",
        "recipe": {
            "recipe_spec": {
                "steps": [
                    {"primitive": "flow.fanout"},
                    {"primitive": "vivado.route_design", "args": {"directive": "Explore"}},
                ]
            }
        },
    }
    diagnosis = diagnose_attempt(attempt).to_dict()
    evidence = RescueEvidenceBuilder(ctx).build(attempts=[attempt], attempt_diagnoses=[diagnosis])

    plan = RescuePlanner().plan(evidence=evidence, attempts=[attempt], attempt_diagnoses=[diagnosis])

    assert diagnosis["outcome_class"] == "invalid"
    assert plan.parent_selector == "parent_of_last_failed_or_low_gain"
    assert "flow.fanout" in plan.avoid_primitives
    assert "fanout" in plan.avoid_flows


def test_manual_scratch_diagnosis_tracks_primitive_without_comparing_routed_fmax():
    attempt = {
        "attempt_index": 1,
        "parent_node_id": "node_001_manual.unroute_all",
        "child_node_id": "node_002_manual.unplace_all",
        "accepted_by_transition_executor": True,
        "publish_eligible": False,
        "validation_status": "CLEAN",
        "delta_vs_parent_mhz": -80.0,
        "manual_action": {"primitive": "manual.unplace_all", "args": {}},
    }

    diagnosis = diagnose_attempt(attempt).to_dict()

    assert diagnosis["outcome_class"] == "structural_progress"
    assert diagnosis["prefer_parent_selector"] == "last_accepted_rescue"
    evidence = RescueEvidenceBuilder(SimpleNamespace()).primitive_effect_history(attempts=[attempt])
    assert "manual.unplace_all" in evidence


def test_rescue_tool_plan_and_registry_keep_act_probes_opt_in(tmp_path: Path):
    registry = default_tool_registry()
    no_act_budget = AnalysisBudget(allow_report_probes=True, allow_act_probes=False)
    act_budget = AnalysisBudget(
        allow_report_probes=True,
        allow_act_probes=True,
        max_act_probe_calls=1,
        max_act_probe_time_sec=30.0,
    )

    assert "probe_place_directive" not in [tool.name for tool in registry.allowed_tools(no_act_budget)]
    assert "probe_place_directive" in [tool.name for tool in registry.allowed_tools(act_budget)]

    tools = StaticAnalysisToolPlanPolicy().plan_tools(
        node=SimpleNamespace(node_id="node_000_root", depth=0),
        trigger_reason="rescue_synthesis_loop_attempt_000",
        budget=act_budget,
        available_tool_names=[tool.name for tool in registry.allowed_tools(act_budget)],
    )
    assert "load_rescue_attempts" in tools
    assert "probe_place_directive" in tools


def test_act_probe_budget_and_arg_guard(tmp_path: Path):
    tool = ProbePlaceDirectiveTool()
    budget = AnalysisBudget(allow_act_probes=True, max_act_probe_calls=1, max_act_probe_time_sec=30.0)
    assert OnlineAnalysisController._tool_allowed_by_budget(tool, budget, tool_calls=[])

    context = ToolContext(
        run_dir=tmp_path,
        node=SimpleNamespace(node_id="node_000_root"),
        nodes={},
        analysis_contexts={},
    )
    result = tool.run({"directive": "Explore", "output_path": "/tmp/bad"}, context)

    assert result["status"] == "rejected"
    assert result["published"] is False
    assert result["graph_node_created"] is False
