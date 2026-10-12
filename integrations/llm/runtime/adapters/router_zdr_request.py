"""Explicit OpenRouter request envelope; transport and accounting stay external.

Uses the shared LLMRequest carrier. This opt-in helper never discovers keys,
selects another model, calls the network or supplies study reasoning defaults.
"""
from copy import deepcopy
import math


def build_zdr_body(request, *, endpoint, max_input_bytes, input_rate, output_rate):
    """Build only for an endpoint verified by the caller against the ZDR catalog.

    Prices are per million tokens; the caller must cover all applicable tiers
    and reserve through its shared ledger before transmitting this body.
    """
    if request.provider != "openrouter" or request.model != endpoint.get("model_id"):
        raise ValueError("Explicit OpenRouter model/endpoint mismatch")
    if endpoint.get("zdr_verified") is not True or not endpoint.get("tag"):
        raise ValueError("A currently verified ZDR endpoint is required")
    if request.temperature is not None:
        raise ValueError("This bounded envelope does not send temperature")
    if type(request.max_tokens) is not int or request.max_tokens <= 0:
        raise ValueError("Explicit completion cap required")
    if not isinstance(request.timeout_sec, (int, float)) or not 0 < request.timeout_sec <= 300:
        raise ValueError("Explicit bounded timeout required")
    if request.reasoning_effort not in {"low", "medium", "high"}:
        raise ValueError("Study must explicitly select reasoning effort")
    if type(max_input_bytes) is not int or max_input_bytes <= 0:
        raise ValueError("Explicit input bound required")
    for rate in (input_rate, output_rate):
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
            raise ValueError("Current conservative prices required")
    messages = deepcopy(request.messages)
    if not messages or any(not isinstance(m, dict) or not isinstance(m.get("content"), str) for m in messages):
        raise ValueError("Text messages required; no implicit conversion")
    if sum(len(m["content"].encode("utf-8")) for m in messages) > max_input_bytes:
        raise ValueError("Input exceeds bound; messages are never truncated")
    supported = set(endpoint.get("supported_parameters", []))
    token_key = "max_completion_tokens" if "max_completion_tokens" in supported else "max_tokens"
    required = {token_key, "reasoning"}
    if request.response_format == "json_object":
        required.add("response_format")
    elif request.response_format not in {None, "text"}:
        raise ValueError("Unsupported response format")
    if not required <= supported:
        raise ValueError("Endpoint lacks required parameters; no silent downgrade")
    body = dict(model=request.model, messages=messages, stream=False,
        reasoning={"effort": request.reasoning_effort},
        provider=dict(only=[endpoint["tag"]], order=[endpoint["tag"]],
            allow_fallbacks=False, require_parameters=True, data_collection="deny", zdr=True,
            max_price=dict(prompt=input_rate, completion=output_rate)),
        usage={"include": True}, plugins=[{"id": "response-healing", "enabled": False},
            {"id": "context-compression", "enabled": False}])
    body[token_key] = request.max_tokens
    if request.response_format == "json_object":
        body["response_format"] = {"type": "json_object"}
    return body
