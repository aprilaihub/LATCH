from __future__ import annotations

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def test_fpl_contest_has_one_active_import_location():
    assert importlib.util.find_spec("optimizer_pipeline.apps.fpl_contest") is not None
    assert importlib.util.find_spec("optimizer_pipeline.contest") is None
    assert not (REPO / "optimizer_pipeline" / "contest").exists()


def test_root_entry_forwards_to_owned_contest_application():
    source = (REPO / "dcp_optimizer.py").read_text()
    assert "from optimizer_pipeline.apps.fpl_contest.cli import" in source
    assert "optimizer_pipeline.contest" not in source


def test_search_has_one_active_import_location():
    assert importlib.util.find_spec("optimizer_pipeline.capabilities.search") is not None
    assert importlib.util.find_spec("optimizer_pipeline.expansion") is None
    assert not (REPO / "optimizer_pipeline" / "expansion").exists()


def test_intervention_has_one_active_import_location():
    assert importlib.util.find_spec("optimizer_pipeline.capabilities.intervention.controller") is not None
    for old_module in (
        "optimizer_pipeline.rescue",
        "optimizer_pipeline.manual_rescue",
        "optimizer_pipeline.recipe_synthesis",
        "optimizer_pipeline.rescue_evidence",
    ):
        assert importlib.util.find_spec(old_module) is None


def test_analysis_has_one_active_import_location():
    assert importlib.util.find_spec("optimizer_pipeline.capabilities.analysis") is not None
    assert importlib.util.find_spec("optimizer_pipeline.analysis") is None
    assert not (REPO / "optimizer_pipeline" / "analysis").exists()


def test_tuning_harness_is_owned_by_external_experiments():
    assert importlib.util.find_spec("experiments.tuning") is not None
    assert importlib.util.find_spec("optimizer_pipeline.experiments") is None
    assert not (REPO / "optimizer_pipeline" / "experiments").exists()


def test_target_contract_boundaries_exist_without_legacy_reexports():
    assert importlib.util.find_spec("optimizer_pipeline.core") is not None
    assert importlib.util.find_spec("optimizer_pipeline.backends") is not None
    # Historical documents are external; no parallel archived runtime may return.
    assert not (REPO / "archive" / "autonomous_dcp_optimizer.py").exists()
    assert not hasattr(__import__("optimizer_pipeline.core", fromlist=["*"]), "OptimizationRunContext")
    assert not hasattr(__import__("optimizer_pipeline.run", fromlist=["*"]), "OptimizationRunContext")


def test_contest_direct_action_and_experiment_have_independent_entries():
    assert importlib.util.find_spec("optimizer_pipeline.apps.fpl_contest.__main__") is not None
    assert importlib.util.find_spec("optimizer_pipeline.apps.direct_action.__main__") is not None
    assert importlib.util.find_spec("optimizer_pipeline.apps.experiment.__main__") is not None


def test_search_controller_depends_on_runtime_contract_not_run_context():
    source = (REPO / "optimizer_pipeline/capabilities/search/controller.py").read_text()
    assert "SearchRuntime" in source
    assert "OptimizationRunContext" not in source
    assert "self.ctx" not in source


def test_contest_delegates_intervention_without_owning_rescue_controller():
    source = (REPO / "optimizer_pipeline/apps/fpl_contest/entry.py").read_text()
    assert "ctx.intervention.run" in source
    assert "RescueController" not in source


def test_intervention_capability_has_no_provider_or_prompt_dependency():
    capability_dir = REPO / "optimizer_pipeline/capabilities/intervention"
    sources = "\n".join(path.read_text() for path in capability_dir.glob("*.py"))
    assert "experiments.llm.runtime" not in sources
    assert "build_llm_client" not in sources
    assert "LLMRequest" not in sources
    assert "role\": \"system" not in sources
    external = (REPO / "integrations/llm/intervention_generator.py").read_text()
    assert "build_llm_client" in external
    assert "LLMRequest" in external


def test_core_import_graph_has_no_provider_prompt_or_experiment_dependency():
    core_sources = "\n".join(path.read_text() for path in (REPO / "optimizer_pipeline/core").rglob("*.py"))
    assert "experiments.llm.runtime" not in core_sources
    assert "experiments.llm.prompts" not in core_sources
    assert "experiments.llm" not in core_sources
