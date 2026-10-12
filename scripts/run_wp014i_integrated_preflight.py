#!/usr/bin/env python3
"""Run the exact 28-cell development-only WP-014I integrated preflight."""

from __future__ import annotations

import argparse
import asyncio
import collections
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
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp013a_e0 as base  # noqa: E402
import run_wp014 as legacy  # noqa: E402
import run_wp014c_scientific as wp014c  # noqa: E402
import run_wp014e as wp014e  # noqa: E402
from optimizer_pipeline.dof.multi_provider import (  # noqa: E402
    MAX_OUTPUT_TOKENS, PROFILES, StrictMultiProviderTransport,
)
from optimizer_pipeline.dof.provider import ProviderRequestBinding, ProviderSettings  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import (  # noqa: E402
    boundary_evidence, strict_boundary_certificate,
)
from optimizer_pipeline.dof.single_submission.action_surface_v3 import (  # noqa: E402
    M_ACTION_KEYS, registry_sha256,
)
from optimizer_pipeline.dof.single_submission.contract_v5 import (  # noqa: E402
    CONTRACT_VERSION, PLANNING_CONTEXT_V22, PROMPT_VERSION_V3,
)
from optimizer_pipeline.dof.single_submission.contract_v6 import (  # noqa: E402
    PLANNER_MESSAGE_V6, PROTOCOL_EPOCH, prompt_for_cell,
)
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402
from scripts.run_wp014f_validation_recon import file_sha256, object_sha256  # noqa: E402
from scripts.run_wp014i_validation_canary import run_memory_aware, write  # noqa: E402


SCHEMA = "dof.wp014i-integrated-preflight.v1"
LABEL = "WP014I_PREFLIGHT_ONLY"


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def verify_embedded_hash(value: Mapping[str, Any], field: str) -> None:
    """Verify a canonical self-hash without mutating the loaded evidence."""
    claimed = value.get(field)
    unsigned = dict(value)
    unsigned.pop(field, None)
    if not isinstance(claimed, str) or digest(unsigned) != claimed:
        raise ValueError("embedded_hash_mismatch:" + field)


def superseded_attempt_accounting(path: Path | None) -> dict[str, Any]:
    if path is None:
        core = {
            "present": False, "path": None, "exchange_count": 0,
            "physical_requests": 0, "cost_usd": 0.0, "exchange_sha256": [],
        }
        return core | {"accounting_sha256": object_sha256(core)}
    exchanges = sorted(path.glob("planning/*/private/turn-*-exchange.json"))
    rows = []
    for exchange in exchanges:
        value = read(exchange)
        requests = value.get("physical_requests")
        if not isinstance(requests, int) or isinstance(requests, bool) or requests not in {0, 1}:
            raise ValueError("superseded_physical_request_count_invalid:" + str(exchange))
        raw_cost = value.get("derived_cost_usd")
        if raw_cost is None:
            raw_cost = value.get("provider_reported_cost_usd")
        cost = float(raw_cost or 0.0)
        if cost < 0:
            raise ValueError("superseded_provider_cost_negative:" + str(exchange))
        rows.append({
            "relative_path": str(exchange.relative_to(path)),
            "sha256": file_sha256(exchange), "physical_requests": requests,
            "cost_usd": cost, "provider": value.get("canonical_provider"),
        })
    core = {
        "present": True, "path": str(path), "exchange_count": len(rows),
        "physical_requests": sum(row["physical_requests"] for row in rows),
        "cost_usd": sum(row["cost_usd"] for row in rows),
        "exchange_sha256": [row["sha256"] for row in rows],
        "providers": dict(collections.Counter(str(row["provider"]) for row in rows)),
        "reason": "shared_turn_accounting_schema_defect_before_backend_dispatch",
        "backend_started": False,
    }
    return core | {"accounting_sha256": object_sha256(core)}


def resolve_execution_child(execution: Mapping[str, Any], output: Path) -> Path | None:
    """Resolve only contract-owned child locations and bind them to the recorded hash."""
    child = execution.get("child")
    expected = child.get("sha256") if isinstance(child, Mapping) else None
    candidates = [
        execution.get("child_path"), execution.get("output_dcp"),
        output / "child.dcp", output / "vivado/child.dcp",
        output / "suffix/child.dcp", output / "suffix/vivado/child.dcp",
        output / "rapidwright/child.dcp",
    ]
    macro = execution.get("macro_execution")
    if isinstance(macro, Mapping):
        candidates.append(macro.get("child_path"))
    matches = []
    for raw in candidates:
        path = Path(str(raw)) if raw else None
        if path and path.is_file() and (expected is None or file_sha256(path) == expected):
            matches.append(path.resolve())
    unique = sorted(set(matches))
    if not unique:
        return None
    if len(unique) > 1:
        raise ValueError("multiple_hash_matching_execution_children:" + ",".join(map(str, unique)))
    return unique[0]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def jobs(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    full_hash = digest({"coverage_regime": "FULL_M8", "actions": M_ACTION_KEYS})
    class_contract = {
        "NEUTRAL_L4": ("N_NEUTRAL", "FREE_CHOICE", "L4", "L4"),
        "M_ONLY": ("C_CONDITIONED", "M_ONLY", "L2", "C"),
        "P_BOUNDARY": ("C_CONDITIONED", "P_BOUNDARY", "L3", "C"),
        "G_STRICT_BOUNDARY": ("C_CONDITIONED", "G_BOUNDARY", "L4", "C"),
    }
    for model in config["models"]:
        for class_name in config["classes"]:
            assignment = config["assignments"][model][class_name]
            root_id = assignment if isinstance(assignment, str) else assignment["root_id"]
            mechanism = None if isinstance(assignment, str) else assignment["mechanism"]
            track, condition, level, agent_level = class_contract[class_name]
            rows.append({
                "job_id": "wp014i__" + digest({"model": model, "class": class_name})[:24],
                "phase": "WP014I_PREFLIGHT_ONLY",
                "label": LABEL,
                "not_a_scientific_repeat": True,
                "root_id": root_id,
                "model": model,
                "preflight_class": class_name,
                "strict_mechanism": mechanism,
                "level": level,
                "agent_level": agent_level,
                "protocol_epoch": PROTOCOL_EPOCH,
                "study_track": track,
                "coverage_regime": "FULL_M8",
                "language_condition": condition,
                "repeat": None,
                "admitted_action_keys": list(M_ACTION_KEYS),
                "mask_evidence_hash": full_hash,
            })
    if len(rows) != 28 or len({row["job_id"] for row in rows}) != 28:
        raise ValueError("integrated_preflight_requires_exactly_28_unique_jobs")
    mechanisms = collections.Counter(
        row["strict_mechanism"] for row in rows if row["strict_mechanism"]
    )
    if set(mechanisms) != {
        "SPATIAL_GENERATED_GEOMETRY", "NONPREFIX_MULTI_NET_BATCH",
        "COUPLED_HARDBLOCK_SITE_ALLOCATION",
    }:
        raise ValueError("integrated_preflight_g_mechanism_rotation_incomplete")
    return rows


class StrictPreflightSession(wp014c.ContextSessionV3):
    def accept(self, message: Mapping[str, Any]):
        job = legacy.CURRENT_JOB.get()
        if (
            job is not None
            and job.get("preflight_class") == "G_STRICT_BOUNDARY"
            and isinstance(message, Mapping)
            and message.get("kind") in {"DRAFT", "VALIDATE", "COMMIT"}
            and isinstance(message.get("candidate"), Mapping)
        ):
            certificate = strict_boundary_certificate(
                candidate=message["candidate"], execution_started=False, execution=None
            )
            if certificate.get("mechanism_family") != job.get("strict_mechanism"):
                return self._feedback(
                    "g_strict_mechanism_required_" + str(job.get("strict_mechanism")),
                    "$.candidate", message,
                )
        return super().accept(message)


def _objective(job: Mapping[str, Any]) -> str:
    if job["preflight_class"] == "NEUTRAL_L4":
        return "Choose freely among M, P, or G under the frozen contract."
    if job["preflight_class"] == "M_ONLY":
        return "Return one direct M-language COMMIT using one admitted Macro."
    if job["preflight_class"] == "P_BOUNDARY":
        return "Return one P-exclusive COMMIT ending in a scoreable routed state; inherited CALL_MACRO alone is insufficient."
    descriptions = {
        "SPATIAL_GENERATED_GEOMETRY": "Generate non-predefined geometry and consume it with ASSIGN_REGION before legal implementation.",
        "NONPREFIX_MULTI_NET_BATCH": "Generate a DIFF-derived non-prefix multi-net collection and consume it with BATCH_ROUTE_NETS.",
        "COUPLED_HARDBLOCK_SITE_ALLOCATION": "Generate a coupled hardblock/site allocation and consume it with APPLY_ALLOCATION before legal implementation.",
    }
    return descriptions[job["strict_mechanism"]]


def _prompt(_level: str) -> bytes:
    job = legacy.CURRENT_JOB.get()
    if job is None:
        raise RuntimeError("wp014i_job_context_missing")
    prompt = prompt_for_cell(
        study_track=job["study_track"],
        language_condition=job["language_condition"],
        level=job["agent_level"],
    )
    return prompt + b" WP014I development-only integrated preflight. " + _objective(job).encode()


def _planner_context(
    *, observation: Any, session: Any, contract: Mapping[str, Any],
    last_result: Mapping[str, Any] | None,
) -> bytes:
    job = legacy.CURRENT_JOB.get()
    if job is None:
        raise RuntimeError("wp014i_job_context_missing")
    return canonical_json_bytes({
        "schema_version": "dof.wp014i-integrated-planning-context.v1",
        "label": LABEL,
        "scientific_repeat": False,
        "protocol_epoch": PROTOCOL_EPOCH,
        "study_track": job["study_track"],
        "coverage_regime": job["coverage_regime"],
        "language_condition": job["language_condition"],
        "preflight_class": job["preflight_class"],
        "required_strict_mechanism": job.get("strict_mechanism"),
        "preflight_objective": _objective(job),
        "common_objective": contract["common_objective"],
        "common_observation": dict(observation.payload),
        "common_observation_sha256": observation.sha256,
        "common_query_catalogue": session.queries.catalogue,
        "language_contract": contract,
        **session.public_status(),
        "previous_result": last_result,
        "qor_feedback_present": False,
        "holdout_or_transfer_data_present": False,
    })


def _binding(
    *, job: Mapping[str, Any], model: str, turn: int, context: bytes,
    prompt: bytes, catalogue_hash: str, reserve: float, phase: str = "preflight",
) -> ProviderRequestBinding:
    del phase
    profile = PROFILES[model]
    identity = f"{job['job_id']}:{turn}"
    return ProviderRequestBinding(
        run_id="wp014i-preflight-" + digest(identity)[:24],
        spec_hash=digest({"protocol": SCHEMA, "job": job}),
        experiment_id="WP-014I-INTEGRATED-PREFLIGHT",
        condition_id="condition-" + digest({
            "class": job["preflight_class"], "root": job["root_id"],
        })[:20],
        repeat_id="NO_REPEAT", decision_id="decision-" + digest(job["job_id"])[:20],
        context_bytes=context, prompt_bytes=prompt,
        prompt_version="dof.wp014i-integrated-preflight-prompt.v1",
        action_interface_version=PLANNER_MESSAGE_V6,
        action_interface_hash=digest(PLANNER_MESSAGE_V6),
        context_interface_version=PLANNING_CONTEXT_V22,
        context_interface_hash=digest(context),
        catalogue_version=f"{PROTOCOL_EPOCH}.wp014i.{job['preflight_class'].lower()}",
        catalogue_hash=catalogue_hash, capability_hash=catalogue_hash,
        mask_evidence_hash=job["mask_evidence_hash"],
        provider_role="wp014i_integrated_preflight_planner",
        provider=profile.provider, model_id=profile.canonical_model_id,
        adapter_version=profile.adapter_version,
        settings=ProviderSettings(
            temperature=None, max_tokens=MAX_OUTPUT_TOKENS, seed=None,
            response_format="json_object", timeout_seconds=300.0,
            max_retries=0, retry_on=(), max_requests=1,
            max_usd=reserve, max_cost_per_request_usd=reserve,
            unknown_cost_policy="reject", unknown_usage_policy="reject",
        ),
    )


def configure(config: Mapping[str, Any], endpoint_tag: str) -> None:
    wp014e.configure_matrix(endpoint_tag)
    legacy.SCHEMA = SCHEMA
    legacy.MODELS = tuple(config["models"])
    legacy.MODEL_RESERVES = dict(config["provider_reserve_usd"])
    base.SCHEMA = SCHEMA
    base.MODELS = tuple(config["models"])
    base._binding = _binding
    base._prompt = _prompt
    base._planner_context = _planner_context
    base.SingleSubmissionSessionV2 = StrictPreflightSession


def _provider_profile_not_ready(admission: Mapping[str, Any], model: str) -> bool:
    rows = [row for row in admission["rows"] if row["model"] == model]
    return bool(rows) and not any(row["terminal"] == "MODEL_OUTPUT_COMPATIBLE" for row in rows)


def _macro_equivalence(bundle: Mapping[str, Any], capture: Path) -> dict[str, Any]:
    root = bundle["roots"]["amd_mini-isp"]
    _observation, private = base._load_trial(bundle, capture, "amd_mini-isp")
    from optimizer_pipeline.dof.single_submission.contract_v5 import compile_candidate_v5
    from optimizer_pipeline.dof.single_submission.language import CompileContext
    context = CompileContext(
        private["registry"], "ROUTED", private["predefined_regions"],
        private["geometry_bounds"], tuple(key.removeprefix("macro.") for key in M_ACTION_KEYS),
        fanout_admitted=bool(private.get("fanout_admitted", False)),
    )
    action_key = "macro.phys_opt"
    direct = compile_candidate_v5({"language": "M", "action_key": action_key}, context)
    embedded = compile_candidate_v5({
        "language": "P",
        "steps": [{"step_id": "s1", "op": "CALL_MACRO", "args": {"action_key": action_key}}],
    }, context)
    def normalized(program: Any) -> list[dict[str, Any]]:
        return [{
            "operation": step.operation,
            "public_args": dict(step.public_args),
            "private_args": dict(step.private_args),
            "heavy_cost": step.heavy_cost,
            "light_cost": step.light_cost,
            "event_class": step.event_class,
        } for step in program.host_steps]
    direct_steps, embedded_steps = normalized(direct), normalized(embedded)
    core = {
        "schema_version": "dof.wp014i-call-macro-equivalence.v1",
        "root_sha256": root["sha256"],
        "action_key": action_key,
        "direct_steps": direct_steps,
        "embedded_steps": embedded_steps,
        "host_step_equivalent": direct_steps == embedded_steps,
        "heavy_cost_equivalent": direct.heavy_calls == embedded.heavy_calls,
        "light_cost_equivalent": direct.light_mutations == embedded.light_mutations,
        "direct_heavy_calls": direct.heavy_calls,
        "embedded_heavy_calls": embedded.heavy_calls,
    }
    return core | {"equivalence_sha256": object_sha256(core)}


async def run(args: argparse.Namespace) -> int:
    if args.out.exists():
        raise FileExistsError(args.out)
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    config = read(args.config)
    superseded = superseded_attempt_accounting(args.superseded_attempt)
    if file_sha256(Path(config["bundle"])) != config["bundle_sha256"]:
        raise ValueError("source_bundle_file_hash_mismatch")
    if file_sha256(Path(config["provider_admission"])) != config["provider_admission_sha256"]:
        raise ValueError("provider_admission_file_hash_mismatch")
    limits = config["limits"]
    if any(limits[key] for key in (
        "provider_retries", "fallbacks", "response_repairs",
        "holdout_or_transfer_optimization_calls",
    )):
        raise ValueError("integrated_preflight_firewall_not_zero")
    if not 1 <= args.max_parallel <= 8:
        raise ValueError("weekday_integrated_parallelism_must_be_1_to_8")
    bundle = read(Path(config["bundle"]))
    admission = read(Path(config["provider_admission"]))
    endpoint = read(Path(config["openrouter_endpoint"]))
    verify_embedded_hash(bundle, "bundle_sha256")
    verify_embedded_hash(admission, "aggregate_sha256")
    if endpoint.get("fallback_allowed") is not False or not endpoint.get("endpoint_tag"):
        raise ValueError("openrouter_endpoint_freeze_invalid")
    for model in config["profile_not_ready"]:
        if not _provider_profile_not_ready(admission, model):
            raise ValueError("profile_not_ready_not_supported_by_gate5:" + model)
    for model in config["models"]:
        profile = PROFILES[model]
        if model not in config["profile_not_ready"] and not os.environ.get(profile.credential_env):
            raise RuntimeError("provider_credential_missing:" + profile.credential_env)
    configure(config, endpoint["endpoint_tag"])
    all_jobs = jobs(config)
    for row in all_jobs:
        if row["root_id"] not in bundle["roots"]:
            raise ValueError("development_root_not_in_frozen_bundle:" + row["root_id"])
    roots_before = {
        root_id: file_sha256(Path(row["root_path"])) for root_id, row in bundle["roots"].items()
    }
    if any(roots_before[key] != row["sha256"] for key, row in bundle["roots"].items()):
        raise ValueError("development_root_hash_mismatch")
    args.out.mkdir(parents=True)
    write(args.out / "frozen-jobs.json", {
        "schema_version": "dof.wp014i-integrated-preflight-jobs.v1",
        "config_sha256": file_sha256(args.config),
        "repository": {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")},
        "jobs": all_jobs,
    })
    equivalence = _macro_equivalence(bundle, Path(config["capture"]))
    write(args.out / "call-macro-equivalence.json", equivalence)
    admission_cost = float(admission["api_cost_usd"])
    admission_requests = int(admission["physical_model_requests"])
    prior_cost = admission_cost + float(superseded["cost_usd"])
    prior_requests = admission_requests + int(superseded["physical_requests"])
    cost = base.SharedCost(float(limits["api_cost_cap_including_admission_usd"]), spent=prior_cost)
    planning_semaphore = asyncio.Semaphore(args.max_parallel)

    async def plan_one(job: Mapping[str, Any]):
        if job["model"] in config["profile_not_ready"]:
            return ({
                **job,
                "canonical_terminal": "PROFILE_NOT_READY_GATE5",
                "planning_wall_seconds": 0.0,
                "provider_requests": 0,
                "cost_usd": 0.0,
                "gate5_evidence_sha256": admission["aggregate_sha256"],
                "retry_count": 0, "fallback_count": 0, "repair_count": 0,
            }, None, None, None)
        async with planning_semaphore:
            return await legacy._plan_with_context(
                job, bundle, Path(config["capture"]), args.out, "", cost,
            )

    planned = await asyncio.gather(*(plan_one(job) for job in all_jobs))
    planned_rows = [item[0] for item in planned]
    requests = sum(int(row.get("provider_requests") or 0) for row in planned_rows)
    if requests + prior_requests > int(limits["provider_request_cap_including_admission"]):
        raise RuntimeError("integrated_provider_request_cap_exceeded")
    if cost.spent > float(limits["api_cost_cap_including_admission_usd"]):
        raise RuntimeError("integrated_provider_cost_cap_exceeded")

    allocations = {}
    remaining_heavy = int(limits["implementation_heavy_call_cap"])
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
        if row["canonical_terminal"] in base.TERMINALS or row["canonical_terminal"] == "PROFILE_NOT_READY_GATE5":
            evidence = boundary_evidence(candidate=candidate, execution_started=False, execution=None)
            return row | {
                "execution_started": False, "actual_heavy_calls": 0,
                "actual_light_mutations": 0, "execution_allocation": allocation,
                "boundary_evidence": evidence,
                "strict_boundary_certificate": strict_boundary_certificate(
                    candidate=candidate, execution_started=False, execution=None
                ),
            }
        if not allocation["dispatch_allowed"]:
            evidence = boundary_evidence(candidate=candidate, execution_started=False, execution=None)
            return row | {
                "canonical_terminal": "BUDGET_BLOCKED", "execution_started": False,
                "actual_heavy_calls": 0, "actual_light_mutations": 0,
                "execution_allocation": allocation, "boundary_evidence": evidence,
            }
        root = Path(bundle["roots"][row["root_id"]]["root_path"])
        out = args.out / "execution" / row["job_id"]
        async with execution_semaphore:
            macro = row.get("selected_macro") or base._macro_from_candidate(candidate)
            if macro:
                result = await base._execute_macro(root=root, flow=macro, out=out, python=args.python)
                result.setdefault("actual_light_mutations", None)
            elif compiled is not None and registry is not None:
                result = await base._execute_typed(
                    compiled=compiled, registry=registry, root=root, out=out,
                    python=args.python, vivado=args.vivado,
                )
            else:
                result = {
                    "canonical_endpoint": "CONTROLLER_FAILURE",
                    "error": "planned_commit_missing_compiled_program",
                    "actual_heavy_calls": 0, "actual_light_mutations": 0,
                }
            write(out / "execution-summary.json", result)
        if int(result.get("actual_heavy_calls") or 0) > allocation["required_heavy_calls"]:
            result = result | {
                "canonical_endpoint": "CONTROLLER_FAILURE",
                "accounting_error": "observed_heavy_calls_exceeded_predispatch_reservation",
            }
        evidence = boundary_evidence(candidate=candidate, execution_started=True, execution=result)
        strict = strict_boundary_certificate(candidate=candidate, execution_started=True, execution=result)
        return row | {
            "canonical_terminal": result["canonical_endpoint"],
            "physical_terminal": result["canonical_endpoint"],
            "execution_started": True, "execution": result,
            "actual_heavy_calls": result.get("actual_heavy_calls", 0),
            "actual_light_mutations": result.get("actual_light_mutations"),
            "execution_allocation": allocation, "boundary_evidence": evidence,
            "boundary_runtime_exercised": evidence["boundary_runtime_exercised"],
            "strict_boundary_certificate": strict,
        }

    executed = await asyncio.gather(*(execute_one(item) for item in planned))
    validation_cases = []
    for row in executed:
        execution = row.get("execution") or {}
        child = resolve_execution_child(execution, args.out / "execution" / row["job_id"])
        if row["canonical_terminal"] != "VALID_COMPLETION" or child is None or not child.is_file():
            continue
        root = bundle["roots"][row["root_id"]]
        child_sha = file_sha256(child)
        validation_cases.append({
            "case_id": "integrated__" + row["job_id"],
            "root_id": row["root_id"], "memory_class": "normal",
            "mechanism": row.get("selected_language"),
            "golden_dcp": root["root_path"], "revised_dcp": str(child),
            "golden_sha256": root["sha256"], "revised_sha256": child_sha,
            "resolved_input_sha256": {"golden": root["sha256"], "revised": child_sha},
            "sealed_holdout_accesses": 0,
            "source_job_id": row["job_id"],
        })
    validation = None
    if validation_cases:
        validation_plan = read(REPO / "configs/wp014f/validation-recon-v1.json")
        validation_plan["boundaries"]["per_case_timeout_seconds"] = int(
            limits["validation_timeout_seconds"]
        )
        scheduler_config = {
            "limits": config["validation_scheduler"] | {
                "per_case_timeout_seconds": int(limits["validation_timeout_seconds"]),
            },
            "memory_estimates_gib": config["memory_estimates_gib"],
        }
        validation = run_memory_aware(
            validation_cases, validation_plan, args.out / "functional", scheduler_config
        )
        write(args.out / "functional" / "aggregate.json", validation)
    functional_by_job = {
        row["case"]["source_job_id"]: row for row in (validation or {}).get("results", [])
    }
    final_rows = []
    for row in executed:
        functional = functional_by_job.get(row["job_id"])
        updated = dict(row)
        if functional is not None:
            updated["functional_terminal"] = functional["terminal"]
            updated["functional_result_sha256"] = functional["result_sha256"]
            if functional["terminal"] != "MULTISEED_FUNCTIONAL_PASS":
                updated["canonical_terminal"] = functional["terminal"]
        final_rows.append(updated)
    roots_after = {
        root_id: file_sha256(Path(row["root_path"])) for root_id, row in bundle["roots"].items()
    }
    terminals = collections.Counter(row["canonical_terminal"] for row in final_rows)
    physical_valid = sum(row.get("physical_terminal") == "VALID_COMPLETION" for row in final_rows)
    functional_executed = len(functional_by_job)
    core = {
        "schema_version": SCHEMA,
        "identity": args.continuation_identity or config["identity"], "label": LABEL,
        "scientific_repeat": False,
        "repository": {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")},
        "config_sha256": file_sha256(args.config),
        "action_surface_registry_sha256": registry_sha256(),
        "planned_jobs": 28, "canonical_jobs": len(final_rows),
        "terminal_counts": dict(sorted(terminals.items())),
        "physical_valid_children": physical_valid,
        "functional_cases_executed": functional_executed,
        "physical_valid_children_all_received_v1": physical_valid == functional_executed,
        "physical_requests_fresh": requests,
        "physical_requests_provider_admission": admission_requests,
        "physical_requests_superseded_attempt": superseded["physical_requests"],
        "physical_requests_including_admission": requests + prior_requests,
        "api_cost_fresh_usd": cost.spent - prior_cost,
        "api_cost_provider_admission_usd": admission_cost,
        "api_cost_superseded_attempt_usd": superseded["cost_usd"],
        "api_cost_including_admission_usd": cost.spent,
        "superseded_attempt": superseded,
        "actual_heavy_calls": sum(int(row.get("actual_heavy_calls") or 0) for row in final_rows),
        "provider_retries": 0, "fallbacks": 0, "response_repairs": 0,
        "call_macro_equivalence": equivalence,
        "roots_before": roots_before, "roots_after": roots_after,
        "all_roots_unchanged": roots_before == roots_after,
        "owned_process_survivors": (validation or {}).get("owned_process_survivors", []),
        "holdout_or_transfer_optimization_calls": 0,
        "sealed_holdouts_accessed": False,
        "rows": final_rows,
        "validation": validation,
    }
    shared_defects = [
        row["job_id"] for row in final_rows
        if row["canonical_terminal"] in {"CONTROLLER_FAILURE", "MODEL_OR_ROUTE_DRIFT"}
    ]
    core["shared_defect_jobs"] = shared_defects
    core["accepted_for_freeze_review"] = bool(
        len(final_rows) == 28
        and not shared_defects
        and roots_before == roots_after
        and not core["owned_process_survivors"]
        and core["physical_valid_children_all_received_v1"]
        and equivalence["host_step_equivalent"]
        and equivalence["heavy_cost_equivalent"]
        and core["physical_requests_including_admission"] <= int(limits["provider_request_cap_including_admission"])
        and core["api_cost_including_admission_usd"] <= float(limits["api_cost_cap_including_admission_usd"])
        and core["actual_heavy_calls"] <= int(limits["implementation_heavy_call_cap"])
    )
    aggregate = core | {"aggregate_sha256": object_sha256(core)}
    write(args.out / "aggregate.json", aggregate)
    print(json.dumps({key: aggregate[key] for key in (
        "accepted_for_freeze_review", "planned_jobs", "terminal_counts",
        "physical_requests_fresh", "api_cost_fresh_usd", "actual_heavy_calls",
        "physical_valid_children", "functional_cases_executed", "shared_defect_jobs",
    )}, sort_keys=True))
    return 0 if aggregate["accepted_for_freeze_review"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--config", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--vivado", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=8)
    value.add_argument("--superseded-attempt", type=Path)
    value.add_argument("--continuation-identity")
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
