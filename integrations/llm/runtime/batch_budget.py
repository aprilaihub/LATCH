"""Opt-in durable accounting for a single acceptance batch, not a billing cap.

No messages, provider exceptions, credentials or handle contexts are persisted.
A pending exchange is uncertain after a crash: it must never be replayed.
"""
from __future__ import annotations

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
from contextlib import contextmanager

from .types import LLMRequest


class BatchStopped(RuntimeError):
    """Fatal acceptance boundary; deliberately not an LLMClientError."""


SCOPES = {"dof-recipe": 4, "dof-direct": 4, "dof-constructed": 4,
          "S1": 60, "S2": 60, "S3": 60, "compatibility": 1}


def validate_limits(cfg):
    if (cfg.get("provider"), cfg.get("model"), cfg.get("reasoning_effort")) != (
            "openai", "gpt-5.6-luna", "medium"):
        raise ValueError("Acceptance requires the approved exact model and medium reasoning")
    for key, ceiling in (("max_requests", 200), ("max_input_bytes", 131072),
                         ("max_output_tokens", 16384), ("timeout_seconds", 300)):
        value = cfg.get(key)
        if type(value) is not int or not 0 < value <= ceiling:
            raise ValueError("Invalid acceptance limit: " + key)
    for key in ("accounted_budget_usd", "input_usd_per_million", "output_usd_per_million"):
        value = cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Invalid acceptance price or budget: " + key)
    if cfg["accounted_budget_usd"] > 10:
        raise ValueError("The approved batch stop threshold is at most $10")
    if not cfg.get("price_source") or not cfg.get("verified_at") or cfg.get("verified_current") is not True:
        raise ValueError("Fresh official pricing evidence is required")
    if cfg.get("rates_cover_all_billed_tokens") is not True:
        raise ValueError("Reservation rates must cover cache writes and reasoning")
    if cfg["input_usd_per_million"] < .25 or cfg["output_usd_per_million"] < 1.20:
        raise ValueError("Reservation rates do not cover the approved short-context price")
    return dict(cfg)


class BatchBudget:
    def __init__(self, path: Path, config, *, validator=validate_limits):
        self.path, self.config = Path(path), validator(config)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = json.dumps(self.config, sort_keys=True, allow_nan=False)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS batch (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, stopped TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL, reserved REAL NOT NULL, accounted REAL, input_tokens INTEGER, output_tokens INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS conditions (name TEXT PRIMARY KEY, status TEXT NOT NULL)")
            row = db.execute("SELECT config FROM batch WHERE id=1").fetchone()
            if row and row[0] != encoded:
                raise ValueError("Cannot change accounting configuration of an existing batch")
            db.execute("INSERT OR IGNORE INTO batch(id,config) VALUES(1,?)", (encoded,))
        os.chmod(self.path, 0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def stop(self, reason):
        # Only caller-defined machine codes, never exception text.
        with self.transaction() as db:
            db.execute("UPDATE batch SET stopped=COALESCE(stopped,?) WHERE id=1", (reason,))

    def reserve(self, scope, input_bytes):
        cfg = self.config
        reason = None
        reservation = ((cfg["max_input_bytes"] + 8192) * cfg["input_usd_per_million"] +
                       cfg["max_output_tokens"] * cfg["output_usd_per_million"]) / 1e6
        with self.transaction() as db:
            stopped = db.execute("SELECT stopped FROM batch WHERE id=1").fetchone()[0]
            if stopped:
                raise BatchStopped(stopped)
            count, accounted, pending = db.execute("SELECT COUNT(*), COALESCE(SUM(COALESCE(accounted,reserved)),0), SUM(status='STARTED') FROM requests").fetchone()
            scoped = db.execute("SELECT COUNT(*) FROM requests WHERE scope=?", (scope,)).fetchone()[0]
            if scope not in SCOPES or input_bytes > cfg["max_input_bytes"]:
                reason = "REQUEST_SCOPE_OR_INPUT_LIMIT"
            elif pending:
                reason = "UNCERTAIN_OR_CONCURRENT_EXCHANGE"
            elif count >= cfg["max_requests"] or scoped >= SCOPES[scope]:
                reason = "REQUEST_LIMIT"
            elif accounted + reservation > cfg["accounted_budget_usd"]:
                reason = "INSUFFICIENT_RESERVATION"
            if reason:
                db.execute("UPDATE batch SET stopped=? WHERE id=1", (reason,))
            else:
                request_id = db.execute("INSERT INTO requests(scope,status,reserved) VALUES(?,'STARTED',?)", (scope, reservation)).lastrowid
        if reason:
            raise BatchStopped(reason)
        return request_id

    def reconcile(self, request_id, response):
        cfg = self.config
        counts = (response.prompt_tokens, response.completion_tokens)
        raw_model = getattr(response.raw_response, "model", None)
        raw_usage = getattr(response.raw_response, "usage", None)
        if isinstance(response.raw_response, dict):
            raw_model = response.raw_response.get("model")
            raw_usage = response.raw_response.get("usage")
        raw_counts = tuple(raw_usage.get(k) if isinstance(raw_usage, dict) else getattr(raw_usage, k, None)
                           for k in ("prompt_tokens", "completion_tokens"))
        reason = None
        if (response.provider, response.model) != (cfg["provider"], cfg["model"]) or raw_model != cfg["model"]:
            reason = "MODEL_IDENTITY_MISMATCH"
        elif any(type(v) is not int or v < 0 for v in (*counts, *raw_counts)) or raw_counts != counts:
            reason = "USAGE_UNKNOWN"
        elif counts[0] > cfg["max_input_bytes"] + 8192 or counts[1] > cfg["max_output_tokens"]:
            reason = "TOKEN_LIMIT_EXCEEDED"
        if reason:
            self.stop(reason)
            raise BatchStopped(reason)
        cost = (counts[0] * cfg["input_usd_per_million"] + counts[1] * cfg["output_usd_per_million"]) / 1e6
        with self.transaction() as db:
            updated = db.execute("UPDATE requests SET status='RECEIVED',accounted=?,input_tokens=?,output_tokens=? WHERE id=? AND status='STARTED'", (cost, *counts, request_id)).rowcount
            if updated != 1:
                raise BatchStopped("INVALID_RECONCILIATION")
        return cost

    def begin_condition(self, name):
        with self.transaction() as db:
            if db.execute("SELECT stopped FROM batch WHERE id=1").fetchone()[0]:
                raise BatchStopped("BATCH_STOPPED")
            if db.execute("SELECT 1 FROM conditions WHERE name=?", (name,)).fetchone():
                raise BatchStopped("CONDITION_ALREADY_STARTED")
            db.execute("INSERT INTO conditions VALUES(?,'STARTED')", (name,))

    def finish_condition(self, name, status):
        with self.transaction() as db:
            if db.execute("UPDATE conditions SET status=? WHERE name=? AND status='STARTED'", (status, name)).rowcount != 1:
                raise BatchStopped("INVALID_CONDITION_TRANSITION")

    def summary(self):
        with self.transaction() as db:
            return {"stopped": db.execute("SELECT stopped FROM batch WHERE id=1").fetchone()[0],
                    "requests": db.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
                    "accounted_or_reserved_usd": db.execute("SELECT COALESCE(SUM(COALESCE(accounted,reserved)),0) FROM requests").fetchone()[0],
                    "conditions": dict(db.execute("SELECT name,status FROM conditions")),
                    "hard_billing_cap": False}

    def record_price_evidence(self, evidence):
        evidence = validate_limits(evidence)
        evidence_fields = {"verified_at", "price_source", "verified_current"}
        if {k:v for k,v in evidence.items() if k not in evidence_fields} != {
                k:v for k,v in self.config.items() if k not in evidence_fields}:
            raise ValueError("New price evidence cannot change approved rates, model or limits")
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS price_checks (id INTEGER PRIMARY KEY, evidence TEXT NOT NULL)")
            db.execute("INSERT INTO price_checks(evidence) VALUES(?)", (json.dumps(evidence,sort_keys=True),))


class BoundedOpenAIClient:
    def __init__(self, *, ledger: BatchBudget, scope: str, sdk=None):
        from .adapters.openai_chat import OpenAIChatClient
        from .provider_profiles import ProviderRequestPolicy
        self.ledger, self.scope, self.lock = ledger, scope, threading.Lock()
        if sdk is None:
            from openai import OpenAI
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise BatchStopped("CREDENTIAL_UNAVAILABLE")
            sdk = OpenAI(api_key=key, base_url="https://api.openai.com/v1", max_retries=0,
                         timeout=ledger.config["timeout_seconds"])
        self.client = OpenAIChatClient(api_key=None, client=sdk, allow_parameter_retry=False,
            request_policy=ProviderRequestPolicy(temperature_policy="omit", max_tokens_param="max_completion_tokens"))

    def complete_json(self, request: LLMRequest):
        cfg = self.ledger.config
        if (request.provider, request.model) != (cfg["provider"], cfg["model"]):
            self.ledger.stop("REQUEST_IDENTITY_MISMATCH")
            raise BatchStopped("REQUEST_IDENTITY_MISMATCH")
        if request.max_tokens is not None and (type(request.max_tokens) is not int or not 0 < request.max_tokens <= cfg["max_output_tokens"]):
            self.ledger.stop("OUTPUT_TOKEN_LIMIT")
            raise BatchStopped("OUTPUT_TOKEN_LIMIT")
        wire = replace(request, temperature=None, max_tokens=request.max_tokens or cfg["max_output_tokens"],
                       timeout_sec=cfg["timeout_seconds"], reasoning_effort="medium")
        size = len(json.dumps(wire.messages, ensure_ascii=False, allow_nan=False).encode())
        with self.lock:
            request_id = self.ledger.reserve(self.scope, size)
            try:
                response = self.client.complete_json(wire)
            except Exception:
                self.ledger.stop("PROVIDER_ATTEMPT_FAILED")
                raise BatchStopped("PROVIDER_ATTEMPT_FAILED; no retry") from None
            cost = self.ledger.reconcile(request_id, response)
            return replace(response, cost_usd=cost, estimated_cost_usd=cost,
                           cost_source="conservative_batch_accounting")


def client_from_config(path, scope):
    cfg = json.loads(Path(path).read_text())
    if cfg["cost"].get("provider") == "openrouter":
        from .router_budget import client_from_config as router_client
        return router_client(path, scope)
    ledger = BatchBudget(Path(cfg["ledger_path"]), cfg["cost"])
    return BoundedOpenAIClient(ledger=ledger, scope=scope)
