"""Replay Matt's original scheduler with shared fake services.

Only import ownership is adapted. The original runner/journal are loaded from
Git; scheduling, selection and termination bodies are not rewritten. This is
orchestration fidelity, not a historical toolchain or whole-collector replay.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
from copy import deepcopy
from pathlib import Path

import pytest

from experiments.ablation.__main__ import study_config
from experiments.ablation.runtime.artifacts import ArtifactStore
from experiments.ablation.runtime.expansion.frontier_runner import FrontierExpansionRunner
from experiments.ablation.runtime.expansion.policies.core import AnalysisPriorityExpansionPolicy, DeterministicExpansionPolicy
from experiments.ablation.runtime.expansion.policies.registry import PolicyResources, build_policy_registry
from optimizer_pipeline import DeterministicAnalyzer, OutputValidator
from tests.latch.test_execution_retry import RiggedExecutor, analysis_provider
from tests.latch.test_historical_semantics import historical


IMPORT_MAP = {
    "optimizer_pipeline.analysis_schema": "experiments.ablation.runtime.analysis_schema",
    "optimizer_pipeline.artifacts": "experiments.ablation.runtime.artifacts",
    "optimizer_pipeline.expansion.policies.core": "experiments.ablation.runtime.expansion.policies.core",
    "optimizer_pipeline.run.config": "experiments.ablation.config",
    "optimizer_pipeline.knowledge": "experiments.ablation.runtime.knowledge",
    "optimizer_pipeline.analysis": "experiments.ablation.runtime.analysis",
    "optimizer_pipeline.preflight": "experiments.ablation.runtime.preflight",
    "optimizer_pipeline.real_analysis_provider": "experiments.ablation.runtime.real_analysis_provider",
    "optimizer_pipeline.llm.decision_events": "integrations.llm.runtime.decision_events",
    "optimizer_pipeline.storage.transition_journal": "_latch_historical_journal",
}


def old_runner():
    journal = types.ModuleType("_latch_historical_journal")
    sys.modules[journal.__name__] = journal
    exec(compile(historical("optimizer_pipeline/storage/transition_journal.py", "a722b1e"),
                 "historical-journal", "exec"), journal.__dict__)
    tree = ast.parse(historical("optimizer_pipeline/expansion/frontier_runner.py", "a722b1e"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in IMPORT_MAP:
            node.module = IMPORT_MAP[node.module]
    module = types.ModuleType("_latch_historical_frontier")
    sys.modules[module.__name__] = module
    exec(compile(tree, "historical-frontier-imports-adapted-only", "exec"), module.__dict__)
    return module.FrontierExpansionRunner


@pytest.mark.parametrize("condition,phase", [("S0", "baseline"), ("S0", "extension"), ("S1", "baseline"), ("S2", "baseline"), ("S3", "baseline")])
@pytest.mark.parametrize("scenario", ["normal", "execution_failure", "missing_output", "functional_failure"])
def test_historical_and_current_frontier_have_same_semantic_trace(tmp_path, condition, phase, scenario):
    old = old_runner()
    traces = []
    for label, runner_type in (("old", old), ("new", FrontierExpansionRunner)):
        root = tmp_path / label / "root.dcp"
        root.parent.mkdir()
        root.write_text("offline root")
        config = study_config(condition, mode="mock", s0_phase=phase)
        # Keep the actual study defaults and fitted S0 resources. Only analysis
        # and physical execution are replaced with deterministic fake services.
        policy = build_policy_registry().create(config.policy_type, resources=PolicyResources(config=config))
        executor = RiggedExecutor({"auto_pblock"} if scenario in {"execution_failure", "missing_output"} else set(),
                                 mode="nonzero_exit" if scenario == "execution_failure" else "missing_dcp")
        checks = []
        async def final_check(golden, child):
            checks.append((Path(golden).name, Path(child).name, len(executor.calls)))
            return {"passed": scenario != "functional_failure"}
        runner = runner_type(config=config, artifact_store=ArtifactStore(root.parent / "run"),
            analyzer=DeterministicAnalyzer(analysis_provider=analysis_provider), executor=executor,
            output_validator=OutputValidator(), policy=policy, equivalence_checker=final_check)
        result = asyncio.run(runner.run(root))
        state = result.state
        traces.append({
            "execution_order": executor.calls,
            "runs": runner.run_count, "scheduled": runner.scheduled_run_count,
            "stop": str(state.stop_reason), "status": str(state.status), "success": result.success,
            "selected": state.best_node_id, "functional_calls": checks,
            "nodes": [(n.node_id, n.parent_id, n.depth, n.flow_sequence, str(n.status),
                       str(n.validation_status), n.metrics.get("final_fmax_mhz"))
                      for n in runner.nodes.values()],
            "pruned": sorted(runner.pruned_node_ids), "retries": runner.retry_count,
        })
    assert traces[0] == traces[1]


def test_analysis_priority_keeps_original_beam_rule():
    old = old_runner()
    from tests.latch.test_expansion_policy import DeterministicExpansionPolicyTest
    helper = DeterministicExpansionPolicyTest()
    current, node, request = helper._runner_and_frontier(AnalysisPriorityExpansionPolicy())
    # Use the same candidate population and configuration on both classes.
    current.config.policy_type = "analysis_priority"
    current.config.beam_width = 1
    second = deepcopy(node)
    second.node_id = "node_002_auto_pblock"
    second.flow_sequence = ["auto_pblock"]
    second.score = 2.0
    current.nodes[second.node_id] = second
    historical_runner = old(config=current.config, artifact_store=None, analyzer=None,
                            executor=None, output_validator=None, policy=AnalysisPriorityExpansionPolicy())
    historical_runner.nodes = current.nodes
    before = [n.node_id for n in historical_runner._frontier_nodes()]
    after = [n.node_id for n in current._frontier_nodes()]
    assert before == after and len(after) == 2


@pytest.mark.parametrize("old_path,new_path", [
    ("optimizer_pipeline/expansion/policies/core.py", "experiments/ablation/runtime/expansion/policies/core.py"),
    ("optimizer_pipeline/expansion/strategies.py", "experiments/ablation/runtime/expansion/strategies.py"),
    ("optimizer_pipeline/auto_top8.py", "experiments/ablation/runtime/auto_top8.py"),
])
def test_migrated_policy_function_bodies_match_original(old_path, new_path):
    # Imports and the default resource locator may move. Executable bodies of
    # the policy/scorer must not change as an unannounced tidy-up.
    root = Path(__file__).resolve().parents[2]
    def bodies(source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in IMPORT_MAP:
                node.module = IMPORT_MAP[node.module]
        return [ast.dump(n, include_attributes=False) for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert bodies(historical(old_path, "a722b1e")) == bodies((root / new_path).read_text())


@pytest.mark.parametrize("name", ["auto_top8_weights_default.json", "frontier_expansion_default.json"])
def test_study_resource_values_match_historical_source(name):
    import json
    root = Path(__file__).resolve().parents[2]
    assert json.loads(historical("configs/" + name, "a722b1e")) == json.loads(
        (root / "experiments/ablation/configs" / name).read_text())


@pytest.mark.parametrize("name", ["reference_manual_final.json", "flow_cards_C2_deploy.json",
                                 "reference_manual_kb.json", "reference_manual_schema.json"])
def test_fixed_knowledge_values_match_historical_source(name):
    import json
    root = Path(__file__).resolve().parents[2]
    assert json.loads(historical("optimizer_pipeline/knowledge/data/" + name, "a722b1e")) == json.loads(
        (root / "experiments/ablation/runtime/knowledge/data" / name).read_text())
