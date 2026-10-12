import asyncio
import json
from types import SimpleNamespace
import pytest
from experiments.dof.runtime.model_planning import ModelPlanner, validate_cost_config
from experiments.dof.runtime.prepared import make_session, mock_material, run_prepared
from integrations.llm.runtime.types import LLMResponse
from experiments.dof.analysis.curated import select, derive


def cost():
    return dict(provider="openai", model="fixture-model", price_source="offline-fixture-not-current-price",
        verified_at="offline-test", verified_current=True, rates_cover_all_billed_tokens=True,
        input_usd_per_million=1, output_usd_per_million=2, accounted_budget_usd=1,
        timeout_seconds=30, max_input_bytes=250000, max_output_tokens=1000, max_requests=4)


def test_model_planner_uses_query_and_stops_after_commit(tmp_path):
    root = tmp_path / "root.dcp"; root.write_text("fixture")
    context, messages = mock_material("recipe")
    context["queries"] = {"route_summary": [{"canonical_identity": "route", "handle_type": "net", "route_status": "FULLY_ROUTED", "count": 1}]}
    calls = []
    class Client:
        def complete_json(self, request):
            calls.append(request)
            response = {"kind": "QUERY", "queries": [{"name": "route_summary", "top_k": 1}]} if len(calls) == 1 else messages[0]
            return LLMResponse(content=json.dumps(response), parsed_json=response, provider="openai", model="fixture-model", prompt_tokens=100, completion_tokens=50)
    planner = ModelPlanner(condition="recipe", provider="openai", model="fixture-model", cost_config=cost(), client=Client())
    result = asyncio.run(run_prepared(condition="recipe", mode="mock", root=root, output=tmp_path / "run",
        context=context, messages=[], planner=planner))
    assert len(calls) == result["turns_used"] == 2
    assert result["planning_terminal"] == "COMMITTED"
    assert json.loads(calls[1].messages[1]["content"])["previous_result"]["status"] == "QUERY_RESULT"
    assert all(r.temperature is None for r in calls)
    assert result["provider_accounting"]["hard_billing_cap"] is False
    assert "handle_secret_hex" not in (tmp_path / "run/summary.json").read_text()


def test_provider_exception_never_retries_or_persists_secret(tmp_path):
    root = tmp_path / "root.dcp"; root.write_text("fixture")
    context, _ = mock_material("recipe")
    calls = []
    class Client:
        def complete_json(self, request):
            calls.append(request)
            raise RuntimeError("SENSITIVE_CREDENTIAL")
    planner = ModelPlanner(condition="recipe", provider="openai", model="fixture-model", cost_config=cost(), client=Client())
    with pytest.raises(RuntimeError, match="no automatic retry"):
        planner(session=make_session(condition="recipe", root=root, context=context, mode="mock"), previous_result=None)
    assert len(calls) == 1
    assert "SENSITIVE_CREDENTIAL" not in json.dumps(planner.accounting())


@pytest.mark.parametrize("change", [{"verified_current": False}, {"model": "wrong"}, {"max_requests": 5}, {"input_usd_per_million": float("nan")}])
def test_cost_unknown_or_wrong_model_stops(change):
    with pytest.raises(ValueError):
        validate_cost_config(cost() | change, "openai", "fixture-model")


def test_curated_missing_and_negative_are_not_zero():
    selection = dict(designs=["d"], models=["m"])
    rows = []
    for c in ("M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY"):
        rows.append(dict(root_id="d", model_key="m", condition=c, repeat_id="r", root_scorer_baseline_fmax_mhz="100",
            valid_executable_submission="True", physical_validity="True", P_exclusive_realisation="True", G_CAUSAL_RUNTIME_EXECUTED="True",
            functional_status_class="PASS" if c == "M_ONLY" else "NOT_ATTEMPTED", functional_scientific_delta_mhz="-5" if c == "M_ONLY" else ""))
    selected = select(rows, selection)
    _, matrix = derive(selected, selection)
    assert matrix[0]["improvement_percent"] == -5
    assert matrix[1]["improvement_percent"] is None
    with pytest.raises(ValueError, match="missing or duplicate"):
        select(rows + [rows[0]], selection)
