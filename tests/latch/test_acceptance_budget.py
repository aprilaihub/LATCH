import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
import threading
import time

import pytest
from integrations.llm.runtime.batch_budget import BatchBudget, BoundedOpenAIClient, BatchStopped, validate_limits
from integrations.llm.runtime.types import LLMRequest, LLMResponse


def limits(**changes):
    return dict(provider="openai", model="gpt-5.6-luna", reasoning_effort="medium",
        max_requests=200, max_input_bytes=131072, max_output_tokens=16384, timeout_seconds=300,
        accounted_budget_usd=10, input_usd_per_million=.25, output_usd_per_million=1.20,
        price_source="https://developers.openai.com/api/docs/pricing", verified_at="2026-10-01T17:05:46Z",
        verified_current=True, rates_cover_all_billed_tokens=True) | changes


def response(**changes):
    return LLMResponse(content='{}', parsed_json={}, provider="openai", model="gpt-5.6-luna",
        prompt_tokens=100, completion_tokens=20,
        raw_response=NS(model="gpt-5.6-luna", usage=NS(prompt_tokens=100, completion_tokens=20))) if not changes else response_with(changes)


def response_with(changes):
    from dataclasses import replace
    if "raw_response" not in changes:
        changes = changes | {"raw_response": NS(model="gpt-5.6-luna", usage=NS(
            prompt_tokens=changes.get("prompt_tokens",100), completion_tokens=changes.get("completion_tokens",20)))}
    return replace(response(), **changes)


def request(**changes):
    return LLMRequest(provider="openai", model="gpt-5.6-luna", messages=[{"role":"user","content":"JSON"}], **changes)


@pytest.mark.parametrize("changes", [dict(model="other"), dict(reasoning_effort="high"), dict(max_requests=201),
    dict(max_input_bytes=131073), dict(max_output_tokens=16385), dict(timeout_seconds=301),
    dict(accounted_budget_usd=10.01), dict(input_usd_per_million=.20), dict(verified_current=False)])
def test_fixed_limits_reject_unapproved_policy(changes):
    with pytest.raises(ValueError):
        validate_limits(limits(**changes))


def test_shared_scope_budget_and_reopening(tmp_path):
    ledger = BatchBudget(tmp_path / "batch.db", limits(accounted_budget_usd=.12))
    a = ledger.reserve("dof-recipe", 20)
    ledger.reconcile(a, response(prompt_tokens=139264, completion_tokens=16384))
    reopened = BatchBudget(tmp_path / "batch.db", ledger.config)
    b = reopened.reserve("S1", 20)
    reopened.reconcile(b, response(prompt_tokens=139264, completion_tokens=16384))
    with pytest.raises(BatchStopped, match="INSUFFICIENT_RESERVATION"):
        ledger.reserve("S3", 20)
    assert reopened.summary()["requests"] == 2
    assert reopened.summary()["stopped"] == "INSUFFICIENT_RESERVATION"


def test_uncertain_request_is_not_replayed_after_restart(tmp_path):
    ledger = BatchBudget(tmp_path / "batch.db", limits())
    ledger.reserve("dof-recipe", 30)
    restarted = BatchBudget(ledger.path, limits())
    with pytest.raises(BatchStopped, match="UNCERTAIN"):
        restarted.reserve("S1", 30)
    assert restarted.summary()["accounted_or_reserved_usd"] > 0


def test_atomic_concurrent_reservation_prevents_double_exchange(tmp_path):
    ledger = BatchBudget(tmp_path / "batch.db", limits())
    barrier = threading.Barrier(2)
    def reserve():
        barrier.wait()
        try:
            return ledger.reserve("S1", 30)
        except BatchStopped:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(x is not None for x in results) == 1
    assert ledger.summary()["requests"] == 1


@pytest.mark.parametrize("change,code", [(dict(prompt_tokens=None),"USAGE_UNKNOWN"),
    (dict(model="other"),"MODEL_IDENTITY_MISMATCH"),
    (dict(raw_response=NS(model="other")),"MODEL_IDENTITY_MISMATCH"),
    (dict(raw_response=NS(model="gpt-5.6-luna",usage=NS(prompt_tokens=100.2,completion_tokens=20))),"USAGE_UNKNOWN"),
    (dict(completion_tokens=16385),"TOKEN_LIMIT_EXCEEDED")])
def test_unknown_or_invalid_exchange_retains_reservation_and_stops(tmp_path, change, code):
    ledger = BatchBudget(tmp_path / "batch.db", limits())
    ident = ledger.reserve("S1", 50)
    with pytest.raises(BatchStopped, match=code):
        ledger.reconcile(ident, response(**change))
    assert ledger.summary()["accounted_or_reserved_usd"] > 0
    with pytest.raises(BatchStopped):
        ledger.reserve("S2", 50)


@pytest.mark.parametrize("scope,count", [("dof-recipe",4),("compatibility",1),("S1",60)])
def test_scope_limits_are_cumulative(scope, count, tmp_path):
    ledger = BatchBudget(tmp_path / "batch.db", limits())
    for _ in range(count):
        ledger.reconcile(ledger.reserve(scope, 1), response())
    with pytest.raises(BatchStopped, match="REQUEST_LIMIT"):
        ledger.reserve(scope, 1)


def test_input_limit_counts_serialized_message_bytes(tmp_path):
    ledger = BatchBudget(tmp_path / "batch.db", limits())
    client = BoundedOpenAIClient(ledger=ledger, scope="S1", sdk=NS())
    with pytest.raises(BatchStopped, match="INPUT_LIMIT"):
        client.complete_json(LLMRequest(provider="openai", model="gpt-5.6-luna",
            messages=[{"role":"user","content":"x" * 131072}]))
    assert ledger.summary()["requests"] == 0


def test_exact_wire_parameters_zero_retry_and_serial_calls(tmp_path):
    ledger = BatchBudget(tmp_path / "batch.db", limits())
    active, peak, seen = 0, 0, []
    def create(**kwargs):
        nonlocal active, peak
        active += 1; peak = max(active, peak)
        time.sleep(.005)
        seen.append(kwargs)
        active -= 1
        return NS(model="gpt-5.6-luna", choices=[NS(message=NS(content='{}'))],
                  usage=NS(prompt_tokens=100, completion_tokens=20, total_tokens=120))
    client = BoundedOpenAIClient(ledger=ledger, scope="S1", sdk=NS(chat=NS(completions=NS(create=create))))
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda _: client.complete_json(request()), range(3)))
    assert peak == 1 and ledger.summary()["requests"] == 3
    for kwargs in seen:
        assert kwargs["reasoning_effort"] == "medium" and kwargs["timeout"] == 300
        assert kwargs["max_completion_tokens"] == 16384
        assert "temperature" not in kwargs and "max_tokens" not in kwargs


def test_failure_never_retries_or_persists_provider_error(tmp_path):
    ledger = BatchBudget(tmp_path / "batch.db", limits())
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("Unsupported temperature secret=SHOULD_NOT_PERSIST")
    client = BoundedOpenAIClient(ledger=ledger, scope="S1", sdk=NS(chat=NS(completions=NS(create=create))))
    for _ in range(2):
        with pytest.raises(BatchStopped):
            client.complete_json(request(temperature=.8))
    assert len(calls) == 1
    assert b"SHOULD_NOT_PERSIST" not in ledger.path.read_bytes()


def test_condition_no_replay_and_configuration_no_mutation(tmp_path):
    ledger = BatchBudget(tmp_path / "batch.db", limits())
    ledger.begin_condition("dof-recipe")
    with pytest.raises(BatchStopped):
        ledger.begin_condition("dof-recipe")
    ledger.finish_condition("dof-recipe", "RESEARCH_RESULT")
    with pytest.raises(ValueError):
        BatchBudget(ledger.path, limits(accounted_budget_usd=9))


def test_sdk_constructor_has_zero_retries_and_explicit_openai_endpoint(tmp_path, monkeypatch):
    import sys
    seen = []
    monkeypatch.setitem(sys.modules, "openai", NS(OpenAI=lambda **kwargs: seen.append(kwargs) or NS()))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fixture-not-a-real-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unapproved.invalid")
    BoundedOpenAIClient(ledger=BatchBudget(tmp_path / "batch.db",limits()),scope="S1")
    assert seen[0]["max_retries"] == 0 and seen[0]["timeout"] == 300
    assert seen[0]["base_url"] == "https://api.openai.com/v1"


def test_price_reverification_keeps_original_accounting_and_limits(tmp_path):
    ledger = BatchBudget(tmp_path / "batch.db",limits())
    ledger.record_price_evidence(limits(verified_at="2026-10-02T08:00:00Z"))
    assert ledger.config["verified_at"] == "2026-10-01T17:05:46Z"
    with pytest.raises(ValueError):
        ledger.record_price_evidence(limits(verified_at="2026-10-02T08:00:00Z",accounted_budget_usd=9))
