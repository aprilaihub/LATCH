#!/usr/bin/env python3
"""WP-013B representation-only overlay and development E0 controller.

Execution/scoring delegate to the accepted WP-013A controller.  This module
binds only the v3 public contract, v2.2 context identity, bounded Q surface,
fresh namespaces, and the declared development Macro challenge.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import run_wp013a_e0 as base  # noqa: E402
from optimizer_pipeline.dof.openrouter import (  # noqa: E402
    OPENROUTER_ADAPTER_VERSION, OPENROUTER_E1_PROFILES,
    OpenRouterChatCompletionsTransport, build_openrouter_outbound,
    discover_openrouter_endpoints, discover_openrouter_models,
)
from optimizer_pipeline.dof.provider import ProviderRequestBinding, ProviderSettings  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v3 import (  # noqa: E402
    COMMON_OBJECTIVE, G_CARDS, M_ACTION_KEYS, M_CARDS, MAX_INPUT_BYTES,
    PLANNER_MESSAGE_V3, PLANNING_CONTEXT_V22, PROMPT_VERSION_V22, P_CARDS, QUERY_NAMES,
    SYNTAX_EXAMPLES, compile_candidate_v3, contract_for_level, contract_hash,
)
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry  # noqa: E402
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.single_submission.observation import build_common_observation  # noqa: E402
from optimizer_pipeline.dof.single_submission.observation_v3 import CommonQuerySurfaceV3  # noqa: E402
from optimizer_pipeline.dof.single_submission.planner_v3 import SingleSubmissionSessionV3  # noqa: E402
from optimizer_pipeline.dof.single_submission.planner import SystemBinding  # noqa: E402
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


SCHEMA = "dof.wp013b-development-e0-v5.v1"
MODELS = ("openai/gpt-5.6-luna", "anthropic/claude-sonnet-5")
ADMISSION_CASES = (
    ("L2", "M"),
    ("L3", "P_EXCLUSIVE"),
    ("L4", "G_SPATIAL_EXCLUSIVE"),
    ("L4", "G_HARDBLOCK_EXCLUSIVE"),
)


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def prompt(level: str) -> bytes:
    return (
        "Return exactly one JSON object and no prose or markdown. Follow the exact closed "
        "schemas and semantic cards in language_contract. " + COMMON_OBJECTIVE + " "
        "QUERY is {\"kind\":\"QUERY\",\"queries\":[{\"name\":QUERY_NAME,\"top_k\":INTEGER}]}. "
        "Candidate messages are {\"kind\":\"DRAFT\"|\"VALIDATE\"|\"COMMIT\",\"candidate\":CANDIDATE}. "
        "Terminal messages are {\"kind\":\"STOP\"} or {\"kind\":\"NO_VALID_SUBMISSION\"}. "
        "On final_submission_turn only use an allowed kind. Never include system-owned fields. "
        "Do not use paths, object names, Tcl, shell, files, network, Python, loops, retries, or hidden calls. "
        "Construction feedback may be repaired before COMMIT; no QoR is available before COMMIT. "
        f"The active level is {level}."
    ).encode()


def binding(
    *, job: Mapping[str, Any], model: str, turn: int, context: bytes,
    prompt: bytes, catalogue_hash: str, reserve: float, phase: str = "e0",
) -> ProviderRequestBinding:
    identity = f"{job['job_id']}:{turn}"
    return ProviderRequestBinding(
        run_id=f"wp013b-{phase}-" + digest(identity)[:24],
        spec_hash=digest({"protocol": SCHEMA, "job": job}),
        experiment_id="wp013b-development-e0-v5" if phase == "e0" else "wp013b-provider-language-admission-v1",
        condition_id="condition-" + digest(job["job_id"])[:20], repeat_id="development-e0-1",
        decision_id="decision-" + digest(job["job_id"])[:20], context_bytes=context,
        prompt_bytes=prompt, prompt_version=PROMPT_VERSION_V22,
        action_interface_version=PLANNER_MESSAGE_V3, action_interface_hash=digest(PLANNER_MESSAGE_V3),
        context_interface_version=PLANNING_CONTEXT_V22, context_interface_hash=digest(context),
        catalogue_version=f"dof.wp013b.{job['level'].lower()}.v3", catalogue_hash=catalogue_hash,
        capability_hash=catalogue_hash, mask_evidence_hash=digest("development-only-no-sealed-holdout"),
        provider_role="wp013b_single_submission_planner" if phase == "e0" else "wp013b_provider_language_admission",
        provider="openrouter", model_id=model, adapter_version=OPENROUTER_ADAPTER_VERSION,
        settings=ProviderSettings(
            temperature=None, max_tokens=4096, seed=None, timeout_seconds=180,
            max_retries=0, retry_on=(), max_requests=1, max_usd=reserve,
            max_cost_per_request_usd=reserve, unknown_cost_policy="reject", unknown_usage_policy="reject",
        ),
    )


def planner_context(*, observation: Any, session: Any, contract: Mapping[str, Any], last_result: Mapping[str, Any] | None) -> bytes:
    return canonical_json_bytes({
        "schema_version": PLANNING_CONTEXT_V22,
        "common_objective": COMMON_OBJECTIVE,
        "common_observation": dict(observation.payload),
        "common_observation_sha256": observation.sha256,
        "common_query_catalogue": session.queries.catalogue,
        "language_contract": contract,
        **session.public_status(),
        "previous_result": last_result,
        "qor_feedback_present": False,
    })


def contract_audit(_context: CompileContext) -> dict[str, Any]:
    contracts = {level: contract_for_level(level) for level in ("L2", "L3", "L4")}
    checks = {
        "m_count": len(M_CARDS) == 8,
        "p_runtime_count": len(P_CARDS) == 16,
        "g_runtime_count": len(G_CARDS) == 20,
        "m_inherited": contracts["L2"]["M"] == contracts["L3"]["M"] == contracts["L4"]["M"],
        "p_inherited": contracts["L3"]["P"] == contracts["L4"]["P"],
        "objective_equal": len({row["common_objective"] for row in contracts.values()}) == 1,
        "ceiling": MAX_INPUT_BYTES == 65_536,
    }
    value = {"schema_version": "dof.wp013b-contract-completeness.v1", "contract_hashes": {level: contract_hash(level) for level in contracts}, "checks": checks, "accepted": all(checks.values())}
    return value | {"audit_sha256": digest(value)}


def configure_base() -> None:
    base.SCHEMA = SCHEMA
    base.MODELS = MODELS
    base.MAX_INPUT_BYTES = MAX_INPUT_BYTES
    base.contract_for_level = contract_for_level
    base.contract_hash = contract_hash
    base.audit_contract_completeness = contract_audit
    base.CommonQuerySurfaceV2 = CommonQuerySurfaceV3
    base.SingleSubmissionSessionV2 = SingleSubmissionSessionV3
    base._prompt = prompt
    base._binding = binding
    base._planner_context = planner_context
    base.CAPTURE_SCHEMA = "dof.wp013b-e0-capture.v1"
    base.CAPTURE_JOB_SCHEMA = "dof.wp013b-observation-capture-job.v1"
    base.CAPTURE_JOB_PREFIX = "wp013b-e0-observe"
    base.HANDLE_NAMESPACE = "wp013b-e0-handles-v3"
    base.TRIAL_ID = "wp013b-e0-v5"
    base.QUERY_EVIDENCE_TAG = "wp013b-frozen-read-only-capture"
    base.EXPECTED_OPENROUTER_COST_CAP_USD = 1.25
    base.EXPECTED_HEAVY_CALL_CAP = 64
    base.EXPECTED_JOB_COUNT = 16


async def preflight(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    models, endpoints = await asyncio.gather(discover_openrouter_models(key), discover_openrouter_endpoints(key))
    selected = {model: endpoints["models"][model] for model in MODELS}
    required_context_tokens = MAX_INPUT_BYTES + 4096
    context_checks = {
        model: isinstance(row.get("context_length"), int) and row["context_length"] >= required_context_tokens
        for model, row in selected.items()
    }
    value = {
        "schema_version": "dof.wp013b-openrouter-preflight.v1",
        "models": {model: models["required_models"][model] for model in MODELS},
        "endpoints": selected,
        "exact_models_available": all(models["required_models"][model] for model in MODELS),
        "exact_endpoints_available": all(row["endpoint_available"] and row["required_parameters_supported"] for row in selected.values()),
        "input_ceiling_bytes": MAX_INPUT_BYTES,
        "pessimistic_required_context_tokens": required_context_tokens,
        "context_capacity_checks": context_checks,
        "expected_profiles": {model: dict(OPENROUTER_E1_PROFILES[model]) for model in MODELS},
        "credential_included": False,
        "cost_cap_usd": 1.25,
    }
    value["accepted"] = bool(value["exact_models_available"] and value["exact_endpoints_available"] and all(context_checks.values()))
    value["preflight_sha256"] = digest(value)
    base.write_atomic(args.output, value)
    print(json.dumps({"accepted": value["accepted"], "context_capacity_checks": context_checks}, sort_keys=True))
    return 0 if value["accepted"] else 2


def _synthetic_state(session_id: str) -> tuple[Any, TrialHandleRegistry, CompileContext, list[dict[str, Any]]]:
    root = hashlib.sha256(("wp013b-admission:" + session_id).encode()).hexdigest()
    observation = build_common_observation(
        root_pseudonym="root-" + root[:16], target_clock={"clock_period_ns": 4.0, "evidence": "synthetic-development-safe"},
        timing={"fmax_mhz": 250.0, "wns_ns": 0.0, "tns_ns": 0.0, "failing_endpoint_count": 0},
        utilization={"lut": 100, "ff": 100, "dsp": 16, "bram": 1, "uram": 0},
        implementation={"placement_state": "PLACED", "route_state": "ROUTED", "route_validity": "VALID"},
        constraints={"pblock_count": 0, "constrained_cell_count": 0},
        geometry={"device_class": "synthetic-development-safe", "clock_region_bounds": [0, 3, 0, 3]},
        congestion={"coarse_class": "unknown", "hotspot_count": 0}, critical_paths=[],
        budget={"planning_turns": 4, "queries": 8, "feedback_rounds": 3, "heavy_calls": 4, "light_mutations": 8},
    )
    registry = TrialHandleRegistry(trial_id=session_id, root_sha256=root, secret=hashlib.sha256((session_id + ":secret").encode()).digest())
    regions = [registry.register(object_type="region", canonical_identity=f"region-{i}", attributes={"bounds": [i % 4, i % 4, i // 4, i // 4], "count": 1, "row_semantics": "synthetic-development-safe"}) for i in range(16)]
    sites = [registry.register(object_type="site", canonical_identity=f"site-{i}", attributes={"site_class": "DSP", "compatible_resource_types": ["DSP"]}) for i in range(16)]
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in QUERY_NAMES}
    for i in range(16):
        start = registry.register(object_type="pin", canonical_identity=f"start-{i}", attributes={"row_semantics": "synthetic"})
        end = registry.register(object_type="pin", canonical_identity=f"end-{i}", attributes={"row_semantics": "synthetic"})
        rows["critical_path_details"].append({"canonical_identity": f"path-{i}", "handle_type": "path", "path_rank": i + 1, "slack_ns": -1.0 + i / 100, "delay_ns": 5.0 - i / 100, "startpoint_handle": start, "endpoint_handle": end, "row_semantics": "synthetic-development-safe"})
        rows["critical_object_locations"].append({"canonical_identity": f"cell-{i}", "handle_type": "cell", "path_member": True, "path_rank": i + 1, "resource_type": "LUT", "location_status": "PLACED", "location_site_handle": sites[i], "location_region_handle": regions[i]})
        rows["high_fanout_net_details"].append({"canonical_identity": f"net-{i}", "handle_type": "net", "fanout": 100 - i, "row_semantics": "synthetic-development-safe"})
        rows["critical_hardblock_candidates"].append({"canonical_identity": f"dsp-{i}", "handle_type": "hardblock", "path_member": True, "path_rank": i + 1, "resource_type": "DSP", "compatible_site_handles": sites, "row_semantics": "synthetic-development-safe"})
        rows["placement_summary"].append({"canonical_identity": f"region-{i}", "handle_type": "region", "bounds": [i % 4, i % 4, i // 4, i // 4], "count": 1, "row_semantics": "synthetic-development-safe"})
    rows["route_summary"] = [{"canonical_identity": "root-route", "handle_type": "net", "route_status": "ROUTED", "count": 1, "row_semantics": "synthetic-development-safe"}]
    surface = CommonQuerySurfaceV3(registry)
    schedule = [(name, 16) for name in QUERY_NAMES] + [("critical_path_details", 15), ("critical_hardblock_candidates", 15)]
    materialized = []
    for name, top_k in schedule:
        result = surface.materialize(query_name=name, top_k=top_k, private_rows=rows[name], tool_call_ids=("synthetic-provider-admission",), latency_seconds=0.0)
        materialized.append(result.public_payload())
    context = CompileContext(registry, "ROUTED", tuple(regions), (0, 3, 0, 3))
    return observation, registry, context, materialized


def _substituted_contract(level: str, context: CompileContext) -> dict[str, Any]:
    contract = contract_for_level(level)
    region = context.predefined_regions[0]
    site = context.registry.objects(object_type="site")[0].handle
    replacements: dict[str, Any] = {"<ADMITTED_ACTION_KEY>": "macro.phys_opt", "<PREDEFINED_REGION_HANDLE>": region, "<COMPATIBLE_SITE_HANDLE>": site, "<X_MIN>": 0, "<X_MAX>": 0, "<Y_MIN>": 0, "<Y_MAX>": 0}
    def walk(item: Any) -> Any:
        if isinstance(item, str): return replacements.get(item, item)
        if isinstance(item, list): return [walk(x) for x in item]
        if isinstance(item, dict): return {key: walk(value) for key, value in item.items()}
        return item
    contract["examples"] = walk(contract["examples"])
    return contract


async def admission(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key: raise SystemExit("OPENROUTER_API_KEY_missing")
    preflight_value = base.read_object(args.preflight)
    if not preflight_value.get("accepted"): raise SystemExit("provider_preflight_not_accepted")
    plan = base.read_object(args.plan)
    if args.out.exists(): raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    reserves = {row["model"]: float(row["per_request_reserve_usd"]) for row in plan["models"]}
    cost = base.SharedCost(1.25)

    async def one(model: str, level: str, case: str) -> dict[str, Any]:
        session_id = f"wp013b-admission__{case.lower()}__{model.replace('/', '_')}"
        observation, registry, compile_context, max_q = _synthetic_state(session_id)
        contract = _substituted_contract(level, compile_context)
        catalogue_hash = digest(contract)
        system = SystemBinding.create(observation_hash=observation.sha256, root_binding=compile_context.registry.trial_binding_hash, catalogue_version=f"dof.wp013b.{level.lower()}.v3", catalogue_hash=catalogue_hash, provider="openrouter", model=model, route=OPENROUTER_E1_PROFILES[model]["endpoint_tag"], session_nonce=session_id)
        session = SingleSubmissionSessionV3(level, observation, CommonQuerySurfaceV3(registry), compile_context, system, lambda _name, _top_k: ((), (), 0.0, "provider-admission"))
        context_value = {"schema_version": PLANNING_CONTEXT_V22, "technical_non_scientific": True, "target_case": case, "common_objective": COMMON_OBJECTIVE, "common_observation": dict(observation.payload), "common_query_catalogue": session.queries.catalogue, "language_contract": contract, **session.public_status()}
        if level == "L4": context_value["fully_materialized_max_q"] = max_q
        context_bytes = canonical_json_bytes(context_value)
        case_instruction = {"M": "Return the M syntax example as COMMIT.", "P_EXCLUSIVE": "Return the P_EXCLUSIVE syntax example as COMMIT; do not use CALL_MACRO.", "G_SPATIAL_EXCLUSIVE": "Return the G_SPATIAL_EXCLUSIVE syntax example as COMMIT; use generated predicate and geometry.", "G_HARDBLOCK_EXCLUSIVE": "Return the G_HARDBLOCK_EXCLUSIVE syntax example as COMMIT; use generated candidate_sites and site_index."}[case]
        prompt_bytes = prompt(level) + (" Provider-only targeted syntax admission; no backend will run. " + case_instruction).encode()
        total = len(context_bytes) + len(prompt_bytes)
        reserve = reserves[model]
        if total > MAX_INPUT_BYTES or not await cost.acquire(reserve):
            return {"session_id": session_id, "model": model, "level": level, "target_case": case, "accepted": False, "error": "input_or_cost_guard", "total_input_size_bytes": total, "physical_requests": 0, "cost_usd": 0.0}
        request = binding(job={"job_id": session_id, "level": level}, model=model, turn=0, context=context_bytes, prompt=prompt_bytes, catalogue_hash=catalogue_hash, reserve=reserve, phase="provider-admission")
        transport = OpenRouterChatCompletionsTransport(api_key=key, expected_outbound_binding=build_openrouter_outbound(request))
        response = None; error = None
        try: response = await transport.send(request, 0)
        except Exception as exc: error = f"{type(exc).__name__}:{exc}"
        actual = float(response.cost_usd or 0.0) if response else float((transport.reconciled_accounting or {}).get("cost_usd", 0.0))
        await cost.settle(reserve, actual)
        base.write_atomic(args.out / "private" / session_id / "exchange.json", transport.last_exchange or {}, private=True)
        planner_result = None; message_sha256 = None
        if response is not None and error is None:
            try:
                message = base.strict_json_object(response.content_bytes)
                message_sha256 = digest(response.content_bytes)
                planner_result = session.accept(message)
            except Exception as exc: error = f"{type(exc).__name__}:{exc}"
        expected_language = case[0]
        exact = bool(response and response.returned_model_id == model and response.returned_model_revision == OPENROUTER_E1_PROFILES[model]["upstream_provider"] and (transport.last_exchange or {}).get("route", {}).get("fallback_allowed") is False)
        accepted = bool(planner_result and planner_result.status == "COMMITTED" and planner_result.compiled and planner_result.compiled.language == expected_language and exact and transport.physical_requests == 1)
        row = {"session_id": session_id, "model": model, "level": level, "target_case": case, "expected_language": expected_language, "accepted": accepted, "error": error, "message_sha256": message_sha256, "planner_result": None if planner_result is None else base._turn_public(planner_result), "physical_requests": transport.physical_requests, "retry_count": 0, "fallback_count": 0, "cost_usd": actual, "returned_model": response.returned_model_id if response else None, "returned_upstream": response.returned_model_revision if response else None, "context_size_bytes": len(context_bytes), "prompt_size_bytes": len(prompt_bytes), "total_input_size_bytes": total, "max_q_result_count": len(max_q) if level == "L4" else 0, "max_q_result_bytes": sum(len(canonical_json_bytes(item)) for item in max_q) if level == "L4" else 0, "contract_hash": catalogue_hash}
        base.write_atomic(args.out / "sessions" / session_id / "result.json", row)
        return row

    rows = await asyncio.gather(*(one(model, level, case) for model in MODELS for level, case in ADMISSION_CASES))
    passed = {case: sum(row["accepted"] for row in rows if row["target_case"] == case) for _, case in ADMISSION_CASES}
    value = {"schema_version": "dof.wp013b-provider-language-admission.v1", "technical_non_scientific": True, "sessions": rows, "session_count": len(rows), "accepted_by_case": passed, "physical_requests": sum(row["physical_requests"] for row in rows), "known_cost_usd": cost.spent, "cost_cap_usd": cost.cap, "retry_count": 0, "fallback_count": 0}
    value["accepted"] = bool(len(rows) == 8 and passed["M"] == 2 and all(passed[case] >= 1 for case in ("P_EXCLUSIVE", "G_SPATIAL_EXCLUSIVE", "G_HARDBLOCK_EXCLUSIVE")) and value["physical_requests"] == 8 and cost.spent <= cost.cap)
    value["aggregate_sha256"] = digest(value)
    base.write_atomic(args.out / "provider-admission.json", value)
    print(json.dumps({"accepted": value["accepted"], "accepted_by_case": passed, "physical_requests": value["physical_requests"], "known_cost_usd": value["known_cost_usd"]}, sort_keys=True))
    return 0 if value["accepted"] else 2


def parser() -> argparse.ArgumentParser:
    result = base.parser()
    # Replace inherited provider-admission behaviour without broadening the
    # CLI; command names remain compatible with the controller workflow.
    return result


def main() -> int:
    configure_base()
    args = parser().parse_args()
    if args.command == "preflight": return asyncio.run(preflight(args))
    if args.command == "provider-admission": return asyncio.run(admission(args))
    if args.command == "capture": return asyncio.run(base.capture(args))
    if args.command == "freeze": return base.freeze(args)
    if args.command == "run": return asyncio.run(base.run(args))
    if args.command == "audit": return base.audit(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
