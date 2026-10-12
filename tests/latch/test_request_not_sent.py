"""Offline distinction between local admission stops and uncertain requests."""
import json
import pytest
from experiments.dof.runtime.model_planning import ModelPlanner
from experiments.dof.runtime.prepared import make_session, mock_material
from integrations.llm.runtime.types import LLMResponse, RequestNotSent
from tests.latch.test_dof_live_adapters import cost


@pytest.mark.parametrize("exception, expected", [
    (RequestNotSent, "NOT_SENT"), (RuntimeError, "PROVIDER_FAILURE")])
def test_not_sent_is_explicit_and_does_not_rewrite_prior_usage(tmp_path, exception, expected):
    root = tmp_path / "root.dcp"
    root.write_text("fixture")
    context, messages = mock_material("recipe")
    calls = []

    class Client:
        def complete_json(self, request):
            calls.append(request)
            if len(calls) == 1:
                return LLMResponse(content="{}", parsed_json=messages[0],
                    provider="openai", model="fixture-model", prompt_tokens=100,
                    completion_tokens=50)
            raise exception("SENSITIVE_CREDENTIAL")

    planner = ModelPlanner(condition="recipe", provider="openai", model="fixture-model",
                           cost_config=cost(), client=Client())
    session = make_session(condition="recipe", root=root, context=context, mode="mock")
    planner(session=session, previous_result=None)
    prior = dict(planner.records[0])
    prior_cost = planner.accounted_usd
    with pytest.raises(exception, match="before provider transport" if exception is RequestNotSent else "no automatic retry"):
        planner(session=session, previous_result=None)
    assert len(calls) == 2
    assert planner.records[0] == prior
    assert planner.records[1]["status"] == expected
    assert "SENSITIVE_CREDENTIAL" not in json.dumps(planner.accounting())
    if exception is RequestNotSent:
        assert planner.accounted_usd == pytest.approx(prior_cost)
        assert planner.records[1]["network_attempted"] is False
        assert planner.records[1]["reserved_usd"] == 0
    else:
        assert planner.accounted_usd > prior_cost
        assert planner.records[1]["reserved_usd"] > 0
