#!/usr/bin/env python3
"""WP-014E multi-provider admission and coverage-matrix controller."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import run_wp013a_e0 as base  # noqa: E402
import run_wp014 as legacy  # noqa: E402
import run_wp014c_scientific as wp014c  # noqa: E402
from optimizer_pipeline.dof.multi_provider import (  # noqa: E402
    MAX_OUTPUT_TOKENS,
    PROFILES,
    StrictMultiProviderTransport,
    discover_openrouter_endpoint,
)
from optimizer_pipeline.dof.provider import ProviderRequestBinding, ProviderSettings  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import file_sha256  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import (  # noqa: E402
    canonical_scientific_outcome,
    classify_functional_validation,
    score_fmax_delta,
)
from optimizer_pipeline.dof.single_submission.action_surface_v3 import (  # noqa: E402
    M_ACTION_KEYS,
    registry_sha256,
)
from optimizer_pipeline.dof.single_submission.contract_v5 import compile_candidate_v5  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v6 import (  # noqa: E402
    PROTOCOL_EPOCH, contract_for_cell, contract_hash_for_cell,
    language_condition_error,
    prompt_for_cell, validate_protocol_cell,
)
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


SCHEMA = "dof.wp014e-provider-admission.v1"
ADMISSION_LABEL = "WP014E_PROVIDER_MODEL_ADMISSION_ONLY"
CAPS = {"openai": 22.0, "anthropic": 8.0, "deepseek": 2.0, "openrouter": 8.0}
MATRIX_SCHEMA = "dof.wp014e-coverage-mini-matrix.v1"
BACKEND_RESUME_SCHEMA = "dof.wp014e-backend-resume-amendment.v1"
MATRIX_LABEL = "WP014E_PROVIDER_MODEL_ADMISSION_ONLY"
MATRIX_MODELS = tuple(PROFILES)
MATRIX_ROOTS = ("amd_mini-isp", "logicnets_jscl")
MATRIX_RESERVES = {
    "openai/gpt-5.6-luna": 0.10,
    "openai/gpt-5.6-terra": 0.30,
    "openai/gpt-5.6-sol": 0.50,
    "anthropic/claude-sonnet-5": 0.50,
    "deepseek/deepseek-v4-flash": 0.05,
    "deepseek/deepseek-v4-pro": 0.10,
    "google/gemini-3.6-flash": 0.50,
}
PHYSICAL_MODEL_REQUEST_CAP = 950
PRIOR_PROVIDER_ONLY_REQUESTS = 28
IMPLEMENTATION_HEAVY_CALL_CAP = 600
_KIMI_ENDPOINT_TAG: str | None = None


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


def repository() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()
    status = git("status", "--porcelain")
    if status:
        raise RuntimeError("tracked_or_untracked_source_not_clean")
    return {
        "branch": git("branch", "--show-current"),
        "head_commit": git("rev-parse", "HEAD"),
        "head_tree": git("rev-parse", "HEAD^{tree}"),
    }


def credential_status() -> dict[str, str]:
    names = sorted({profile.credential_env for profile in PROFILES.values()})
    return {name: "PRESENT" if os.environ.get(name) else "MISSING" for name in names}


def _binding(
    *, profile: Any, admission: str, prompt: bytes, context: bytes,
    catalogue_hash: str,
) -> ProviderRequestBinding:
    decision = f"wp014e-{admission.lower()}-{profile.canonical_model_id.replace('/', '-')}"
    return ProviderRequestBinding(
        run_id="wp014e-provider-admission-v1",
        spec_hash=digest({"admission": admission, "model": profile.canonical_model_id}),
        experiment_id="WP-014E", condition_id=admission,
        repeat_id="NO_REPEAT", decision_id=decision,
        context_bytes=context, prompt_bytes=prompt,
        prompt_version="dof.wp014e-provider-admission-prompt.v1",
        action_interface_version="dof.single-submission.planner-message.v6",
        action_interface_hash=catalogue_hash,
        context_interface_version="dof.single-submission.planning-context.v2.2",
        context_interface_hash=digest(context),
        catalogue_version="dof.action-surface.v3",
        catalogue_hash=catalogue_hash,
        capability_hash=digest({"provider": profile.provider, "adapter": profile.adapter_version}),
        mask_evidence_hash=digest({"coverage_regime": "FULL_M8"}),
        provider_role="planner",
        provider=profile.provider,
        model_id=profile.canonical_model_id,
        adapter_version=profile.adapter_version,
        settings=ProviderSettings(
            temperature=None, max_tokens=MAX_OUTPUT_TOKENS, seed=None,
            response_format="json_object", timeout_seconds=300.0,
            max_retries=0, retry_on=(), max_requests=1,
            unknown_cost_policy="reject", unknown_usage_policy="reject",
        ),
    )


def _contexts(bundle: Mapping[str, Any]) -> dict[str, tuple[bytes, bytes, dict[str, Any]]]:
    observation = bundle["observations"]["amd_mini-isp"]["payload"]
    m_contract = contract_for_cell(
        study_track="C_CONDITIONED", coverage_regime="REDUCED_M_STRESS",
        language_condition="M_ONLY", level="C",
    )
    m_prompt = prompt_for_cell(
        study_track="C_CONDITIONED", language_condition="M_ONLY", level="C",
    )
    m_context = canonical_json_bytes({
        "schema_version": "dof.wp014e-provider-admission-context.v1",
        "admission": "A_SIMPLE_M",
        "label": ADMISSION_LABEL,
        "objective": "Return one legal COMMIT using one admitted M action.",
        "common_observation": observation,
        "language_contract": m_contract,
        "qor_feedback_present": False,
    })

    g_contract = contract_for_cell(
        study_track="C_CONDITIONED", coverage_regime="REDUCED_M_STRESS",
        language_condition="G_BOUNDARY", level="C",
    )
    g_prompt = prompt_for_cell(
        study_track="C_CONDITIONED", language_condition="G_BOUNDARY", level="C",
    )
    g_context = canonical_json_bytes({
        "schema_version": "dof.wp014e-provider-admission-context.v1",
        "admission": "B_FULL_G_ENVELOPE",
        "label": ADMISSION_LABEL,
        "objective": "Return the final legal G-boundary COMMIT after considering the bounded query and construction feedback already shown.",
        "common_observation": observation,
        "common_observation_sha256": bundle["observations"]["amd_mini-isp"]["sha256"],
        "common_query_catalogue": {
            "max_queries": 8,
            "operations": [
                "critical_path_details", "critical_object_locations",
                "high_fanout_net_details", "critical_hardblock_candidates",
                "placement_summary", "route_summary",
            ],
        },
        "bounded_query_evidence": {
            "queries_used": 2,
            "query_result_hash": bundle["observations"]["amd_mini-isp"]["query_private_rows_sha256"],
            "raw_private_objects_exposed": False,
        },
        "construction_feedback": {
            "feedback_round": 1,
            "error_code": "generated_geometry_out_of_bounds",
            "location": "$.candidate.steps[0].args",
            "qor_feedback_present": False,
        },
        "turn_budget": {"max_turns": 4, "turns_already_represented": 2, "final_commit_required_now": True},
        "language_contract": g_contract,
        "qor_feedback_present": False,
    })
    return {
        "A_SIMPLE_M": (m_prompt, m_context, m_contract),
        "B_FULL_G_ENVELOPE": (g_prompt, g_context, g_contract),
    }


def _policy_classification(
    admission: str,
    content: bytes,
    compile_context: CompileContext,
) -> dict[str, Any]:
    try:
        message = base.strict_json_object(content)
    except Exception as exc:
        return {"terminal": "MODEL_OUTPUT_NOT_STRICT_JSON", "reason": type(exc).__name__}
    if message.get("kind") != "COMMIT" or not isinstance(message.get("candidate"), Mapping):
        return {"terminal": "MODEL_OUTPUT_NOT_FINAL_COMMIT", "message_sha256": digest(content)}
    candidate = message["candidate"]
    condition = "M_ONLY" if admission == "A_SIMPLE_M" else "G_BOUNDARY"
    condition_error = language_condition_error(candidate, condition)
    if condition_error:
        return {"terminal": "MODEL_OUTPUT_LANGUAGE_CONDITION_REJECTION", "reason": condition_error, "candidate_sha256": digest(candidate)}
    try:
        compiled = compile_candidate_v5(candidate, compile_context)
    except Exception as exc:
        return {"terminal": "MODEL_OUTPUT_CONSTRUCTION_REJECTION", "reason": getattr(exc, "reason", type(exc).__name__), "candidate_sha256": digest(candidate)}
    return {
        "terminal": "MODEL_OUTPUT_COMPATIBLE",
        "candidate_sha256": digest(candidate),
        "compiled_sha256": compiled.public_sha256,
        "selected_language": compiled.language,
        "heavy_calls": compiled.heavy_calls,
        "light_mutations": compiled.light_mutations,
    }


async def provider_admission(args: argparse.Namespace) -> int:
    repo = repository()
    credentials = credential_status()
    if "MISSING" in credentials.values():
        raise SystemExit("required_provider_credential_missing")
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    bundle = read(args.bundle)
    if bundle.get("sealed_holdouts_accessed") is not False:
        raise RuntimeError("bundle_holdout_boundary_invalid")
    if file_sha256(bundle["roots"]["amd_mini-isp"]["root_path"]) != bundle["roots"]["amd_mini-isp"]["sha256"]:
        raise RuntimeError("development_root_hash_mismatch")

    # Kimi inference is admitted only after one exact, non-inference endpoint
    # metadata request proves the Moonshot route and required parameters.
    kimi_freeze = await discover_openrouter_endpoint(os.environ["OPENROUTER_API_KEY"])
    write(args.out / "kimi-endpoint-freeze.json", kimi_freeze)
    contexts = _contexts(bundle)
    if any(len(prompt) + len(context) > 65_536 for prompt, context, _ in contexts.values()):
        raise RuntimeError("canonical_provider_admission_input_too_large")

    wp014c.configure()
    _observation, private = base._load_trial(bundle, args.capture, "amd_mini-isp")
    compile_context = CompileContext(
        private["registry"], "ROUTED", private["predefined_regions"],
        private["geometry_bounds"],
        tuple(key.removeprefix("macro.") for key in bundle["roots"]["amd_mini-isp"].get("admitted_action_keys", wp014c.M_ACTION_KEYS)),
        fanout_admitted=bool(private.get("fanout_admitted", False)),
        action_surface_version="v3",
    )
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def one(model: str, admission: str) -> dict[str, Any]:
        profile = PROFILES[model]
        prompt, context, contract = contexts[admission]
        request = _binding(
            profile=profile, admission=admission, prompt=prompt, context=context,
            catalogue_hash=digest(contract),
        )
        transport = StrictMultiProviderTransport(
            profile=profile,
            api_key=os.environ[profile.credential_env],
            pinned_endpoint_tag=kimi_freeze["endpoint_tag"] if profile.provider == "openrouter" else None,
        )
        started = time.monotonic()
        response = None
        error = None
        async with semaphore:
            try:
                response = await transport.send(request, 0)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
        exchange = transport.last_exchange
        if exchange is not None:
            write(args.out / "private" / model.replace("/", "_") / f"{admission}.json", exchange, private=True)
        policy = (
            _policy_classification(admission, response.content_bytes, compile_context)
            if response is not None else {"terminal": "NOT_EVALUATED_TRANSPORT_FAILURE"}
        )
        return {
            "admission": admission,
            "label": ADMISSION_LABEL,
            "model": model,
            "provider": profile.provider,
            "route": profile.route,
            "adapter_version": profile.adapter_version,
            "request_contract_hash": request.request_contract_hash,
            "prompt_sha256": request.prompt_hash,
            "context_sha256": request.context_hash,
            "input_size_bytes": len(prompt) + len(context),
            "transport_terminal": "COMPATIBLE" if response is not None else "TECHNICAL_FAILURE",
            "technical_error": error,
            "physical_requests": transport.physical_requests,
            "returned_model": response.returned_model_id if response else None,
            "provider_request_id_present": bool(response and response.provider_request_id),
            "input_tokens": response.input_tokens if response else None,
            "output_tokens": response.output_tokens if response else None,
            "accounted_cost_usd": response.cost_usd if response else None,
            "cost_source": response.cost_source if response else None,
            "latency_seconds": time.monotonic() - started,
            "retry_count": 0,
            "fallback_count": 0,
            "repair_count": 0,
            "policy_compatibility": policy,
            "exchange_sha256": digest(exchange) if exchange is not None else None,
        }

    rows = await asyncio.gather(*(
        one(model, admission)
        for model in PROFILES for admission in contexts
    ))
    provider_cost = {
        provider: sum(float(row["accounted_cost_usd"] or 0.0) for row in rows if row["provider"] == provider)
        for provider in CAPS
    }
    technical_failures = [
        f"{row['model']}:{row['admission']}" for row in rows
        if row["transport_terminal"] != "COMPATIBLE"
    ]
    core = {
        "schema_version": SCHEMA,
        "label": ADMISSION_LABEL,
        "not_a_wp015_repeat": True,
        "source": repo,
        "credential_status": credentials,
        "credential_values_persisted": False,
        "kimi_endpoint_freeze_sha256": digest(kimi_freeze),
        "metadata_requests": kimi_freeze["physical_metadata_requests"],
        "inference_admissions": rows,
        "planned_inference_requests": 14,
        "physical_inference_requests": sum(row["physical_requests"] for row in rows),
        "technical_failures": technical_failures,
        "model_policy_terminal_counts": {
            terminal: sum(row["policy_compatibility"]["terminal"] == terminal for row in rows)
            for terminal in sorted({row["policy_compatibility"]["terminal"] for row in rows})
        },
        "provider_cost_usd": provider_cost,
        "total_cost_usd": sum(provider_cost.values()),
        "caps_usd": CAPS,
        "retry_count": 0,
        "fallback_count": 0,
        "repair_count": 0,
        "sealed_holdouts_accessed": False,
    }
    core["accepted_for_matrix_freeze"] = bool(
        len(rows) == 14
        and core["physical_inference_requests"] == 14
        and not technical_failures
        and all(provider_cost[key] <= CAPS[key] for key in CAPS)
        and core["total_cost_usd"] <= 40.0
    )
    value = core | {"aggregate_sha256": digest(core)}
    write(args.out / "provider-admission.json", value)
    print(json.dumps({
        "accepted_for_matrix_freeze": value["accepted_for_matrix_freeze"],
        "physical_inference_requests": value["physical_inference_requests"],
        "technical_failures": value["technical_failures"],
        "model_policy_terminal_counts": value["model_policy_terminal_counts"],
        "provider_cost_usd": value["provider_cost_usd"],
    }, sort_keys=True))
    return 0 if value["accepted_for_matrix_freeze"] else 2


def _matrix_provider_runtime(model: str) -> dict[str, Any]:
    profile = PROFILES[model]
    return {
        "provider": profile.provider,
        "route": profile.route,
        "expected_returned_model": profile.canonical_model_id,
        "expected_returned_upstream": (
            profile.required_upstream_provider
            if profile.provider == "openrouter"
            else profile.provider + "_direct"
        ),
    }


def _matrix_transport(request: ProviderRequestBinding, model: str, _key: str):
    profile = PROFILES[model]
    return StrictMultiProviderTransport(
        profile=profile,
        api_key=os.environ[profile.credential_env],
        pinned_endpoint_tag=_KIMI_ENDPOINT_TAG if profile.provider == "openrouter" else None,
    )


def _matrix_binding(
    *, job: Mapping[str, Any], model: str, turn: int, context: bytes,
    prompt: bytes, catalogue_hash: str, reserve: float, phase: str = "mini",
) -> ProviderRequestBinding:
    del phase
    profile = PROFILES[model]
    identity = f"{job['job_id']}:{turn}"
    return ProviderRequestBinding(
        run_id="wp014e-mini-" + digest(identity)[:24],
        spec_hash=digest({"protocol": MATRIX_SCHEMA, "job": job}),
        experiment_id="WP-014E-COVERAGE-MINI-MATRIX",
        condition_id="condition-" + digest({
            "subclass": job["subclass"], "root": job["root_id"],
        })[:20],
        repeat_id="NO_REPEAT",
        decision_id="decision-" + digest(job["job_id"])[:20],
        context_bytes=context,
        prompt_bytes=prompt,
        prompt_version="dof.wp014e-mini-matrix-prompt.v1",
        action_interface_version="dof.single-submission.planner-message.v6",
        action_interface_hash=digest("dof.single-submission.planner-message.v6"),
        context_interface_version="dof.single-submission.planning-context.v2.2",
        context_interface_hash=digest(context),
        catalogue_version=f"{PROTOCOL_EPOCH}.wp014e.{job['subclass'].lower()}",
        catalogue_hash=catalogue_hash,
        capability_hash=catalogue_hash,
        mask_evidence_hash=job["mask_evidence_hash"],
        provider_role="wp014e_single_submission_planner",
        provider=profile.provider,
        model_id=model,
        adapter_version=profile.adapter_version,
        settings=ProviderSettings(
            temperature=None,
            max_tokens=MAX_OUTPUT_TOKENS,
            seed=None,
            response_format="json_object",
            timeout_seconds=300.0,
            max_retries=0,
            retry_on=(),
            max_requests=1,
            max_usd=reserve,
            max_cost_per_request_usd=reserve,
            unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )


def configure_matrix(endpoint_tag: str) -> None:
    global _KIMI_ENDPOINT_TAG
    _KIMI_ENDPOINT_TAG = endpoint_tag
    wp014c.configure()
    legacy.SCHEMA = MATRIX_SCHEMA
    legacy.MODELS = MATRIX_MODELS
    legacy.MODEL_RESERVES = MATRIX_RESERVES
    legacy.OPENROUTER_COST_CAP_USD = 40.0
    legacy.IMPLEMENTATION_HEAVY_CALL_CAP = IMPLEMENTATION_HEAVY_CALL_CAP
    legacy.QUEUE_WALL_SECONDS_CAP = 86_400
    base.MODELS = MATRIX_MODELS
    base._binding = _matrix_binding
    base._provider_runtime_profile = _matrix_provider_runtime
    base._provider_transport = _matrix_transport


def _reduced_binding(source: Mapping[str, Any], root_id: str) -> dict[str, Any]:
    candidates = [
        row for row in source["jobs"]
        if row["root_id"] == root_id and row["phase"] == "S_N"
    ]
    if not candidates:
        raise ValueError("reduced_binding_missing:" + root_id)
    first = candidates[0]
    if any(
        row["admitted_action_keys"] != first["admitted_action_keys"]
        or row["mask_evidence_hash"] != first["mask_evidence_hash"]
        for row in candidates
    ):
        raise ValueError("reduced_binding_not_unique:" + root_id)
    return {
        "admitted_action_keys": list(first["admitted_action_keys"]),
        "mask_evidence_hash": first["mask_evidence_hash"],
    }


def matrix_jobs(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    full_hash = digest({"coverage_regime": "FULL_M8", "actions": M_ACTION_KEYS})
    for root_id in MATRIX_ROOTS:
        for level in ("L0", "L1"):
            jobs.append({
                "job_id": f"wp014e__reference__{root_id}__{level.lower()}",
                "subclass": "REFERENCE_" + level,
                "phase": "MINI_MATRIX", "root_id": root_id,
                "model": "MODEL_INDEPENDENT", "level": level,
                "agent_level": level, "protocol_epoch": PROTOCOL_EPOCH,
                "study_track": "REFERENCE", "coverage_regime": "FULL_M8",
                "language_condition": "NOT_APPLICABLE", "repeat": None,
                "admitted_action_keys": list(M_ACTION_KEYS),
                "mask_evidence_hash": full_hash, "label": MATRIX_LABEL,
                "not_a_wp015_repeat": True,
            })
        reduced = _reduced_binding(source, root_id)
        for model in MATRIX_MODELS:
            for level in ("L2", "L3", "L4"):
                jobs.append({
                    "job_id": f"wp014e__n__{root_id}__{model.replace('/', '_')}__{level.lower()}",
                    "subclass": "N_" + level, "phase": "MINI_MATRIX",
                    "root_id": root_id, "model": model, "level": level,
                    "agent_level": level, "protocol_epoch": PROTOCOL_EPOCH,
                    "study_track": "N_NEUTRAL", "coverage_regime": "FULL_M8",
                    "language_condition": "FREE_CHOICE", "repeat": None,
                    "admitted_action_keys": list(M_ACTION_KEYS),
                    "mask_evidence_hash": full_hash, "label": MATRIX_LABEL,
                    "not_a_wp015_repeat": True,
                })
            for condition in ("M_ONLY", "P_BOUNDARY", "G_BOUNDARY"):
                level = {"M_ONLY": "L2", "P_BOUNDARY": "L3", "G_BOUNDARY": "L4"}[condition]
                jobs.append({
                    "job_id": f"wp014e__c__{root_id}__{model.replace('/', '_')}__{condition.lower()}",
                    "subclass": "C_" + condition, "phase": "MINI_MATRIX",
                    "root_id": root_id, "model": model, "level": level,
                    "agent_level": "C", "protocol_epoch": PROTOCOL_EPOCH,
                    "study_track": "C_CONDITIONED",
                    "coverage_regime": "REDUCED_M_STRESS",
                    "language_condition": condition, "repeat": None,
                    "admitted_action_keys": reduced["admitted_action_keys"],
                    "mask_evidence_hash": reduced["mask_evidence_hash"],
                    "label": MATRIX_LABEL, "not_a_wp015_repeat": True,
                })
            for level in ("L2", "L3", "L4"):
                jobs.append({
                    "job_id": f"wp014e__s__{root_id}__{model.replace('/', '_')}__{level.lower()}",
                    "subclass": "S_" + level, "phase": "MINI_MATRIX",
                    "root_id": root_id, "model": model, "level": level,
                    "agent_level": level, "protocol_epoch": PROTOCOL_EPOCH,
                    "study_track": "S_NEUTRAL_REDUCED",
                    "coverage_regime": "REDUCED_M_STRESS",
                    "language_condition": "FREE_CHOICE", "repeat": None,
                    "admitted_action_keys": reduced["admitted_action_keys"],
                    "mask_evidence_hash": reduced["mask_evidence_hash"],
                    "label": MATRIX_LABEL, "not_a_wp015_repeat": True,
                })
    return jobs


def freeze_matrix(args: argparse.Namespace) -> int:
    repo = repository()
    source = read(args.bundle)
    admission = read(args.admission)
    endpoint = read(args.kimi_endpoint)
    if not admission.get("accepted_for_matrix_freeze") or admission.get("technical_failures"):
        raise SystemExit("gate3_v2_not_accepted")
    if not endpoint.get("endpoint_tag") or endpoint.get("fallback_allowed") is not False:
        raise SystemExit("kimi_endpoint_freeze_invalid")
    if source.get("sealed_holdouts_accessed") is not False:
        raise SystemExit("source_holdout_boundary_invalid")
    jobs = matrix_jobs(source)
    counts = {name: sum(row["subclass"] == name for row in jobs) for name in sorted({row["subclass"] for row in jobs})}
    if len(jobs) != 130 or counts.get("REFERENCE_L0") != 2 or counts.get("REFERENCE_L1") != 2:
        raise SystemExit("wp014e_130_job_inventory_invalid")
    if any(counts.get(name) != 14 for name in (
        "N_L2", "N_L3", "N_L4", "C_M_ONLY", "C_P_BOUNDARY",
        "C_G_BOUNDARY", "S_L2", "S_L3", "S_L4",
    )):
        raise SystemExit("wp014e_subclass_inventory_invalid")
    for job in jobs:
        if job["model"] == "MODEL_INDEPENDENT":
            continue
        validate_protocol_cell(
            study_track=job["study_track"],
            coverage_regime=job["coverage_regime"],
            language_condition=job["language_condition"],
            level=job["agent_level"],
        )
    references = read(REPO / "configs/wp014/reference-reuse-audit-v1.json")
    reference_rows = [
        row for row in references["rows"]
        if row["root_id"] in MATRIX_ROOTS and row["coverage_regime"] == "FULL_M8"
    ]
    if len(reference_rows) != 2 or not all(row["l1"]["canonical_endpoint"] == "VALID_COMPLETION" for row in reference_rows):
        raise SystemExit("model_independent_reference_binding_invalid")
    roots = {root_id: source["roots"][root_id] for root_id in MATRIX_ROOTS}
    if any(file_sha256(row["root_path"]) != row["sha256"] for row in roots.values()):
        raise SystemExit("development_root_hash_mismatch")
    core = {
        "schema_version": MATRIX_SCHEMA,
        "label": MATRIX_LABEL,
        "not_a_wp015_repeat": True,
        "freeze_status": "FROZEN",
        "repository": repo,
        "source_bundle_sha256": source["bundle_sha256"],
        "gate3_v2_aggregate_sha256": admission["aggregate_sha256"],
        "gate3_provider_cost_usd": admission["provider_cost_usd"],
        "gate3_total_cost_usd": admission["total_cost_usd"],
        "kimi_endpoint": endpoint,
        "roots": roots,
        "observations": {root_id: source["observations"][root_id] for root_id in MATRIX_ROOTS},
        "query_audits": {root_id: source["query_audits"][root_id] for root_id in MATRIX_ROOTS},
        "jobs": jobs,
        "job_count": len(jobs),
        "subclass_counts": counts,
        "reference_rows": reference_rows,
        "models": [asdict(PROFILES[model]) for model in MATRIX_MODELS],
        "action_surface_registry_sha256": registry_sha256(),
        "guards": {
            "provider_only_requests_already_executed": PRIOR_PROVIDER_ONLY_REQUESTS,
            "physical_model_requests": PHYSICAL_MODEL_REQUEST_CAP,
            "implementation_heavy_calls": IMPLEMENTATION_HEAVY_CALL_CAP,
            "functional_validation_cases": 160,
            "api_cost_usd": 40.0,
            "provider_cost_usd": CAPS,
            "experiment_queue_wall_seconds": 86_400,
            "validator_queue_wall_seconds": 86_400,
            "transient_validation_disk_bytes": 150 * 1024**3,
            "max_parallel_weekday": 8,
            "max_parallel_validators": 2,
        },
        "sealed_holdouts_accessed": False,
    }
    value = core | {"matrix_sha256": digest(core)}
    if args.output.exists():
        raise FileExistsError(args.output)
    write(args.output, value)
    print(json.dumps({"frozen": True, "jobs": len(jobs), "subclass_counts": counts, "matrix_sha256": value["matrix_sha256"]}, sort_keys=True))
    return 0


def _reference_results(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    references = {row["root_id"]: row for row in bundle["reference_rows"]}
    rows = []
    for job in bundle["jobs"]:
        if job["model"] != "MODEL_INDEPENDENT":
            continue
        reference = references[job["root_id"]]
        if job["level"] == "L0":
            rows.append(dict(job) | {
                "canonical_terminal": "VALID_ROOT_REFERENCE",
                "execution_started": False,
                "provider_requests": 0,
                "cost_usd": 0.0,
                "actual_heavy_calls": 0,
                "actual_light_mutations": 0,
                "reference": reference["l0"],
            })
        else:
            rows.append(dict(job) | {
                "canonical_terminal": "VALID_COMPLETION",
                "selected_language": "M",
                "selected_macro": reference["l1"]["action_key"].removeprefix("macro."),
                "execution_started": False,
                "provider_requests": 0,
                "cost_usd": 0.0,
                "actual_heavy_calls": 0,
                "actual_light_mutations": 0,
                "evidence_reused": True,
                "execution": {
                    "canonical_endpoint": "VALID_COMPLETION",
                    "child_path": reference["l1"]["child_path"],
                    "child": {"sha256": reference["l1"]["child_sha256"]},
                    "initial_fmax_mhz": reference["l0"]["fmax_mhz"],
                    "final_fmax_mhz": reference["l1"]["fmax_mhz"],
                    "initial_wns_ns": reference["l0"]["wns_ns"],
                    "final_wns_ns": reference["l1"]["wns_ns"],
                    "root_unchanged": True,
                },
            })
    return rows


def _verified_object(path: Path, hash_key: str) -> dict[str, Any]:
    value = read(path)
    claimed = value.pop(hash_key)
    if digest(value) != claimed:
        raise SystemExit(f"{hash_key}_invalid:{path}")
    return value | {hash_key: claimed}


def freeze_backend_resume(args: argparse.Namespace) -> int:
    matrix = _verified_object(args.matrix, "matrix_sha256")
    planning = _verified_object(args.planning_stop, "aggregate_sha256")
    if planning.get("matrix_sha256") != matrix["matrix_sha256"]:
        raise SystemExit("planning_matrix_binding_mismatch")
    planned = sorted(row["job_id"] for row in planning["planned_rows"] if row["canonical_terminal"] == "PLANNED_COMMIT")
    no_valid = sorted(row["job_id"] for row in planning["planned_rows"] if row["canonical_terminal"] == "NO_VALID_SUBMISSION")
    provider_failure = sorted(row["job_id"] for row in planning["planned_rows"] if row["canonical_terminal"] == "PROVIDER_FAILURE")
    if (len(planned), len(no_valid), len(provider_failure)) != (112, 13, 1):
        raise SystemExit("amended_terminal_inventory_invalid")
    if provider_failure != planning.get("technical_rows"):
        raise SystemExit("amended_provider_failure_binding_invalid")
    stop_summary = read(REPO / ".research-flow/artifacts/WP-014E/gate5-planning-stop-v2.json")
    if (
        stop_summary.get("planning_stop_aggregate_sha256") != planning["aggregate_sha256"]
        or stop_summary.get("wp014e_physical_requests_cumulative") != 806
        or stop_summary.get("total_cost_usd_cumulative") != 19.0757475988
    ):
        raise SystemExit("amendment_resource_ledger_mismatch")
    core = {
        "schema_version": BACKEND_RESUME_SCHEMA,
        "status": "FROZEN",
        "repository": repository(),
        "matrix_sha256": matrix["matrix_sha256"],
        "planning_stop_aggregate_sha256": planning["aggregate_sha256"],
        "protocol_amendment": {
            "provider_transport_failure_scope": "PER_CELL_SCIENTIFIC_TERMINAL",
            "planning_phase_accepted": True,
            "planning_rerun_allowed": False,
            "retry_allowed": False,
            "repair_allowed": False,
            "backfill_allowed": False,
            "fallback_allowed": False,
            "wp015_freeze_binding_required": True,
        },
        "dispatch_job_ids": planned,
        "no_backend_job_ids": no_valid + provider_failure,
        "provider_failure_job_ids": provider_failure,
        "counts": {"dispatch": 112, "no_valid_submission": 13, "provider_failure": 1, "references": 4, "final_jobs": 130},
        "resource_ledger_before_backend": {
            "physical_model_requests": 806,
            "physical_model_request_cap": 950,
            "provider_cost_usd": stop_summary["provider_cost_usd_cumulative"],
            "total_cost_usd": stop_summary["total_cost_usd_cumulative"],
            "total_cost_cap_usd": matrix["guards"]["api_cost_usd"],
            "implementation_heavy_calls": 0,
        },
        "guards": matrix["guards"],
        "sealed_holdouts_accessed": False,
    }
    value = core | {"resume_sha256": digest(core)}
    if args.output.exists():
        raise FileExistsError(args.output)
    write(args.output, value)
    print(json.dumps({"frozen": True, "dispatch_jobs": 112, "no_backend_jobs": 14, "resume_sha256": value["resume_sha256"]}, sort_keys=True))
    return 0


def _reconstruct_planned_item(
    *, row: Mapping[str, Any], job: Mapping[str, Any], bundle: Mapping[str, Any],
    capture: Path, planning_dir: Path,
) -> tuple[dict[str, Any], Any, Any, dict[str, Any]]:
    local = dict(bundle)
    local["roots"] = dict(bundle["roots"])
    local["roots"][job["root_id"]] = dict(bundle["roots"][job["root_id"]]) | {
        "admitted_action_keys": list(job["admitted_action_keys"]),
    }
    observation, private = base._load_trial(local, capture, job["root_id"])
    # Replay only the persisted read-only Q requests. Materialisation registers
    # the exact queried private objects in the deterministic root-bound registry
    # and must reproduce every public result hash before COMMIT reconstruction.
    queries = base.CommonQuerySurfaceV2(private["registry"])
    for turn in row["turns"]:
        for item in (turn.get("planner_result") or {}).get("query_batch", {}).get("items", []):
            request = item["request"]
            result = queries.materialize(
                query_name=request["name"], top_k=request["top_k"],
                private_rows=private["rows"].get(request["name"], []),
                tool_call_ids=(base.QUERY_EVIDENCE_TAG,), latency_seconds=0.0,
                missing_reason=private["missing_reasons"].get(request["name"]),
            )
            if result.sha256 != item["result_sha256"]:
                raise RuntimeError("resume_query_result_mismatch:" + job["job_id"])
    if observation.sha256 != row["observation_sha256"]:
        raise RuntimeError("resume_observation_mismatch:" + job["job_id"])
    candidate = read(planning_dir / job["job_id"] / "private" / "committed-source.json")
    context = CompileContext(
        private["registry"], "ROUTED", private["predefined_regions"], private["geometry_bounds"],
        tuple(key.removeprefix("macro.") for key in job["admitted_action_keys"]),
        fanout_admitted=bool(private.get("fanout_admitted", False)),
    )
    compiled = compile_candidate_v5(candidate, context)
    if compiled.public_sha256 != row["committed_sha256"]:
        raise RuntimeError("resume_committed_program_mismatch:" + job["job_id"])
    if private["registry"].public_manifest() != row["query_handle_manifest"]:
        raise RuntimeError("resume_handle_manifest_mismatch:" + job["job_id"])
    return dict(row), private["registry"], compiled, candidate


async def run_matrix(args: argparse.Namespace) -> int:
    credentials = credential_status()
    if "MISSING" in credentials.values():
        raise SystemExit("required_provider_credential_missing")
    bundle = read(args.matrix)
    claimed = bundle.pop("matrix_sha256")
    if digest(bundle) != claimed:
        raise SystemExit("matrix_hash_invalid")
    bundle["matrix_sha256"] = claimed
    if bundle.get("freeze_status") != "FROZEN" or bundle.get("job_count") != 130:
        raise SystemExit("matrix_not_frozen")
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    base.assert_clean_repository(bundle["repository"]["head_commit"])
    configure_matrix(bundle["kimi_endpoint"]["endpoint_tag"])
    roots_before = {
        root_id: file_sha256(root["root_path"])
        for root_id, root in bundle["roots"].items()
    }
    if any(roots_before[root_id] != row["sha256"] for root_id, row in bundle["roots"].items()):
        raise SystemExit("root_hash_mismatch_before_matrix")
    started = time.monotonic()
    cost = base.SharedCost(40.0, spent=float(bundle["gate3_total_cost_usd"]))
    llm_jobs = [row for row in bundle["jobs"] if row["model"] != "MODEL_INDEPENDENT"]
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def plan_one(job: Mapping[str, Any]):
        async with semaphore:
            return await legacy._plan_with_context(
                job, bundle, args.capture, args.out,
                os.environ["OPENROUTER_API_KEY"], cost,
            )

    planned = await asyncio.gather(*(plan_one(job) for job in llm_jobs))
    planned_rows = [item[0] for item in planned]
    provider_cost_fresh = {
        provider: sum(
            float(row.get("cost_usd") or 0.0)
            for row in planned_rows
            if PROFILES[row["model"]].provider == provider
        )
        for provider in CAPS
    }
    provider_cost_cumulative = {
        provider: float(bundle["gate3_provider_cost_usd"][provider]) + provider_cost_fresh[provider]
        for provider in CAPS
    }
    physical_requests_fresh = sum(int(row.get("provider_requests") or 0) for row in planned_rows)
    prior_requests = int(bundle["guards"]["provider_only_requests_already_executed"])
    request_cap = int(bundle["guards"]["physical_model_requests"])
    technical_rows = [
        row["job_id"] for row in planned_rows
        if row["canonical_terminal"] in {"PROVIDER_FAILURE", "MODEL_OR_ROUTE_DRIFT", "CONTROLLER_FAILURE"}
    ]
    planning_stop = bool(
        technical_rows
        or physical_requests_fresh + prior_requests > request_cap
        or cost.spent > 40.0
        or any(provider_cost_cumulative[p] > CAPS[p] for p in CAPS)
    )
    if planning_stop:
        core = {
            "schema_version": MATRIX_SCHEMA,
            "matrix_sha256": claimed,
            "stage": "PLANNING_STOP",
            "technical_rows": technical_rows,
            "planned_rows": planned_rows,
            "physical_requests_fresh": physical_requests_fresh,
            "provider_cost_fresh": provider_cost_fresh,
            "provider_cost_cumulative": provider_cost_cumulative,
            "total_cost_cumulative": cost.spent,
            "backend_dispatches": 0,
            "sealed_holdouts_accessed": False,
        }
        write(args.out / "planning-stop.json", core | {"aggregate_sha256": digest(core)})
        print(json.dumps({"accepted_for_functional_validation": False, "planning_stop": True, "technical_rows": technical_rows}, sort_keys=True))
        return 2

    heavy_cap = int(bundle["guards"]["implementation_heavy_calls"])
    remaining_heavy = heavy_cap
    allocations: dict[str, dict[str, Any]] = {}
    for item in sorted(planned, key=lambda value: digest(value[0]["job_id"])):
        required = legacy._required_heavy(item)
        row = item[0]
        allowed = row["canonical_terminal"] not in base.TERMINALS and required <= remaining_heavy
        allocations[row["job_id"]] = {
            "required_heavy_calls": required,
            "dispatch_allowed": allowed,
            "remaining_before": remaining_heavy,
            "allocation_basis": "SHA256(job_id); QoR-independent",
        }
        if allowed:
            remaining_heavy -= required
        allocations[row["job_id"]]["remaining_after"] = remaining_heavy

    execution_semaphore = asyncio.Semaphore(args.max_parallel)

    async def execute_one(item: Any) -> dict[str, Any]:
        row, registry, compiled, candidate = item
        allocation = allocations[row["job_id"]]
        if row["canonical_terminal"] in base.TERMINALS:
            evidence = legacy.boundary_evidence(candidate=candidate, execution_started=False, execution=None)
            return row | {
                "execution_started": False, "actual_heavy_calls": 0,
                "actual_light_mutations": 0, "execution_allocation": allocation,
                "boundary_evidence": evidence,
                "boundary_runtime_exercised": evidence["boundary_runtime_exercised"],
            }
        if not allocation["dispatch_allowed"]:
            evidence = legacy.boundary_evidence(candidate=candidate, execution_started=False, execution=None)
            return row | {
                "canonical_terminal": "BUDGET_BLOCKED", "execution_started": False,
                "actual_heavy_calls": 0, "actual_light_mutations": 0,
                "execution_allocation": allocation, "boundary_evidence": evidence,
                "boundary_runtime_exercised": evidence["boundary_runtime_exercised"],
            }
        root = Path(bundle["roots"][row["root_id"]]["root_path"])
        execution_dir = args.out / "execution" / row["job_id"]
        async with execution_semaphore:
            macro = row.get("selected_macro") or base._macro_from_candidate(candidate)
            if macro:
                result = await base._execute_macro(
                    root=root, flow=macro, out=execution_dir, python=args.python,
                )
                result.setdefault("actual_light_mutations", None)
            elif compiled is not None and registry is not None:
                result = await base._execute_typed(
                    compiled=compiled, registry=registry, root=root,
                    out=execution_dir, python=args.python, vivado=args.vivado,
                )
            else:
                result = {
                    "canonical_endpoint": "CONTROLLER_FAILURE",
                    "error": "planned_commit_missing_compiled_program",
                    "actual_heavy_calls": 0, "actual_light_mutations": 0,
                }
            write(execution_dir / "execution-summary.json", result)
            if int(result.get("actual_heavy_calls") or 0) > allocation["required_heavy_calls"]:
                result = result | {
                    "canonical_endpoint": "CONTROLLER_FAILURE",
                    "accounting_error": "observed_heavy_calls_exceeded_predispatch_reservation",
                }
            evidence = legacy.boundary_evidence(candidate=candidate, execution_started=True, execution=result)
            return row | {
                "canonical_terminal": result["canonical_endpoint"],
                "execution_started": True, "execution": result,
                "actual_heavy_calls": result.get("actual_heavy_calls", 0),
                "actual_light_mutations": result.get("actual_light_mutations"),
                "execution_allocation": allocation, "boundary_evidence": evidence,
                "boundary_runtime_exercised": evidence["boundary_runtime_exercised"],
            }

    executed = await asyncio.gather(*(execute_one(item) for item in planned))
    references = _reference_results(bundle)
    by_id = {row["job_id"]: row for row in [*references, *executed]}
    final_rows = [by_id[row["job_id"]] for row in bundle["jobs"]]
    roots_after = {
        root_id: file_sha256(root["root_path"])
        for root_id, root in bundle["roots"].items()
    }
    heavy = sum(int(row.get("actual_heavy_calls") or 0) for row in executed)
    terminal_counts = {
        terminal: sum(row["canonical_terminal"] == terminal for row in final_rows)
        for terminal in sorted({row["canonical_terminal"] for row in final_rows})
    }
    core = {
        "schema_version": MATRIX_SCHEMA,
        "label": MATRIX_LABEL, "not_a_wp015_repeat": True,
        "matrix_sha256": claimed, "source_commit": bundle["repository"]["head_commit"],
        "jobs": final_rows, "planned_jobs": 130,
        "terminal_counts": terminal_counts,
        "physical_requests_fresh": physical_requests_fresh,
        "physical_requests_cumulative": physical_requests_fresh + prior_requests,
        "provider_cost_fresh": provider_cost_fresh,
        "provider_cost_cumulative": provider_cost_cumulative,
        "total_cost_fresh": sum(provider_cost_fresh.values()),
        "total_cost_cumulative": cost.spent,
        "actual_heavy_calls": heavy,
        "roots_before": roots_before, "roots_after": roots_after,
        "all_roots_unchanged": roots_before == roots_after,
        "repository_after": base.assert_clean_repository(bundle["repository"]["head_commit"]),
        "queue_wall_seconds": time.monotonic() - started,
        "precommit_qor_feedback_count": sum(int(row.get("planner_evidence", {}).get("precommit_qor_feedback_count", 0)) for row in executed),
        "precommit_mutation_count": sum(int(row.get("planner_evidence", {}).get("precommit_mutation_count", 0)) for row in executed),
        "postcommit_resubmission_count": sum(int(row.get("planner_evidence", {}).get("postcommit_resubmission_count", 0)) for row in executed),
        "sealed_holdouts_accessed": False, "track_pooling_performed": False,
    }
    core["accepted_for_functional_validation"] = bool(
        len(final_rows) == 130 and roots_before == roots_after
        and heavy <= heavy_cap
        and physical_requests_fresh + prior_requests <= request_cap
        and cost.spent <= 40.0
        and all(provider_cost_cumulative[p] <= CAPS[p] for p in CAPS)
        and "BUDGET_BLOCKED" not in terminal_counts
        and "CONTROLLER_FAILURE" not in terminal_counts
        and core["precommit_qor_feedback_count"] == 0
        and core["precommit_mutation_count"] == 0
        and core["postcommit_resubmission_count"] == 0
    )
    value = core | {"aggregate_sha256": digest(core)}
    write(args.out / "physical-aggregate.json", value)
    print(json.dumps({key: value[key] for key in (
        "accepted_for_functional_validation", "planned_jobs", "terminal_counts",
        "physical_requests_fresh", "total_cost_fresh", "actual_heavy_calls",
        "queue_wall_seconds",
    )}, sort_keys=True))
    return 0 if value["accepted_for_functional_validation"] else 2


async def resume_backend(args: argparse.Namespace) -> int:
    matrix = _verified_object(args.matrix, "matrix_sha256")
    planning = _verified_object(args.planning_stop, "aggregate_sha256")
    amendment = _verified_object(args.amendment, "resume_sha256")
    if (
        amendment.get("status") != "FROZEN"
        or amendment.get("matrix_sha256") != matrix["matrix_sha256"]
        or amendment.get("planning_stop_aggregate_sha256") != planning["aggregate_sha256"]
    ):
        raise SystemExit("backend_resume_binding_invalid")
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    base.assert_clean_repository(amendment["repository"]["head_commit"])
    configure_matrix(matrix["kimi_endpoint"]["endpoint_tag"])
    roots_before = {root_id: file_sha256(root["root_path"]) for root_id, root in matrix["roots"].items()}
    if any(roots_before[root_id] != row["sha256"] for root_id, row in matrix["roots"].items()):
        raise SystemExit("root_hash_mismatch_before_resume")
    jobs = {row["job_id"]: row for row in matrix["jobs"]}
    planned_rows = {row["job_id"]: row for row in planning["planned_rows"]}
    dispatch_ids = amendment["dispatch_job_ids"]
    if set(dispatch_ids) != {job_id for job_id, row in planned_rows.items() if row["canonical_terminal"] == "PLANNED_COMMIT"}:
        raise SystemExit("backend_resume_dispatch_set_mismatch")
    reconstructed = [
        _reconstruct_planned_item(
            row=planned_rows[job_id], job=jobs[job_id], bundle=matrix,
            capture=args.capture, planning_dir=args.planning_stop.parent / "planning",
        )
        for job_id in dispatch_ids
    ]
    heavy_cap = int(matrix["guards"]["implementation_heavy_calls"])
    remaining_heavy = heavy_cap
    allocations: dict[str, dict[str, Any]] = {}
    for item in sorted(reconstructed, key=lambda value: digest(value[0]["job_id"])):
        required = legacy._required_heavy(item)
        row = item[0]
        allowed = required <= remaining_heavy
        allocations[row["job_id"]] = {
            "required_heavy_calls": required, "dispatch_allowed": allowed,
            "remaining_before": remaining_heavy,
            "allocation_basis": "SHA256(job_id); QoR-independent",
        }
        if allowed:
            remaining_heavy -= required
        allocations[row["job_id"]]["remaining_after"] = remaining_heavy
    if not all(row["dispatch_allowed"] for row in allocations.values()):
        raise SystemExit("backend_resume_heavy_budget_insufficient")
    started = time.monotonic()
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def execute_one(item: Any) -> dict[str, Any]:
        row, registry, compiled, candidate = item
        allocation = allocations[row["job_id"]]
        root = Path(matrix["roots"][row["root_id"]]["root_path"])
        execution_dir = args.out / "execution" / row["job_id"]
        async with semaphore:
            macro = row.get("selected_macro") or base._macro_from_candidate(candidate)
            if macro:
                result = await base._execute_macro(root=root, flow=macro, out=execution_dir, python=args.python)
                result.setdefault("actual_light_mutations", None)
            else:
                result = await base._execute_typed(
                    compiled=compiled, registry=registry, root=root, out=execution_dir,
                    python=args.python, vivado=args.vivado,
                )
            write(execution_dir / "execution-summary.json", result)
            if int(result.get("actual_heavy_calls") or 0) > allocation["required_heavy_calls"]:
                result = result | {"canonical_endpoint": "CONTROLLER_FAILURE", "accounting_error": "observed_heavy_calls_exceeded_predispatch_reservation"}
            evidence = legacy.boundary_evidence(candidate=candidate, execution_started=True, execution=result)
            return row | {
                "canonical_terminal": result["canonical_endpoint"], "execution_started": True,
                "execution": result, "actual_heavy_calls": result.get("actual_heavy_calls", 0),
                "actual_light_mutations": result.get("actual_light_mutations"),
                "execution_allocation": allocation, "boundary_evidence": evidence,
                "boundary_runtime_exercised": evidence["boundary_runtime_exercised"],
            }

    executed = await asyncio.gather(*(execute_one(item) for item in reconstructed))
    terminals = []
    for job_id in amendment["no_backend_job_ids"]:
        row = planned_rows[job_id]
        terminals.append(row | {
            "execution_started": False, "actual_heavy_calls": 0, "actual_light_mutations": 0,
            "backend_skip_reason": "PRE_BACKEND_SCIENTIFIC_TERMINAL",
            "boundary_evidence": legacy.boundary_evidence(candidate=None, execution_started=False, execution=None),
            "boundary_runtime_exercised": False,
        })
    references = _reference_results(matrix)
    by_id = {row["job_id"]: row for row in [*references, *terminals, *executed]}
    final_rows = [by_id[row["job_id"]] for row in matrix["jobs"]]
    roots_after = {root_id: file_sha256(root["root_path"]) for root_id, root in matrix["roots"].items()}
    heavy = sum(int(row.get("actual_heavy_calls") or 0) for row in executed)
    terminal_counts = {terminal: sum(row["canonical_terminal"] == terminal for row in final_rows) for terminal in sorted({row["canonical_terminal"] for row in final_rows})}
    ledger = amendment["resource_ledger_before_backend"]
    core = {
        "schema_version": MATRIX_SCHEMA, "label": MATRIX_LABEL, "not_a_wp015_repeat": True,
        "matrix_sha256": matrix["matrix_sha256"], "backend_resume_sha256": amendment["resume_sha256"],
        "source_commit": matrix["repository"]["head_commit"],
        "execution_source_commit": amendment["repository"]["head_commit"],
        "jobs": final_rows, "planned_jobs": 130, "terminal_counts": terminal_counts,
        "physical_requests_fresh": planning["physical_requests_fresh"],
        "physical_requests_cumulative": ledger["physical_model_requests"],
        "provider_cost_fresh": planning["provider_cost_fresh"],
        "provider_cost_cumulative": ledger["provider_cost_usd"],
        "total_cost_fresh": sum(planning["provider_cost_fresh"].values()),
        "total_cost_cumulative": ledger["total_cost_usd"], "actual_heavy_calls": heavy,
        "roots_before": roots_before, "roots_after": roots_after,
        "all_roots_unchanged": roots_before == roots_after,
        "repository_after": base.assert_clean_repository(amendment["repository"]["head_commit"]),
        "queue_wall_seconds": time.monotonic() - started,
        "backend_dispatched_jobs": len(executed), "pre_backend_terminal_jobs": len(terminals),
        "provider_failure_is_per_cell_scientific_terminal": True,
        "precommit_qor_feedback_count": sum(int(row.get("planner_evidence", {}).get("precommit_qor_feedback_count", 0)) for row in executed),
        "precommit_mutation_count": sum(int(row.get("planner_evidence", {}).get("precommit_mutation_count", 0)) for row in executed),
        "postcommit_resubmission_count": sum(int(row.get("planner_evidence", {}).get("postcommit_resubmission_count", 0)) for row in executed),
        "sealed_holdouts_accessed": False, "track_pooling_performed": False,
    }
    core["accepted_for_functional_validation"] = bool(
        len(final_rows) == 130 and len(executed) == 112 and len(terminals) == 14
        and roots_before == roots_after and heavy <= heavy_cap
        and ledger["physical_model_requests"] <= matrix["guards"]["physical_model_requests"]
        and ledger["total_cost_usd"] <= matrix["guards"]["api_cost_usd"]
        and all(ledger["provider_cost_usd"][p] <= CAPS[p] for p in CAPS)
        and "BUDGET_BLOCKED" not in terminal_counts and "CONTROLLER_FAILURE" not in terminal_counts
        and core["precommit_qor_feedback_count"] == 0 and core["precommit_mutation_count"] == 0
        and core["postcommit_resubmission_count"] == 0
    )
    value = core | {"aggregate_sha256": digest(core)}
    write(args.out / "physical-aggregate.json", value)
    print(json.dumps({key: value[key] for key in (
        "accepted_for_functional_validation", "backend_dispatched_jobs", "pre_backend_terminal_jobs",
        "terminal_counts", "actual_heavy_calls", "queue_wall_seconds",
    )}, sort_keys=True))
    return 0 if value["accepted_for_functional_validation"] else 2


def _child_path(row: Mapping[str, Any], run: Path) -> Path:
    execution = row.get("execution") or {}
    expected = (execution.get("child") or {}).get("sha256")
    direct = execution.get("child_path")
    # The execution record is the canonical publication boundary. Some macro
    # wrappers retain an additional byte-identical copy inside their run tree;
    # that is not ambiguous provenance when the bound direct path still exists.
    if direct:
        canonical = Path(str(direct))
        if canonical.is_file() and file_sha256(canonical) == expected:
            return canonical
    candidates = list((run / "execution" / row["job_id"]).rglob("*.dcp"))
    matches = [path for path in candidates if path.is_file() and file_sha256(path) == expected]
    unique = {str(path.resolve()): path for path in matches}
    if len(unique) != 1:
        raise RuntimeError(f"unique_child_path_not_found:{row['job_id']}:{len(unique)}")
    return next(iter(unique.values()))


def functional_plan(args: argparse.Namespace) -> int:
    aggregate = read(args.run / "physical-aggregate.json")
    claimed = aggregate.pop("aggregate_sha256")
    if digest(aggregate) != claimed or not aggregate.get("accepted_for_functional_validation"):
        raise SystemExit("physical_aggregate_not_accepted")
    aggregate["aggregate_sha256"] = claimed
    matrix = read(args.matrix)
    cases = []
    for row in aggregate["jobs"]:
        if row["canonical_terminal"] != "VALID_COMPLETION":
            continue
        root = Path(matrix["roots"][row["root_id"]]["root_path"])
        child = _child_path(row, args.run)
        cases.append({
            "case_id": row["job_id"],
            "job_id": row["job_id"],
            "root_id": row["root_id"],
            "model": row["model"],
            "subclass": row["subclass"],
            "language": row.get("selected_language") or "M",
            "golden_dcp": str(root), "golden_sha256": file_sha256(root),
            "revised_dcp": str(child), "revised_sha256": file_sha256(child),
            "timeout_class": "ordinary",
        })
    if len(cases) > 160:
        raise SystemExit("functional_validation_case_cap_exceeded")
    waves = [
        {"wave": index // 2, "max_workers": 2, "case_ids": [row["case_id"] for row in cases[index:index + 2]]}
        for index in range(0, len(cases), 2)
    ]
    core = {
        "schema_version": "dof.wp014e-functional-validation-plan.v1",
        "physical_aggregate_sha256": claimed,
        "validator": {
            "vectors": 200, "precheck_vectors": 100,
            "strict_pass_requires_phase2": True,
            "ordinary_timeout_seconds": 7200,
            "large_timeout_seconds": 7200,
            "term_grace_seconds": 30, "sample_interval_seconds": 5,
            "single_case_peak_rss_stop_bytes": 137438953472,
            "single_case_swap_growth_stop_bytes": 4294967296,
            "artifact_bytes_stop": 107374182400,
        },
        "boundaries": {
            "new_scientific_jobs": 0, "provider_calls": 0,
            "sealed_holdout_accesses": 0,
            "maximum_parallel_validators": 2,
            "root_and_child_inputs_read_only": True,
            "self_control_required": False,
        },
        "waves": waves, "cases": cases,
    }
    value = core | {"plan_sha256": digest(core)}
    if args.output.exists():
        raise FileExistsError(args.output)
    write(args.output, value)
    print(json.dumps({"functional_cases": len(cases), "waves": len(waves), "plan_sha256": value["plan_sha256"]}, sort_keys=True))
    return 0


def _fmax_pair(row: Mapping[str, Any], target_period: float) -> tuple[float | None, float | None]:
    execution = row.get("execution") or {}
    initial = execution.get("initial_fmax_mhz")
    final = execution.get("final_fmax_mhz")
    if initial is not None and final is not None:
        return float(initial), float(final)
    pre, post = execution.get("pre_timing") or {}, execution.get("post_timing") or {}
    if pre.get("wns") is None or post.get("wns") is None:
        return None, None
    return (
        1000.0 / (target_period - float(pre["wns"])),
        1000.0 / (target_period - float(post["wns"])),
    )


def finalize_matrix(args: argparse.Namespace) -> int:
    matrix = read(args.matrix)
    physical = read(args.run / "physical-aggregate.json")
    physical_claim = physical.pop("aggregate_sha256")
    if digest(physical) != physical_claim or not physical.get("accepted_for_functional_validation"):
        raise SystemExit("physical_aggregate_not_accepted")
    physical["aggregate_sha256"] = physical_claim
    validation = read(args.validation / "aggregate.json")
    validation_results = {row["case"]["job_id"]: row for row in validation["results"]}
    valid_rows = [row for row in physical["jobs"] if row["canonical_terminal"] == "VALID_COMPLETION"]
    if set(validation_results) != {row["job_id"] for row in valid_rows}:
        raise SystemExit("functional_result_binding_incomplete")
    final_rows = []
    for row in physical["jobs"]:
        root = matrix["roots"][row["root_id"]]
        if row["canonical_terminal"] == "VALID_ROOT_REFERENCE":
            final_rows.append(dict(row) | {
                "functional_terminal": "NOT_APPLICABLE_ROOT_REFERENCE",
                "scientific_outcome": {
                    "physical_endpoint": "VALID_ROOT_REFERENCE",
                    "functional_pass": None,
                    "valid_child_conditional_delta_fmax_mhz": None,
                    "root_fallback_deployment_delta_fmax_mhz": 0.0,
                },
            })
            continue
        result = validation_results.get(row["job_id"])
        functional_terminal = None
        if result is not None:
            functional_terminal = classify_functional_validation(
                result.get("validator_report"),
                returncode=result.get("returncode"),
                timed_out=bool(result.get("timed_out")),
            )
        initial, final = _fmax_pair(row, float(root["target_period_ns"]))
        child_sha = ((row.get("execution") or {}).get("child") or {}).get("sha256")
        delta = score_fmax_delta(
            root_fmax_mhz=initial, child_fmax_mhz=final,
            root_sha256=root["sha256"], child_sha256=child_sha,
            epsilon_fmax=0.0,
        )
        outcome = canonical_scientific_outcome(
            physical_endpoint=row["canonical_terminal"],
            functional_terminal=functional_terminal, delta=delta,
        )
        final_rows.append(dict(row) | {
            "functional_terminal": functional_terminal,
            "functional_result_sha256": result.get("result_sha256") if result else None,
            "fmax_delta": delta, "scientific_outcome": outcome,
        })
    functional_values = {row.get("functional_terminal") for row in final_rows}
    functional_counts = {
        str(terminal): sum(row.get("functional_terminal") == terminal for row in final_rows)
        for terminal in sorted(functional_values, key=lambda value: str(value))
    }
    core = {
        "schema_version": MATRIX_SCHEMA,
        "label": MATRIX_LABEL, "not_a_wp015_repeat": True,
        "matrix_sha256": matrix["matrix_sha256"],
        "physical_aggregate_sha256": physical_claim,
        "functional_aggregate_sha256": validation["aggregate_sha256"],
        "jobs": final_rows, "jobs_count": len(final_rows),
        "terminal_counts": physical["terminal_counts"],
        "functional_terminal_counts": functional_counts,
        "provider_cost_fresh": physical["provider_cost_fresh"],
        "provider_cost_cumulative": physical["provider_cost_cumulative"],
        "total_cost_fresh": physical["total_cost_fresh"],
        "total_cost_cumulative": physical["total_cost_cumulative"],
        "physical_requests_fresh": physical["physical_requests_fresh"],
        "physical_requests_cumulative": physical["physical_requests_cumulative"],
        "actual_heavy_calls": physical["actual_heavy_calls"],
        "functional_validation_cases": len(validation_results),
        "queue_wall_seconds": physical["queue_wall_seconds"],
        "validator_wall_seconds": validation["ended_epoch_seconds"] - validation["started_epoch_seconds"],
        "all_roots_unchanged": physical["all_roots_unchanged"],
        "sealed_holdouts_accessed": False, "track_pooling_performed": False,
    }
    core["accepted_for_evidence_review"] = bool(
        len(final_rows) == 130
        and len(validation_results) == len(valid_rows) <= 160
        and validation.get("stopped_reason") is None
        and not validation.get("owned_process_survivors")
        and physical["all_roots_unchanged"]
        and physical["actual_heavy_calls"] <= matrix["guards"]["implementation_heavy_calls"]
        and physical["physical_requests_cumulative"] <= matrix["guards"]["physical_model_requests"]
        and physical["total_cost_cumulative"] <= 40.0
    )
    value = core | {"aggregate_sha256": digest(core)}
    if args.output.exists():
        raise FileExistsError(args.output)
    write(args.output, value)
    print(json.dumps({key: value[key] for key in (
        "accepted_for_evidence_review", "jobs_count", "terminal_counts",
        "functional_terminal_counts", "total_cost_fresh", "actual_heavy_calls",
        "functional_validation_cases", "queue_wall_seconds", "validator_wall_seconds",
    )}, sort_keys=True))
    return 0 if value["accepted_for_evidence_review"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    commands = value.add_subparsers(dest="command", required=True)
    admission = commands.add_parser("provider-admission")
    admission.add_argument("--bundle", type=Path, required=True)
    admission.add_argument("--capture", type=Path, required=True)
    admission.add_argument("--out", type=Path, required=True)
    admission.add_argument("--max-parallel", type=int, default=4)
    freeze = commands.add_parser("freeze-matrix")
    freeze.add_argument("--bundle", type=Path, required=True)
    freeze.add_argument("--admission", type=Path, required=True)
    freeze.add_argument("--kimi-endpoint", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run-matrix")
    run.add_argument("--matrix", type=Path, required=True)
    run.add_argument("--capture", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--python", type=Path, required=True)
    run.add_argument("--vivado", type=Path, required=True)
    run.add_argument("--max-parallel", type=int, default=8)
    resume_freeze = commands.add_parser("freeze-backend-resume")
    resume_freeze.add_argument("--matrix", type=Path, required=True)
    resume_freeze.add_argument("--planning-stop", type=Path, required=True)
    resume_freeze.add_argument("--output", type=Path, required=True)
    resume = commands.add_parser("resume-backend")
    resume.add_argument("--matrix", type=Path, required=True)
    resume.add_argument("--planning-stop", type=Path, required=True)
    resume.add_argument("--amendment", type=Path, required=True)
    resume.add_argument("--capture", type=Path, required=True)
    resume.add_argument("--out", type=Path, required=True)
    resume.add_argument("--python", type=Path, required=True)
    resume.add_argument("--vivado", type=Path, required=True)
    resume.add_argument("--max-parallel", type=int, default=8)
    functional = commands.add_parser("functional-plan")
    functional.add_argument("--matrix", type=Path, required=True)
    functional.add_argument("--run", type=Path, required=True)
    functional.add_argument("--output", type=Path, required=True)
    final = commands.add_parser("finalize-matrix")
    final.add_argument("--matrix", type=Path, required=True)
    final.add_argument("--run", type=Path, required=True)
    final.add_argument("--validation", type=Path, required=True)
    final.add_argument("--output", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    if args.command == "provider-admission":
        if not 1 <= args.max_parallel <= 8:
            raise SystemExit("provider_parallelism_must_be_1_to_8")
        return asyncio.run(provider_admission(args))
    if args.command == "freeze-matrix":
        return freeze_matrix(args)
    if args.command == "run-matrix":
        if not 1 <= args.max_parallel <= 8:
            raise SystemExit("weekday_parallelism_must_be_1_to_8")
        return asyncio.run(run_matrix(args))
    if args.command == "freeze-backend-resume":
        return freeze_backend_resume(args)
    if args.command == "resume-backend":
        if not 1 <= args.max_parallel <= 8:
            raise SystemExit("weekday_parallelism_must_be_1_to_8")
        return asyncio.run(resume_backend(args))
    if args.command == "functional-plan":
        return functional_plan(args)
    if args.command == "finalize-matrix":
        return finalize_matrix(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
