"""Bounded planning over shared LLM clients, using the WP015 language contract."""
from __future__ import annotations
import json
import math
from .planning_request import planning_messages
from integrations.llm.runtime.types import LLMRequest, RequestNotSent


def validate_cost_config(config, provider, model):
    if (config.get("provider"), config.get("model")) != (provider, model):
        raise ValueError("Price evidence must identify the exact provider and model")
    if not config.get("price_source") or not config.get("verified_at") or config.get("verified_current") is not True:
        raise ValueError("Current price evidence is required; historical snapshots are not a current budget")
    for key in ("input_usd_per_million", "output_usd_per_million", "accounted_budget_usd", "timeout_seconds"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Explicit positive cost configuration required: " + key)
    for key in ("max_input_bytes", "max_output_tokens", "max_requests"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError("Explicit positive integer cost configuration required: " + key)
    if config["max_requests"] > 4:
        raise ValueError("WP015 permits at most four planning requests per trial")
    # These are conservative accounted reservations, not a provider-enforced
    # billing ceiling. Rates must cover cache creation and billed reasoning.
    if config.get("rates_cover_all_billed_tokens") is not True:
        raise ValueError("Price evidence must cover all billed token categories")
    return config


class ModelPlanner:
    def __init__(self, *, condition, provider, model, cost_config, client=None):
        self.config = validate_cost_config(cost_config, provider, model)
        self.condition, self.provider, self.model = condition, provider, model
        if client is None:
            from integrations.llm.runtime.no_retry import build_one_attempt_client
            client = build_one_attempt_client(provider=provider, model=model,
                                              timeout_seconds=self.config["timeout_seconds"])
        self.client = client
        self.records = []
        self.accounted_usd = 0.0

    def __call__(self, *, session, previous_result):
        if session.terminal is not None or not session.turns_remaining:
            raise RuntimeError("Planning prohibited after terminal or exhausted turn budget")
        cfg = self.config
        if len(self.records) >= cfg["max_requests"]:
            raise RuntimeError("Approved provider request limit exhausted")
        messages = planning_messages(session=session, condition=self.condition, previous_result=previous_result)
        byte_count = sum(len(message["content"].encode()) for message in messages)
        if byte_count > cfg["max_input_bytes"]:
            raise RuntimeError("Approved planning input limit exceeded")
        reservation = (cfg["max_input_bytes"] * cfg["input_usd_per_million"] +
                       cfg["max_output_tokens"] * cfg["output_usd_per_million"]) / 1e6
        if self.accounted_usd + reservation > cfg["accounted_budget_usd"]:
            raise RuntimeError("Accounted budget cannot reserve another request")
        record = {"request": len(self.records) + 1, "status": "STARTED", "reserved_usd": reservation}
        self.records.append(record)
        self.accounted_usd += reservation
        try:
            response = self.client.complete_json(LLMRequest(provider=self.provider, model=self.model,
                messages=messages,
                temperature=None, max_tokens=cfg["max_output_tokens"], timeout_sec=cfg["timeout_seconds"],
                reasoning_effort=cfg.get("reasoning_effort")))
        except RequestNotSent:
            # This adapter assertion applies only to this local reservation;
            # it cannot release uncertain provider or shared-ledger charges.
            self.accounted_usd -= reservation
            record.update(status="NOT_SENT", network_attempted=False,
                          reserved_usd=0.0, accounted_usd=0.0)
            raise RequestNotSent("Planning request stopped before provider transport") from None
        except Exception as exc:
            record.update(status="PROVIDER_FAILURE", error_type=type(exc).__name__)
            raise RuntimeError("Provider attempt failed; no automatic retry or substitution") from None
        if (response.provider, response.model) != (self.provider, self.model):
            record["status"] = "PROVIDER_IDENTITY_MISMATCH"
            raise RuntimeError("Provider/model response identity differs from approved request")
        counts = (response.prompt_tokens, response.completion_tokens)
        if any(type(v) is not int or v < 0 for v in counts):
            record["status"] = "USAGE_UNKNOWN"
            raise RuntimeError("Provider usage missing; reserved cost retained and trial stopped")
        measured = (counts[0] * cfg["input_usd_per_million"] + counts[1] * cfg["output_usd_per_million"]) / 1e6
        self.accounted_usd += measured - reservation
        record.update(status="RECEIVED", prompt_tokens=counts[0], completion_tokens=counts[1], accounted_usd=measured)
        if self.accounted_usd > cfg["accounted_budget_usd"]:
            record["status"] = "ACCOUNTED_BUDGET_EXCEEDED"
            raise RuntimeError("Accounted budget exceeded; no further planning")
        # Invalid JSON is a protocol trial result, not an instruction to retry.
        return response.parsed_json if isinstance(response.parsed_json, dict) else {"kind": "INVALID_JSON"}

    def accounting(self):
        return {"provider": self.provider, "model": self.model, "requests": list(self.records),
                "accounted_usd": self.accounted_usd, "hard_billing_cap": False,
                "price_source": self.config["price_source"], "verified_at": self.config["verified_at"]}
