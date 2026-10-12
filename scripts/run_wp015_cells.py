#!/usr/bin/env python3
"""Run WP-015 canary or frozen scientific cells as resume-safe complete jobs.

One worker is assigned to each frozen model profile.  This keeps the exact
three-consecutive-provider-failure circuit breaker well-defined while allowing
seven independent complete jobs to progress concurrently.  A provider request
is never repeated after an interrupted exchange, and a committed backend
failure is always terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp013a_e0 as base  # noqa: E402
import run_wp014 as legacy  # noqa: E402
import run_wp014c_scientific as wp014c  # noqa: E402
from optimizer_pipeline.dof.provider import ProviderRequestBinding, ProviderSettings  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import (  # noqa: E402
    boundary_evidence, strict_boundary_certificate,
)
from optimizer_pipeline.dof.wp015a_strict_g import strict_family_feedback  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v5 import (  # noqa: E402
    PLANNING_CONTEXT_V22, PROMPT_VERSION_V3, compile_candidate_v5,
)
from optimizer_pipeline.dof.single_submission.contract_v6 import (  # noqa: E402
    PLANNER_MESSAGE_V6, PROTOCOL_EPOCH, prompt_for_cell,
)
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.wp015_provider import (  # noqa: E402
    MODEL_RESERVES_USD, PROVIDER_CAPS_USD, TOTAL_OUTPUT_TOKEN_CEILING,
    runtime_profile, transport,
)
from scripts.run_wp014i_integrated_preflight import resolve_execution_child  # noqa: E402
from scripts.run_wp014h_strict_witnesses import mechanism_deltas  # noqa: E402


SCHEMA = "dof.wp015-complete-cell-controller.v1"
PROTOCOL_ID = "WP015_LEAN_R1_V3"
PROVIDER_TERMINALS = frozenset({"PROVIDER_FAILURE", "MODEL_OR_ROUTE_DRIFT"})
STRICT_FAMILIES = frozenset({
    "SPATIAL_GENERATED_GEOMETRY",
    "NONPREFIX_MULTI_NET_BATCH",
    "COUPLED_HARDBLOCK_SITE_ALLOCATION",
})
STRICT_WITNESS_IDS = {
    "SPATIAL_GENERATED_GEOMETRY": "spatial_generated_geometry",
    "NONPREFIX_MULTI_NET_BATCH": "nonprefix_multi_net_batch",
    "COUPLED_HARDBLOCK_SITE_ALLOCATION": "coupled_hardblock_site_allocation",
}


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write(path: Path, value: Any, *, private: bool = False) -> None:
    base.write_atomic(path, value, private=private)


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else base.canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    return base.file_digest(path)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def _objective(job: Mapping[str, Any]) -> str:
    if job["track"] != "C_CONDITIONED":
        return "Choose freely among the languages admitted at this neutral level."
    if job["condition"] == "M_ONLY":
        return "Return one direct M-language COMMIT using one admitted Macro."
    if job["condition"] == "P_BOUNDARY":
        return "Return one P-exclusive COMMIT; inherited CALL_MACRO alone is insufficient."
    mechanism = job.get("strict_mechanism")
    suffix = ""
    if mechanism:
        suffix = " The required sufficient mechanism family is " + str(mechanism) + "."
    return (
        "Return one G COMMIT matching a frozen sufficient strict-G family; "
        "a merely G-labelled or P-reachable program is insufficient." + suffix
    )


def _prompt(_level: str) -> bytes:
    job = legacy.CURRENT_JOB.get()
    if job is None:
        raise RuntimeError("wp015_job_context_missing")
    prompt = prompt_for_cell(
        study_track=job["track"], language_condition=job["language_condition"],
        level=job["agent_level"],
    )
    return prompt + b" WP015 frozen single-repeat trial. " + _objective(job).encode()


def _planner_context(
    *, observation: Any, session: Any, contract: Mapping[str, Any],
    last_result: Mapping[str, Any] | None,
) -> bytes:
    job = legacy.CURRENT_JOB.get()
    if job is None:
        raise RuntimeError("wp015_job_context_missing")
    return base.canonical_json_bytes({
        "schema_version": "dof.wp015-planning-context.v1",
        "protocol_id": PROTOCOL_ID, "protocol_epoch": PROTOCOL_EPOCH,
        "job_id": job["job_id"], "root_pseudonym": job["root_pseudonym"],
        "study_track": job["track"], "coverage_regime": job["coverage_regime"],
        "language_condition": job["language_condition"],
        "strict_g_required": bool(job.get("strict_g_required")),
        "required_strict_mechanism": job.get("strict_mechanism"),
        "objective": _objective(job), "common_objective": contract["common_objective"],
        "common_observation": dict(observation.payload),
        "common_observation_sha256": observation.sha256,
        "common_query_catalogue": session.queries.catalogue,
        "language_contract": contract, **session.public_status(),
        "previous_result": last_result, "qor_feedback_present": False,
        "private_reasoning_requested_or_persisted": False,
    })


def _binding(
    *, job: Mapping[str, Any], model: str, turn: int, context: bytes,
    prompt: bytes, catalogue_hash: str, reserve: float, phase: str = "scientific",
) -> ProviderRequestBinding:
    del phase
    profile = runtime_profile(model)
    identity = f"{job['job_id']}:{turn}"
    return ProviderRequestBinding(
        run_id="wp015-" + digest(identity)[:24],
        spec_hash=digest({"schema": SCHEMA, "job": job}),
        experiment_id=PROTOCOL_ID, condition_id="condition-" + digest({
            "track": job["track"], "condition": job["condition"],
            "root": job["root_id"],
        })[:20],
        repeat_id=job["repeat_id"], decision_id="decision-" + digest(job["job_id"])[:20],
        context_bytes=context, prompt_bytes=prompt,
        prompt_version=PROMPT_VERSION_V3 + ".wp015",
        action_interface_version=PLANNER_MESSAGE_V6,
        action_interface_hash=digest(PLANNER_MESSAGE_V6),
        context_interface_version=PLANNING_CONTEXT_V22,
        context_interface_hash=digest(context),
        catalogue_version=f"{PROTOCOL_EPOCH}.wp015.{job['condition'].lower()}",
        catalogue_hash=catalogue_hash, capability_hash=catalogue_hash,
        mask_evidence_hash=job["mask_evidence_hash"],
        provider_role="wp015_frozen_planner", provider=profile["provider"],
        model_id=model, adapter_version=profile["adapter_version"],
        settings=ProviderSettings(
            temperature=None, max_tokens=TOTAL_OUTPUT_TOKEN_CEILING, seed=None,
            response_format="json_object", timeout_seconds=300.0,
            max_retries=0, retry_on=(), max_requests=1,
            max_usd=reserve, max_cost_per_request_usd=reserve,
            unknown_cost_policy="reject", unknown_usage_policy="reject",
        ),
    )


class WP015Session(wp014c.ContextSessionV3):
    """Apply the candidate-only sufficient strict-G admission firewall."""

    def accept(self, message: Mapping[str, Any]):
        job = legacy.CURRENT_JOB.get()
        if (
            job is not None and job.get("strict_g_required")
            and isinstance(message, Mapping)
            and message.get("kind") in {"DRAFT", "VALIDATE", "COMMIT"}
            and isinstance(message.get("candidate"), Mapping)
        ):
            required = job.get("strict_mechanism")
            feedback = strict_family_feedback(
                message["candidate"], required_family=required,
            )
            if feedback is not None:
                return self._feedback(
                    feedback, "$.candidate", message,
                )
        return super().accept(message)


def configure() -> None:
    wp014c.configure()
    legacy.SCHEMA = SCHEMA
    legacy.MODELS = tuple(MODEL_RESERVES_USD)
    legacy.MODEL_RESERVES = dict(MODEL_RESERVES_USD)
    base.SCHEMA = SCHEMA
    base.MODELS = tuple(MODEL_RESERVES_USD)
    base._binding = _binding
    base._prompt = _prompt
    base._planner_context = _planner_context
    base._provider_runtime_profile = runtime_profile
    base._provider_transport = transport
    base.SingleSubmissionSessionV2 = WP015Session


def configure_wp015b_hardblock_compatibility(scan_dir: Path) -> None:
    """Install the audited exact-Q overlay; fail closed on absent Gate-1 evidence."""

    audit = read(scan_dir / "gate1-audit.json")
    aggregate = read(scan_dir / "aggregate.json")
    if audit.get("status") != "PASS" or aggregate.get("status") != "PASS":
        raise ValueError("wp015b_gate1_scan_not_accepted")
    if audit.get("scan_aggregate_sha256") != aggregate.get("aggregate_sha256"):
        raise ValueError("wp015b_gate1_audit_binding_mismatch")
    base.HARDBLOCK_COMPATIBILITY_SCAN_DIR = scan_dir.resolve(strict=True)
    base.REQUIRE_EXACT_HARDBLOCK_COMPATIBILITY = True


def canary_jobs(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    models = list(MODEL_RESERVES_USD)
    candidates = [
        row for row in inventory["jobs"]
        if row["split"] == "DEVELOPMENT_KNOWN" and row["track"] != "S_NEUTRAL_REDUCED"
    ]
    roots = sorted({row["root_id"] for row in candidates})
    if len(roots) < 4:
        raise ValueError("wp015_canary_development_roots_incomplete")
    conditions = ("L4", "M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY")
    rows = []
    for model_index, model in enumerate(models):
        for condition_index, condition in enumerate(conditions):
            selected_root = (
                "amd_mini-isp" if condition == "G_STRICT_BOUNDARY"
                else roots[(model_index + condition_index) % len(roots)]
            )
            source = next(
                row for row in candidates
                if row["model_key"] == model and row["condition"] == condition
                and row["root_id"] == selected_root
            )
            identity = {
                "protocol_id": PROTOCOL_ID, "model": model,
                "condition": condition, "root": source["root_id"],
            }
            row = dict(source) | {
                "job_id": "wp015b-canary__" + digest(identity)[:28],
                "repeat_id": "WP015B_FINAL_CANARY_NO_REPEAT",
                "scientific_cell_reuse": False, "not_a_scientific_repeat": True,
                "model": source["model_key"], "study_track": source["track"],
            }
            if condition == "G_STRICT_BOUNDARY":
                # The scientific G condition admits any frozen strict family.
                # Per-family runtime feasibility is proved separately at Gate 3.
                row["strict_mechanism"] = None
            rows.append(row)
    if len(rows) != 28 or len({row["job_id"] for row in rows}) != 28:
        raise ValueError("wp015_canary_not_exact_28")
    return rows


def _compile_saved_candidate(
    job: Mapping[str, Any], bundle: Mapping[str, Any], capture: Path,
    candidate: Mapping[str, Any],
) -> tuple[Any, Any]:
    _observation, private = base._load_trial(bundle, capture, job["root_id"])
    admitted = tuple(job["admitted_action_keys"])
    compiled = compile_candidate_v5(
        candidate,
        CompileContext(
            private["registry"], "ROUTED", private["predefined_regions"],
            private["geometry_bounds"], tuple(key.removeprefix("macro.") for key in admitted),
            fanout_admitted=bool(private.get("fanout_admitted", False)),
            hardblock_compatibility_relation=private.get("hardblock_compatibility_relation"),
        ),
    )
    return private["registry"], compiled


def _load_or_terminal_interrupted_plan(
    job: Mapping[str, Any], output: Path,
) -> tuple[dict[str, Any], Any, Any, dict[str, Any] | None] | None:
    session = output / "planning" / job["job_id"]
    result_path = session / "planning-result.json"
    exchanges = sorted((session / "private").glob("turn-*-exchange.json"))
    if not result_path.is_file():
        if not exchanges:
            return None
        row = dict(job) | {
            "canonical_terminal": "INTERRUPTED_AFTER_PROVIDER_REQUEST",
            "provider_requests": sum(int(read(path).get("physical_requests") or 0) for path in exchanges),
            "cost_usd": sum(float(read(path).get("derived_cost_usd") or 0.0) for path in exchanges),
            "resume_policy": "terminalised_without_retry",
        }
        return row, None, None, None
    row = read(result_path)
    candidate_path = session / "private" / "committed-source.json"
    candidate = read(candidate_path) if candidate_path.is_file() else None
    return row, None, None, candidate


def _public_exchange_accounting(output: Path, job_id: str) -> list[dict[str, Any]]:
    paths = sorted((output / "planning" / job_id / "private").glob("turn-*-exchange.json"))
    allowed = {
        "schema_version", "canonical_provider", "provider_route",
        "canonical_model_id", "requested_api_model_id", "returned_api_model_id",
        "returned_upstream_provider", "provider_request_id", "request_contract_hash",
        "prompt_sha256", "context_sha256", "request_body_sha256", "response_sha256",
        "final_text_sha256", "final_text_present", "physical_requests",
        "finish_reason", "final_text_truncated", "silent_truncation_detected",
        "configured_total_output_token_ceiling",
        "reserved_final_answer_capacity_tokens", "final_answer_tokens",
        "retry_count", "fallback_count", "repair_count", "native_usage",
        "provider_reported_cost_usd", "derived_cost_usd", "pricing_snapshot_id",
        "effective_price_window", "cost_formula_version",
        "billing_reconciliation_status", "raw_reasoning_persisted", "failure_class",
    }
    return [
        {key: value for key, value in read(path).items() if key in allowed}
        for path in paths
    ]


def _nonexecution_result(
    row: Mapping[str, Any], candidate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    evidence = boundary_evidence(candidate=candidate, execution_started=False, execution=None)
    strict = strict_boundary_certificate(candidate=candidate, execution_started=False, execution=None)
    return dict(row) | {
        "execution_started": False, "actual_heavy_calls": 0,
        "actual_light_mutations": 0, "boundary_evidence": evidence,
        "boundary_runtime_exercised": evidence["boundary_runtime_exercised"],
        "strict_boundary_certificate": strict,
        "committed_candidate": dict(candidate) if candidate is not None else None,
        "cell_completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }


async def _complete_job(
    *, job: Mapping[str, Any], bundle: Mapping[str, Any], capture: Path,
    output: Path, cost: base.SharedCost, python: Path, vivado: Path,
) -> dict[str, Any]:
    job_dir = output / "jobs" / job["job_id"]
    result_path = job_dir / "cell-result.json"
    if result_path.is_file():
        return read(result_path)
    saved = _load_or_terminal_interrupted_plan(job, output)
    if saved is None:
        key = os.environ.get(runtime_profile(job["model_key"])["credential_env"], "")
        planned = await legacy._plan_with_context(job, bundle, capture, output, key, cost)
    else:
        planned = saved
    row, registry, compiled, candidate = planned
    row = dict(row) | {
        "provider_exchange_accounting": _public_exchange_accounting(output, job["job_id"]),
    }
    if row["canonical_terminal"] != "PLANNED_COMMIT":
        result = _nonexecution_result(row, candidate)
        write(result_path, result)
        return result
    if candidate is None:
        result = _nonexecution_result(
            dict(row) | {"canonical_terminal": "CONTROLLER_FAILURE",
                         "error": "planned_commit_source_missing_without_retry"}, None,
        )
        write(result_path, result)
        return result
    if compiled is None or registry is None:
        registry, compiled = _compile_saved_candidate(job, bundle, capture, candidate)
    execution_dir = job_dir / "execution"
    summary_path = execution_dir / "execution-summary.json"
    if summary_path.is_file():
        execution = read(summary_path)
    else:
        macro = row.get("selected_macro") or base._macro_from_candidate(candidate)
        if macro:
            execution = await base._execute_macro(
                root=Path(bundle["roots"][job["root_id"]]["root_path"]),
                flow=macro, out=execution_dir, python=python,
            )
            execution.setdefault("actual_light_mutations", None)
        else:
            execution = await base._execute_typed(
                compiled=compiled, registry=registry,
                root=Path(bundle["roots"][job["root_id"]]["root_path"]),
                out=execution_dir, python=python, vivado=vivado,
            )
        write(summary_path, execution)
    evidence = boundary_evidence(candidate=candidate, execution_started=True, execution=execution)
    planned_strict = strict_boundary_certificate(
        candidate=candidate, execution_started=False, execution=None,
    ).get("mechanism_family")
    if planned_strict in STRICT_WITNESS_IDS:
        execution["binding_records"] = [dict(row) for row in compiled.binding_records]
        execution["mechanism_state_deltas"] = mechanism_deltas(
            execution_dir, STRICT_WITNESS_IDS[str(planned_strict)],
        )
        # Persist the instrumentation augmentation before scientific evidence
        # is projected; it contains hashes/counts and sanitized markers only.
        write(summary_path, execution)
    strict = strict_boundary_certificate(candidate=candidate, execution_started=True, execution=execution)
    child = resolve_execution_child(execution, execution_dir)
    result = dict(row) | {
        "canonical_terminal": execution["canonical_endpoint"],
        "physical_terminal": execution["canonical_endpoint"],
        "execution_started": True, "execution": execution,
        "actual_heavy_calls": int(execution.get("actual_heavy_calls") or 0),
        "actual_light_mutations": execution.get("actual_light_mutations"),
        "boundary_evidence": evidence,
        "boundary_runtime_exercised": evidence["boundary_runtime_exercised"],
        "strict_boundary_certificate": strict,
        "committed_candidate": dict(candidate),
        "resolved_child_path": str(child) if child else None,
        "resolved_child_sha256": file_sha256(child) if child else None,
        "functional_terminal": "PENDING_V1" if execution["canonical_endpoint"] == "VALID_COMPLETION" and child else None,
        "cell_completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write(result_path, result)
    return result


def _aggregate(*, jobs: list[Mapping[str, Any]], output: Path, bundle: Mapping[str, Any]) -> dict[str, Any]:
    rows = [read(output / "jobs" / job["job_id"] / "cell-result.json") for job in jobs]
    core = {
        "schema_version": "dof.wp015-cell-aggregate.v1",
        "protocol_id": bundle["protocol_id"], "bundle_sha256": bundle["bundle_sha256"],
        "shared_scientific_core_hash": bundle["shared_scientific_core_hash"],
        "scientific_source_commit": bundle["repository"]["head_commit"],
        "scientific_source_tree": bundle["repository"]["head_tree"],
        "job_count": len(rows), "completed_job_count": len(rows),
        "terminal_counts": dict(sorted(collections.Counter(row["canonical_terminal"] for row in rows).items())),
        "provider_requests": sum(int(row.get("provider_requests") or 0) for row in rows),
        "api_cost_usd": sum(float(row.get("cost_usd") or 0.0) for row in rows),
        "actual_heavy_calls": sum(int(row.get("actual_heavy_calls") or 0) for row in rows),
        "physical_valid_children": sum(row.get("functional_terminal") == "PENDING_V1" for row in rows),
        "strict_g_exercised": sum(bool(row.get("strict_boundary_certificate", {}).get("strict_boundary_exercised")) for row in rows),
        "retry_count": 0, "fallback_count": 0, "repair_count": 0,
        "postcommit_resubmission_count": 0, "jobs": rows,
    }
    return core | {"aggregate_sha256": digest(core)}


def prior_provider_accounting(paths: list[Path]) -> tuple[dict[str, float], int, list[dict[str, Any]]]:
    spent = collections.defaultdict(float)
    requests = 0
    evidence = []
    for path in paths:
        value = read(path)
        row_list = value.get("jobs") if isinstance(value.get("jobs"), list) else value.get("rows")
        if isinstance(row_list, list):
            for row in row_list:
                provider = runtime_profile(row["model_key"])["provider"]
                spent[provider] += float(row.get("cost_usd") or 0.0)
                requests += int(row.get("provider_requests") or 0)
        else:
            provider = value.get("provider")
            if provider not in PROVIDER_CAPS_USD:
                raise ValueError("wp015_prior_provider_unknown:" + str(provider))
            spent[provider] += float(value.get("api_cost_usd") or 0.0)
            requests += int(value.get("physical_model_requests") or 0)
        evidence.append({"path": str(path), "sha256": file_sha256(path)})
    return dict(spent), requests, evidence


async def run(args: argparse.Namespace) -> int:
    configure()
    configure_wp015b_hardblock_compatibility(args.hardblock_scan)
    bundle = read(args.bundle)
    claimed = bundle.pop("bundle_sha256")
    if digest(bundle) != claimed:
        raise ValueError("wp015_bundle_hash_invalid")
    bundle["bundle_sha256"] = claimed
    inventory = read(args.inventory)
    if (
        inventory.get("inventory_sha256") != bundle.get("inventory_sha256")
        or inventory.get("shared_scientific_core_hash") != bundle.get("shared_scientific_core_hash")
    ):
        raise ValueError("wp015_inventory_bundle_mismatch")
    base.assert_clean_repository(bundle["repository"]["head_commit"])
    if args.mode == "canary":
        jobs = canary_jobs(inventory)
    else:
        jobs = [
            dict(row) | {"model": row["model_key"], "study_track": row["track"]}
            for row in inventory["jobs"]
        ]
        if len(jobs) != 1134:
            raise ValueError("wp015_scientific_inventory_not_exact_1134")
    local_now = datetime.now(ZoneInfo("Europe/London"))
    launch_limit = 16 if local_now.weekday() >= 5 else 8
    if not 1 <= args.max_parallel <= launch_limit:
        raise ValueError("wp015_complete_job_parallelism_exceeds_day_policy")
    for model in MODEL_RESERVES_USD:
        env = runtime_profile(model)["credential_env"]
        if not os.environ.get(env):
            raise RuntimeError("provider_credential_missing:" + env)
    if args.output.exists() and not (args.output / "run-manifest.json").is_file():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True, exist_ok=True)
    prior_spent, prior_requests, prior_evidence = prior_provider_accounting(args.prior_aggregate)
    if prior_requests + 4 * len(jobs) > 5_200:
        raise ValueError("wp015_physical_model_request_hard_cap_not_provable")
    manifest_path = args.output / "run-manifest.json"
    manifest = {
        "schema_version": "dof.wp015-run-manifest.v1", "mode": args.mode,
        "bundle_sha256": claimed, "inventory_sha256": inventory["inventory_sha256"],
        "scientific_source_commit": bundle["repository"]["head_commit"],
        "scientific_source_tree": bundle["repository"]["head_tree"],
        "job_count": len(jobs), "job_ids_sha256": digest([row["job_id"] for row in jobs]),
        "concurrency_policy": {"weekday_max": 8, "weekend_max": 16},
        "prior_accounting": prior_evidence,
        "retry_count": 0, "fallback_count": 0, "repair_count": 0,
    }
    if manifest_path.is_file() and read(manifest_path) != manifest:
        raise ValueError("wp015_resume_manifest_mismatch")
    write(manifest_path, manifest)
    for root in bundle["roots"].values():
        if file_sha256(Path(root["root_path"])) != root["sha256"]:
            raise ValueError("wp015_root_hash_mismatch_before:" + root["root_id"])
    existing = [
        read(args.output / "jobs" / row["job_id"] / "cell-result.json")
        for row in jobs if (args.output / "jobs" / row["job_id"] / "cell-result.json").is_file()
    ]
    spent_by_provider = collections.defaultdict(float, prior_spent)
    for row in existing:
        provider = runtime_profile(row["model_key"])["provider"]
        spent_by_provider[provider] += float(row.get("cost_usd") or 0.0)
    costs = {
        provider: base.SharedCost(cap, spent=spent_by_provider[provider])
        for provider, cap in PROVIDER_CAPS_USD.items()
    }
    by_model = {
        model: collections.deque(row for row in jobs if row["model_key"] == model)
        for model in MODEL_RESERVES_USD
    }
    streak = {model: 0 for model in MODEL_RESERVES_USD}
    completed_existing = sorted(
        existing, key=lambda row: (str(row.get("cell_completed_at_utc") or ""), row["job_id"]),
    )
    for result in completed_existing:
        model = result["model_key"]
        if result["canonical_terminal"] in PROVIDER_TERMINALS:
            streak[model] += 1
        elif result["canonical_terminal"] != "PROFILE_CIRCUIT_BREAKER_PAUSED":
            streak[model] = 0
    completed_ids = {row["job_id"] for row in existing}
    for model in by_model:
        by_model[model] = collections.deque(
            row for row in by_model[model] if row["job_id"] not in completed_ids
        )
    running: dict[asyncio.Task[dict[str, Any]], tuple[str, Mapping[str, Any]]] = {}
    in_flight = collections.Counter()
    models = list(MODEL_RESERVES_USD)
    per_model_limit = max(1, (args.max_parallel + len(models) - 1) // len(models))

    def refill() -> None:
        base.assert_clean_repository(bundle["repository"]["head_commit"])
        made_progress = True
        while len(running) < args.max_parallel and made_progress:
            made_progress = False
            for model in models:
                if len(running) >= args.max_parallel:
                    break
                if streak[model] >= 3 or in_flight[model] >= per_model_limit or not by_model[model]:
                    continue
                job = by_model[model].popleft()
                provider = runtime_profile(model)["provider"]
                task = asyncio.create_task(_complete_job(
                    job=job, bundle=bundle, capture=args.capture,
                    output=args.output, cost=costs[provider], python=args.python,
                    vivado=args.vivado,
                ))
                running[task] = (model, job)
                in_flight[model] += 1
                made_progress = True

    started = time.monotonic()
    refill()
    while running:
        done, _pending = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            model, _job = running.pop(task)
            in_flight[model] -= 1
            result = task.result()
            if result["canonical_terminal"] in PROVIDER_TERMINALS:
                streak[model] += 1
            else:
                streak[model] = 0
        refill()
    for model in models:
        if streak[model] < 3:
            continue
        while by_model[model]:
            job = by_model[model].popleft()
            result_path = args.output / "jobs" / job["job_id"] / "cell-result.json"
            result = _nonexecution_result(dict(job) | {
                "canonical_terminal": "PROFILE_CIRCUIT_BREAKER_PAUSED",
                "provider_requests": 0, "cost_usd": 0.0,
                "circuit_breaker_basis": "three_consecutive_provider_transport_or_integrity_terminals",
            }, None)
            write(result_path, result)
    for root in bundle["roots"].values():
        if file_sha256(Path(root["root_path"])) != root["sha256"]:
            raise ValueError("wp015_root_hash_mismatch_after:" + root["root_id"])
    base.assert_clean_repository(bundle["repository"]["head_commit"])
    aggregate = _aggregate(jobs=jobs, output=args.output, bundle=bundle)
    aggregate["prior_provider_cost_usd"] = sum(prior_spent.values())
    aggregate["prior_provider_requests"] = prior_requests
    aggregate["cumulative_provider_cost_usd"] = aggregate["api_cost_usd"] + sum(prior_spent.values())
    aggregate["cumulative_provider_requests"] = aggregate["provider_requests"] + prior_requests
    aggregate["queue_wall_seconds"] = time.monotonic() - started
    unsigned = dict(aggregate)
    unsigned.pop("aggregate_sha256", None)
    aggregate["aggregate_sha256"] = digest(unsigned)
    write(args.output / "aggregate.json", aggregate)
    print(json.dumps({key: aggregate[key] for key in (
        "job_count", "terminal_counts", "provider_requests", "api_cost_usd",
        "actual_heavy_calls", "physical_valid_children", "strict_g_exercised",
        "queue_wall_seconds",
    )}, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--mode", choices=("canary", "scientific"), required=True)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--inventory", type=Path, required=True)
    value.add_argument("--capture", type=Path, required=True)
    value.add_argument("--hardblock-scan", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--vivado", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=8)
    value.add_argument("--prior-aggregate", type=Path, action="append", default=[])
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
