"""Offline Matt analysis-policy request/decision replay; no provider or tool."""
import ast
from dataclasses import asdict
import json
import sys
import types

import pytest

from experiments.ablation.runtime.analysis import dynamic_loop as current
from experiments.ablation.runtime.knowledge import build_knowledge_context, DEFAULT_KNOWLEDGE_BASE_PATH
from integrations.llm.runtime.types import LLMResponse
from tests.latch.test_historical_semantics import historical


def original():
    tree = ast.parse(historical("optimizer_pipeline/analysis/dynamic_loop.py", "a722b1e"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("optimizer_pipeline.llm"):
            node.module = node.module.replace("optimizer_pipeline.llm", "integrations.llm.runtime", 1)
    module = types.ModuleType("_latch_historical_dynamic_loop")
    module.__package__ = "experiments.ablation.runtime.analysis"
    sys.modules[module.__name__] = module
    exec(compile(tree, "historical-analysis-imports-adapted-only", "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("knowledge", [False, True])
@pytest.mark.parametrize("scenario", ["tool", "stop", "illegal_tool", "invalid_json", "cost_stop", "token_stop"])
def test_original_analysis_request_and_decision_match(knowledge, scenario):
    old = original()
    payload = dict(continue_analysis=scenario in {"tool", "illegal_tool"},
        next_tool_name="load_node_summary" if scenario == "tool" else "mutating_shell",
        next_tool_args={}, stop_reason="enough_evidence", confidence=.4,
        candidate_priorities=[dict(flow="phys_opt", priority_score=.5, risk_score=.1,
            confidence=.4, evidence_basis="heuristic_prior", reason="fixture")])
    traces = []
    for implementation in (old, current):
        requests = []
        class FixedClient:
            def complete_json(self, request):
                requests.append(asdict(request))
                return LLMResponse(content="not json" if scenario == "invalid_json" else json.dumps(payload),
                    provider="openai", model="fixture", prompt_tokens=100, completion_tokens=30,
                    total_tokens=130, cost_usd=.001, latency_sec=.01)
        policy = implementation.ProviderAnalysisDecisionPolicy(provider="openai", model="fixture",
            client=FixedClient(), cost_budget_usd=1,
            knowledge_context=build_knowledge_context(str(DEFAULT_KNOWLEDGE_BASE_PATH), mode="reference_manual")
                if knowledge else None)
        if scenario == "cost_stop": policy.total_cost_usd = 1
        if scenario == "token_stop": policy.total_tokens = policy.hard_token_budget
        state = implementation.AnalysisLoopState(session_id="fixture", node_id="node", trigger_reason="root",
            elapsed_sec=1, remaining_budget_sec=30, allowed_flows=["phys_opt", "route"],
            candidate_priors=[dict(flow="route", priority_delta=100)],
            evidence_summary={"heuristic_ranked_flows": ["route"], "timing": {"wns_ns": -.3}},
            optimization_state_context={"consumed_runs": 2, "remaining_runs": 9}, max_tool_calls=3)
        decision = policy.decide_next_tool(state=state, available_tool_names=["load_node_summary", "load_trajectory_outcomes"])
        traces.append(dict(requests=requests, decision=decision.to_dict(), usage=policy.usage_summary()))
    assert traces[0] == traces[1]
    assert len(traces[1]["requests"]) == (0 if scenario in {"cost_stop", "token_stop"} else 1)


def test_original_dynamic_policy_definitions_match_except_injected_client_adapter():
    # The only executable edit is a default-None shared-client parameter in the
    # builder. The policy/state/request/parsers themselves must match exactly.
    def definitions(source):
        tree = ast.parse(source)
        return {n.name: ast.dump(n, include_attributes=False) for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.name != "build_analysis_decision_policy"}
    from pathlib import Path
    assert definitions(historical("optimizer_pipeline/analysis/dynamic_loop.py", "a722b1e")) == definitions(
        Path(current.__file__).read_text())
