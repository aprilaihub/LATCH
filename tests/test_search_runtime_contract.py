import asyncio

import pytest

from optimizer_pipeline.capabilities.search import ExpansionController, SearchRuntime


class MockSearchRuntime:
    def __init__(self, budget: int) -> None:
        self.remaining_eda_runs = budget
        self.stopped = False
        self.transitions = []
        self.frontiers = []

    async def execute_transition(self, optimizer_name, **kwargs):
        self.transitions.append((optimizer_name, kwargs))
        self.remaining_eda_runs -= 1
        return optimizer_name

    async def execute_frontier(self, **kwargs):
        self.frontiers.append(kwargs)
        self.remaining_eda_runs -= int(kwargs.get("max_transitions") or 0)
        return kwargs


def test_fixed_sequence_runs_against_runtime_contract_in_order_and_respects_budget():
    runtime = MockSearchRuntime(budget=2)
    assert isinstance(runtime, SearchRuntime)

    records = asyncio.run(ExpansionController(runtime).fixed(["retime", "phys_opt", "pblock"]))

    assert records == ["retime", "phys_opt"]
    assert [name for name, _ in runtime.transitions] == ["retime", "phys_opt"]
    assert runtime.remaining_eda_runs == 0


def test_linear_search_pins_single_path_and_parallel_budget_settings():
    runtime = MockSearchRuntime(budget=5)

    result = asyncio.run(ExpansionController(runtime).linear(3, policy="deterministic"))

    assert result["max_transitions"] == 3
    assert result["parallelism"] == 1
    assert result["branching_factor"] == 1
    assert result["beam_width"] == 1
    assert result["batch_size"] == 1
    assert result["max_depth"] == 3
    assert runtime.remaining_eda_runs == 2


def test_frontier_if_budget_never_calls_runtime_after_budget_exhaustion():
    runtime = MockSearchRuntime(budget=0)

    result = asyncio.run(ExpansionController(runtime).frontier_if_budget(max_transitions=4))

    assert result is None
    assert runtime.frontiers == []


def test_controller_rejects_context_shaped_objects_outside_runtime_contract():
    with pytest.raises(TypeError, match="SearchRuntime"):
        ExpansionController(object())
