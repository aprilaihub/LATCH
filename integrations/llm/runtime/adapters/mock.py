"""Mock LLM client for tests and harness smoke runs."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from integrations.llm.runtime.client import LLMClient
from integrations.llm.runtime.pricing import PricingProfile
from integrations.llm.runtime.types import LLMRequest, LLMResponse


DEFAULT_MOCK_PAYLOAD: Dict[str, Any] = {
    "decision_type": "expand_node",
    "target_node_id": "node_000_root",
    "candidates": [
        {
            "target_node_id": "node_000_root",
            "flow": "phys_opt",
            "priority": 0.9,
            "expected_effect": "Mock frontier LLM candidate.",
            "risk": "medium",
            "confidence": "high",
        }
    ],
    "prune_node_ids": [],
    "stop": False,
    "reason": "Mock LLM response.",
}


class MockLLMClient(LLMClient):
    def __init__(
        self,
        payload: Optional[Dict[str, Any]] = None,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        pricing_profile: Optional[PricingProfile] = None,
    ) -> None:
        self.payload = payload
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.pricing_profile = pricing_profile
        self.requests: list[LLMRequest] = []

    def complete_json(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        payload = self.payload or payload_for_profile(request)
        if payload is None:
            content = "not valid JSON from mock_malformed_json"
            parsed_json = None
        else:
            content = json.dumps(payload, sort_keys=True)
            parsed_json = dict(payload)
        total_tokens = self.prompt_tokens + self.completion_tokens
        estimated_cost = self.pricing_profile.estimate_cost(prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens) if self.pricing_profile is not None else None
        return LLMResponse(
            content=content,
            parsed_json=parsed_json,
            provider=request.provider,
            model=request.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=total_tokens,
            cost_usd=estimated_cost,
            provider_reported_cost_usd=None,
            estimated_cost_usd=estimated_cost,
            cost_source="estimated_pricing_profile" if estimated_cost is not None else "unknown",
            pricing_profile_id=self.pricing_profile.name if self.pricing_profile is not None else None,
            latency_sec=0.0,
            raw_response={"mock": True, "profile": request.model},
        )


def payload_for_profile(request: LLMRequest) -> Optional[Dict[str, Any]]:
    profile = str(request.model or "mock_good")
    context = _compact_request_from_messages(request)
    if profile == "mock_malformed_json":
        return None
    if profile == "mock_bad_target":
        return _bad_target_payload(context)
    if profile == "mock_bad_flow":
        return _bad_flow_payload(context)
    if profile == "mock_noisy":
        if context.get("previous_invalid_decisions"):
            return _good_payload(context, reason="Mock noisy repaired after feedback.")
        if _stable_bucket(context, profile) % 3 == 0:
            return _bad_flow_payload(context, reason="Mock noisy emitted an invalid flow.")
        if _stable_bucket(context, profile) % 3 == 1:
            return _bad_target_payload(context, reason="Mock noisy emitted an invalid target.")
        return _good_payload(context, reason="Mock noisy emitted a valid candidate.")
    if profile in {"mock_good", "mock_model"}:
        return _good_payload(context)
    return _good_payload(context) if context else dict(DEFAULT_MOCK_PAYLOAD)


def _good_payload(context: Dict[str, Any], *, reason: str = "Mock LLM selected a valid frontier candidate.") -> Dict[str, Any]:
    candidates = _valid_candidates(context)
    return {
        "decision_type": "expansion_plan" if len(candidates) > 1 else "expand_node",
        "target_node_id": candidates[0]["target_node_id"] if candidates else _first_frontier_id(context),
        "candidates": candidates,
        "prune_node_ids": [],
        "stop": not bool(candidates),
        "reason": reason if candidates else "Mock LLM found no legal candidate.",
    }


def _bad_target_payload(context: Dict[str, Any], *, reason: str = "Mock LLM intentionally emitted an invalid target.") -> Dict[str, Any]:
    flow = _first_allowed_flow(context) or "phys_opt"
    return {
        "decision_type": "expand_node",
        "target_node_id": "mock_missing_frontier_node",
        "candidates": [_candidate("mock_missing_frontier_node", flow, priority=0.9, expected_effect="Intentional invalid target.")],
        "prune_node_ids": [],
        "stop": False,
        "reason": reason,
    }


def _bad_flow_payload(context: Dict[str, Any], *, reason: str = "Mock LLM intentionally emitted an invalid flow.") -> Dict[str, Any]:
    target = _first_frontier_id(context) or "node_000_root"
    return {
        "decision_type": "expand_node",
        "target_node_id": target,
        "candidates": [_candidate(target, "mock_unknown_flow", priority=0.9, expected_effect="Intentional invalid flow.")],
        "prune_node_ids": [],
        "stop": False,
        "reason": reason,
    }


def _valid_candidates(context: Dict[str, Any]) -> list[Dict[str, Any]]:
    allowed = [str(flow) for flow in (context.get("allowed_flows") or []) if flow]
    frontier = context.get("frontier") if isinstance(context.get("frontier"), list) else []
    frontier_ids = context.get("frontier_node_ids") if isinstance(context.get("frontier_node_ids"), list) else []
    batch_size = int(context.get("batch_size") or context.get("branching_factor") or 1)
    existing_sequences = {tuple(seq) for seq in (context.get("existing_sequences") or []) if isinstance(seq, list)}
    candidates: list[Dict[str, Any]] = []
    for item in frontier or [{"node_id": node_id, "flow_sequence": []} for node_id in frontier_ids]:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "")
        if not node_id:
            continue
        sequence = list(item.get("flow_sequence") or [])
        last_flow = sequence[-1] if sequence else None
        for flow in allowed:
            if flow == last_flow:
                continue
            if tuple(sequence + [flow]) in existing_sequences:
                continue
            candidates.append(_candidate(node_id, flow))
            break
        if len(candidates) >= max(1, batch_size):
            break
    if not candidates and not context:
        return list(DEFAULT_MOCK_PAYLOAD["candidates"])
    return candidates


def _candidate(target_node_id: str, flow: str, *, priority: float = 0.9, expected_effect: str = "Mock frontier LLM candidate.") -> Dict[str, Any]:
    return {
        "target_node_id": target_node_id,
        "flow": flow,
        "priority": priority,
        "expected_effect": expected_effect,
        "risk": "medium",
        "confidence": "high",
    }


def _compact_request_from_messages(request: LLMRequest) -> Dict[str, Any]:
    for message in reversed(request.messages or []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        marker = "High-value search context:\n"
        if marker in content:
            raw = content.split(marker, 1)[1].strip()
            try:
                payload = json.loads(raw)
                return payload if isinstance(payload, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _first_frontier_id(context: Dict[str, Any]) -> Optional[str]:
    frontier_ids = context.get("frontier_node_ids") if isinstance(context.get("frontier_node_ids"), list) else []
    if frontier_ids:
        return str(frontier_ids[0])
    frontier = context.get("frontier") if isinstance(context.get("frontier"), list) else []
    for item in frontier:
        if isinstance(item, dict) and item.get("node_id"):
            return str(item.get("node_id"))
    return None


def _first_allowed_flow(context: Dict[str, Any]) -> Optional[str]:
    allowed = context.get("allowed_flows") if isinstance(context.get("allowed_flows"), list) else []
    return str(allowed[0]) if allowed else None


def _stable_bucket(context: Dict[str, Any], profile: str) -> int:
    basis = json.dumps({"profile": profile, "frontier_node_ids": context.get("frontier_node_ids"), "previous_invalid_decisions": context.get("previous_invalid_decisions")}, sort_keys=True)
    return int(hashlib.sha256(basis.encode("utf-8")).hexdigest()[:8], 16)
