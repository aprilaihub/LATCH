#!/usr/bin/env python3
"""WP-014 Dual-Track DoF Protocol v1 controller.

This is a metadata/eligibility overlay on the accepted WP-013B execution
path.  It deliberately delegates observation, compilation, mutation,
validation, scoring and process ownership to that frozen implementation.
"""

from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
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
import run_wp013b_e0 as v3  # noqa: E402
from optimizer_pipeline.dof.openrouter import (  # noqa: E402
    OPENROUTER_ADAPTER_VERSION, OPENROUTER_E1_PROFILES,
    OpenRouterChatCompletionsTransport, build_openrouter_outbound,
    discover_openrouter_endpoints, discover_openrouter_models,
)
from optimizer_pipeline.dof.provider import ProviderRequestBinding, ProviderSettings  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import boundary_evidence  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v3 import (  # noqa: E402
    M_ACTION_KEYS, MAX_INPUT_BYTES, PLANNING_CONTEXT_V22, PROMPT_VERSION_V22,
)
from optimizer_pipeline.dof.single_submission.contract_v4 import (  # noqa: E402
    PLANNER_MESSAGE_V4, PROTOCOL_EPOCH, contract_for_cell,
    contract_hash_for_cell, prompt_for_cell, validate_protocol_cell,
)
from optimizer_pipeline.dof.single_submission.language import CompileContext, CompiledProgram  # noqa: E402
from optimizer_pipeline.dof.single_submission.observation_v3 import CommonQuerySurfaceV3  # noqa: E402
from optimizer_pipeline.dof.single_submission.planner import SystemBinding  # noqa: E402
from optimizer_pipeline.dof.single_submission.planner_v4 import SingleSubmissionSessionV4  # noqa: E402
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


SCHEMA = "dof.wp014-dual-track-repeat1.v1"
MODELS = (
    "openai/gpt-5.6-luna",
    "google/gemini-3.6-flash",
    "anthropic/claude-sonnet-5",
)
MODEL_RESERVES = {
    "openai/gpt-5.6-luna": 0.05,
    "google/gemini-3.6-flash": 0.05,
    "anthropic/claude-sonnet-5": 0.15,
}
CONDITIONS = ("M_ONLY", "P_EXCLUSIVE", "G_EXCLUSIVE")
LEVELS = ("L2", "L3", "L4")
PHASES = ("CALIBRATION", "N_K", "S_N", "C_S")
DEVELOPMENT_ROOT_IDS = frozenset({
    "amd_mini-isp", "corescore_500_mod", "finn_radioml", "logicnets_jscl",
    "rosetta_3d-rendering", "rosetta_digit-recognition", "rosetta_optical-flow",
    "rosetta_spam-filter", "vexriscv_re-place", "vexriscv_re-place_v2",
})
CURRENT_JOB: ContextVar[Mapping[str, Any] | None] = ContextVar("wp014_job", default=None)
OPENROUTER_COST_CAP_USD = 8.0
IMPLEMENTATION_HEAVY_CALL_CAP = 760
QUEUE_WALL_SECONDS_CAP = 129600


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


def _root_specs(roots_path: Path, stress_path: Path) -> list[dict[str, Any]]:
    roots_value, stress = read(roots_path), read(stress_path)
    derivations = {row["root_id"]: row for row in stress["derivation_table"]}
    if {row["root_id"] for row in roots_value["roots"]} != DEVELOPMENT_ROOT_IDS:
        raise ValueError("wp014_requires_exact_development_root_set")
    prefix = Path(roots_value["root_path_prefix"])
    rows = []
    for root in roots_value["roots"]:
        derived = derivations[root["root_id"]]
        rows.append(
            dict(root)
            | {
                "root_path": str(prefix / root["filename"]),
                "target_period_ns": derived["target_period_ns"],
                "target_period_evidence": "accepted 80-cell Macro panel root timing",
            }
        )
    return rows


def _runtime_level(condition: str) -> str:
    return {"M_ONLY": "L2", "P_EXCLUSIVE": "L3", "G_EXCLUSIVE": "L4"}[condition]


def _job_contract(job: Mapping[str, Any], admitted: tuple[str, ...]) -> dict[str, Any]:
    return contract_for_cell(
        study_track=job["study_track"], coverage_regime=job["coverage_regime"],
        language_condition=job["language_condition"], level=job["agent_level"],
        admitted_action_keys=admitted,
    )


class ContextSession(SingleSubmissionSessionV4):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        job = CURRENT_JOB.get()
        if job is None:
            raise RuntimeError("wp014_job_context_missing")
        kwargs["language_condition"] = job["language_condition"]
        super().__init__(*args, **kwargs)


def _language_contract(_level: str, admitted_action_keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    job = CURRENT_JOB.get()
    if job is None:
        raise RuntimeError("wp014_job_context_missing")
    return _job_contract(job, admitted_action_keys or M_ACTION_KEYS)


def _prompt(_level: str) -> bytes:
    job = CURRENT_JOB.get()
    if job is None:
        raise RuntimeError("wp014_job_context_missing")
    return prompt_for_cell(
        study_track=job["study_track"], language_condition=job["language_condition"],
        level=job["agent_level"],
    )


def _binding(
    *, job: Mapping[str, Any], model: str, turn: int, context: bytes,
    prompt: bytes, catalogue_hash: str, reserve: float, phase: str = "e0",
) -> ProviderRequestBinding:
    identity = f"{job['job_id']}:{turn}"
    return ProviderRequestBinding(
        run_id="wp014-" + digest(identity)[:24],
        spec_hash=digest({"protocol": SCHEMA, "job": job}),
        experiment_id="wp014-dual-track-repeat1",
        condition_id="condition-" + digest({
            "track": job["study_track"], "regime": job["coverage_regime"],
            "condition": job["language_condition"], "level": job["agent_level"],
        })[:20],
        repeat_id="1", decision_id="decision-" + digest(job["job_id"])[:20],
        context_bytes=context, prompt_bytes=prompt, prompt_version=PROMPT_VERSION_V22,
        action_interface_version=PLANNER_MESSAGE_V4,
        action_interface_hash=digest(PLANNER_MESSAGE_V4),
        context_interface_version=PLANNING_CONTEXT_V22,
        context_interface_hash=digest(context),
        catalogue_version=f"{PROTOCOL_EPOCH}.{job['study_track'].lower()}.{job['language_condition'].lower()}",
        catalogue_hash=catalogue_hash, capability_hash=catalogue_hash,
        mask_evidence_hash=job["mask_evidence_hash"],
        provider_role="wp014_single_submission_planner", provider="openrouter",
        model_id=model, adapter_version=OPENROUTER_ADAPTER_VERSION,
        settings=ProviderSettings(
            temperature=None, max_tokens=4096, seed=None, timeout_seconds=180,
            max_retries=0, retry_on=(), max_requests=1, max_usd=reserve,
            max_cost_per_request_usd=reserve, unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )


def _planner_context(*, observation: Any, session: Any, contract: Mapping[str, Any], last_result: Mapping[str, Any] | None) -> bytes:
    job = CURRENT_JOB.get()
    if job is None:
        raise RuntimeError("wp014_job_context_missing")
    return canonical_json_bytes(
        {
            "schema_version": PLANNING_CONTEXT_V22,
            "protocol_epoch": PROTOCOL_EPOCH,
            "study_track": job["study_track"],
            "coverage_regime": job["coverage_regime"],
            "language_condition": job["language_condition"],
            "common_objective": contract["common_objective"],
            "common_observation": dict(observation.payload),
            "common_observation_sha256": observation.sha256,
            "common_query_catalogue": session.queries.catalogue,
            "language_contract": contract,
            **session.public_status(),
            "previous_result": last_result,
            "qor_feedback_present": False,
        }
    )


def configure_base() -> None:
    base.SCHEMA = SCHEMA
    base.MODELS = MODELS
    base.MAX_INPUT_BYTES = MAX_INPUT_BYTES
    base.CommonQuerySurfaceV2 = CommonQuerySurfaceV3
    base.SingleSubmissionSessionV2 = ContextSession
    base._language_contract = _language_contract
    base._prompt = _prompt
    base._binding = _binding
    base._planner_context = _planner_context
    base.CAPTURE_SCHEMA = "dof.wp014-observation-capture.v1"
    base.CAPTURE_JOB_SCHEMA = "dof.wp014-observation-capture-job.v1"
    base.CAPTURE_JOB_PREFIX = "wp014-observe"
    base.HANDLE_NAMESPACE = "wp014-handles-v1"
    base.TRIAL_ID = "wp014-repeat1"
    base.QUERY_EVIDENCE_TAG = "wp014-frozen-read-only-capture"
    base.EXPECTED_OPENROUTER_COST_CAP_USD = OPENROUTER_COST_CAP_USD


async def capture(args: argparse.Namespace) -> int:
    roots = _root_specs(args.roots, args.stress)
    base.assert_clean_repository()
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def one(root: Mapping[str, Any]) -> dict[str, Any]:
        async with semaphore:
            root_path = Path(root["root_path"]).resolve(strict=True)
            if base.file_digest(root_path) != root["sha256"]:
                raise RuntimeError("root_hash_mismatch:" + root["root_id"])
            root_dir = args.out / root["root_id"]
            observation = await asyncio.to_thread(
                base.capture_real_observation, base._probe_job(root["root_id"], root["sha256"]),
                root_path, root_dir / "vivado-observation", decision_index=0, history=[],
                heavy_remaining=4, model_requests_remaining=4, vivado_exec=args.vivado,
                python_exec=args.python, repository_anchor=REPO,
                # The ten characterised development roots include designs
                # whose read-only timing report exceeds E0's 30-second smoke
                # bound.  This existing profile changes only time limits, not
                # the read-only operation catalogue or evidence schema.
                e1_protocol=True,
            )
            command = (
                str(args.python), str(REPO / "scripts/run_wp013_admission.py"),
                "--mode", "contract-probe", "--root", str(root_path),
                "--out", str(root_dir / "contract-probe"), "--python", str(args.python),
                "--vivado", str(args.vivado),
            )
            if getattr(args, "omit_vivado_fanout", False):
                command += ("--omit-vivado-fanout",)
            completed = await asyncio.to_thread(
                subprocess.run, command, cwd=REPO, text=True, capture_output=True, timeout=3600,
            )
            summary_path = root_dir / "contract-probe" / "admission-summary.json"
            summary = read(summary_path) if summary_path.exists() else None
            row = {
                "root_id": root["root_id"], "root_sha256": root["sha256"],
                "observation_sha256": observation["observation_sha256"],
                "probe_returncode": completed.returncode,
                "probe_stdout_sha256": digest(completed.stdout.encode()),
                "probe_stderr_sha256": digest(completed.stderr.encode()),
                "probe_summary": summary,
                "accepted": bool(
                    observation["origin"] == "real_read_only_vivado" and completed.returncode == 0
                    and summary is not None and summary.get("accepted")
                ),
            }
            write(root_dir / "capture-summary.json", row)
            return row

    rows = await asyncio.gather(*(one(root) for root in roots))
    core = {
        "schema_version": base.CAPTURE_SCHEMA, "roots": rows,
        "root_count": len(rows), "accepted": len(rows) == 10 and all(row["accepted"] for row in rows),
    }
    value = core | {"capture_sha256": digest(core)}
    write(args.out / "capture-aggregate.json", value)
    print(json.dumps({"accepted": value["accepted"], "roots": len(rows)}, sort_keys=True))
    return 0 if value["accepted"] else 2


async def preflight(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
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
        "schema_version": "dof.wp014-openrouter-preflight.v1",
        "models": {model: models["required_models"][model] for model in MODELS},
        "endpoints": selected,
        "expected_profiles": {model: dict(OPENROUTER_E1_PROFILES[model]) for model in MODELS},
        "context_capacity_checks": context, "required_context_tokens": required,
        "credential_included": False,
    }
    core["accepted"] = bool(
        all(core["models"].values()) and all(
            selected[m]["endpoint_available"] and selected[m]["required_parameters_supported"]
            for m in MODELS
        ) and all(context.values())
    )
    value = core | {"preflight_sha256": digest(core)}
    write(args.output, value)
    print(json.dumps({"accepted": value["accepted"], "context": context}, sort_keys=True))
    return 0 if value["accepted"] else 2


def _replace_placeholders(value: Any, context: CompileContext) -> Any:
    replacements: dict[str, Any] = {
        "<ADMITTED_ACTION_KEY>": "macro.phys_opt",
        "<PREDEFINED_REGION_HANDLE>": context.predefined_regions[0],
        "<COMPATIBLE_SITE_HANDLE>": context.registry.objects(object_type="site")[0].handle,
        "<X_MIN>": 0, "<X_MAX>": 0, "<Y_MIN>": 0, "<Y_MAX>": 0,
    }
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_placeholders(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, context) for key, item in value.items()}
    return value


async def provider_admission(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    if not read(args.preflight).get("accepted"):
        raise SystemExit("provider_preflight_not_accepted")
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    cost = base.SharedCost(OPENROUTER_COST_CAP_USD)

    async def request_one(model: str, condition: str | None) -> dict[str, Any]:
        profile_only = condition is None
        label = "profile" if profile_only else condition.lower()
        session_id = f"wp014-admission__{label}__{model.replace('/', '_')}"
        observation, _registry, compile_context, max_q = v3._synthetic_state(session_id)
        job = {
            "job_id": session_id, "root_id": "synthetic", "model": model,
            "protocol_epoch": PROTOCOL_EPOCH,
            "study_track": "N_NEUTRAL" if profile_only else "C_CONDITIONED",
            "coverage_regime": "FULL_M8",
            "language_condition": "FREE_CHOICE" if profile_only else condition,
            "agent_level": "L4" if profile_only else "C",
            "level": "L4" if profile_only else _runtime_level(condition),
            "repeat": 1, "mask_evidence_hash": digest("synthetic-full-m8"),
        }
        contract = _replace_placeholders(_job_contract(job, M_ACTION_KEYS), compile_context)
        catalogue_hash = digest(contract)
        system = SystemBinding.create(
            observation_hash=observation.sha256,
            root_binding=compile_context.registry.trial_binding_hash,
            catalogue_version=f"{PROTOCOL_EPOCH}.admission", catalogue_hash=catalogue_hash,
            provider="openrouter", model=model,
            route=OPENROUTER_E1_PROFILES[model]["endpoint_tag"], session_nonce=session_id,
        )
        session = SingleSubmissionSessionV4(
            level=job["level"], observation=observation,
            queries=CommonQuerySurfaceV3(compile_context.registry),
            compile_context=compile_context, binding=system,
            query_provider=lambda _name, _top_k: ((), (), 0.0, "provider-admission"),
            language_condition=job["language_condition"],
        )
        context_value = {
            "schema_version": PLANNING_CONTEXT_V22, "technical_non_scientific": True,
            "protocol_epoch": PROTOCOL_EPOCH, "study_track": job["study_track"],
            "coverage_regime": job["coverage_regime"],
            "language_condition": job["language_condition"],
            "common_objective": contract["common_objective"],
            "common_observation": dict(observation.payload),
            "common_observation_sha256": observation.sha256,
            "common_query_catalogue": session.queries.catalogue,
            "language_contract": contract, **session.public_status(),
            "fully_materialized_max_q": max_q, "qor_feedback_present": False,
        }
        context_bytes = canonical_json_bytes(context_value)
        prompt = prompt_for_cell(
            study_track=job["study_track"], language_condition=job["language_condition"],
            level=job["agent_level"],
        )
        if profile_only:
            prompt += b" Technical route admission only; return {\"kind\":\"STOP\"}."
        else:
            prompt += b" Provider-only language admission; return any valid COMMIT satisfying only the declared eligibility rule."
        reserve = MODEL_RESERVES[model]
        total = len(context_bytes) + len(prompt)
        if total > MAX_INPUT_BYTES or not await cost.acquire(reserve):
            return {**job, "profile_only": profile_only, "mechanical_accepted": False,
                    "language_valid": False, "error": "input_or_cost_guard",
                    "physical_requests": 0, "cost_usd": 0.0, "total_input_size_bytes": total}
        request = _binding(
            job=job, model=model, turn=0, context=context_bytes, prompt=prompt,
            catalogue_hash=catalogue_hash, reserve=reserve,
        )
        transport = OpenRouterChatCompletionsTransport(
            api_key=key, expected_outbound_binding=build_openrouter_outbound(request)
        )
        response = None
        error = None
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
        result = None
        if response is not None and error is None:
            try:
                token = CURRENT_JOB.set(job)
                try:
                    result = session.accept(base.strict_json_object(response.content_bytes))
                finally:
                    CURRENT_JOB.reset(token)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
        exchange = transport.last_exchange or {}
        exact = bool(
            response and response.returned_model_id == model
            and response.returned_model_revision == OPENROUTER_E1_PROFILES[model]["upstream_provider"]
            and exchange.get("route", {}).get("fallback_allowed") is False
            and exchange.get("route", {}).get("attempt") == 1
            and transport.physical_requests == 1
        )
        language_valid = bool(
            result and result.status == "COMMITTED" and result.compiled
            and result.compiled.language == (None if profile_only else condition[0])
        )
        mechanical = bool(exact and error is None and result is not None)
        row = {
            **job, "profile_only": profile_only, "mechanical_accepted": mechanical,
            "language_valid": language_valid,
            "planner_result": None if result is None else base._turn_public(result),
            "error": error, "physical_requests": transport.physical_requests,
            "cost_usd": actual, "returned_model": response.returned_model_id if response else None,
            "returned_upstream": response.returned_model_revision if response else None,
            "route": exchange.get("route"), "context_size_bytes": len(context_bytes),
            "prompt_size_bytes": len(prompt), "total_input_size_bytes": total,
            "max_q_result_count": len(max_q),
            "max_q_result_bytes": sum(len(canonical_json_bytes(item)) for item in max_q),
            "contract_hash": catalogue_hash, "wall_seconds": time.monotonic() - started,
        }
        write(args.out / "sessions" / session_id / "result.json", row)
        return row

    rows = await asyncio.gather(
        *(request_one(model, None) for model in MODELS),
        *(request_one(model, condition) for model in MODELS for condition in CONDITIONS),
    )
    core = {
        "schema_version": "dof.wp014-three-model-admission.v1",
        "technical_non_scientific": True, "sessions": rows,
        "profile_sessions": 3, "language_sessions": 9,
        "physical_requests": sum(row["physical_requests"] for row in rows),
        "known_cost_usd": cost.spent, "cost_cap_usd": cost.cap,
        "retry_count": 0, "fallback_count": 0,
        "language_valid_counts": {
            condition: sum(row["language_valid"] for row in rows if row["language_condition"] == condition)
            for condition in CONDITIONS
        },
    }
    core["accepted"] = bool(
        len(rows) == 12 and all(row["mechanical_accepted"] for row in rows)
        and core["physical_requests"] == 12
    )
    value = core | {"aggregate_sha256": digest(core)}
    write(args.out / "provider-admission.json", value)
    write(
        args.out / "resource-ledger.json",
        {"schema_version": "dof.wp014-resource-ledger.v1", "cost_usd": cost.spent,
         "heavy_calls": 0, "queue_wall_seconds": 0.0, "completed_phases": ["ADMISSION"]},
    )
    print(json.dumps({"accepted": value["accepted"], "language_valid_counts": core["language_valid_counts"], "cost_usd": cost.spent}, sort_keys=True))
    return 0 if value["accepted"] else 2


def reference_audit(args: argparse.Namespace) -> int:
    roots = _root_specs(args.roots, args.stress)
    stress = read(args.stress)
    masks = {row["root_id"]: row for row in stress["selected_stress_roots"]}
    panel = json.loads(args.panel.read_text())
    by_cell = {(row["root_id"], row["action_key"]): row for row in panel}
    ranking = read(args.l1)["global_ranking"]
    rows = []
    for root in roots:
        regimes = ["FULL_M8"] + (["REDUCED_M_STRESS"] if root["root_id"] in masks else [])
        root_path = Path(root["root_path"]).resolve(strict=True)
        if base.file_digest(root_path) != root["sha256"]:
            raise RuntimeError("root_hash_mismatch:" + root["root_id"])
        for regime in regimes:
            admitted = tuple(M_ACTION_KEYS) if regime == "FULL_M8" else tuple(masks[root["root_id"]]["retained_action_keys"])
            selected = "macro." + next(key for key in ranking if "macro." + key in admitted)
            panel_row = by_cell[(root["root_id"], selected)]
            job_root = args.panel.parent.parent / "jobs" / panel_row["job_id"]
            summary = read(job_root / "flow_matrix_summary.json")["flows"][0]
            child = Path(summary["assertions"]["published_best_source_dcp"]).resolve(strict=True)
            rows.append(
                {
                    "root_id": root["root_id"], "coverage_regime": regime,
                    "l0": {"root_path": str(root_path), "root_sha256": base.file_digest(root_path),
                           "fmax_mhz": panel_row["initial_fmax_mhz"], "wns_ns": panel_row["initial_wns_ns"]},
                    "l1": {"action_key": selected, "source_panel_job_id": panel_row["job_id"],
                           "child_path": str(child), "child_sha256": base.file_digest(child),
                           "fmax_mhz": panel_row["final_fmax_mhz"], "wns_ns": panel_row["final_wns_ns"],
                           "canonical_endpoint": panel_row["canonical_endpoint"]},
                    "admitted_action_keys": list(admitted), "rerun_required": False,
                }
            )
    core = {
        "schema_version": "dof.wp014-reference-reuse-audit.v1",
        "protocol_epoch": PROTOCOL_EPOCH, "rows": rows, "bindings": len(rows),
        "macro_panel_sha256": base.file_digest(args.panel),
        "l1_heuristic_sha256": base.file_digest(args.l1),
        "all_exact_matches": all(not row["rerun_required"] and row["l1"]["canonical_endpoint"] == "VALID_COMPLETION" for row in rows),
        "sealed_holdouts_accessed": False,
    }
    value = core | {"audit_sha256": digest(core)}
    if args.output.exists():
        raise FileExistsError(args.output)
    write(args.output, value)
    print(json.dumps({"accepted": value["all_exact_matches"], "bindings": len(rows)}, sort_keys=True))
    return 0 if value["all_exact_matches"] else 2


def _jobs(stress: Mapping[str, Any]) -> list[dict[str, Any]]:
    full_ids = [row["root_id"] for row in stress["derivation_table"]]
    stress_rows = {row["root_id"]: row for row in stress["selected_stress_roots"]}
    result: list[dict[str, Any]] = []
    full_mask_hash = digest({"coverage_regime": "FULL_M8", "actions": M_ACTION_KEYS})
    for root_id in full_ids:
        for model in MODELS:
            for level in LEVELS:
                result.append(
                    {
                        "job_id": f"nk__{root_id}__{model.replace('/', '_')}__{level.lower()}",
                        "phase": "N_K", "root_id": root_id, "model": model,
                        "level": level, "agent_level": level,
                        "protocol_epoch": PROTOCOL_EPOCH, "study_track": "N_NEUTRAL",
                        "coverage_regime": "FULL_M8", "language_condition": "FREE_CHOICE",
                        "repeat": 1, "admitted_action_keys": list(M_ACTION_KEYS),
                        "mask_evidence_hash": full_mask_hash,
                    }
                )
    for root_id, stress_row in stress_rows.items():
        admitted = stress_row["retained_action_keys"]
        mask_hash = digest(stress_row)
        for model in MODELS:
            for level in LEVELS:
                result.append(
                    {
                        "job_id": f"sn__{root_id}__{model.replace('/', '_')}__{level.lower()}",
                        "phase": "S_N", "root_id": root_id, "model": model,
                        "level": level, "agent_level": level,
                        "protocol_epoch": PROTOCOL_EPOCH, "study_track": "N_NEUTRAL",
                        "coverage_regime": "REDUCED_M_STRESS", "language_condition": "FREE_CHOICE",
                        "repeat": 1, "admitted_action_keys": list(admitted),
                        "mask_evidence_hash": mask_hash,
                    }
                )
            for condition in CONDITIONS:
                runtime_level = _runtime_level(condition)
                result.append(
                    {
                        "job_id": f"cs__{root_id}__{model.replace('/', '_')}__{condition.lower()}",
                        "phase": "C_S", "root_id": root_id, "model": model,
                        "level": runtime_level, "agent_level": "C",
                        "protocol_epoch": PROTOCOL_EPOCH, "study_track": "C_CONDITIONED",
                        "coverage_regime": "REDUCED_M_STRESS", "language_condition": condition,
                        "repeat": 1, "admitted_action_keys": list(admitted),
                        "mask_evidence_hash": mask_hash,
                        "calibration_cell": root_id in {"logicnets_jscl", "corescore_500_mod"},
                    }
                )
    return result


def freeze(args: argparse.Namespace) -> int:
    repository = base.assert_clean_repository()
    stress, admission, references = read(args.stress), read(args.admission), read(args.references)
    capture_value = read(args.capture / "capture-aggregate.json")
    if not capture_value.get("accepted") or not admission.get("accepted") or not references.get("all_exact_matches"):
        raise SystemExit("freeze_prerequisite_not_accepted")
    roots_list = _root_specs(args.roots, args.stress)
    roots = {row["root_id"]: row for row in roots_list}
    observations: dict[str, Any] = {}
    query_audits: dict[str, Any] = {}
    for root_id, root in roots.items():
        observation, private = base._common_observation(root, args.capture / root_id)
        if not private["query_semantic_audit"]["accepted"]:
            raise SystemExit("query_semantic_audit_failed:" + root_id)
        observations[root_id] = {
            "payload": dict(observation.payload),
            "canonical_utf8": observation.canonical_bytes.decode(),
            "sha256": observation.sha256,
            "real_observation_sha256": private["real_observation_sha256"],
            "query_private_rows_sha256": digest(private["rows"]),
            "handle_manifest": private["registry"].public_manifest(),
            "predefined_regions": list(private["predefined_regions"]),
            "geometry_bounds": list(private["geometry_bounds"]),
        }
        query_audits[root_id] = private["query_semantic_audit"]
    jobs = _jobs(stress)
    counts = {phase: sum(row["phase"] == phase for row in jobs) for phase in ("N_K", "S_N", "C_S")}
    calibration_count = sum(row["phase"] == "C_S" and row["calibration_cell"] for row in jobs)
    if counts != {"N_K": 90, "S_N": 36, "C_S": 36} or calibration_count != 18 or len(jobs) != 162:
        raise SystemExit("wp014_matrix_invalid")
    for job in jobs:
        validate_protocol_cell(
            study_track=job["study_track"], coverage_regime=job["coverage_regime"],
            language_condition=job["language_condition"], level=job["agent_level"],
        )
    contracts = {
        job["job_id"]: contract_hash_for_cell(
            study_track=job["study_track"], coverage_regime=job["coverage_regime"],
            language_condition=job["language_condition"], level=job["agent_level"],
            admitted_action_keys=tuple(job["admitted_action_keys"]),
        )
        for job in jobs
    }
    neutral_prompt_hashes = {level: digest(prompt_for_cell(study_track="N_NEUTRAL", language_condition="FREE_CHOICE", level=level)) for level in LEVELS}
    core = {
        "schema_version": SCHEMA, "protocol_epoch": PROTOCOL_EPOCH,
        "freeze_stage": args.stage, "repository": repository,
        "roots": roots, "observations": observations, "query_audits": query_audits,
        "jobs": jobs, "job_counts": counts, "calibration_job_count": calibration_count,
        "models": [
            {"model": model, "per_request_reserve_usd": MODEL_RESERVES[model],
             **dict(OPENROUTER_E1_PROFILES[model])} for model in MODELS
        ],
        "contract_hash_by_job": contracts,
        "neutral_prompt_hashes": neutral_prompt_hashes,
        "common_component_hashes": {
            path: base.file_digest(REPO / path) for path in (
                "optimizer_pipeline/dof/single_submission/contract_v3.py",
                "optimizer_pipeline/dof/single_submission/contract_v4.py",
                "optimizer_pipeline/dof/single_submission/observation_v3.py",
                "optimizer_pipeline/dof/single_submission/planner_v3.py",
                "optimizer_pipeline/dof/single_submission/planner_v4.py",
                "optimizer_pipeline/dof/single_submission/language.py",
                "optimizer_pipeline/dof/vivado_mutation_worker.py",
                "optimizer_pipeline/backends/xilinx/legacy.py",
                "scripts/run_wp013a_e0.py", "scripts/run_wp014.py",
            )
        },
        "stress_masks_sha256": stress["stress_masks_sha256"],
        "reference_audit_sha256": references["audit_sha256"],
        "capture_sha256": capture_value["capture_sha256"],
        "provider_admission_sha256": admission["aggregate_sha256"],
        "provider_admission_cost_usd": admission["known_cost_usd"],
        "guards": {
            "openrouter_cost_cap_usd": OPENROUTER_COST_CAP_USD,
            "implementation_heavy_call_cap": IMPLEMENTATION_HEAVY_CALL_CAP,
            "queue_wall_seconds": QUEUE_WALL_SECONDS_CAP, "max_parallel_weekday": 8,
            "max_parallel_weekend": 16, "provider_retries": 0,
            "provider_fallbacks": 0, "postcommit_resubmissions": 0,
            "planning_input_byte_ceiling": MAX_INPUT_BYTES,
        },
        "track_pooling_forbidden": True, "conditioned_in_neutral_aggregate_allowed": False,
        "sealed_holdouts_accessed": False,
    }
    if args.calibration is not None:
        calibration = read(args.calibration / "phase-aggregate.json")
        if not calibration.get("accepted_for_evidence_review"):
            raise SystemExit("calibration_not_accepted")
        core["accepted_calibration_sha256"] = calibration["aggregate_sha256"]
    value = core | {"bundle_sha256": digest(core)}
    if args.output.exists():
        raise FileExistsError(args.output)
    write(args.output, value)
    print(json.dumps({"status": "FROZEN", "stage": args.stage, "jobs": len(jobs), "bundle_sha256": value["bundle_sha256"]}, sort_keys=True))
    return 0


async def _plan_with_context(
    job: Mapping[str, Any], bundle: Mapping[str, Any], capture_dir: Path,
    output: Path, key: str, cost: base.SharedCost,
) -> tuple[dict[str, Any], Any, CompiledProgram | None, dict[str, Any] | None]:
    local = dict(bundle)
    local["roots"] = dict(bundle["roots"])
    local["roots"][job["root_id"]] = dict(bundle["roots"][job["root_id"]]) | {
        "admitted_action_keys": list(job["admitted_action_keys"])
    }
    token = CURRENT_JOB.set(job)
    try:
        return await base._plan_job(
            job, local, capture_dir, output, key, cost, MODEL_RESERVES
        )
    finally:
        CURRENT_JOB.reset(token)


def _required_heavy(item: tuple[dict[str, Any], Any, CompiledProgram | None, dict[str, Any] | None]) -> int:
    row, _registry, compiled, candidate = item
    if row["canonical_terminal"] in base.TERMINALS:
        return 0
    macro = row.get("selected_macro") or base._macro_from_candidate(candidate)
    if macro is not None:
        return int(base.MACRO_OBSERVED_HEAVY_CALLS[macro])
    return int(compiled.heavy_calls) if compiled is not None else 0


async def run_phase(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    bundle = read(args.bundle)
    claimed = bundle.pop("bundle_sha256")
    if digest(bundle) != claimed:
        raise SystemExit("bundle_hash_invalid")
    bundle["bundle_sha256"] = claimed
    base.assert_clean_repository(bundle["repository"]["head_commit"])
    if not 1 <= args.max_parallel <= 16:
        raise SystemExit("parallelism_must_be_1_to_16")
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    phase = args.phase
    if phase == "CALIBRATION":
        jobs = [row for row in bundle["jobs"] if row["phase"] == "C_S" and row["calibration_cell"]]
    else:
        jobs = [row for row in bundle["jobs"] if row["phase"] == phase]
    expected = {"CALIBRATION": 18, "N_K": 90, "S_N": 36, "C_S": 36}[phase]
    if len(jobs) != expected:
        raise SystemExit("phase_job_count_invalid")

    reused: dict[str, dict[str, Any]] = {}
    if args.reuse_run is not None:
        prior = read(args.reuse_run / "phase-aggregate.json")
        if not prior.get("accepted_for_evidence_review"):
            raise SystemExit("reuse_run_not_accepted")
        reused = {
            row["job_id"]: dict(row) | {
                "evidence_reused": True,
                "evidence_reused_from_aggregate_sha256": prior["aggregate_sha256"],
            }
            for row in prior["jobs"] if row["job_id"] in {job["job_id"] for job in jobs}
        }
    if phase == "C_S" and len(reused) != 18:
        raise SystemExit("cs_requires_exact_18_job_calibration_reuse")
    if phase != "C_S" and reused:
        raise SystemExit("reuse_only_allowed_for_cs")

    ledger_before = read(args.resource_ledger)
    if ledger_before["cost_usd"] > OPENROUTER_COST_CAP_USD or ledger_before["heavy_calls"] > IMPLEMENTATION_HEAVY_CALL_CAP or ledger_before["queue_wall_seconds"] > QUEUE_WALL_SECONDS_CAP:
        raise SystemExit("resource_guard_already_exceeded")
    cost = base.SharedCost(OPENROUTER_COST_CAP_USD, spent=float(ledger_before["cost_usd"]))
    started = time.monotonic()
    roots_before = {
        root_id: base.file_digest(Path(root["root_path"]))
        for root_id, root in bundle["roots"].items()
    }
    if any(roots_before[root_id] != root["sha256"] for root_id, root in bundle["roots"].items()):
        raise SystemExit("root_hash_mismatch_before_run")

    fresh_jobs = [job for job in jobs if job["job_id"] not in reused]
    planning_semaphore = asyncio.Semaphore(args.max_parallel)

    async def plan_one(job: Mapping[str, Any]):
        async with planning_semaphore:
            return await _plan_with_context(job, bundle, args.capture, args.out, key, cost)

    planned = await asyncio.gather(*(plan_one(job) for job in fresh_jobs))
    remaining_heavy = IMPLEMENTATION_HEAVY_CALL_CAP - int(ledger_before["heavy_calls"])
    allocations: dict[str, dict[str, Any]] = {}
    for item in sorted(planned, key=lambda item: digest(item[0]["job_id"])):
        row = item[0]
        required = _required_heavy(item)
        allowed = row["canonical_terminal"] not in base.TERMINALS and required <= remaining_heavy
        allocations[row["job_id"]] = {
            "required_heavy_calls": required, "dispatch_allowed": allowed,
            "remaining_before": remaining_heavy,
            "allocation_basis": "SHA256(job_id); QoR-independent",
        }
        if allowed:
            remaining_heavy -= required
        allocations[row["job_id"]]["remaining_after"] = remaining_heavy

    execution_semaphore = asyncio.Semaphore(args.max_parallel)

    async def execute_one(item: tuple[dict[str, Any], Any, CompiledProgram | None, dict[str, Any] | None]) -> dict[str, Any]:
        row, registry, compiled, candidate = item
        allocation = allocations[row["job_id"]]
        if row["canonical_terminal"] in base.TERMINALS:
            evidence = boundary_evidence(candidate=candidate, execution_started=False, execution=None)
            return row | {"execution_started": False, "actual_heavy_calls": 0,
                          "actual_light_mutations": 0, "execution_allocation": allocation,
                          "boundary_evidence": evidence,
                          "boundary_runtime_exercised": evidence["boundary_runtime_exercised"]}
        if not allocation["dispatch_allowed"]:
            evidence = boundary_evidence(candidate=candidate, execution_started=False, execution=None)
            return row | {"canonical_terminal": "BUDGET_BLOCKED", "execution_started": False,
                          "actual_heavy_calls": 0, "actual_light_mutations": 0,
                          "execution_allocation": allocation, "boundary_evidence": evidence,
                          "boundary_runtime_exercised": evidence["boundary_runtime_exercised"]}
        root = Path(bundle["roots"][row["root_id"]]["root_path"])
        execution_dir = args.out / "execution" / row["job_id"]
        async with execution_semaphore:
            macro = row.get("selected_macro") or base._macro_from_candidate(candidate)
            if macro:
                result = await base._execute_macro(
                    root=root, flow=macro, out=execution_dir, python=args.python
                )
                result.setdefault("actual_light_mutations", None)
            elif compiled is not None and registry is not None:
                result = await base._execute_typed(
                    compiled=compiled, registry=registry, root=root, out=execution_dir,
                    python=args.python, vivado=args.vivado,
                )
            else:
                result = {"canonical_endpoint": "CONTROLLER_FAILURE",
                          "error": "planned_commit_missing_compiled_program",
                          "actual_heavy_calls": 0, "actual_light_mutations": 0}
            write(execution_dir / "execution-summary.json", result)
            if int(result.get("actual_heavy_calls") or 0) > allocation["required_heavy_calls"]:
                result = result | {"canonical_endpoint": "CONTROLLER_FAILURE",
                                   "accounting_error": "observed_heavy_calls_exceeded_predispatch_reservation"}
            evidence = boundary_evidence(candidate=candidate, execution_started=True, execution=result)
            return row | {
                "canonical_terminal": result["canonical_endpoint"], "execution_started": True,
                "execution": result, "actual_heavy_calls": result.get("actual_heavy_calls", 0),
                "actual_light_mutations": result.get("actual_light_mutations"),
                "execution_allocation": allocation, "boundary_evidence": evidence,
                "boundary_runtime_exercised": evidence["boundary_runtime_exercised"],
            }

    fresh_rows = await asyncio.gather(*(execute_one(item) for item in planned))
    final_by_id = {row["job_id"]: row for row in fresh_rows} | reused
    final_rows = [final_by_id[job["job_id"]] for job in jobs]
    roots_after = {
        root_id: base.file_digest(Path(root["root_path"]))
        for root_id, root in bundle["roots"].items()
    }
    repository_after = base.assert_clean_repository(bundle["repository"]["head_commit"])
    phase_wall = time.monotonic() - started
    fresh_heavy = sum(int(row.get("actual_heavy_calls") or 0) for row in fresh_rows)
    new_ledger = {
        "schema_version": "dof.wp014-resource-ledger.v1", "cost_usd": cost.spent,
        "heavy_calls": int(ledger_before["heavy_calls"]) + fresh_heavy,
        "queue_wall_seconds": float(ledger_before["queue_wall_seconds"]) + phase_wall,
        "completed_phases": list(ledger_before["completed_phases"]) + [phase],
    }
    write(args.resource_ledger, new_ledger)

    terminal_counts = {
        terminal: sum(row["canonical_terminal"] == terminal for row in final_rows)
        for terminal in sorted({row["canonical_terminal"] for row in final_rows})
    }
    llm_rows = final_rows
    o_equal = all(
        len({row.get("observation_sha256") for row in llm_rows if row["root_id"] == root_id}) == 1
        for root_id in {row["root_id"] for row in llm_rows}
    )
    q_equal = all(
        len({digest(row.get("planner_evidence", {}).get("query_catalogue")) for row in llm_rows if row["root_id"] == root_id}) == 1
        for root_id in {row["root_id"] for row in llm_rows}
    )
    metadata_complete = all(
        row.get("protocol_epoch") == PROTOCOL_EPOCH
        and row.get("study_track") in {"N_NEUTRAL", "S_NEUTRAL_REDUCED", "C_CONDITIONED"}
        and row.get("coverage_regime") in {"FULL_M8", "REDUCED_M_STRESS"}
        and row.get("language_condition") in {"FREE_CHOICE", *CONDITIONS}
        and row.get("repeat") == 1 for row in final_rows
    )
    condition_dispatch_errors = []
    for row in final_rows:
        if row["study_track"] != "C_CONDITIONED" or not row.get("execution_started"):
            continue
        expected_language = row["language_condition"][0]
        if row.get("selected_language") != expected_language:
            condition_dispatch_errors.append(row["job_id"])
    core = {
        "schema_version": SCHEMA, "protocol_epoch": PROTOCOL_EPOCH, "phase": phase,
        "bundle_sha256": claimed, "source_commit": bundle["repository"]["head_commit"],
        "jobs": final_rows, "planned_jobs": len(jobs), "fresh_jobs": len(fresh_jobs),
        "reused_jobs": len(reused),
        "canonical_terminal_jobs": sum(row["canonical_terminal"] in base.TERMINALS for row in final_rows),
        "terminal_counts": terminal_counts,
        "provider_requests_fresh": sum(row.get("provider_requests", 0) for row in fresh_rows),
        "fresh_cost_usd": cost.spent - float(ledger_before["cost_usd"]),
        "cumulative_cost_usd": cost.spent, "fresh_heavy_calls": fresh_heavy,
        "cumulative_heavy_calls": new_ledger["heavy_calls"],
        "phase_wall_seconds": phase_wall,
        "cumulative_queue_wall_seconds": new_ledger["queue_wall_seconds"],
        "roots_before": roots_before, "roots_after": roots_after,
        "all_roots_unchanged": roots_before == roots_after,
        "repository_before": bundle["repository"], "repository_after": repository_after,
        "all_o_hashes_equal_by_root": o_equal, "all_q_catalogues_equal_by_root": q_equal,
        "metadata_complete": metadata_complete,
        "condition_dispatch_errors": condition_dispatch_errors,
        "precommit_qor_feedback_count": sum(int(row.get("planner_evidence", {}).get("precommit_qor_feedback_count", 0)) for row in final_rows),
        "precommit_mutation_count": sum(int(row.get("planner_evidence", {}).get("precommit_mutation_count", 0)) for row in final_rows),
        "postcommit_resubmission_count": sum(int(row.get("planner_evidence", {}).get("postcommit_resubmission_count", 0)) for row in final_rows),
        "sealed_holdouts_accessed": False, "track_pooling_performed": False,
    }
    core["accepted_for_evidence_review"] = bool(
        len(final_rows) == expected and core["canonical_terminal_jobs"] == expected
        and core["all_roots_unchanged"] and o_equal and q_equal and metadata_complete
        and not condition_dispatch_errors
        and core["precommit_qor_feedback_count"] == 0
        and core["precommit_mutation_count"] == 0
        and core["postcommit_resubmission_count"] == 0
        and core["cumulative_cost_usd"] <= OPENROUTER_COST_CAP_USD
        and core["cumulative_heavy_calls"] <= IMPLEMENTATION_HEAVY_CALL_CAP
        and core["cumulative_queue_wall_seconds"] <= QUEUE_WALL_SECONDS_CAP
        and "NO_VALID_SUBMISSION_COST_GUARD" not in terminal_counts
        and "BUDGET_BLOCKED" not in terminal_counts
    )
    value = core | {"aggregate_sha256": digest(core)}
    write(args.out / "phase-aggregate.json", value)
    print(json.dumps({key: value[key] for key in (
        "phase", "accepted_for_evidence_review", "planned_jobs", "fresh_jobs", "reused_jobs",
        "terminal_counts", "fresh_cost_usd", "cumulative_cost_usd", "fresh_heavy_calls",
        "cumulative_heavy_calls", "phase_wall_seconds",
    )}, sort_keys=True))
    return 0 if value["accepted_for_evidence_review"] else 2


def audit_phase(args: argparse.Namespace) -> int:
    aggregate = read(args.run / "phase-aggregate.json")
    claimed = aggregate.pop("aggregate_sha256")
    errors: list[str] = []
    if digest(aggregate) != claimed:
        errors.append("aggregate_hash_invalid")
    aggregate["aggregate_sha256"] = claimed
    jobs = aggregate.get("jobs", [])
    if len(jobs) != aggregate.get("planned_jobs"):
        errors.append("job_count_mismatch")
    if aggregate.get("canonical_terminal_jobs") != len(jobs):
        errors.append("noncanonical_terminal")
    for key in ("precommit_qor_feedback_count", "precommit_mutation_count", "postcommit_resubmission_count"):
        if aggregate.get(key) != 0:
            errors.append(key + "_nonzero")
    if not aggregate.get("all_roots_unchanged"):
        errors.append("root_integrity_failed")
    if not aggregate.get("metadata_complete") or aggregate.get("track_pooling_performed"):
        errors.append("track_regime_separation_failed")
    if aggregate.get("condition_dispatch_errors"):
        errors.append("condition_dispatch_error")
    for row in jobs:
        validate_protocol_cell(
            study_track=row["study_track"], coverage_regime=row["coverage_regime"],
            language_condition=row["language_condition"], level=row["agent_level"],
        )
        expected = OPENROUTER_E1_PROFILES[row["model"]]
        for turn in row.get("turns", []):
            if turn.get("returned_model") is not None and turn["returned_model"] != row["model"]:
                errors.append("returned_model_mismatch:" + row["job_id"])
            if turn.get("returned_upstream") is not None and turn["returned_upstream"] != expected["upstream_provider"]:
                errors.append("returned_upstream_mismatch:" + row["job_id"])
            route = turn.get("route") or {}
            if turn.get("physical_requests", 0) and (
                route.get("fallback_allowed") is not False or route.get("attempt") != 1
            ):
                errors.append("provider_policy_mismatch:" + row["job_id"])
        evidence = row.get("planner_evidence", {})
        if evidence.get("valid_commits", 0) > 1:
            errors.append("multiple_commits:" + row["job_id"])
        if evidence.get("query_requests_used", 0) > 8:
            errors.append("query_budget_exceeded:" + row["job_id"])
        if row.get("execution_started") and row["study_track"] == "C_CONDITIONED":
            if row.get("selected_language") != row["language_condition"][0]:
                errors.append("forbidden_language_dispatched:" + row["job_id"])
    if not aggregate.get("accepted_for_evidence_review"):
        errors.append("aggregate_acceptance_false")
    core = {
        "schema_version": "dof.wp014-phase-independent-audit.v1",
        "phase": aggregate.get("phase"), "aggregate_sha256": claimed,
        "jobs": len(jobs), "errors": sorted(set(errors)), "accepted": not errors,
        "checks": {
            "canonical_terminals": aggregate.get("canonical_terminal_jobs"),
            "cost_usd": aggregate.get("cumulative_cost_usd"),
            "heavy_calls": aggregate.get("cumulative_heavy_calls"),
            "roots_unchanged": aggregate.get("all_roots_unchanged"),
            "metadata_complete": aggregate.get("metadata_complete"),
        },
    }
    value = core | {"audit_sha256": digest(core)}
    write(args.output, value)
    print(json.dumps({"accepted": value["accepted"], "errors": value["errors"]}, sort_keys=True))
    return 0 if value["accepted"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    commands = value.add_subparsers(dest="command", required=True)
    c = commands.add_parser("capture")
    c.add_argument("--roots", type=Path, required=True)
    c.add_argument("--stress", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--python", type=Path, required=True)
    c.add_argument("--vivado", type=Path, required=True)
    c.add_argument("--max-parallel", type=int, default=8)
    p = commands.add_parser("preflight")
    p.add_argument("--output", type=Path, required=True)
    d = commands.add_parser("provider-admission")
    d.add_argument("--preflight", type=Path, required=True)
    d.add_argument("--out", type=Path, required=True)
    x = commands.add_parser("reference-audit")
    x.add_argument("--roots", type=Path, required=True)
    x.add_argument("--stress", type=Path, required=True)
    x.add_argument("--panel", type=Path, required=True)
    x.add_argument("--l1", type=Path, required=True)
    x.add_argument("--output", type=Path, required=True)
    f = commands.add_parser("freeze")
    f.add_argument("--stage", choices=("PROVISIONAL_CALIBRATION", "FINAL_REPEAT1"), required=True)
    f.add_argument("--roots", type=Path, required=True)
    f.add_argument("--stress", type=Path, required=True)
    f.add_argument("--capture", type=Path, required=True)
    f.add_argument("--admission", type=Path, required=True)
    f.add_argument("--references", type=Path, required=True)
    f.add_argument("--calibration", type=Path)
    f.add_argument("--output", type=Path, required=True)
    r = commands.add_parser("run")
    r.add_argument("--phase", choices=PHASES, required=True)
    r.add_argument("--bundle", type=Path, required=True)
    r.add_argument("--capture", type=Path, required=True)
    r.add_argument("--resource-ledger", type=Path, required=True)
    r.add_argument("--reuse-run", type=Path)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--python", type=Path, required=True)
    r.add_argument("--vivado", type=Path, required=True)
    r.add_argument("--max-parallel", type=int, default=8)
    a = commands.add_parser("audit")
    a.add_argument("--run", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    return value


def main() -> int:
    configure_base()
    args = parser().parse_args()
    if args.command == "capture":
        return asyncio.run(capture(args))
    if args.command == "preflight":
        return asyncio.run(preflight(args))
    if args.command == "provider-admission":
        return asyncio.run(provider_admission(args))
    if args.command == "reference-audit":
        return reference_audit(args)
    if args.command == "freeze":
        return freeze(args)
    if args.command == "run":
        return asyncio.run(run_phase(args))
    if args.command == "audit":
        return audit_phase(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
