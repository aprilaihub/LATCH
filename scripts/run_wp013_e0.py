#!/usr/bin/env python3
"""Freeze, execute, and audit the bounded WP-013 development E0.

This controller is deliberately development-only.  It binds two real roots,
one immutable observation per root, the exact OpenRouter model/route profiles,
the one-COMMIT planner, and one canonical terminal for each of the 16 jobs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.backends.xilinx.legacy import parse_timing_summary_static  # noqa: E402
from optimizer_pipeline.dof.e0_job import capture_real_observation  # noqa: E402
from optimizer_pipeline.dof.openrouter import (  # noqa: E402
    OPENROUTER_ADAPTER_VERSION,
    OPENROUTER_E1_PROFILES,
    OpenRouterChatCompletionsTransport,
    build_openrouter_outbound,
    discover_openrouter_endpoints,
    discover_openrouter_models,
)
from optimizer_pipeline.dof.provider import ProviderRequestBinding, ProviderSettings  # noqa: E402
from optimizer_pipeline.dof.provenance import observe_repository  # noqa: E402
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry  # noqa: E402
from optimizer_pipeline.dof.single_submission.language import (  # noqa: E402
    CompileContext,
    CompiledProgram,
)
from optimizer_pipeline.dof.single_submission.observation import (  # noqa: E402
    CommonQuerySurface,
    build_common_observation,
)
from optimizer_pipeline.dof.single_submission.planner import (  # noqa: E402
    PlannerError,
    SingleSubmissionSession,
    SystemBinding,
)
from optimizer_pipeline.dof.single_submission.specs import (  # noqa: E402
    build_protocol_freeze,
    canonical_json_bytes,
    protocol_freeze_hash,
)
from optimizer_pipeline.dof.processes import (  # noqa: E402
    OwnedLineProtocolSession,
    OwnedProcessCancelled,
    OwnedProcessRunner,
    ProcessLimits,
)
from optimizer_pipeline.dof.mutation_worker_binding import (  # noqa: E402
    build_mutation_worker_argv,
    build_mutation_worker_environment,
    mutation_worker_entry_path,
)
from optimizer_pipeline.dof.vivado_mutation_worker import (  # noqa: E402
    READY_LINE,
    WORKER_TRANSPORT_PROTOCOL_VERSION,
    WORKER_VERSION,
    effective_vivado_environment_hash,
    request_bytes,
    terminal_marker,
)
from scripts.run_wp013_admission import (  # noqa: E402
    Admission,
    _verify_worker_attestation,
    b64,
    relocation_effect_count,
    route_status_clean,
)


SCHEMA = "dof.wp013-development-e0.v1"
MODELS = ("openai/gpt-5.6-luna", "anthropic/claude-sonnet-5")
LLM_LEVELS = ("L2", "L3", "L4")
MACRO_OBSERVED_HEAVY_CALLS = {
    "phys_opt": 3,
    "place_route": 2,
    "auto_pblock": 2,
    "retime": 3,
    "net_swap": 3,
    "memory_path_opt": 4,
    "hardblock_relocate": 4,
    "congestion_spread": 2,
}
TERMINALS = frozenset(
    {
        "VALID_ROOT_REFERENCE",
        "VALID_COMPLETION",
        "TOOL_FAILURE",
        "TOOL_TIMEOUT",
        "INVALID_ARTIFACT",
        "NO_VALID_SUBMISSION",
        "NO_VALID_SUBMISSION_COST_GUARD",
        "PROVIDER_FAILURE",
        "MODEL_OR_ROUTE_DRIFT",
        "CAPABILITY_ACTION_REJECTION",
        "BUDGET_BLOCKED",
        "CONTROLLER_FAILURE",
    }
)


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_atomic(path: Path, value: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if private:
        path.chmod(0o600)


def strict_json_object(raw: bytes) -> dict[str, Any]:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("planner_message_not_object")
    return value


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, check=True, text=True, capture_output=True
    ).stdout.strip()


def assert_clean_repository(expected_commit: str | None = None) -> dict[str, Any]:
    repository = observe_repository(REPO)
    if repository.tracked_tree_dirty:
        raise RuntimeError("tracked_product_tree_not_clean")
    if expected_commit is not None and repository.head_commit != expected_commit:
        raise RuntimeError("source_commit_binding_mismatch")
    return {
        "head_commit": repository.head_commit,
        "head_tree": repository.head_tree,
        "binary_diff_sha256": repository.binary_diff_sha256,
        "tracked_tree_dirty": False,
        "branch_name": repository.branch_name,
    }


def _probe_job(root_id: str, root_hash: str) -> dict[str, Any]:
    core = {
        "schema_version": "dof.wp013-observation-capture-job.v1",
        "job": {"benchmark": root_id, "job_id": f"wp013-e0-observe-{root_id}"},
        "root_sha256": root_hash,
    }
    return core | {"job_spec_sha256": digest(core)}


async def capture(args: argparse.Namespace) -> int:
    plan = read_object(args.plan)
    assert_clean_repository()
    args.out.mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def one(root: Mapping[str, Any]) -> dict[str, Any]:
        async with semaphore:
            root_path = Path(root["root_path"]).resolve(strict=True)
            if file_digest(root_path) != root["sha256"]:
                raise RuntimeError(f"root_hash_mismatch:{root['root_id']}")
            root_dir = args.out / root["root_id"]
            observation = await asyncio.to_thread(
                capture_real_observation,
                _probe_job(root["root_id"], root["sha256"]),
                root_path,
                root_dir / "vivado-observation",
                decision_index=0,
                history=[],
                heavy_remaining=4,
                model_requests_remaining=4,
                vivado_exec=args.vivado,
                python_exec=args.python,
                repository_anchor=REPO,
            )
            probe_rows: dict[str, Any] = {}
            for mode in ("probe", "hardblock-probe"):
                command = (
                    str(args.python),
                    str(REPO / "scripts" / "run_wp013_admission.py"),
                    "--mode", mode,
                    "--root", str(root_path),
                    "--out", str(root_dir / mode),
                    "--python", str(args.python),
                    "--vivado", str(args.vivado),
                )
                completed = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    cwd=REPO,
                    text=True,
                    capture_output=True,
                    timeout=3600,
                )
                summary_path = root_dir / mode / "admission-summary.json"
                probe_rows[mode] = {
                    "returncode": completed.returncode,
                    "stdout_sha256": digest(completed.stdout.encode()),
                    "stderr_sha256": digest(completed.stderr.encode()),
                    "summary": read_object(summary_path) if summary_path.exists() else None,
                }
            accepted = (
                observation["origin"] == "real_read_only_vivado"
                and all(
                    row["returncode"] == 0
                    and row["summary"] is not None
                    and row["summary"]["accepted"]
                    for row in probe_rows.values()
                )
            )
            result = {
                "root_id": root["root_id"],
                "root_sha256": root["sha256"],
                "observation_sha256": observation["observation_sha256"],
                "probes": probe_rows,
                "accepted": accepted,
            }
            write_atomic(root_dir / "capture-summary.json", result)
            return result

    rows = await asyncio.gather(*(one(root) for root in plan["roots"]))
    aggregate = {
        "schema_version": "dof.wp013-e0-capture.v1",
        "roots": rows,
        "accepted": len(rows) == 2 and all(row["accepted"] for row in rows),
    }
    aggregate["capture_sha256"] = digest(aggregate)
    write_atomic(args.out / "capture-aggregate.json", aggregate)
    print(json.dumps({"accepted": aggregate["accepted"], "roots": len(rows)}))
    return 0 if aggregate["accepted"] else 2


def _registry_rows(capture_root: Path, root_hash: str) -> tuple[TrialHandleRegistry, dict[str, list[dict[str, Any]]], tuple[str, ...], tuple[int, int, int, int]]:
    secret = hashlib.sha256(("wp013-e0-handles-v1:" + root_hash).encode()).digest()
    registry = TrialHandleRegistry(
        trial_id="wp013-e0-v3", root_sha256=root_hash, secret=secret
    )
    spatial = read_object(capture_root / "probe" / "private" / "resolved-objects.json")
    hard = read_object(capture_root / "hardblock-probe" / "private" / "resolved-objects.json")
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in (
        "critical_path_details", "critical_object_locations",
        "high_fanout_net_details", "congested_region_details",
        "critical_hardblock_candidates", "pblock_constraint_details",
        "placement_summary", "route_summary",
    )}

    for item in spatial.get("critical_cells", []):
        attrs = {"path_member": True, "path_rank": int(item["index"]) + 1}
        registry.register(object_type="cell", canonical_identity=item["identity"], attributes=attrs)
        rows["critical_object_locations"].append(
            {"canonical_identity": item["identity"], "handle_type": "cell", **attrs}
        )
    for item in spatial.get("high_fanout_nets", []):
        attrs = {"fanout": int(item["attribute"])}
        registry.register(object_type="net", canonical_identity=item["identity"], attributes=attrs)
        rows["high_fanout_net_details"].append(
            {"canonical_identity": item["identity"], "handle_type": "net", **attrs}
        )
    region_handles: list[str] = []
    bounds: list[list[int]] = []
    for item in spatial.get("device_regions", []):
        match = re.fullmatch(r"(?:CLOCKREGION_)?X(\d+)Y(\d+)", item["identity"])
        if match is None:
            continue
        coordinate = [int(match.group(1)), int(match.group(1)), int(match.group(2)), int(match.group(2))]
        attrs = {"bounds": coordinate}
        handle = registry.register(object_type="region", canonical_identity=item["identity"], attributes=attrs)
        region_handles.append(handle)
        bounds.append(coordinate)
        rows["placement_summary"].append(
            {"canonical_identity": item["identity"], "handle_type": "region", **attrs}
        )

    site_handles: list[str] = []
    resource = "ANY"
    hardblocks = hard.get("critical_hardblocks", [])
    if hardblocks:
        observed = hardblocks[0]["attribute"].upper()
        resource = "DSP" if "DSP" in observed else "URAM" if "URAM" in observed else "BRAM" if "RAMB" in observed else "ANY"
    for item in hard.get("compatible_sites", []):
        attrs = {"site_class": item["attribute"], "compatible_resource_types": [resource]}
        site_handles.append(registry.register(object_type="site", canonical_identity=item["identity"], attributes=attrs))
    for item in hardblocks:
        observed = item["attribute"].upper()
        item_resource = "DSP" if "DSP" in observed else "URAM" if "URAM" in observed else "BRAM" if "RAMB" in observed else "ANY"
        attrs = {
            "path_member": True,
            "path_rank": int(item["index"]) + 1,
            "resource_type": item_resource,
            "compatible_site_handles": site_handles if item is hardblocks[0] else [],
        }
        registry.register(object_type="hardblock", canonical_identity=item["identity"], attributes=attrs)
        rows["critical_hardblock_candidates"].append(
            {"canonical_identity": item["identity"], "handle_type": "hardblock", **attrs}
        )

    if bounds:
        geometry = (0, max(item[1] for item in bounds), 0, max(item[3] for item in bounds))
    else:
        geometry = (0, 0, 0, 0)
    return registry, rows, tuple(region_handles), geometry


def _common_observation(plan_root: Mapping[str, Any], capture_root: Path) -> tuple[Any, dict[str, Any]]:
    real = read_object(capture_root / "vivado-observation" / "real-observation.json")
    source = real["source"]
    measurements = source["state"]["measurements"]
    timing = measurements["timing"]
    period = float(plan_root["target_period_ns"])
    wns = timing.get("wns_ns")
    fmax = 1000.0 / (period - float(wns)) if isinstance(wns, (int, float)) and period - float(wns) > 0 else None
    registry, rows, regions, geometry_bounds = _registry_rows(capture_root, plan_root["sha256"])
    observation = build_common_observation(
        root_pseudonym="root-" + plan_root["sha256"][:16],
        target_clock={"clock_period_ns": period, "evidence": plan_root["target_period_evidence"]},
        timing={"fmax_mhz": fmax, "wns_ns": wns, "tns_ns": timing.get("tns_ns"), "failing_endpoint_count": timing.get("failing_endpoints")},
        utilization=measurements["utilization"],
        implementation={"placement_state": "PLACED", "route_state": "ROUTED", "route_validity": measurements["route"]["status"]},
        constraints={"pblock_count": None, "constrained_cell_count": None, "missing_reason": "read_only_probe_did_not_report_constraints"},
        geometry={"device_class": "development-device-pseudonym", "clock_region_bounds": list(geometry_bounds), "observed_region_count": len(regions)},
        congestion={"coarse_class": "unknown", "hotspot_count": None, "missing_reason": "read_only_probe_did_not_report_congestion"},
        critical_paths=[{"path_rank": index + 1, "status": "object_details_available_via_Q"} for index in range(min(8, len(rows["critical_object_locations"])))],
        budget={"planning_turns": 4, "queries": 8, "feedback_rounds": 3, "heavy_calls": 4, "light_mutations": 8, "wall_time_seconds": 7200, "usd": 3.0},
    )
    private = {
        "registry": registry,
        "rows": rows,
        "predefined_regions": regions,
        "geometry_bounds": geometry_bounds,
        "real_observation_sha256": real["observation_sha256"],
    }
    return observation, private


def freeze(args: argparse.Namespace) -> int:
    plan = read_object(args.plan)
    repository = assert_clean_repository()
    capture_aggregate = read_object(args.capture / "capture-aggregate.json")
    if not capture_aggregate.get("accepted"):
        raise SystemExit("capture_not_accepted")
    roots: dict[str, Any] = {}
    observations: dict[str, Any] = {}
    for root in plan["roots"]:
        path = Path(root["root_path"]).resolve(strict=True)
        if file_digest(path) != root["sha256"]:
            raise SystemExit("root_hash_mismatch")
        observation, private = _common_observation(root, args.capture / root["root_id"])
        roots[root["root_id"]] = dict(root) | {"size_bytes": path.stat().st_size}
        observations[root["root_id"]] = {
            "payload": dict(observation.payload),
            "canonical_utf8": observation.canonical_bytes.decode(),
            "sha256": observation.sha256,
            "real_observation_sha256": private["real_observation_sha256"],
            "query_private_rows_sha256": digest(private["rows"]),
            "handle_manifest": private["registry"].public_manifest(),
            "predefined_regions": list(private["predefined_regions"]),
            "geometry_bounds": list(private["geometry_bounds"]),
        }
    jobs = []
    for root_id in roots:
        jobs.extend(
            [
                {"job_id": f"{root_id}__l0", "root_id": root_id, "level": "L0", "model": None},
                {"job_id": f"{root_id}__l1", "root_id": root_id, "level": "L1", "model": None},
            ]
        )
        for level in LLM_LEVELS:
            for model in MODELS:
                jobs.append({"job_id": f"{root_id}__{level.lower()}__{model.replace('/', '_')}", "root_id": root_id, "level": level, "model": model})
    core = {
        "schema_version": SCHEMA,
        "protocol_freeze_sha256": protocol_freeze_hash(),
        "plan_sha256": file_digest(args.plan),
        "capture_sha256": capture_aggregate["capture_sha256"],
        "repository": repository,
        "roots": roots,
        "observations": observations,
        "jobs": jobs,
        "job_count": len(jobs),
        "models": plan["models"],
        "guards": plan["guards"],
        "l1_heuristic_sha256": file_digest(REPO / plan["l1_heuristic_path"]),
        "macro_manifest_sha256": file_digest(REPO / plan["macro_manifest_path"]),
        "macro_heavy_accounting_erratum_sha256": file_digest(REPO / plan["macro_heavy_accounting_erratum_path"]),
        "primary_aggregate_eligible": False,
        "sealed_holdouts_accessed": False,
    }
    if (
        len(jobs) != 16
        or core["guards"]["pre_e0_observed_heavy_calls"] >= core["guards"]["wp013_total_heavy_call_cap"]
        or core["guards"]["e0_heavy_budget_remaining"]
        != core["guards"]["wp013_total_heavy_call_cap"] - core["guards"]["pre_e0_observed_heavy_calls"]
    ):
        raise SystemExit("e0_resource_or_matrix_contract_invalid")
    bundle = core | {"bundle_sha256": digest(core)}
    write_atomic(args.output, bundle)
    print(json.dumps({"status": "FROZEN", "jobs": len(jobs), "bundle_sha256": bundle["bundle_sha256"]}))
    return 0


async def preflight(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    models, endpoints = await asyncio.gather(
        discover_openrouter_models(key), discover_openrouter_endpoints(key)
    )
    selected = {model: endpoints["models"][model] for model in MODELS}
    payload = {
        "schema_version": "dof.wp013-openrouter-preflight.v1",
        "models": {model: models["required_models"][model] for model in MODELS},
        "endpoints": selected,
        "exact_models_available": all(models["required_models"][model] for model in MODELS),
        "exact_endpoints_available": all(
            selected[model]["endpoint_available"]
            and selected[model]["required_parameters_supported"]
            for model in MODELS
        ),
        "expected_profiles": {
            model: dict(OPENROUTER_E1_PROFILES[model]) for model in MODELS
        },
        "credential_included": False,
        "cost_cap_usd": 3.0,
    }
    payload["accepted"] = bool(
        payload["exact_models_available"] and payload["exact_endpoints_available"]
    )
    payload["preflight_sha256"] = digest(payload)
    write_atomic(args.output, payload)
    print(json.dumps({"accepted": payload["accepted"], "models": payload["models"]}))
    return 0 if payload["accepted"] else 2


def _spec_bodies() -> dict[str, dict[str, Any]]:
    return {item.name: item.payload()["body"] for item in build_protocol_freeze()}


def _macro_docs(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "action_key": row["action_key"],
            "semantics": row["semantics"],
            "preconditions": row["preconditions"],
            "cost": row["cost"],
            "failure_no_target_semantics": row["failure_no_target_semantics"],
        }
        for row in body["guaranteed_actions"]
    ]


def _language_contract(level: str) -> dict[str, Any]:
    specs = _spec_bodies()
    result: dict[str, Any] = {
        "level": level,
        "allowed_languages": {"L2": ["M"], "L3": ["M", "P"], "L4": ["M", "P", "G"]}[level],
        "M": _macro_docs(specs["M_LANGUAGE_V1"]),
        "planner": {
            "turns": 4,
            "queries": 8,
            "feedback_rounds": 3,
            "commit": 1,
            "qor_before_commit": False,
            "messages": ["QUERY", "DRAFT", "VALIDATE", "COMMIT", "STOP", "NO_VALID_SUBMISSION"],
        },
    }
    if level in {"L3", "L4"}:
        result["P"] = specs["P_LANGUAGE_V1"]
    if level == "L4":
        result["G"] = specs["G_AST_LANGUAGE_V1"]
    return result


def _prompt(level: str) -> bytes:
    return (
        "Return exactly one JSON object and no prose or markdown. The object is one planner message. "
        "Allowed forms are {\"kind\":\"QUERY\",\"query\":{\"name\":QUERY_NAME,\"top_k\":INTEGER}}, "
        "{\"kind\":\"DRAFT\"|\"VALIDATE\"|\"COMMIT\",\"candidate\":CANDIDATE}, "
        "or {\"kind\":\"STOP\"|\"NO_VALID_SUBMISSION\"}. "
        "Never include system-owned decision/proposal/request/catalogue/root/context/provider/model/route fields. "
        "Do not use paths, object names, Tcl, shell, files, network, Python, loops, retries, or hidden calls. "
        "Construction feedback may be repaired before COMMIT. No QoR is available before COMMIT. "
        f"The active level is {level}; choose one intervention intended to preserve validity and improve final valid Fmax."
    ).encode()


def _binding(
    *,
    job: Mapping[str, Any],
    model: str,
    turn: int,
    context: bytes,
    prompt: bytes,
    catalogue_hash: str,
    reserve: float,
) -> ProviderRequestBinding:
    identity = f"{job['job_id']}:{turn}"
    return ProviderRequestBinding(
        run_id="wp013-e0-" + digest(identity)[:24],
        spec_hash=digest({"protocol": SCHEMA, "job": job}),
        experiment_id="wp013-development-e0-v3",
        condition_id="condition-" + digest(job["job_id"])[:20],
        repeat_id="development-e0-1",
        decision_id="decision-" + digest(job["job_id"])[:20],
        context_bytes=context,
        prompt_bytes=prompt,
        prompt_version="dof.wp013-one-submission-prompt.v1",
        action_interface_version="dof.single-submission.planner-message.v1",
        action_interface_hash=digest("dof.single-submission.planner-message.v1"),
        context_interface_version="dof.wp013-planning-context.v1",
        context_interface_hash=digest(context),
        catalogue_version=f"dof.wp013.{job['level'].lower()}.v1",
        catalogue_hash=catalogue_hash,
        capability_hash=catalogue_hash,
        mask_evidence_hash=digest("development-roots-only-no-holdout"),
        provider_role="wp013_single_submission_planner",
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
            max_usd=reserve,
            max_cost_per_request_usd=reserve,
            unknown_cost_policy="reject",
            unknown_usage_policy="reject",
        ),
    )


@dataclass
class SharedCost:
    cap: float
    spent: float = 0.0
    reserved: float = 0.0
    lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        self.lock = asyncio.Lock()

    async def acquire(self, amount: float) -> bool:
        assert self.lock is not None
        async with self.lock:
            if self.spent + self.reserved + amount > self.cap + 1e-12:
                return False
            self.reserved += amount
            return True

    async def settle(self, reserve: float, actual: float) -> None:
        assert self.lock is not None
        async with self.lock:
            self.reserved -= reserve
            self.spent += actual
            if self.spent > self.cap + 1e-12:
                raise RuntimeError("openrouter_actual_cost_cap_exceeded")


def _load_trial(bundle: Mapping[str, Any], capture_dir: Path, root_id: str) -> tuple[Any, dict[str, Any]]:
    root = bundle["roots"][root_id]
    observation, private = _common_observation(root, capture_dir / root_id)
    frozen = bundle["observations"][root_id]
    if observation.sha256 != frozen["sha256"] or observation.canonical_bytes.decode() != frozen["canonical_utf8"]:
        raise RuntimeError("common_observation_replay_mismatch")
    return observation, private


def _query_provider(private: Mapping[str, Any]):
    def provide(name: str, top_k: int):
        rows = private["rows"].get(name, [])
        missing = None if rows else "read_only_probe_has_no_rows_for_query"
        return rows, ("wp013-frozen-read-only-capture",), 0.0, missing
    return provide


def _planner_context(
    *,
    observation: Any,
    session: SingleSubmissionSession,
    contract: Mapping[str, Any],
    last_result: Mapping[str, Any] | None,
) -> bytes:
    value = {
        "schema_version": "dof.wp013-planning-context.v1",
        "objective": "Preserve mandatory validity and maximise final valid Fmax under one committed intervention.",
        "common_observation": dict(observation.payload),
        "common_observation_sha256": observation.sha256,
        "common_query_catalogue": session.queries.catalogue,
        "language_contract": contract,
        "turns_remaining": 4 - session.turns_used,
        "construction_feedback_rounds_remaining": 3 - session.feedback_rounds,
        "previous_result": last_result,
        "qor_feedback_present": False,
    }
    return canonical_json_bytes(value)


def _turn_public(result: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"status": result.status, "feedback": list(result.feedback)}
    if result.query_result is not None:
        row["query_result"] = result.query_result.public_payload()
        row["query_result_sha256"] = result.query_result.sha256
        row["query_latency_seconds"] = result.query_result.latency_seconds
        row["private_raw_sha256"] = result.query_result.private_raw_sha256
    if result.compiled is not None:
        row["compiled"] = result.compiled.public_payload()
        row["compiled_sha256"] = result.compiled.public_sha256
    return row


async def _plan_job(
    job: Mapping[str, Any],
    bundle: Mapping[str, Any],
    capture_dir: Path,
    output: Path,
    key: str,
    cost: SharedCost,
    model_reserves: Mapping[str, float],
) -> tuple[dict[str, Any], TrialHandleRegistry | None, CompiledProgram | None, dict[str, Any] | None]:
    started = time.monotonic()
    root_id, level, model = job["root_id"], job["level"], job["model"]
    if level == "L0":
        return ({**job, "canonical_terminal": "VALID_ROOT_REFERENCE", "planning_wall_seconds": time.monotonic() - started, "provider_requests": 0, "cost_usd": 0.0}, None, None, None)
    if level == "L1":
        heuristic = read_object(REPO / "configs/wp013/l1-heuristic-v1.json")
        macro = heuristic["global_ranking"][0]
        return ({**job, "canonical_terminal": "PLANNED_COMMIT", "selected_language": "M", "selected_macro": macro, "planning_wall_seconds": time.monotonic() - started, "provider_requests": 0, "cost_usd": 0.0, "l1_heuristic_sha256": heuristic["heuristic_sha256"]}, None, None, {"language": "M", "macro": macro})

    observation, private = _load_trial(bundle, capture_dir, root_id)
    queries = CommonQuerySurface(private["registry"])
    contract = _language_contract(level)
    catalogue_hash = digest(contract)
    binding = SystemBinding.create(
        observation_hash=observation.sha256,
        root_binding=bundle["roots"][root_id]["sha256"],
        catalogue_version=f"dof.wp013.{level.lower()}.v1",
        catalogue_hash=catalogue_hash,
        provider="openrouter",
        model=model,
        route=OPENROUTER_E1_PROFILES[model]["endpoint_tag"],
        session_nonce=job["job_id"],
    )
    session = SingleSubmissionSession(
        level=level,
        observation=observation,
        queries=queries,
        compile_context=CompileContext(
            private["registry"], "ROUTED", private["predefined_regions"], private["geometry_bounds"]
        ),
        binding=binding,
        query_provider=_query_provider(private),
    )
    prompt = _prompt(level)
    reserve = model_reserves[model]
    turns: list[dict[str, Any]] = []
    last_result: dict[str, Any] | None = None
    final_candidate = None
    terminal_override = None
    session_dir = output / "planning" / job["job_id"]
    for turn in range(4):
        if not await cost.acquire(reserve):
            terminal_override = "NO_VALID_SUBMISSION_COST_GUARD"
            break
        context = _planner_context(observation=observation, session=session, contract=contract, last_result=last_result)
        if len(context) + len(prompt) > 16384:
            await cost.settle(reserve, 0.0)
            terminal_override = "CONTROLLER_FAILURE"
            turns.append({"turn": turn + 1, "error": "provider_input_byte_bound_exceeded", "input_bytes": len(context) + len(prompt)})
            break
        request = _binding(job=job, model=model, turn=turn, context=context, prompt=prompt, catalogue_hash=catalogue_hash, reserve=reserve)
        transport = OpenRouterChatCompletionsTransport(
            api_key=key, expected_outbound_binding=build_openrouter_outbound(request)
        )
        response = None
        error = None
        try:
            response = await transport.send(request, 0)
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
        actual = float(response.cost_usd or 0.0) if response is not None else float((transport.reconciled_accounting or {}).get("cost_usd", 0.0))
        await cost.settle(reserve, actual)
        exchange = transport.last_exchange or {}
        write_atomic(session_dir / "private" / f"turn-{turn + 1}-exchange.json", exchange, private=True)
        row: dict[str, Any] = {
            "turn": turn + 1,
            "request_contract_hash": request.request_contract_hash,
            "prompt_hash": request.prompt_hash,
            "context_hash": request.context_hash,
            "context_size_bytes": len(context),
            "physical_requests": transport.physical_requests,
            "cost_usd": actual,
            "input_tokens": response.input_tokens if response else None,
            "output_tokens": response.output_tokens if response else None,
            "returned_model": response.returned_model_id if response else None,
            "returned_upstream": response.returned_model_revision if response else None,
            "route": exchange.get("route"),
            "failure_class": exchange.get("failure_class"),
            "error": error,
        }
        if error is not None or response is None:
            turns.append(row)
            terminal_override = "MODEL_OR_ROUTE_DRIFT" if exchange.get("failure_class") == "MODEL_OR_ROUTE_DRIFT" else "PROVIDER_FAILURE"
            break
        if response.returned_model_id != model or response.returned_model_revision != OPENROUTER_E1_PROFILES[model]["upstream_provider"]:
            turns.append(row)
            terminal_override = "MODEL_OR_ROUTE_DRIFT"
            break
        try:
            message = strict_json_object(response.content_bytes)
            result = session.accept(message)
            public = _turn_public(result)
            row["message_sha256"] = digest(response.content_bytes)
            row["planner_result"] = public
            last_result = public
            if result.status == "COMMITTED":
                final_candidate = dict(message["candidate"])
        except (ValueError, PlannerError) as exc:
            reason = getattr(exc, "reason", str(exc))
            row["planner_result"] = {"status": "CONSTRUCTION_INVALID", "feedback": [reason]}
            last_result = row["planner_result"]
            if session.terminal is not None:
                terminal_override = "NO_VALID_SUBMISSION"
        turns.append(row)
        if session.terminal is not None:
            break
    terminal = terminal_override or ("PLANNED_COMMIT" if session.committed else "NO_VALID_SUBMISSION")
    evidence = session.evidence()
    result_row = {
        **job,
        "canonical_terminal": terminal,
        "selected_language": session.committed.language if session.committed else None,
        "committed_sha256": session.committed.public_sha256 if session.committed else None,
        "provider_requests": sum(row["physical_requests"] for row in turns),
        "cost_usd": sum(row["cost_usd"] for row in turns),
        "turns": turns,
        "planner_evidence": evidence,
        "observation_sha256": observation.sha256,
        "observation_size_bytes": len(observation.canonical_bytes),
        "query_handle_manifest": private["registry"].public_manifest(),
        "planning_wall_seconds": time.monotonic() - started,
    }
    write_atomic(session_dir / "planning-result.json", result_row)
    if final_candidate is not None:
        write_atomic(session_dir / "private" / "committed-source.json", final_candidate, private=True)
    return result_row, private["registry"], session.committed, final_candidate


def _macro_from_candidate(candidate: Mapping[str, Any] | None) -> str | None:
    if not isinstance(candidate, Mapping):
        return None
    if candidate.get("language") == "M" and set(candidate) == {"language", "macro"}:
        return str(candidate["macro"]).removeprefix("macro.")
    if candidate.get("language") == "P":
        steps = candidate.get("steps")
        if isinstance(steps, list) and len(steps) == 1 and isinstance(steps[0], Mapping) and steps[0].get("op") == "CALL_MACRO":
            return str(steps[0].get("args", {}).get("macro", "")).removeprefix("macro.")
    if candidate.get("language") == "G":
        statements = candidate.get("statements")
        if isinstance(statements, list) and len(statements) == 1 and isinstance(statements[0], Mapping):
            row = statements[0]
            if row.get("node") == "CALL_MACRO":
                return str(row.get("args", {}).get("macro", "")).removeprefix("macro.")
            if row.get("node") == "P_PROGRAM":
                return _macro_from_candidate(row.get("args", {}).get("program"))
    return None


def _macro_attempt(job_dir: Path, flow: str) -> dict[str, Any]:
    log_path = job_dir / "logs" / f"01_{flow}.log"
    call_counts = {"place_design": 0, "route_design": 0, "phys_opt_design": 0}
    if log_path.exists():
        pattern = re.compile(r"\[VIVADO\] Calling (place_design|route_design|phys_opt_design) with args:")
        for operation in pattern.findall(log_path.read_text(errors="replace")):
            call_counts[operation] += 1
    accounting = {
        "actual_heavy_calls": sum(call_counts.values()),
        "actual_heavy_call_counts": call_counts,
        "heavy_accounting_source": "exact host-call start markers in immutable flow log",
    }
    summary_path = job_dir / "flow_matrix_summary.json"
    if not summary_path.exists():
        return accounting | {"canonical_endpoint": "TOOL_FAILURE", "missing_reason": "flow_matrix_summary_missing"}
    outer = read_object(summary_path)["flows"][0]
    run_db = job_dir / "runs" / f"01_{flow}" / "run.db"
    result: dict[str, Any] = {"outer_status": outer.get("status"), "outer_exit_code": outer.get("exit_code"), "outer_duration_seconds": outer.get("duration_seconds")}
    if not run_db.exists():
        return result | accounting | {"canonical_endpoint": "TOOL_FAILURE", "missing_reason": "run_db_missing"}
    connection = sqlite3.connect(run_db)
    connection.row_factory = sqlite3.Row
    try:
        attempt = connection.execute("SELECT * FROM attempts ORDER BY scheduled_at DESC LIMIT 1").fetchone()
        if attempt is None:
            return result | accounting | {"canonical_endpoint": "TOOL_FAILURE", "missing_reason": "attempt_missing"}
        validation = connection.execute("SELECT * FROM validations WHERE attempt_id=? ORDER BY checked_at DESC LIMIT 1", (attempt["attempt_id"],)).fetchone()
        outcome = connection.execute("SELECT * FROM outcomes WHERE attempt_id=? ORDER BY finished_at DESC LIMIT 1", (attempt["attempt_id"],)).fetchone()
        metrics = json.loads(outcome["metrics_json"] or "{}") if outcome else {}
        validation_status = validation["status"] if validation else None
        endpoint = {
            "CLEAN": "VALID_COMPLETION",
            "VALID": "VALID_COMPLETION",
            "INVALID": "INVALID_ARTIFACT",
            "TOOL_FAILED": "TOOL_FAILURE",
            "TIMEOUT": "TOOL_TIMEOUT",
        }.get(validation_status, "TOOL_FAILURE")
        return result | accounting | {
            "canonical_endpoint": endpoint,
            "attempt_id": attempt["attempt_id"],
            "validation_status": validation_status,
            "outcome_status": outcome["status"] if outcome else None,
            "error_type": outcome["error_type"] if outcome else None,
            "output_artifact_id": outcome["output_artifact_id"] if outcome else None,
            "initial_wns_ns": metrics.get("initial_wns_ns"),
            "final_wns_ns": metrics.get("final_wns_ns"),
            "initial_fmax_mhz": metrics.get("initial_fmax_mhz"),
            "final_fmax_mhz": metrics.get("final_fmax_mhz"),
            "fmax_improvement_mhz": metrics.get("fmax_improvement_mhz"),
            "duration_seconds": metrics.get("duration_seconds"),
        }
    finally:
        connection.close()


async def _execute_macro(
    *, root: Path, flow: str, out: Path, python: Path
) -> dict[str, Any]:
    command = (
        str(python), str(REPO / "scripts" / "run_flow_matrix.py"), str(root),
        "--flows", flow, "--out", str(out), "--run-mode", "research-dev",
        "--skip-functional-validation",
    )
    started = time.monotonic()
    limits = ProcessLimits(
        initialization_timeout_seconds=60.0,
        whole_run_timeout_seconds=7200.0,
        graceful_close_seconds=20.0,
        terminate_grace_seconds=10.0,
        kill_grace_seconds=15.0,
        cleanup_timeout_seconds=60.0,
        output_limit_bytes=1024 * 1024,
    )
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    try:
        process = await OwnedProcessRunner(limits).run(command, cwd=REPO, env=environment)
    except OwnedProcessCancelled as exc:
        process = exc.result
    attempted = _macro_attempt(out, flow)
    if process.endpoint.value == "TIMED_OUT":
        attempted["canonical_endpoint"] = "TOOL_TIMEOUT"
    elif not process.cleanup_ok and attempted.get("canonical_endpoint") == "VALID_COMPLETION":
        attempted["canonical_endpoint"] = "TOOL_FAILURE"
    return attempted | {
        "process": process.to_dict(include_output_text=False),
        "wall_seconds": time.monotonic() - started,
    }


def _typed_supported(compiled: CompiledProgram) -> tuple[bool, list[str]]:
    supported = {
        "vivado.create_pblock", "vivado.assign_pblock", "vivado.remove_pblock",
        "vivado.relocate_hardblock", "vivado.unroute", "vivado.unplace",
        "vivado.place", "vivado.route", "vivado.phys_opt",
    }
    unknown = sorted({step.operation for step in compiled.host_steps} - supported)
    return not unknown, unknown


async def _execute_typed(
    *,
    compiled: CompiledProgram,
    registry: TrialHandleRegistry,
    root: Path,
    out: Path,
    python: Path,
    vivado: Path,
) -> dict[str, Any]:
    supported, unknown = _typed_supported(compiled)
    if not supported:
        return {"canonical_endpoint": "CAPABILITY_ACTION_REJECTION", "unsupported_host_operations": unknown, "actual_heavy_calls": 0, "actual_light_mutations": 0}
    root_before = file_digest(root)
    backend = Admission(SimpleNamespace(mode="probe", root=root, out=out, python=python, vivado=vivado))
    backend.registry = registry
    backend.home.mkdir(parents=True, mode=0o700)
    limits = ProcessLimits(
        initialization_timeout_seconds=120.0,
        whole_run_timeout_seconds=7200.0,
        graceful_close_seconds=20.0,
        terminate_grace_seconds=5.0,
        kill_grace_seconds=10.0,
        cleanup_timeout_seconds=40.0,
        output_limit_bytes=8 * 1024 * 1024,
    )
    environment = build_mutation_worker_environment(os.environ)
    argv = build_mutation_worker_argv(
        python_exec=python,
        worker_entry=mutation_worker_entry_path(),
        vivado_exec=vivado,
        initialization_timeout_seconds=120.0,
        run_workspace=out,
        isolated_home=backend.home,
    )
    backend.session = OwnedLineProtocolSession(
        argv,
        ready_line=READY_LINE,
        close_command=request_bytes("worker.exit", "cleanup", timeout_seconds=10.0),
        limits=limits,
        cwd=out,
        env=environment,
    )
    started = time.monotonic()
    error = None
    actual = None
    pre_timing = post_timing = route = None
    pblock_ids: dict[str, str] = {}
    try:
        await backend.session.start()
        _verify_worker_attestation(
            backend.session.startup_output.data,
            expected_vivado="2025.1",
            expected_python=platform.python_version(),
            expected_environment_hash=effective_vivado_environment_hash(environment, backend.home),
            expected_worker=WORKER_VERSION,
            expected_protocol=WORKER_TRANSPORT_PROTOCOL_VERSION,
        )
        await backend.call("vivado.open_checkpoint", {"dcp_path_b64": b64(root)}, timeout=900.0)
        pre_timing = await backend.call("vivado.report_timing_summary", timeout=300.0)
        actual = {"selected": 0, "transformed": 0, "heavy": 0, "light": 0}
        for index, step in enumerate(compiled.host_steps):
            op = step.operation
            if op == "vivado.create_pblock":
                region = registry.resolve(str(step.public_args["region"]), expected_type="region")
                bounds = region.attributes["bounds"]
                region_name = region.canonical_identity if re.fullmatch(r"(?:CLOCKREGION_)?X\d+Y\d+", region.canonical_identity) else f"X{bounds[0]}Y{bounds[2]}"
                pblock_handle = str(step.public_args.get("pblock") or next(obj.handle for obj in registry.objects(object_type="pblock") if obj.attributes.get("region_handle") == region.handle))
                pblock_id = f"wp013_e0_pb{index}"
                pblock_ids[pblock_handle] = pblock_id
                await backend.call("vivado.wp013_create_pblock", {"pblock_id": pblock_id, "region_name_b64": b64(region_name)})
                actual["transformed"] += 1
            elif op == "vivado.assign_pblock":
                targets = [registry.resolve(handle).canonical_identity for handle in step.public_args["targets"]]
                await backend.call("vivado.wp013_assign_pblock", {"pblock_id": pblock_ids[str(step.public_args["pblock"])], "cell_names_b64": [b64(name) for name in targets]})
                actual["selected"] += len(targets); actual["transformed"] += len(targets)
            elif op == "vivado.remove_pblock":
                await backend.call("vivado.wp013_remove_pblock", {"pblock_id": pblock_ids[str(step.public_args["pblock"])]})
                actual["transformed"] += 1
            elif op == "vivado.relocate_hardblock":
                targets = [registry.resolve(handle, expected_type="hardblock").canonical_identity for handle in step.public_args["targets"]]
                site = registry.resolve(str(step.public_args["destination"]), expected_type="site").canonical_identity
                output = await backend.call("vivado.wp013_relocate_hardblocks", {"cell_names_b64": [b64(name) for name in targets], "site_names_b64": [b64(site) for _ in targets]}, timeout=900.0)
                actual["selected"] += len(targets); actual["transformed"] += relocation_effect_count(output, len(targets))
            elif op == "vivado.unroute":
                await backend.call("vivado.unroute_all", timeout=900.0)
            elif op == "vivado.unplace":
                await backend.call("vivado.unplace_all", timeout=900.0)
            elif op == "vivado.place":
                await backend.call("vivado.place_design", {"directive": str(step.public_args.get("directive", "Default"))}, timeout=900.0)
            elif op == "vivado.route":
                directive = str(step.public_args.get("directive", "Default"))
                if directive not in {"Default", "Explore"}:
                    raise RuntimeError("typed_executor_route_directive_not_supported_by_attested_worker")
                await backend.call("vivado.route_design", {"directive": directive}, timeout=900.0)
            elif op == "vivado.phys_opt":
                directive = str(step.public_args.get("directive"))
                options = list(step.public_args.get("options", []))
                if options or directive not in {"Explore", "AlternateFlowWithRetiming"}:
                    raise RuntimeError("typed_executor_phys_opt_combination_not_supported_by_attested_worker")
                await backend.call("vivado.phys_opt_design", {"directive": directive}, timeout=900.0)
            actual["heavy" if step.event_class == "IMPLEMENTATION_HEAVY" else "light"] += 1
        await backend.call("vivado.write_checkpoint", {"dcp_path_b64": b64(backend.child)}, timeout=900.0)
        post_timing = await backend.call("vivado.report_timing_summary", timeout=300.0)
        route = await backend.call("vivado.report_route_status", timeout=300.0)
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
    finally:
        process = None
        try:
            process_result = await backend.session.close()
            process = process_result.to_dict(include_output_text=False)
        except Exception as exc:
            if error is None:
                error = f"worker_close:{type(exc).__name__}:{exc}"
            if backend.session.result is not None:
                process = backend.session.result.to_dict(include_output_text=False)
    root_after = file_digest(root)
    clean_route = bool(route and route_status_clean(route))
    child = {"sha256": file_digest(backend.child), "size_bytes": backend.child.stat().st_size} if backend.child.exists() else None
    endpoint = "VALID_COMPLETION" if error is None and clean_route and child is not None and process and process["cleanup_ok"] else "TOOL_FAILURE"
    observed_heavy = sum(
        row["operation"] in {"vivado.place_design", "vivado.route_design", "vivado.phys_opt_design"}
        for row in backend.trace
    )
    observed_light = sum(
        row["operation"] in {
            "vivado.unroute_all", "vivado.unplace_all", "vivado.wp013_create_pblock",
            "vivado.wp013_assign_pblock", "vivado.wp013_remove_pblock",
            "vivado.wp013_relocate_hardblocks",
        }
        for row in backend.trace
    )
    return {
        "canonical_endpoint": endpoint,
        "error": error,
        "root_unchanged": root_after == root_before,
        "child": child,
        "route_valid": clean_route,
        "pre_timing": parse_timing_summary_static(pre_timing.decode(errors="replace")) if pre_timing else None,
        "post_timing": parse_timing_summary_static(post_timing.decode(errors="replace")) if post_timing else None,
        "actual_heavy_calls": observed_heavy,
        "actual_light_mutations": observed_light,
        "call_trace": backend.trace,
        "selected_count": actual["selected"] if actual else 0,
        "transformed_count": actual["transformed"] if actual else 0,
        "process": process,
        "wall_seconds": time.monotonic() - started,
    }


async def run(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    bundle = read_object(args.bundle)
    claimed = bundle.pop("bundle_sha256")
    if digest(bundle) != claimed:
        raise SystemExit("bundle_hash_invalid")
    bundle["bundle_sha256"] = claimed
    preflight_value = read_object(args.preflight)
    if not preflight_value.get("accepted"):
        raise SystemExit("provider_preflight_not_accepted")
    assert_clean_repository(bundle["repository"]["head_commit"])
    if args.max_parallel < 1 or args.max_parallel > 8:
        raise SystemExit("weekday_parallelism_must_be_1_to_8")
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    started = time.monotonic()
    roots_before = {
        root_id: file_digest(Path(root["root_path"])) for root_id, root in bundle["roots"].items()
    }
    if any(roots_before[root_id] != root["sha256"] for root_id, root in bundle["roots"].items()):
        raise SystemExit("root_hash_mismatch_before_run")
    model_reserves = {row["model"]: float(row["per_request_reserve_usd"]) for row in bundle["models"]}
    cost = SharedCost(float(bundle["guards"]["openrouter_cost_cap_usd"]))
    planning_semaphore = asyncio.Semaphore(args.max_parallel)

    async def plan_one(job: Mapping[str, Any]):
        async with planning_semaphore:
            return await _plan_job(job, bundle, args.capture, args.out, key, cost, model_reserves)

    planned = await asyncio.gather(*(plan_one(job) for job in bundle["jobs"]))
    execution_semaphore = asyncio.Semaphore(args.max_parallel)

    def required_heavy(item: tuple[dict[str, Any], TrialHandleRegistry | None, CompiledProgram | None, dict[str, Any] | None]) -> int:
        row, _registry, compiled, candidate = item
        if row["canonical_terminal"] in TERMINALS:
            return 0
        macro = row.get("selected_macro") or _macro_from_candidate(candidate)
        if macro is not None:
            try:
                return MACRO_OBSERVED_HEAVY_CALLS[macro]
            except KeyError as exc:
                raise RuntimeError("selected_macro_not_in_observed_accounting_map") from exc
        return int(compiled.heavy_calls) if compiled is not None else 0

    # Allocate before any implementation starts.  L1 is guaranteed first;
    # LLM jobs then use a hash-stable order independent of QoR or provider cost.
    execution_allocations: dict[str, dict[str, Any]] = {}
    remaining_heavy = int(bundle["guards"]["e0_heavy_budget_remaining"])
    allocation_order = sorted(
        planned,
        key=lambda item: (
            0 if item[0]["level"] == "L1" else 1,
            digest(item[0]["job_id"]),
        ),
    )
    for item in allocation_order:
        row = item[0]
        required = required_heavy(item)
        allowed = row["canonical_terminal"] not in TERMINALS and required <= remaining_heavy
        execution_allocations[row["job_id"]] = {
            "required_heavy_calls": required,
            "dispatch_allowed": allowed,
            "remaining_before": remaining_heavy,
            "allocation_basis": "L1-first then SHA256(job_id); QoR-independent",
        }
        if allowed:
            remaining_heavy -= required
        execution_allocations[row["job_id"]]["remaining_after"] = remaining_heavy

    async def execute_one(item: tuple[dict[str, Any], TrialHandleRegistry | None, CompiledProgram | None, dict[str, Any] | None]) -> dict[str, Any]:
        row, registry, compiled, candidate = item
        allocation = execution_allocations[row["job_id"]]
        if row["canonical_terminal"] in TERMINALS:
            return row | {"execution_started": False, "actual_heavy_calls": 0, "actual_light_mutations": 0, "execution_allocation": allocation}
        if not allocation["dispatch_allowed"]:
            return row | {"canonical_terminal": "BUDGET_BLOCKED", "execution_started": False, "actual_heavy_calls": 0, "actual_light_mutations": 0, "execution_allocation": allocation}
        root = Path(bundle["roots"][row["root_id"]]["root_path"])
        execution_dir = args.out / "execution" / row["job_id"]
        async with execution_semaphore:
            macro = row.get("selected_macro") or _macro_from_candidate(candidate)
            if macro:
                result = await _execute_macro(root=root, flow=macro, out=execution_dir, python=args.python)
                result.setdefault("actual_light_mutations", None)
            elif compiled is not None and registry is not None:
                result = await _execute_typed(compiled=compiled, registry=registry, root=root, out=execution_dir, python=args.python, vivado=args.vivado)
            else:
                result = {"canonical_endpoint": "CONTROLLER_FAILURE", "error": "planned_commit_missing_compiled_program", "actual_heavy_calls": 0, "actual_light_mutations": 0}
            write_atomic(execution_dir / "execution-summary.json", result)
            if int(result.get("actual_heavy_calls") or 0) > allocation["required_heavy_calls"]:
                result = result | {"canonical_endpoint": "CONTROLLER_FAILURE", "accounting_error": "observed_heavy_calls_exceeded_predispatch_reservation"}
            return row | {"canonical_terminal": result["canonical_endpoint"], "execution_started": True, "execution": result, "actual_heavy_calls": result.get("actual_heavy_calls", 0), "actual_light_mutations": result.get("actual_light_mutations"), "execution_allocation": allocation}

    final_rows = await asyncio.gather(*(execute_one(item) for item in planned))
    roots_after = {
        root_id: file_digest(Path(root["root_path"])) for root_id, root in bundle["roots"].items()
    }
    repository_after = assert_clean_repository(bundle["repository"]["head_commit"])
    terminal_counts = {terminal: sum(row["canonical_terminal"] == terminal for row in final_rows) for terminal in sorted({row["canonical_terminal"] for row in final_rows})}
    aggregate = {
        "schema_version": SCHEMA,
        "bundle_sha256": claimed,
        "source_commit": bundle["repository"]["head_commit"],
        "jobs": final_rows,
        "planned_jobs": len(bundle["jobs"]),
        "canonical_terminal_jobs": sum(row["canonical_terminal"] in TERMINALS for row in final_rows),
        "terminal_counts": terminal_counts,
        "provider_requests": sum(row.get("provider_requests", 0) for row in final_rows),
        "known_openrouter_cost_usd": cost.spent,
        "openrouter_cost_cap_usd": cost.cap,
        "actual_heavy_calls_e0": sum(int(row.get("actual_heavy_calls") or 0) for row in final_rows),
        "pre_e0_observed_heavy_calls": bundle["guards"]["pre_e0_observed_heavy_calls"],
        "pre_e0_accounting": bundle["guards"]["pre_e0_accounting"],
        "wp013_observed_heavy_total": bundle["guards"]["pre_e0_observed_heavy_calls"] + sum(int(row.get("actual_heavy_calls") or 0) for row in final_rows),
        "wp013_heavy_cap": bundle["guards"]["wp013_total_heavy_call_cap"],
        "roots_before": roots_before,
        "roots_after": roots_after,
        "all_roots_unchanged": roots_before == roots_after,
        "repository_before": bundle["repository"],
        "repository_after": repository_after,
        "all_o_hashes_equal_by_root": all(
            len({row.get("observation_sha256") for row in final_rows if row["root_id"] == root_id and row["level"] in LLM_LEVELS}) == 1
            for root_id in bundle["roots"]
        ),
        "precommit_qor_feedback_count": sum(
            int(row.get("planner_evidence", {}).get("precommit_qor_feedback_count", 0)) for row in final_rows
        ),
        "precommit_mutation_count": sum(
            int(row.get("planner_evidence", {}).get("precommit_mutation_count", 0)) for row in final_rows
        ),
        "postcommit_resubmission_count": sum(
            int(row.get("planner_evidence", {}).get("postcommit_resubmission_count", 0)) for row in final_rows
        ),
        "sealed_holdouts_accessed": False,
        "primary_aggregate_eligible": False,
        "queue_wall_seconds": time.monotonic() - started,
    }
    aggregate["accepted_for_evidence_review"] = bool(
        aggregate["canonical_terminal_jobs"] == 16
        and aggregate["known_openrouter_cost_usd"] <= aggregate["openrouter_cost_cap_usd"]
        and aggregate["wp013_observed_heavy_total"] <= aggregate["wp013_heavy_cap"]
        and aggregate["all_roots_unchanged"]
        and aggregate["all_o_hashes_equal_by_root"]
        and aggregate["precommit_qor_feedback_count"] == 0
        and aggregate["precommit_mutation_count"] == 0
        and aggregate["postcommit_resubmission_count"] == 0
    )
    aggregate["aggregate_sha256"] = digest(aggregate)
    write_atomic(args.out / "e0-aggregate.json", aggregate)
    print(json.dumps({key: aggregate[key] for key in ("accepted_for_evidence_review", "planned_jobs", "canonical_terminal_jobs", "terminal_counts", "provider_requests", "known_openrouter_cost_usd", "actual_heavy_calls_e0", "queue_wall_seconds")}))
    return 0 if aggregate["accepted_for_evidence_review"] else 2


def audit(args: argparse.Namespace) -> int:
    aggregate = read_object(args.run / "e0-aggregate.json")
    claimed = aggregate.pop("aggregate_sha256")
    errors: list[str] = []
    if digest(aggregate) != claimed:
        errors.append("aggregate_hash_invalid")
    aggregate["aggregate_sha256"] = claimed
    jobs = aggregate.get("jobs", [])
    if len(jobs) != 16:
        errors.append("job_count_not_16")
    if sum(row.get("canonical_terminal") in TERMINALS for row in jobs) != 16:
        errors.append("canonical_terminal_count_not_16")
    if aggregate.get("known_openrouter_cost_usd", 4.0) > 3.0:
        errors.append("openrouter_cost_cap_exceeded")
    if aggregate.get("wp013_observed_heavy_total", 281) > 280:
        errors.append("heavy_call_cap_exceeded")
    if not aggregate.get("all_roots_unchanged"):
        errors.append("root_integrity_failed")
    if not aggregate.get("all_o_hashes_equal_by_root"):
        errors.append("o_hash_equality_failed")
    for key in ("precommit_qor_feedback_count", "precommit_mutation_count", "postcommit_resubmission_count"):
        if aggregate.get(key) != 0:
            errors.append(key + "_nonzero")
    for row in jobs:
        if row["level"] in LLM_LEVELS:
            expected = OPENROUTER_E1_PROFILES[row["model"]]
            for turn in row.get("turns", []):
                if turn.get("returned_model") is not None and turn["returned_model"] != row["model"]:
                    errors.append("returned_model_mismatch:" + row["job_id"])
                if turn.get("returned_upstream") is not None and turn["returned_upstream"] != expected["upstream_provider"]:
                    errors.append("returned_upstream_mismatch:" + row["job_id"])
                if turn.get("physical_requests", 0) not in {0, 1}:
                    errors.append("physical_request_cardinality_invalid:" + row["job_id"])
        evidence = row.get("planner_evidence")
        if evidence and evidence.get("valid_commits", 0) > 1:
            errors.append("more_than_one_commit:" + row["job_id"])
    audit_value = {
        "schema_version": "dof.wp013-e0-independent-audit.v1",
        "aggregate_sha256": claimed,
        "accepted": not errors,
        "errors": sorted(set(errors)),
        "checks": {
            "jobs": len(jobs),
            "canonical_terminals": sum(row.get("canonical_terminal") in TERMINALS for row in jobs),
            "cost_usd": aggregate.get("known_openrouter_cost_usd"),
            "heavy_total": aggregate.get("wp013_observed_heavy_total"),
            "roots_unchanged": aggregate.get("all_roots_unchanged"),
            "o_hashes_equal": aggregate.get("all_o_hashes_equal_by_root"),
        },
    }
    audit_value["audit_sha256"] = digest(audit_value)
    write_atomic(args.output, audit_value)
    print(json.dumps({"accepted": audit_value["accepted"], "errors": audit_value["errors"]}))
    return 0 if audit_value["accepted"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    commands = value.add_subparsers(dest="command", required=True)
    c = commands.add_parser("capture")
    c.add_argument("--plan", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--python", type=Path, required=True)
    c.add_argument("--vivado", type=Path, required=True)
    c.add_argument("--max-parallel", type=int, default=2)
    f = commands.add_parser("freeze")
    f.add_argument("--plan", type=Path, required=True)
    f.add_argument("--capture", type=Path, required=True)
    f.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("preflight")
    p.add_argument("--output", type=Path, required=True)
    r = commands.add_parser("run")
    r.add_argument("--bundle", type=Path, required=True)
    r.add_argument("--capture", type=Path, required=True)
    r.add_argument("--preflight", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--python", type=Path, required=True)
    r.add_argument("--vivado", type=Path, required=True)
    r.add_argument("--max-parallel", type=int, default=8)
    a = commands.add_parser("audit")
    a.add_argument("--run", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    if args.command == "capture":
        return asyncio.run(capture(args))
    if args.command == "freeze":
        return freeze(args)
    if args.command == "preflight":
        return asyncio.run(preflight(args))
    if args.command == "run":
        return asyncio.run(run(args))
    return audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
