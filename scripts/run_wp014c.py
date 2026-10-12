#!/usr/bin/env python3
"""WP-014C Action Surface v3 provider and Dual-Track controller."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import run_wp013a_e0 as base  # noqa: E402
import run_wp013b_e0 as synthetic  # noqa: E402
from optimizer_pipeline.dof.openrouter import (  # noqa: E402
    OPENROUTER_ADAPTER_VERSION,
    OPENROUTER_E1_PROFILES,
    OpenRouterChatCompletionsTransport,
    build_openrouter_outbound,
    discover_openrouter_endpoints,
    discover_openrouter_models,
)
from optimizer_pipeline.dof.provider import ProviderRequestBinding, ProviderSettings  # noqa: E402
from optimizer_pipeline.dof.single_submission.action_surface_v3 import M_ACTION_KEYS, registry_sha256  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v5 import (  # noqa: E402
    MAX_INPUT_BYTES,
    PLANNING_CONTEXT_V22,
    PROMPT_VERSION_V3,
)
from optimizer_pipeline.dof.single_submission.contract_v6 import (  # noqa: E402
    PLANNER_MESSAGE_V6,
    PROTOCOL_EPOCH,
    contract_for_cell,
    prompt_for_cell,
)
from optimizer_pipeline.dof.single_submission.observation_v4 import CommonQuerySurfaceV4  # noqa: E402
from optimizer_pipeline.dof.single_submission.planner import SystemBinding  # noqa: E402
from optimizer_pipeline.dof.single_submission.planner_v5 import SingleSubmissionSessionV5  # noqa: E402
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


SCHEMA = "dof.wp014c-dual-track-repeat1.v3"
GATE4_SCHEMA = "dof.wp014c-provider-contract-admission.v1"
MODELS = (
    "openai/gpt-5.6-luna",
    "google/gemini-3.6-flash",
    "anthropic/claude-sonnet-5",
)
MODEL_RESERVES = {
    "openai/gpt-5.6-luna": 0.08,
    "google/gemini-3.6-flash": 0.08,
    "anthropic/claude-sonnet-5": 0.20,
}
CASES = (
    "M_ONLY",
    "P_WHOLE_DESIGN",
    "P_TARGETED_SPATIAL",
    "G_SPATIAL",
    "G_TARGET_SPECIFIC",
)
CASE_CONDITION = {
    "M_ONLY": "M_ONLY",
    "P_WHOLE_DESIGN": "P_BOUNDARY",
    "P_TARGETED_SPATIAL": "P_BOUNDARY",
    "G_SPATIAL": "G_BOUNDARY",
    "G_TARGET_SPECIFIC": "G_BOUNDARY",
}
EXPECTED_LANGUAGE = {
    "M_ONLY": "M",
    "P_WHOLE_DESIGN": "P",
    "P_TARGETED_SPATIAL": "P",
    "G_SPATIAL": "G",
    "G_TARGET_SPECIFIC": "G",
}
OPENROUTER_COST_CAP_USD = 12.0


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write(path: Path, value: Any, *, private: bool = False) -> None:
    base.write_atomic(path, value, private=private)


def assert_clean() -> dict[str, str]:
    return base.assert_clean_repository()


def binding(
    *, session_id: str, model: str, context: bytes, prompt: bytes,
    catalogue_hash: str, reserve: float,
) -> ProviderRequestBinding:
    return ProviderRequestBinding(
        run_id="wp014c-" + digest(session_id)[:24],
        spec_hash=digest({"protocol": SCHEMA, "session": session_id}),
        experiment_id="wp014c-provider-contract-admission",
        condition_id="condition-" + digest(session_id)[:20],
        repeat_id="gate4", decision_id="decision-" + digest(session_id)[:20],
        context_bytes=context, prompt_bytes=prompt, prompt_version=PROMPT_VERSION_V3,
        action_interface_version=PLANNER_MESSAGE_V6,
        action_interface_hash=digest(PLANNER_MESSAGE_V6),
        context_interface_version=PLANNING_CONTEXT_V22,
        context_interface_hash=digest(context),
        catalogue_version=PROTOCOL_EPOCH + ".provider-admission",
        catalogue_hash=catalogue_hash, capability_hash=catalogue_hash,
        mask_evidence_hash=digest({"coverage_regime": "FULL_M8", "actions": M_ACTION_KEYS}),
        provider_role="wp014c_provider_contract_admission", provider="openrouter",
        model_id=model, adapter_version=OPENROUTER_ADAPTER_VERSION,
        settings=ProviderSettings(
            temperature=None, max_tokens=4096, seed=None, timeout_seconds=180,
            max_retries=0, retry_on=(), max_requests=1, max_usd=reserve,
            max_cost_per_request_usd=reserve, unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )


def candidate_for_case(case: str, context: Any) -> dict[str, Any]:
    region = context.predefined_regions[0]
    site = context.registry.objects(object_type="site")[0].handle
    if case == "M_ONLY":
        return {"language": "M", "action_key": "macro.phys_opt"}
    if case == "P_WHOLE_DESIGN":
        return {"language": "P", "steps": [
            {"step_id": "s1", "op": "UNROUTE", "args": {}},
            {"step_id": "s2", "op": "ROUTE", "args": {"directive": "Explore"}},
        ]}
    if case == "P_TARGETED_SPATIAL":
        return {"language": "P", "steps": [
            {"step_id": "s1", "op": "SELECT_CRITICAL_CELLS", "args": {"top_k": 1}, "bind": "targets"},
            {"step_id": "s2", "op": "CREATE_PBLOCK", "args": {"predefined_region_id": region}, "bind": "box"},
            {"step_id": "s3", "op": "ASSIGN", "args": {"target_handle": {"ref": "targets"}, "pblock_handle": {"ref": "box"}}},
            {"step_id": "s4", "op": "UNROUTE", "args": {}},
            {"step_id": "s5", "op": "UNPLACE", "args": {}},
            {"step_id": "s6", "op": "SET_SITE_PROHIBIT", "args": {"target_handle": site, "value": True}},
            {"step_id": "s7", "op": "PLACE", "args": {"directive": "Default"}},
            {"step_id": "s8", "op": "ROUTE", "args": {"directive": "Default"}},
        ]}
    if case == "G_SPATIAL":
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "cell", "predicate": {"op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True}, "top_k": 1}, "bind": "targets"},
            {"step_id": "s2", "op": "CREATE_REGION", "args": {"x_min": 0, "x_max": 0, "y_min": 0, "y_max": 0}, "bind": "region"},
            {"step_id": "s3", "op": "ASSIGN_REGION", "args": {"target_handle": {"ref": "targets"}, "region_handle": {"ref": "region"}}},
            {"step_id": "s4", "op": "UNROUTE", "args": {}},
            {"step_id": "s5", "op": "UNPLACE", "args": {}},
            {"step_id": "s6", "op": "PLACE", "args": {"directive": "Default"}},
            {"step_id": "s7", "op": "ROUTE", "args": {"directive": "Default"}},
        ]}
    if case == "G_TARGET_SPECIFIC":
        return {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "net", "predicate": {"op": "COMPARE", "field": "fanout", "operator": "GE", "value": 2}, "top_k": 1}, "bind": "target"},
            {"step_id": "s2", "op": "ROUTE_NET", "args": {"typed_net": {"ref": "target"}, "delay_mode": "AUTO_DELAY"}},
        ]}
    raise ValueError("provider_admission_case_unknown")


def case_is_satisfied(case: str, candidate: Mapping[str, Any]) -> bool:
    if candidate.get("language") != EXPECTED_LANGUAGE[case]:
        return False
    if case == "M_ONLY":
        return candidate.get("action_key") in M_ACTION_KEYS
    steps = candidate.get("steps")
    if not isinstance(steps, list):
        return False
    operations = {row.get("op") for row in steps if isinstance(row, Mapping)}
    if case == "P_WHOLE_DESIGN":
        return bool(operations & {"PLACE", "ROUTE", "PHYS_OPT"})
    if case == "P_TARGETED_SPATIAL":
        return bool(operations & {"ROUTE_NET", "REPLICATE_NET", "REPLICATE_DRIVER", "CREATE_PBLOCK", "ASSIGN", "RELOCATE_HARDBLOCK", "SET_SITE_PROHIBIT"})
    if case == "G_SPATIAL":
        return "CREATE_REGION" in operations and "ASSIGN_REGION" in operations
    return "SELECT_WHERE" in operations and bool(operations & {"ROUTE_NET", "REPLICATE_NET", "REPLICATE_DRIVER", "PLACE_CELL", "RELOCATE_HARDBLOCK"})


async def preflight(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    repository = assert_clean()
    models, endpoints = await asyncio.gather(
        discover_openrouter_models(key), discover_openrouter_endpoints(key)
    )
    selected = {model: endpoints["models"][model] for model in MODELS}
    required = MAX_INPUT_BYTES + 4096
    context = {
        model: isinstance(selected[model].get("context_length"), int)
        and selected[model]["context_length"] >= required for model in MODELS
    }
    core = {
        "schema_version": "dof.wp014c-openrouter-preflight.v1",
        "repository": repository,
        "models": {model: models["required_models"][model] for model in MODELS},
        "endpoints": selected,
        "expected_profiles": {model: dict(OPENROUTER_E1_PROFILES[model]) for model in MODELS},
        "context_capacity_checks": context,
        "required_context_tokens": required,
        "credential_included": False,
    }
    core["accepted"] = bool(
        all(core["models"].values())
        and all(row["endpoint_available"] and row["required_parameters_supported"] for row in selected.values())
        and all(context.values())
    )
    value = core | {"preflight_sha256": digest(core)}
    write(args.output, value)
    print(json.dumps({"accepted": value["accepted"], "context": context}, sort_keys=True))
    return 0 if value["accepted"] else 2


async def provider_admission(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    preflight_value = read(args.preflight)
    if not preflight_value.get("accepted"):
        raise SystemExit("provider_preflight_not_accepted")
    repository = assert_clean()
    if preflight_value.get("repository") != repository:
        raise SystemExit("provider_preflight_repository_binding_failed")
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    cost = base.SharedCost(OPENROUTER_COST_CAP_USD)

    async def one(model: str, case: str) -> dict[str, Any]:
        session_id = f"wp014c-gate4__{case.lower()}__{model.replace('/', '_')}"
        observation, registry, compile_context, _max_q = synthetic._synthetic_state(session_id)
        compile_context = replace(compile_context, fanout_admitted=True, action_surface_version="v3")
        condition = CASE_CONDITION[case]
        level = {"M_ONLY": "L2", "P_BOUNDARY": "L3", "G_BOUNDARY": "L4"}[condition]
        contract = contract_for_cell(
            study_track="C_CONDITIONED", coverage_regime="FULL_M8",
            language_condition=condition, level="C",
        )
        target_candidate = candidate_for_case(case, compile_context)
        catalogue_hash = digest(contract)
        system = SystemBinding.create(
            observation_hash=observation.sha256,
            root_binding=registry.trial_binding_hash,
            catalogue_version=PROTOCOL_EPOCH + ".provider-admission",
            catalogue_hash=catalogue_hash, provider="openrouter", model=model,
            route=OPENROUTER_E1_PROFILES[model]["endpoint_tag"], session_nonce=session_id,
        )
        session = SingleSubmissionSessionV5(
            level=level, observation=observation, queries=CommonQuerySurfaceV4(registry),
            compile_context=compile_context, binding=system,
            query_provider=lambda _name, _top_k: ((), (), 0.0, "provider-admission"),
            language_condition=condition,
        )
        context_value = {
            "schema_version": PLANNING_CONTEXT_V22,
            "technical_non_scientific": True,
            "backend_dispatch_permitted": False,
            "protocol_epoch": PROTOCOL_EPOCH,
            "target_case": case,
            "language_condition": condition,
            "common_observation": dict(observation.payload),
            "common_observation_sha256": observation.sha256,
            "common_query_catalogue": session.queries.catalogue,
            "language_contract": contract,
            "admission_target_example": {"kind": "COMMIT", "candidate": target_candidate},
            **session.public_status(),
            "qor_feedback_present": False,
        }
        context_bytes = canonical_json_bytes(context_value)
        prompt = prompt_for_cell(
            study_track="C_CONDITIONED", language_condition=condition, level="C",
        ) + (
            " Provider-only technical admission: no backend will run. Return the exact "
            "admission_target_example object from the user context; output JSON only."
        ).encode()
        total_input = len(context_bytes) + len(prompt)
        reserve = MODEL_RESERVES[model]
        if total_input > MAX_INPUT_BYTES or not await cost.acquire(reserve):
            return {
                "session_id": session_id, "model": model, "target_case": case,
                "language_condition": condition, "transport_exact": False,
                "candidate_admitted": False, "canonical_terminal": "INPUT_OR_COST_GUARD",
                "error": "input_or_cost_guard", "physical_requests": 0,
                "cost_usd": 0.0, "total_input_size_bytes": total_input,
                "backend_dispatches": 0,
            }
        request = binding(
            session_id=session_id, model=model, context=context_bytes, prompt=prompt,
            catalogue_hash=catalogue_hash, reserve=reserve,
        )
        transport = OpenRouterChatCompletionsTransport(
            api_key=key, expected_outbound_binding=build_openrouter_outbound(request)
        )
        response = None
        error = None
        message = None
        result = None
        started = time.monotonic()
        try:
            response = await transport.send(request, 0)
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
        actual = float(response.cost_usd or 0.0) if response else float(
            (transport.reconciled_accounting or {}).get("cost_usd", 0.0)
        )
        await cost.settle(reserve, actual)
        write(args.out / "private" / session_id / "exchange.json", transport.last_exchange or {}, private=True)
        if response is not None and error is None:
            try:
                message = base.strict_json_object(response.content_bytes)
                result = session.accept(message)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
        exchange = transport.last_exchange or {}
        route = exchange.get("route") or {}
        transport_exact = bool(
            response is not None
            and response.returned_model_id == model
            and response.returned_model_revision == OPENROUTER_E1_PROFILES[model]["upstream_provider"]
            and route.get("fallback_allowed") is False
            and route.get("attempt") == 1
            and transport.physical_requests == 1
        )
        candidate = message.get("candidate") if isinstance(message, Mapping) and isinstance(message.get("candidate"), Mapping) else {}
        candidate_admitted = bool(
            transport_exact and error is None and result is not None
            and result.status == "COMMITTED" and result.compiled is not None
            and result.compiled.language == EXPECTED_LANGUAGE[case]
            and case_is_satisfied(case, candidate)
        )
        terminal = "ADMITTED_COMMIT" if candidate_admitted else (
            "TRANSPORT_OR_ROUTE_FAILURE" if not transport_exact else "INVALID_POLICY_OUTPUT"
        )
        row = {
            "session_id": session_id, "model": model, "target_case": case,
            "language_condition": condition, "expected_language": EXPECTED_LANGUAGE[case],
            "transport_exact": transport_exact, "candidate_admitted": candidate_admitted,
            "canonical_terminal": terminal, "error": error,
            "message_sha256": digest(response.content_bytes) if response else None,
            "planner_result": None if result is None else base._turn_public(result),
            "physical_requests": transport.physical_requests,
            "retry_count": 0, "fallback_count": 0, "repair_count": 0,
            "backend_dispatches": 0, "cost_usd": actual,
            "returned_model": response.returned_model_id if response else None,
            "returned_upstream": response.returned_model_revision if response else None,
            "route": route or None,
            "context_size_bytes": len(context_bytes), "prompt_size_bytes": len(prompt),
            "total_input_size_bytes": total_input,
            "contract_hash": catalogue_hash, "wall_seconds": time.monotonic() - started,
        }
        write(args.out / "sessions" / session_id / "result.json", row)
        return row

    rows = await asyncio.gather(*(one(model, case) for model in MODELS for case in CASES))
    admitted_by_case = {
        case: sum(row["candidate_admitted"] for row in rows if row["target_case"] == case)
        for case in CASES
    }
    terminals = {
        terminal: sum(row["canonical_terminal"] == terminal for row in rows)
        for terminal in sorted({row["canonical_terminal"] for row in rows})
    }
    core = {
        "schema_version": GATE4_SCHEMA,
        "technical_non_scientific": True,
        "repository": repository,
        "action_surface_registry_sha256": registry_sha256(),
        "sessions": rows,
        "session_count": len(rows),
        "physical_requests": sum(row["physical_requests"] for row in rows),
        "known_cost_usd": cost.spent, "cost_cap_usd": cost.cap,
        "retry_count": 0, "fallback_count": 0, "repair_count": 0,
        "backend_dispatches": 0, "sealed_holdout_accesses": 0,
        "admitted_by_case": admitted_by_case,
        "terminal_counts": terminals,
        "maximum_total_input_bytes": max(row["total_input_size_bytes"] for row in rows),
        "input_ceiling_bytes": MAX_INPUT_BYTES,
    }
    core["accepted"] = bool(
        len(rows) == 15
        and core["physical_requests"] == 15
        and all(row["transport_exact"] for row in rows)
        and all(count >= 2 for count in admitted_by_case.values())
        and core["known_cost_usd"] <= OPENROUTER_COST_CAP_USD
        and core["maximum_total_input_bytes"] <= MAX_INPUT_BYTES
        and core["backend_dispatches"] == 0
    )
    value = core | {"aggregate_sha256": digest(core)}
    write(args.out / "provider-admission.json", value)
    write(args.out / "resource-ledger.json", {
        "schema_version": "dof.wp014c-resource-ledger.v1",
        "cost_usd": cost.spent, "heavy_calls": 0,
        "queue_wall_seconds": 0.0, "completed_phases": ["GATE4_PROVIDER_ADMISSION"],
    })
    print(json.dumps({
        "accepted": value["accepted"], "admitted_by_case": admitted_by_case,
        "terminal_counts": terminals, "cost_usd": cost.spent,
        "maximum_total_input_bytes": core["maximum_total_input_bytes"],
    }, sort_keys=True))
    return 0 if value["accepted"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    commands = value.add_subparsers(dest="command", required=True)
    p = commands.add_parser("preflight")
    p.add_argument("--output", type=Path, required=True)
    a = commands.add_parser("provider-admission")
    a.add_argument("--preflight", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    if args.command == "preflight":
        return asyncio.run(preflight(args))
    if args.command == "provider-admission":
        return asyncio.run(provider_admission(args))
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
