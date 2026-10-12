"""No-network router accounting and fail-closed routing tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
import json
import time
import pytest
from tests.latch.test_acceptance_budget import limits
from integrations.llm.runtime.router_budget import RouterBudget, BoundedRouterClient
from integrations.llm.runtime.batch_budget import BatchStopped
from integrations.llm.runtime.types import LLMRequest


def config():
    return limits(provider="openrouter", model="openai/gpt-5.6-luna",
        verified_at=datetime.now(timezone.utc).isoformat(),
        endpoint=dict(model_id="openai/gpt-5.6-luna", tag="azure", provider_name="Azure", zdr_verified=True,
            supported_parameters=["reasoning", "response_format", "max_completion_tokens"]),
        response_models=["openai/gpt-5.6-luna-20260709"])


def request():
    return LLMRequest(provider="openrouter", model="openai/gpt-5.6-luna", messages=[{"role":"user","content":"fixture"}], reasoning_effort="medium")


def response():
    return dict(model="openai/gpt-5.6-luna-20260709", provider="Azure",
        usage=dict(prompt_tokens=100, completion_tokens=20, cost=.000044),
        choices=[dict(message=dict(content='{"ok":true}'))])


def test_serial_requests_share_durable_budget(tmp_path):
    cfg = config(); path = tmp_path / "batch.db"
    ledger = RouterBudget(path, cfg)
    active = []
    def transport(body, key, timeout):
        assert not active
        active.append(True)
        assert body["provider"]["zdr"] and not body["provider"]["allow_fallbacks"]
        assert body["reasoning"]["effort"] == "medium"
        time.sleep(.01)
        active.pop()
        return response()
    clients = [BoundedRouterClient(ledger=ledger, scope=s, key="fixture", transport=transport) for s in ("S1","S2","S3")]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda c:c.complete_json(request()), clients))
    assert all(r.parsed_json == {"ok":True} for r in results)
    reopened = RouterBudget(path, cfg)
    with reopened.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM router_receipts").fetchone()[0] == 3
        assert db.execute("SELECT SUM(accounted) FROM requests").fetchone()[0] == pytest.approx(3 * .000049)


@pytest.mark.parametrize("fault", ["usage", "cost", "identity", "transport", "overreserve"])
def test_unknown_or_failed_exchange_stops_without_retry(tmp_path, fault):
    ledger = RouterBudget(tmp_path / "batch.db", config()); calls=[]
    def transport(*args):
        calls.append(1)
        value=response()
        if fault == "transport": raise TimeoutError("not exposed")
        if fault == "usage": value.pop("usage")
        if fault == "cost": value["usage"].pop("cost")
        if fault == "identity": value["provider"]="other"
        if fault == "overreserve": value["usage"]["cost"]=1.
        return value
    client=BoundedRouterClient(ledger=ledger, scope="S1", key="fixture", transport=transport)
    with pytest.raises(BatchStopped): client.complete_json(request())
    with pytest.raises(BatchStopped): client.complete_json(request())
    assert calls == [1]
    with ledger.transaction() as db:
        assert db.execute("SELECT stopped FROM batch").fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
        status=db.execute("SELECT status FROM requests").fetchone()[0]
        assert status == ("RECEIVED" if fault == "overreserve" else "STARTED")


def test_stale_catalog_cannot_create_ledger(tmp_path):
    cfg=config(); cfg["verified_at"]="2020-01-01T00:00:00Z"
    with pytest.raises(ValueError): RouterBudget(tmp_path / "batch.db", cfg)
    assert not (tmp_path / "batch.db").exists()


def test_common_factory_dispatches_only_explicit_router_config(tmp_path):
    from integrations.llm.runtime.batch_budget import client_from_config
    path=tmp_path / "config.json"
    path.write_text(json.dumps(dict(ledger_path=str(tmp_path / "batch.db"), cost=config())))
    client=client_from_config(path, "S1")
    assert isinstance(client, BoundedRouterClient)
    assert client.scope == "S1"
    with client.ledger.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
