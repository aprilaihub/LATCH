"""Opt-in OpenRouter/ZDR acceptance using the existing transactional ledger.

No provider substitution, SDK retry, credential discovery, or study defaults.
The caller verifies and records the live endpoint/prices before opting in.
"""
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import urllib.request

from .batch_budget import BatchBudget, BatchStopped, validate_limits
from .types import LLMResponse
from .adapters.router_zdr_request import build_zdr_body


def validate_router_limits(cfg):
    if (cfg.get("provider"), cfg.get("model"), cfg.get("reasoning_effort")) != (
            "openrouter", "openai/gpt-5.6-luna", "medium"):
        raise ValueError("This acceptance configuration requires exact Luna medium")
    validate_limits(cfg | {"provider": "openai", "model": "gpt-5.6-luna"})
    endpoint = cfg.get("endpoint", {})
    if (endpoint.get("model_id") != cfg["model"] or endpoint.get("zdr_verified") is not True
            or not endpoint.get("tag") or not endpoint.get("provider_name")):
        raise ValueError("Verified exact ZDR endpoint required")
    if not cfg.get("response_models") or not all(isinstance(x, str) and x for x in cfg["response_models"]):
        raise ValueError("Explicit response model identities required")
    checked = datetime.fromisoformat(cfg["verified_at"].replace("Z", "+00:00"))
    if checked.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - checked).total_seconds() <= 86400:
        raise ValueError("Refresh endpoint and pricing evidence before new calls")
    return dict(cfg)


def post_once(body, key, timeout):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    request = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        return json.load(response)


class RouterBudget(BatchBudget):
    def __init__(self, path, config):
        super().__init__(path, config, validator=validate_router_limits)

    def settle_router(self, request_id, response):
        cfg = self.config
        usage = response.get("usage")
        if response.get("error") or response.get("model") not in cfg["response_models"] or response.get("provider") != cfg["endpoint"]["provider_name"]:
            raise BatchStopped("ROUTER_IDENTITY_OR_RESPONSE_ERROR")
        if not isinstance(usage, dict):
            raise BatchStopped("USAGE_UNKNOWN")
        prompt, completion, actual = (usage.get(k) for k in ("prompt_tokens", "completion_tokens", "cost"))
        if any(type(n) is not int or n < 0 for n in (prompt, completion)):
            raise BatchStopped("USAGE_UNKNOWN")
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual) or actual < 0:
            raise BatchStopped("COST_UNKNOWN")
        if prompt > cfg["max_input_bytes"] + 8192 or completion > cfg["max_output_tokens"]:
            raise BatchStopped("TOKEN_LIMIT_EXCEEDED")
        cost = max(actual, (prompt * cfg["input_usd_per_million"] + completion * cfg["output_usd_per_million"]) / 1e6)
        with self.transaction() as db:
            row = db.execute("SELECT status,reserved FROM requests WHERE id=?", (request_id,)).fetchone()
            if not row or row[0] != "STARTED":
                raise BatchStopped("INVALID_RECONCILIATION")
            db.execute("UPDATE requests SET status='RECEIVED',accounted=?,input_tokens=?,output_tokens=? WHERE id=?", (cost, prompt, completion, request_id))
            db.execute("CREATE TABLE IF NOT EXISTS router_receipts(request_id INTEGER PRIMARY KEY, model TEXT, provider TEXT, reported_cost REAL)")
            db.execute("INSERT INTO router_receipts VALUES(?,?,?,?)", (request_id, response["model"], response["provider"], actual))
            exceeded = cost > row[1]
            if exceeded:
                db.execute("UPDATE batch SET stopped=COALESCE(stopped,'RESERVATION_EXCEEDED') WHERE id=1")
        if exceeded:
            raise BatchStopped("RESERVATION_EXCEEDED")
        return prompt, completion, actual, cost


class BoundedRouterClient:
    def __init__(self, *, ledger, scope, key=None, transport=post_once):
        self.ledger, self.scope, self.key, self.transport = ledger, scope, key, transport

    def complete_json(self, request):
        cfg = self.ledger.config
        validate_router_limits(cfg)
        if (request.provider, request.model) != (cfg["provider"], cfg["model"]):
            raise BatchStopped("REQUEST_IDENTITY_MISMATCH")
        if request.reasoning_effort not in (None, cfg["reasoning_effort"]):
            raise BatchStopped("REQUEST_REASONING_MISMATCH")
        if request.max_tokens is not None and (type(request.max_tokens) is not int or not 0 < request.max_tokens <= cfg["max_output_tokens"]):
            raise BatchStopped("OUTPUT_TOKEN_LIMIT")
        wire = replace(request, temperature=None, max_tokens=request.max_tokens or cfg["max_output_tokens"],
            timeout_sec=cfg["timeout_seconds"], reasoning_effort=cfg["reasoning_effort"])
        body = build_zdr_body(wire, endpoint=cfg["endpoint"], max_input_bytes=cfg["max_input_bytes"],
            input_rate=cfg["input_usd_per_million"], output_rate=cfg["output_usd_per_million"])
        key = self.key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise BatchStopped("CREDENTIAL_UNAVAILABLE")
        size = len(json.dumps(wire.messages, ensure_ascii=False, allow_nan=False).encode())
        # Serialize model exchanges across parallel physical workers. A stale
        # STARTED row is still uncertain; acquiring this lock never releases it.
        with self.ledger.path.with_suffix(".request-lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            request_id = self.ledger.reserve(self.scope, size)
            try:
                response = self.transport(body, key, cfg["timeout_seconds"])
                prompt, completion, actual, cost = self.ledger.settle_router(request_id, response)
            except Exception:
                self.ledger.stop("ROUTER_EXCHANGE_OR_ACCOUNTING_UNCERTAIN")
                raise BatchStopped("ROUTER_EXCHANGE_OR_ACCOUNTING_UNCERTAIN; no retry") from None
        choices = response.get("choices")
        content = choices[0].get("message", {}).get("content") if isinstance(choices, list) and choices else None
        try:
            parsed = json.loads(content) if isinstance(content, str) else None
        except ValueError:
            parsed = None
        return LLMResponse(content=content, provider=request.provider, model=request.model,
            parsed_json=parsed if isinstance(parsed, dict) else None, prompt_tokens=prompt,
            completion_tokens=completion, total_tokens=prompt + completion,
            cost_usd=cost, provider_reported_cost_usd=actual, cost_source="router_conservative_accounting")


def client_from_config(path, scope):
    cfg = json.loads(Path(path).read_text())
    return BoundedRouterClient(ledger=RouterBudget(Path(cfg["ledger_path"]), cfg["cost"]), scope=scope)
