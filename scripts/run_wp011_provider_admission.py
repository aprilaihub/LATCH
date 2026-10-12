#!/usr/bin/env python3
"""Bounded three-model OpenRouter admission for Protocol E1-v2.4."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from optimizer_pipeline.dof.openrouter import (
    OPENROUTER_ADAPTER_VERSION,
    OPENROUTER_E1_MODELS,
    OPENROUTER_E1_PROFILES,
    OpenRouterChatCompletionsTransport,
    build_openrouter_outbound,
)
from optimizer_pipeline.dof.provider import (
    DoFProviderObserver,
    ProviderAttemptStatus,
    ProviderRequestBinding,
    ProviderRunLedger,
    ProviderSettings,
    verify_provider_attempts,
)


SCHEMA = "research-flow.wp011-provider-admission.v1"
COST_CAP_USD = 0.50
PER_REQUEST_RESERVE_USD = 0.10


def _sha(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(raw).hexdigest()


def _write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _representative_context(model: str) -> bytes:
    actions = [
        {
            "action_key": f"candidate.{index:03d}",
            "mechanism": "representative_non_claim_context",
            "legal": True,
            "parameters": {"index": index, "bounded": True},
        }
        for index in range(72)
    ]
    return json.dumps(
        {
            "schema_version": "dof.wp011-provider-admission-context.v1",
            "purpose": "provider_and_output_boundary_only_no_backend_dispatch",
            "model": model,
            "decision": {
                "decision_id": "wp011-provider-admission",
                "stage": "ROUTED",
                "budget_remaining": {
                    "IMPLEMENTATION_HEAVY": 4,
                    "MUTATION_LIGHT": 8,
                    "LLM_REQUEST": 4,
                },
            },
            "actions": actions,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _binding(model: str) -> ProviderRequestBinding:
    context = _representative_context(model)
    prompt = (
        b"Return exactly one JSON object. For this non-claim provider admission, "
        b"return {\"schema_version\":\"action_proposal.v1\",\"proposal_id\":"
        b"\"wp011-admission\",\"decision_id\":\"wp011-provider-admission\","
        b"\"catalogue_version\":\"wp011-admission-catalogue.v1\","
        b"\"catalogue_hash\":\""
        + _sha("catalogue").encode()
        + b"\",\"kind\":\"STOP\",\"steps\":[]}. No prose or fences."
    )
    identity = model.replace("/", "-")
    return ProviderRequestBinding(
        run_id="wp011-admission-" + _sha(model)[:20],
        spec_hash=_sha("wp011-admission-spec:" + model),
        experiment_id="wp011-provider-admission",
        condition_id="wp011-" + identity,
        repeat_id="admission-1",
        decision_id="wp011-provider-admission",
        context_bytes=context,
        prompt_bytes=prompt,
        prompt_version="dof.wp011-admission-prompt.v1",
        action_interface_version="action_proposal.v1",
        action_interface_hash=_sha("action_proposal.v1"),
        context_interface_version="dof.wp011-admission-context.v1",
        context_interface_hash=_sha(context),
        catalogue_version="wp011-admission-catalogue.v1",
        catalogue_hash=_sha("catalogue"),
        capability_hash=_sha("stop-only-capability"),
        mask_evidence_hash=_sha("no-mask"),
        provider_role="wp011_non_claim_admission",
        provider="openrouter",
        model_id=model,
        adapter_version=OPENROUTER_ADAPTER_VERSION,
        settings=ProviderSettings(
            temperature=None,
            max_tokens=4096,
            seed=None,
            timeout_seconds=180,
            max_retries=0,
            retry_on=(),
            max_requests=1,
            max_usd=PER_REQUEST_RESERVE_USD,
            max_cost_per_request_usd=PER_REQUEST_RESERVE_USD,
            unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )


def run(output: Path, api_key: str) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    cost_lower = 0.0
    cost_upper = 0.0
    for model in OPENROUTER_E1_MODELS:
        binding = _binding(model)
        outbound = build_openrouter_outbound(binding)
        transport = OpenRouterChatCompletionsTransport(
            api_key=api_key,
            expected_outbound_binding=outbound,
        )
        decision = DoFProviderObserver(
            run_ledger=ProviderRunLedger.for_binding(binding)
        ).request(binding, transport)
        errors = verify_provider_attempts(decision.attempts, binding)
        if errors:
            raise RuntimeError("provider_attempt_evidence_invalid:" + ";".join(errors))
        if transport.send_attempts != 1 or transport.physical_requests != 1:
            raise RuntimeError("provider_admission_physical_request_count_invalid")
        exchange = transport.last_exchange or {}
        if exchange.get("failure_class") == "MODEL_OR_ROUTE_DRIFT":
            raise RuntimeError("provider_admission_model_or_route_drift")
        route = exchange.get("route")
        route_valid = bool(
            isinstance(route, dict)
            and route.get("requested_model") == model
            and route.get("returned_model") == model
            and route.get("selected_upstream_provider")
            == OPENROUTER_E1_PROFILES[model]["upstream_provider"]
            and route.get("fallback_allowed") is False
        )
        finish_reason = exchange.get("finish_reason")
        if model == "google/gemini-3.6-flash" and finish_reason == "length":
            raise RuntimeError("gemini_admission_truncated_at_4096")
        last = decision.attempts[-1]
        known = float(last.cost_usd or 0.0)
        reserved = 0.0 if last.cost_usd is not None else float(last.accounted_cost_usd)
        cost_lower += known
        cost_upper += known + reserved
        provider_error = exchange.get("provider_error")
        category = (
            provider_error.get("category")
            if isinstance(provider_error, dict)
            else None
        )
        if category in {
            "AUTHENTICATION_OR_KEY",
            "PAYMENT_OR_BUDGET",
            "PERMISSION_OR_WORKSPACE",
            "UNCLASSIFIED_4XX",
        }:
            raise RuntimeError("provider_admission_global_breaker:" + category)
        private_dir = output / "private" / model.replace("/", "__")
        _write_new(private_dir / "exchange.json", exchange)
        _write_new(private_dir / "attempt.json", last.to_dict())
        private_response = transport.private_response_bytes
        if isinstance(private_response, bytes):
            private_dir.mkdir(parents=True, exist_ok=True)
            with (private_dir / "error-response.bin").open("xb") as handle:
                handle.write(private_response)
                handle.flush()
                os.fsync(handle.fileno())
        rows.append(
            {
                "model": model,
                "required_endpoint_tag": OPENROUTER_E1_PROFILES[model]["endpoint_tag"],
                "attempt_status": last.status.value,
                "decision_endpoint": decision.endpoint,
                "accepted": decision.accepted,
                "physical_requests": transport.physical_requests,
                "route_valid": route_valid,
                "finish_reason": finish_reason,
                "provider_error_category": category,
                "response_sha256": exchange.get("raw_response_sha256"),
                "response_size_bytes": exchange.get("raw_response_size_bytes"),
                "input_tokens": last.input_tokens,
                "output_tokens": last.output_tokens,
                "total_tokens": last.total_tokens,
                "known_cost_usd": known,
                "reserved_unknown_cost_usd": reserved,
                "strict_complete_output": (
                    last.status
                    in {
                        ProviderAttemptStatus.SUCCEEDED,
                        ProviderAttemptStatus.POLICY_OUTPUT_REJECTED,
                    }
                    and finish_reason != "length"
                ),
                "retries": 0,
                "fallbacks": 0,
                "repairs": 0,
            }
        )
    if cost_upper > COST_CAP_USD + 1e-12:
        raise RuntimeError("provider_admission_cost_cap_exceeded")
    core = {
        "schema_version": SCHEMA,
        "models": rows,
        "model_count": len(rows),
        "physical_requests": sum(row["physical_requests"] for row in rows),
        "cost_lower_bound_usd": cost_lower,
        "cost_upper_bound_usd": cost_upper,
        "cost_cap_usd": COST_CAP_USD,
        "all_request_shapes_valid": all(
            row["physical_requests"] == 1 for row in rows
        ),
        "all_success_routes_valid": all(
            row["route_valid"]
            for row in rows
            if row["attempt_status"]
            in {"SUCCEEDED", "POLICY_OUTPUT_REJECTED"}
        ),
        "gemini_not_length_terminated": all(
            row["finish_reason"] != "length"
            for row in rows
            if row["model"] == "google/gemini-3.6-flash"
        ),
    }
    summary = core | {"summary_sha256": _sha(json.dumps(core, sort_keys=True, separators=(",", ":")))}
    _write_new(output / "provider-admission-public.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is required")
    print(json.dumps(run(args.output.resolve(), key), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
