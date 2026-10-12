#!/usr/bin/env python3
"""Freeze, admit, execute, and audit the bounded WP-013A development E0 v4.

This controller is deliberately development-only.  It binds two real roots,
one immutable observation per root, the exact OpenRouter model/route profiles,
the one-COMMIT planner, and one canonical terminal for each of the 16 jobs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass, replace
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

from optimizer_pipeline.backends.xilinx.legacy import FPGAOptimizerTest, parse_timing_summary_static  # noqa: E402
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
from optimizer_pipeline.dof.scientific_evidence import boundary_evidence  # noqa: E402
from optimizer_pipeline.dof.hardblock_compatibility import (  # noqa: E402
    build_relation, validate_allocation, validate_relation,
)
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry  # noqa: E402
from optimizer_pipeline.dof.single_submission.language import (  # noqa: E402
    CompileContext,
    CompiledProgram,
)
from optimizer_pipeline.dof.single_submission.observation import (  # noqa: E402
    build_common_observation,
)
from optimizer_pipeline.dof.single_submission.observation_v2 import (  # noqa: E402
    CommonQuerySurfaceV2,
    audit_query_semantics,
)
from optimizer_pipeline.dof.single_submission.planner import (  # noqa: E402
    PlannerError,
    SystemBinding,
)
from optimizer_pipeline.dof.single_submission.planner_v2 import (  # noqa: E402
    SingleSubmissionSessionV2,
    result_public,
)
from optimizer_pipeline.dof.single_submission.contract_v2 import (  # noqa: E402
    MAX_INPUT_BYTES,
    M_ACTION_KEYS,
    M_HEAVY_CALLS,
    audit_contract_completeness,
    compile_candidate_v2,
    contract_for_level,
    contract_hash,
    macro_from_candidate_v2,
)
from optimizer_pipeline.dof.single_submission.specs import (  # noqa: E402
    canonical_json_bytes,
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
    allocation_effect_evidence,
    b64,
    relocation_effect_count,
    route_status_clean,
)


SCHEMA = "dof.wp013a-development-e0-v4.v1"
CAPTURE_SCHEMA = "dof.wp013a-e0-capture.v1"
CAPTURE_JOB_SCHEMA = "dof.wp013a-observation-capture-job.v1"
CAPTURE_JOB_PREFIX = "wp013a-e0-observe"
HANDLE_NAMESPACE = "wp013a-e0-handles-v2"
TRIAL_ID = "wp013a-e0-v4"
QUERY_EVIDENCE_TAG = "wp013a-frozen-read-only-capture"
HARDBLOCK_COMPATIBILITY_SCAN_DIR: Path | None = None
REQUIRE_EXACT_HARDBLOCK_COMPATIBILITY = False
EXPECTED_OPENROUTER_COST_CAP_USD = 1.0
EXPECTED_HEAVY_CALL_CAP = 64
EXPECTED_JOB_COUNT = 16
MODELS = ("openai/gpt-5.6-luna", "anthropic/claude-sonnet-5")


def _provider_runtime_profile(model: str) -> dict[str, Any]:
    """Return the default OpenRouter runtime identity for one model.

    Later protocols may replace this seam before planning.  Keeping provider
    identity and transport construction injectable prevents a multi-provider
    experiment from pretending that direct requests traversed OpenRouter.
    """

    profile = OPENROUTER_E1_PROFILES[model]
    return {
        "provider": "openrouter",
        "route": profile["endpoint_tag"],
        "expected_returned_model": model,
        "expected_returned_upstream": profile["upstream_provider"],
    }


def _provider_transport(request: ProviderRequestBinding, _model: str, key: str):
    return OpenRouterChatCompletionsTransport(
        api_key=key, expected_outbound_binding=build_openrouter_outbound(request)
    )
LLM_LEVELS = ("L2", "L3", "L4")
MACRO_OBSERVED_HEAVY_CALLS = {
    key.removeprefix("macro."): value for key, value in M_HEAVY_CALLS.items()
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
        "schema_version": CAPTURE_JOB_SCHEMA,
        "job": {"benchmark": root_id, "job_id": f"{CAPTURE_JOB_PREFIX}-{root_id}"},
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
            for mode in ("contract-probe",):
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
        "schema_version": CAPTURE_SCHEMA,
        "roots": rows,
        "accepted": len(rows) == 2 and all(row["accepted"] for row in rows),
    }
    aggregate["capture_sha256"] = digest(aggregate)
    write_atomic(args.out / "capture-aggregate.json", aggregate)
    print(json.dumps({"accepted": aggregate["accepted"], "roots": len(rows)}))
    return 0 if aggregate["accepted"] else 2


def _registry_rows(
    capture_root: Path, root_hash: str, route_status: str
) -> tuple[
    TrialHandleRegistry,
    dict[str, list[dict[str, Any]]],
    dict[str, str | None],
    tuple[str, ...],
    tuple[int, int, int, int],
    dict[str, Any] | None,
]:
    secret = hashlib.sha256((HANDLE_NAMESPACE + ":" + root_hash).encode()).digest()
    registry = TrialHandleRegistry(
        trial_id=TRIAL_ID, root_sha256=root_hash, secret=secret
    )
    captured = read_object(
        capture_root / "contract-probe" / "private" / "resolved-objects.json"
    )
    rows: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "critical_path_details", "critical_object_locations",
            "high_fanout_net_details", "critical_hardblock_candidates",
            "placement_summary", "route_summary",
        )
    }
    missing: dict[str, str | None] = {name: None for name in rows}

    region_handles: list[str] = []
    bounds: list[list[int]] = []
    regions_by_identity: dict[str, str] = {}
    for item in captured.get("device_regions", []):
        match = re.fullmatch(r"(?:CLOCKREGION_)?X(\d+)Y(\d+)", item["identity"])
        if match is None:
            continue
        coordinate = [
            int(match.group(1)), int(match.group(1)),
            int(match.group(2)), int(match.group(2)),
        ]
        attrs = {"bounds": coordinate, "count": 1, "row_semantics": "observed_clock_region"}
        handle = registry.register(
            object_type="region", canonical_identity=item["identity"], attributes=attrs
        )
        regions_by_identity[item["identity"]] = handle
        region_handles.append(handle)
        bounds.append(coordinate)
        rows["placement_summary"].append(
            {"canonical_identity": item["identity"], "handle_type": "region", **attrs}
        )

    for item in captured.get("timing_paths", []):
        parts = item["attribute"].split("\x1f")
        if len(parts) != 4:
            raise RuntimeError("captured_timing_path_shape_invalid")
        slack, delay, startpoint, endpoint = parts
        start_handle = registry.register(
            object_type="pin", canonical_identity=startpoint,
            attributes={"row_semantics": "timing_path_pin"},
        )
        end_handle = registry.register(
            object_type="pin", canonical_identity=endpoint,
            attributes={"row_semantics": "timing_path_pin"},
        )
        attrs = {
            "path_rank": int(item["index"]) + 1,
            "slack_ns": float(slack),
            "delay_ns": float(delay),
            "startpoint_handle": start_handle,
            "endpoint_handle": end_handle,
            "row_semantics": "real_vivado_timing_path",
        }
        rows["critical_path_details"].append(
            {"canonical_identity": item["identity"], "handle_type": "path", **attrs}
        )

    for item in captured.get("critical_cell_locations", []):
        parts = item["attribute"].split("\x1f")
        if len(parts) != 3:
            raise RuntimeError("captured_critical_location_shape_invalid")
        resource, site, region = parts
        site_handle = registry.register(
            object_type="location_site",
            canonical_identity=site or "UNPLACED:" + item["identity"],
            attributes={"site_class": "observed_location"},
        )
        region_handle = regions_by_identity.get(region)
        if region_handle is None:
            region_handle = registry.register(
                object_type="location_region",
                canonical_identity=region or "UNKNOWN_REGION:" + item["identity"],
                attributes={"row_semantics": "location_region"},
            )
        attrs = {
            "path_member": True,
            "path_rank": int(item["index"]) + 1,
            "resource_type": resource,
            "location_status": "PLACED" if site else "UNPLACED",
            "location_site_handle": site_handle,
            "location_region_handle": region_handle,
        }
        rows["critical_object_locations"].append(
            {"canonical_identity": item["identity"], "handle_type": "cell", **attrs}
        )

    for item in captured.get("high_fanout_nets", []):
        attrs = {"fanout": int(item["attribute"]), "row_semantics": "nonclock_net"}
        rows["high_fanout_net_details"].append(
            {"canonical_identity": item["identity"], "handle_type": "net", **attrs}
        )

    hardblock_relation = None
    exact_capture = None
    if HARDBLOCK_COMPATIBILITY_SCAN_DIR is not None:
        scan = HARDBLOCK_COMPATIBILITY_SCAN_DIR
        aggregate = read_object(scan / "aggregate.json")
        matching_rows = [
            row for row in aggregate.get("rows", ())
            if isinstance(row, Mapping) and row.get("root_sha256") == root_hash
        ]
        if len(matching_rows) == 1:
            root_id = str(matching_rows[0]["root_id"])
            raw = read_object(scan / "probes" / root_id / "private" / "resolved-objects.json")
            metadata = read_object(scan / "probes" / root_id / "private" / "hardblock-compatibility-queries.json")
            exact_capture = (raw, metadata)
        elif REQUIRE_EXACT_HARDBLOCK_COMPATIBILITY:
            exact_capture = ({"critical_hardblocks": []}, {"targets": []})

    if exact_capture is not None:
        raw, metadata = exact_capture
        hardblocks = raw.get("critical_hardblocks", [])
        meta_by_index = {
            int(item["target_index"]): item for item in metadata.get("targets", ())
            if isinstance(item, Mapping)
        }
        if len(meta_by_index) != len(hardblocks):
            raise RuntimeError("wp015b_exact_target_query_metadata_mismatch")
        site_rows_by_index: dict[int, list[dict[str, Any]]] = {}
        site_attributes: dict[str, dict[str, Any]] = {}
        target_resources: dict[int, str] = {}
        target_site_query_hashes: dict[int, str] = {}
        for index, item in enumerate(hardblocks):
            observed = str(item["attribute"]).upper()
            resource = (
                "DSP" if "DSP" in observed else "URAM" if "URAM" in observed
                else "BRAM" if "RAMB" in observed else "ANY"
            )
            target_resources[index] = resource
            meta = meta_by_index[index]
            site_rows = raw.get(str(meta["evidence_key"]), [])
            if not isinstance(site_rows, list):
                raise RuntimeError("wp015b_exact_site_rows_invalid")
            observed_query_hash = digest(site_rows)
            if observed_query_hash != meta.get("query_result_sha256"):
                raise RuntimeError("wp015b_exact_site_query_hash_mismatch")
            target_site_query_hashes[index] = observed_query_hash
            site_rows_by_index[index] = site_rows
            for site in site_rows:
                identity = str(site["identity"])
                attrs = site_attributes.setdefault(identity, {
                    "site_class": str(site["attribute"]),
                    "compatible_resource_types": [],
                })
                if attrs["site_class"] != str(site["attribute"]):
                    raise RuntimeError("wp015b_site_type_changed_within_capture")
                if resource not in attrs["compatible_resource_types"]:
                    attrs["compatible_resource_types"].append(resource)
        sites_by_identity = {
            identity: registry.register(
                object_type="site", canonical_identity=identity,
                attributes={
                    "site_class": attrs["site_class"],
                    "compatible_resource_types": sorted(attrs["compatible_resource_types"]),
                },
            )
            for identity, attrs in sorted(site_attributes.items())
        }
        target_rows = []
        target_query_sha = digest(hardblocks)
        query_hashes_by_handle: dict[str, str] = {}
        for index, item in enumerate(hardblocks):
            compatible = [sites_by_identity[str(site["identity"])] for site in site_rows_by_index[index]]
            attrs = {
                "path_member": True,
                "path_rank": int(item["index"]) + 1,
                "resource_type": target_resources[index],
                "compatible_site_handles": compatible,
            }
            handle = registry.register(
                object_type="hardblock", canonical_identity=str(item["identity"]),
                attributes=attrs,
            )
            rows["critical_hardblock_candidates"].append(
                {"canonical_identity": item["identity"], "handle_type": "hardblock", **attrs}
            )
            target_rows.append({
                "handle": handle,
                "target_identity_sha256": hashlib.sha256(str(item["identity"]).encode()).hexdigest(),
                "resource_type": target_resources[index],
                "compatible_site_handles": compatible,
            })
            query_hashes_by_handle[handle] = target_site_query_hashes[index]
        hardblock_relation = build_relation(
            root_sha256=root_hash, stage="ROUTED", target_rows=target_rows,
            target_query_sha256=target_query_sha,
            target_site_query_sha256=query_hashes_by_handle,
        )
        validate_relation(hardblock_relation)
        registry.register(
            object_type="compatibility",
            canonical_identity="wp015b-relation:" + hardblock_relation["compatibility_relation_sha256"],
            attributes={"relation": hardblock_relation}, source_tool="compiler",
        )
    else:
        hardblocks = captured.get("critical_hardblocks", [])
        resource = "ANY"
        if hardblocks:
            observed = hardblocks[0]["attribute"].upper()
            resource = (
                "DSP" if "DSP" in observed else "URAM" if "URAM" in observed
                else "BRAM" if "RAMB" in observed else "ANY"
            )
        site_handles = []
        for item in captured.get("compatible_sites", []):
            attrs = {
                "site_class": item["attribute"],
                "compatible_resource_types": [resource],
            }
            site_handles.append(registry.register(
                object_type="site", canonical_identity=item["identity"], attributes=attrs,
            ))
        for item in hardblocks:
            observed = item["attribute"].upper()
            item_resource = (
                "DSP" if "DSP" in observed else "URAM" if "URAM" in observed
                else "BRAM" if "RAMB" in observed else "ANY"
            )
            attrs = {
                "path_member": True, "path_rank": int(item["index"]) + 1,
                "resource_type": item_resource,
                "compatible_site_handles": site_handles if item is hardblocks[0] else [],
            }
            rows["critical_hardblock_candidates"].append(
                {"canonical_identity": item["identity"], "handle_type": "hardblock", **attrs}
            )

    rows["route_summary"].append(
        {
            "canonical_identity": "route-summary:" + root_hash,
            "handle_type": "net",
            "route_status": route_status,
            "count": 1,
            "row_semantics": "root_read_only_route_summary",
        }
    )
    policies = {
        "critical_path_details": "no_timing_paths_returned",
        "critical_object_locations": "no_critical_cells_returned",
        "high_fanout_net_details": "no_nonclock_nets_returned",
        "critical_hardblock_candidates": "no_critical_hardblocks_returned",
        "placement_summary": "no_clock_regions_returned",
        "route_summary": "route_evidence_missing",
    }
    for name, values in rows.items():
        if not values:
            missing[name] = policies[name]

    geometry = (
        (0, max(item[1] for item in bounds), 0, max(item[3] for item in bounds))
        if bounds else (0, 0, 0, 0)
    )
    return registry, rows, missing, tuple(region_handles), geometry, hardblock_relation


def _common_observation(plan_root: Mapping[str, Any], capture_root: Path) -> tuple[Any, dict[str, Any]]:
    real = read_object(capture_root / "vivado-observation" / "real-observation.json")
    source = real["source"]
    measurements = source["state"]["measurements"]
    timing = measurements["timing"]
    period = float(plan_root["target_period_ns"])
    wns = timing.get("wns_ns")
    fmax = 1000.0 / (period - float(wns)) if isinstance(wns, (int, float)) and period - float(wns) > 0 else None
    route_status = str(measurements["route"]["status"])
    registry, rows, missing_reasons, regions, geometry_bounds, hardblock_relation = _registry_rows(
        capture_root, plan_root["sha256"], route_status
    )
    observation = build_common_observation(
        root_pseudonym="root-" + plan_root["sha256"][:16],
        target_clock={"clock_period_ns": period, "evidence": plan_root["target_period_evidence"]},
        timing={"fmax_mhz": fmax, "wns_ns": wns, "tns_ns": timing.get("tns_ns"), "failing_endpoint_count": timing.get("failing_endpoints")},
        utilization=measurements["utilization"],
        implementation={"placement_state": "PLACED", "route_state": "ROUTED", "route_validity": measurements["route"]["status"]},
        constraints={"pblock_count": None, "constrained_cell_count": None, "missing_reason": "read_only_probe_did_not_report_constraints"},
        geometry={"device_class": "development-device-pseudonym", "clock_region_bounds": list(geometry_bounds), "observed_region_count": len(regions)},
        congestion={"coarse_class": "unknown", "hotspot_count": None, "missing_reason": "read_only_probe_did_not_report_congestion"},
        critical_paths=[
            {
                "path_rank": row["path_rank"],
                "slack_ns": row["slack_ns"],
                "delay_ns": row["delay_ns"],
                "status": "object_details_available_via_Q",
            }
            for row in rows["critical_path_details"][:8]
        ],
        budget={"planning_turns": 4, "queries": 8, "feedback_rounds": 3, "heavy_calls": 4, "light_mutations": 8, "wall_time_seconds": 28800, "usd": EXPECTED_OPENROUTER_COST_CAP_USD},
    )
    private = {
        "registry": registry,
        "rows": rows,
        "missing_reasons": missing_reasons,
        "predefined_regions": regions,
        "geometry_bounds": geometry_bounds,
        "hardblock_compatibility_relation": hardblock_relation,
        "real_observation_sha256": real["observation_sha256"],
        "query_semantic_audit": audit_query_semantics(
            rows, missing_reasons, require_e0_paths=True
        ),
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
    contract_audit: dict[str, Any] | None = None
    for root in plan["roots"]:
        path = Path(root["root_path"]).resolve(strict=True)
        if file_digest(path) != root["sha256"]:
            raise SystemExit("root_hash_mismatch")
        observation, private = _common_observation(root, args.capture / root["root_id"])
        if not private["query_semantic_audit"]["accepted"]:
            raise SystemExit("query_semantic_audit_failed:" + root["root_id"])
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
            "query_semantic_audit": private["query_semantic_audit"],
        }
        if contract_audit is None:
            contract_audit = audit_contract_completeness(
                CompileContext(
                    private["registry"], "ROUTED",
                    private["predefined_regions"], private["geometry_bounds"],
                )
            )
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
        "protocol_freeze_sha256": file_digest(REPO / plan["protocol_freeze_path"]),
        "model_contract_hashes": {level: contract_hash(level) for level in LLM_LEVELS},
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
        "l1_heuristic_path": plan["l1_heuristic_path"],
        "macro_manifest_sha256": file_digest(REPO / plan["macro_manifest_path"]),
        "trace_reconciliation_sha256": file_digest(REPO / plan["trace_reconciliation_path"]),
        "contract_completeness_audit": contract_audit,
        "primary_aggregate_eligible": False,
        "sealed_holdouts_accessed": False,
    }
    if (
        len(jobs) != EXPECTED_JOB_COUNT
        or core["guards"]["implementation_heavy_call_cap"] != EXPECTED_HEAVY_CALL_CAP
        or core["guards"]["openrouter_cost_cap_usd"] != EXPECTED_OPENROUTER_COST_CAP_USD
        or core["guards"]["max_parallel_weekday"] != 8
        or not core["contract_completeness_audit"]["accepted"]
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
        "schema_version": "dof.wp013a-openrouter-preflight.v1",
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
        "cost_cap_usd": 1.0,
    }
    payload["accepted"] = bool(
        payload["exact_models_available"] and payload["exact_endpoints_available"]
    )
    payload["preflight_sha256"] = digest(payload)
    write_atomic(args.output, payload)
    print(json.dumps({"accepted": payload["accepted"], "models": payload["models"]}))
    return 0 if payload["accepted"] else 2


def _language_contract(
    level: str, admitted_action_keys: tuple[str, ...] | None = None
) -> dict[str, Any]:
    if admitted_action_keys is None:
        return contract_for_level(level)
    # v2 contracts do not accept a mask; representation-only later epochs do.
    try:
        return contract_for_level(level, admitted_action_keys=admitted_action_keys)
    except TypeError:
        return contract_for_level(level)


def _prompt(level: str) -> bytes:
    return (
        "Return exactly one JSON object and no prose or markdown. Follow the exact closed schemas in language_contract. "
        "QUERY is {\"kind\":\"QUERY\",\"queries\":[{\"name\":QUERY_NAME,\"top_k\":INTEGER}]}. "
        "Candidate messages are {\"kind\":\"DRAFT\"|\"VALIDATE\"|\"COMMIT\",\"candidate\":CANDIDATE}. "
        "Terminal messages are {\"kind\":\"STOP\"} or {\"kind\":\"NO_VALID_SUBMISSION\"}. "
        "On final_submission_turn only use a kind listed in allowed_kinds_this_turn. "
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
    phase: str = "e0",
) -> ProviderRequestBinding:
    identity = f"{job['job_id']}:{turn}"
    if phase not in {"e0", "provider-admission"}:
        raise ValueError("provider_binding_phase_invalid")
    return ProviderRequestBinding(
        run_id=f"wp013a-{phase}-" + digest(identity)[:24],
        spec_hash=digest({"protocol": SCHEMA, "job": job}),
        experiment_id=(
            "wp013a-development-e0-v4"
            if phase == "e0" else "wp013a-provider-contract-admission-v1"
        ),
        condition_id="condition-" + digest(job["job_id"])[:20],
        repeat_id="replacement-development-e0-1",
        decision_id="decision-" + digest(job["job_id"])[:20],
        context_bytes=context,
        prompt_bytes=prompt,
        prompt_version="dof.wp013a-one-submission-prompt.v2",
        action_interface_version="dof.single-submission.planner-message.v2",
        action_interface_hash=digest("dof.single-submission.planner-message.v2"),
        context_interface_version="dof.single-submission.planning-context.v2",
        context_interface_hash=digest(context),
        catalogue_version=f"dof.wp013a.{job['level'].lower()}.v2",
        catalogue_hash=catalogue_hash,
        capability_hash=catalogue_hash,
        mask_evidence_hash=digest("development-roots-only-no-holdout"),
        provider_role=(
            "wp013a_single_submission_planner"
            if phase == "e0" else "wp013a_provider_contract_admission"
        ),
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


def _synthetic_admission_observation(session_id: str):
    root = hashlib.sha256(("wp013a-provider-admission:" + session_id).encode()).hexdigest()
    observation = build_common_observation(
        root_pseudonym="root-" + root[:16],
        target_clock={"clock_period_ns": 4.0, "evidence": "synthetic_contract_only"},
        timing={"fmax_mhz": 250.0, "wns_ns": 0.0, "tns_ns": 0.0, "failing_endpoint_count": 0},
        utilization={"lut": 100, "ff": 100, "dsp": 1, "bram": 1, "uram": 0},
        implementation={"placement_state": "PLACED", "route_state": "ROUTED", "route_validity": "VALID"},
        constraints={"pblock_count": 0, "constrained_cell_count": 0},
        geometry={"device_class": "synthetic-development-safe", "clock_region_bounds": [0, 1, 0, 1]},
        congestion={"coarse_class": "unknown", "hotspot_count": 0},
        critical_paths=[],
        budget={"planning_turns": 4, "queries": 8, "feedback_rounds": 3, "heavy_calls": 4, "light_mutations": 8},
    )
    registry = TrialHandleRegistry(
        trial_id=session_id, root_sha256=root,
        secret=hashlib.sha256((session_id + ":secret").encode()).digest(),
    )
    return root, observation, registry


async def provider_admission(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    plan = read_object(args.plan)
    preflight_value = read_object(args.preflight)
    if not preflight_value.get("accepted"):
        raise SystemExit("provider_preflight_not_accepted")
    assert_clean_repository()
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True)
    reserves = {row["model"]: float(row["per_request_reserve_usd"]) for row in plan["models"]}
    cost = SharedCost(float(plan["guards"]["openrouter_cost_cap_usd"]))

    async def one(level: str, model: str) -> dict[str, Any]:
        target_language = {"L2": "M", "L3": "P", "L4": "G"}[level]
        session_id = f"provider-admission__{level.lower()}__{model.replace('/', '_')}"
        root, observation, registry = _synthetic_admission_observation(session_id)
        contract = contract_for_level(level)
        catalogue_hash = digest(contract)
        binding = SystemBinding.create(
            observation_hash=observation.sha256,
            root_binding=root,
            catalogue_version=f"dof.wp013a.{level.lower()}.v2",
            catalogue_hash=catalogue_hash,
            provider="openrouter",
            model=model,
            route=OPENROUTER_E1_PROFILES[model]["endpoint_tag"],
            session_nonce=session_id,
        )

        def no_query(name: str, top_k: int):
            return (), ("synthetic-provider-admission",), 0.0, "provider_admission_has_no_real_q_rows"

        session = SingleSubmissionSessionV2(
            level, observation, CommonQuerySurfaceV2(registry),
            CompileContext(registry, "ROUTED"), binding, no_query,
        )
        context = canonical_json_bytes(
            {
                "schema_version": "dof.wp013a-provider-admission-context.v1",
                "technical_non_scientific": True,
                "target_language": target_language,
                "language_contract": contract,
                **session.public_status(),
            }
        )
        prompt = _prompt(level) + (
            f" Provider-only syntax admission: return one COMMIT in language {target_language}. "
            "Use the syntax-only CALL_MACRO template with any documented action_key. "
            "This request does not choose or run a backend action."
        ).encode()
        total_input = len(context) + len(prompt)
        if total_input > MAX_INPUT_BYTES:
            return {
                "session_id": session_id, "level": level, "model": model,
                "accepted": False, "error": "provider_input_byte_bound_exceeded",
                "total_input_size_bytes": total_input, "physical_requests": 0, "cost_usd": 0.0,
            }
        reserve = reserves[model]
        if not await cost.acquire(reserve):
            return {
                "session_id": session_id, "level": level, "model": model,
                "accepted": False, "error": "cost_guard", "physical_requests": 0, "cost_usd": 0.0,
            }
        job = {"job_id": session_id, "level": level}
        request = _binding(
            job=job, model=model, turn=0, context=context, prompt=prompt,
            catalogue_hash=catalogue_hash, reserve=reserve, phase="provider-admission",
        )
        transport = OpenRouterChatCompletionsTransport(
            api_key=key, expected_outbound_binding=build_openrouter_outbound(request)
        )
        response = None
        error = None
        try:
            response = await transport.send(request, 0)
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
        actual = (
            float(response.cost_usd or 0.0) if response is not None
            else float((transport.reconciled_accounting or {}).get("cost_usd", 0.0))
        )
        await cost.settle(reserve, actual)
        exchange = transport.last_exchange or {}
        write_atomic(args.out / "private" / session_id / "exchange.json", exchange, private=True)
        planner_result = None
        message_sha256 = None
        if response is not None and error is None:
            try:
                message = strict_json_object(response.content_bytes)
                message_sha256 = digest(response.content_bytes)
                planner_result = session.accept(message)
            except (ValueError, PlannerError):
                planner_result = session.accept({})
        returned_model = response.returned_model_id if response else None
        returned_upstream = response.returned_model_revision if response else None
        exact_identity = bool(
            returned_model == model
            and returned_upstream == OPENROUTER_E1_PROFILES[model]["upstream_provider"]
            and exchange.get("route", {}).get("fallback_used") is not True
        )
        accepted = bool(
            planner_result is not None
            and planner_result.status == "COMMITTED"
            and planner_result.compiled is not None
            and planner_result.compiled.language == target_language
            and exact_identity
            and transport.physical_requests == 1
        )
        row = {
            "session_id": session_id,
            "level": level,
            "target_language": target_language,
            "model": model,
            "accepted": accepted,
            "error": error,
            "message_sha256": message_sha256,
            "planner_result": None if planner_result is None else _turn_public(planner_result),
            "returned_model": returned_model,
            "returned_upstream": returned_upstream,
            "route": exchange.get("route"),
            "physical_requests": transport.physical_requests,
            "retry_count": 0,
            "fallback_count": 0,
            "cost_usd": actual,
            "input_tokens": response.input_tokens if response else None,
            "output_tokens": response.output_tokens if response else None,
            "context_size_bytes": len(context),
            "prompt_size_bytes": len(prompt),
            "total_input_size_bytes": total_input,
            "contract_hash": catalogue_hash,
        }
        write_atomic(args.out / "sessions" / session_id / "result.json", row)
        return row

    rows = await asyncio.gather(
        *(one(level, model) for level in LLM_LEVELS for model in MODELS)
    )
    accepted_by_level = {
        level: sum(row["accepted"] for row in rows if row["level"] == level)
        for level in LLM_LEVELS
    }
    feedback_rows = [
        feedback
        for row in rows
        for feedback in (row.get("planner_result") or {}).get("feedback", [])
    ]
    undocumented = [
        row for row in feedback_rows if not row.get("expected_shape_or_schema_ref")
    ]
    aggregate = {
        "schema_version": "dof.wp013a-provider-contract-admission.v1",
        "technical_non_scientific": True,
        "sessions": rows,
        "session_count": len(rows),
        "accepted_by_level": accepted_by_level,
        "physical_requests": sum(row["physical_requests"] for row in rows),
        "known_cost_usd": cost.spent,
        "cost_cap_usd": cost.cap,
        "retry_count": 0,
        "fallback_count": 0,
        "undocumented_feedback_count": len(undocumented),
        "accepted": bool(
            len(rows) == 6
            and all(count >= 1 for count in accepted_by_level.values())
            and sum(row["physical_requests"] for row in rows) == 6
            and cost.spent <= cost.cap
            and not undocumented
        ),
    }
    aggregate["aggregate_sha256"] = digest(aggregate)
    write_atomic(args.out / "provider-admission.json", aggregate)
    print(json.dumps({key: aggregate[key] for key in ("accepted", "accepted_by_level", "physical_requests", "known_cost_usd")}))
    return 0 if aggregate["accepted"] else 2


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
        missing = private["missing_reasons"].get(name)
        return rows, (QUERY_EVIDENCE_TAG,), 0.0, missing
    return provide


def _planner_context(
    *,
    observation: Any,
    session: SingleSubmissionSessionV2,
    contract: Mapping[str, Any],
    last_result: Mapping[str, Any] | None,
) -> bytes:
    value = {
        "schema_version": "dof.single-submission.planning-context.v2",
        "objective": "Preserve mandatory validity and maximise final valid Fmax under one committed intervention.",
        "common_observation": dict(observation.payload),
        "common_observation_sha256": observation.sha256,
        "common_query_catalogue": session.queries.catalogue,
        "language_contract": contract,
        **session.public_status(),
        "previous_result": last_result,
        "qor_feedback_present": False,
    }
    return canonical_json_bytes(value)


def _turn_public(result: Any) -> dict[str, Any]:
    return result_public(result)


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
        heuristic = read_object(
            REPO / bundle.get("l1_heuristic_path", "configs/wp013a/l1-heuristic-v2-overlay.json")
        )
        admitted = tuple(bundle["roots"][root_id].get("admitted_action_keys", M_ACTION_KEYS))
        macro = next(
            item for item in heuristic["global_ranking"]
            if "macro." + item in admitted
        )
        return ({**job, "canonical_terminal": "PLANNED_COMMIT", "selected_language": "M", "selected_macro": macro, "planning_wall_seconds": time.monotonic() - started, "provider_requests": 0, "cost_usd": 0.0, "l1_heuristic_sha256": heuristic["overlay_sha256"], "l1_public_name": heuristic.get("public_name", "Fixed Global Macro Prior"), "admitted_action_keys": list(admitted)}, None, None, {"language": "M", "action_key": "macro." + macro})

    observation, private = _load_trial(bundle, capture_dir, root_id)
    queries = CommonQuerySurfaceV2(private["registry"])
    admitted_action_keys = tuple(bundle["roots"][root_id].get("admitted_action_keys", M_ACTION_KEYS))
    contract = _language_contract(level, admitted_action_keys)
    catalogue_hash = digest(contract)
    provider_runtime = _provider_runtime_profile(model)
    binding = SystemBinding.create(
        observation_hash=observation.sha256,
        root_binding=bundle["roots"][root_id]["sha256"],
        catalogue_version=f"dof.wp013a.{level.lower()}.v2",
        catalogue_hash=catalogue_hash,
        provider=provider_runtime["provider"],
        model=model,
        route=provider_runtime["route"],
        session_nonce=job["job_id"],
    )
    session = SingleSubmissionSessionV2(
        level=level,
        observation=observation,
        queries=queries,
        compile_context=CompileContext(
            private["registry"], "ROUTED", private["predefined_regions"], private["geometry_bounds"],
            tuple(key.removeprefix("macro.") for key in admitted_action_keys),
            fanout_admitted=bool(private.get("fanout_admitted", False)),
            hardblock_compatibility_relation=private.get("hardblock_compatibility_relation"),
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
        if len(context) + len(prompt) > MAX_INPUT_BYTES:
            await cost.settle(reserve, 0.0)
            terminal_override = "NO_VALID_SUBMISSION"
            turns.append({
                "turn": turn + 1,
                "error": "provider_input_byte_bound_exceeded",
                "failure_class": "CONTEXT_LIMIT_AFTER_MODEL_TURN",
                "input_bytes": len(context) + len(prompt),
                "physical_requests": 0,
                "cost_usd": 0.0,
            })
            break
        request = _binding(job=job, model=model, turn=turn, context=context, prompt=prompt, catalogue_hash=catalogue_hash, reserve=reserve)
        transport = _provider_transport(request, model, key)
        response = None
        error = None
        try:
            response = await transport.send(request, 0)
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
        actual = float(response.cost_usd or 0.0) if response is not None else float(
            (getattr(transport, "reconciled_accounting", None) or {}).get("cost_usd", 0.0)
        )
        await cost.settle(reserve, actual)
        exchange = transport.last_exchange or {}
        write_atomic(session_dir / "private" / f"turn-{turn + 1}-exchange.json", exchange, private=True)
        row: dict[str, Any] = {
            "turn": turn + 1,
            "request_contract_hash": request.request_contract_hash,
            "prompt_hash": request.prompt_hash,
            "context_hash": request.context_hash,
            "context_size_bytes": len(context),
            "prompt_size_bytes": len(prompt),
            "total_input_size_bytes": len(context) + len(prompt),
            "physical_requests": transport.physical_requests,
            "cost_usd": actual,
            "input_tokens": response.input_tokens if response else None,
            "output_tokens": response.output_tokens if response else None,
            "returned_model": response.returned_model_id if response else None,
            "returned_upstream": response.returned_model_revision if response else None,
            "route": exchange.get("route") or {
                "provider": exchange.get("canonical_provider"),
                "provider_route": exchange.get("provider_route"),
                "fallback_allowed": False,
                "attempt": 1,
            },
            "failure_class": exchange.get("failure_class"),
            "error": error,
        }
        if error is not None or response is None:
            turns.append(row)
            terminal_override = "MODEL_OR_ROUTE_DRIFT" if exchange.get("failure_class") == "MODEL_OR_ROUTE_DRIFT" else "PROVIDER_FAILURE"
            break
        if (
            response.returned_model_id != provider_runtime["expected_returned_model"]
            or response.returned_model_revision != provider_runtime["expected_returned_upstream"]
        ):
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
            try:
                invalid_result = session.accept({})
                row["planner_result"] = _turn_public(invalid_result)
            except PlannerError:
                row["planner_result"] = {
                    "status": "NO_VALID_SUBMISSION",
                    "feedback": [{"error_code": str(reason), "location": "$"}],
                }
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
        "provider_requests": sum(int(row["physical_requests"]) for row in turns),
        "cost_usd": sum(float(row["cost_usd"]) for row in turns),
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
    return macro_from_candidate_v2(candidate)


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
    output_dcp = Path(str(outer.get("output_dcp") or ""))
    result: dict[str, Any] = {
        "outer_status": outer.get("status"),
        "outer_exit_code": outer.get("exit_code"),
        "outer_duration_seconds": outer.get("duration_seconds"),
        "child_path": str(output_dcp) if output_dcp.is_file() else None,
        "child": {
            "sha256": file_digest(output_dcp), "size_bytes": output_dcp.stat().st_size,
        } if output_dcp.is_file() else None,
    }
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
        "vivado.wp014c_create_pblock", "vivado.wp014c_resize_pblock",
        "vivado.wp014c_assign_pblock", "vivado.wp014c_unassign_pblock",
        "vivado.wp014c_delete_pblock", "vivado.wp014c_relocate_hardblocks",
        "vivado.wp014c_replicate_net", "vivado.wp014c_route_net",
        "vivado.wp014c_place_cell", "vivado.wp014c_unplace_cell",
        "vivado.wp014c_swap_cells", "vivado.wp014c_set_cell_loc_fixed",
        "vivado.wp014c_set_net_dont_touch",
        "vivado.wp014c_set_pblock_exclude_placement",
        "vivado.wp014c_set_site_prohibit",
        "vivado.wp014h_batch_route_nets", "vivado.wp014h_apply_allocation",
        "rapidwright.optimize_fanout",
    }
    unknown = sorted({step.operation for step in compiled.host_steps} - supported)
    return not unknown, unknown


async def _execute_rapidwright_typed(
    *, compiled: CompiledProgram, registry: TrialHandleRegistry, root: Path,
    out: Path, python: Path, vivado: Path,
) -> dict[str, Any]:
    """Execute the one admitted RapidWright target mutation, then fixed Vivado steps."""

    indices = [index for index, step in enumerate(compiled.host_steps) if step.operation == "rapidwright.optimize_fanout"]
    if len(indices) != 1 or indices[0] != 0:
        return {"canonical_endpoint": "CAPABILITY_ACTION_REJECTION", "error": "rapidwright_step_shape_invalid", "actual_heavy_calls": 0, "actual_light_mutations": 0}
    step = compiled.host_steps[0]
    intermediate = out / "rapidwright" / "child.dcp"
    intermediate.parent.mkdir(parents=True, exist_ok=True)
    tester = FPGAOptimizerTest(debug=False, run_dir=out / "rapidwright" / "mcp")
    trace: list[dict[str, Any]] = []
    started = time.monotonic()
    root_before = file_digest(root)
    try:
        await tester.start_servers()
        calls = (
            ("initialize_rapidwright", {"jvm_max_memory": "8G"}, 120.0),
            ("read_checkpoint", {"dcp_path": str(root.resolve())}, 600.0),
            ("optimize_fanout", {"net_name": str(step.private_args["net"]), "split_factor": int(step.private_args["split_factor"])}, 300.0),
            ("write_checkpoint", {"dcp_path": str(intermediate), "overwrite": True}, 600.0),
        )
        optimization: dict[str, Any] | None = None
        for name, arguments, timeout in calls:
            call_started = time.monotonic()
            output = await tester.call_rapidwright_tool(name, arguments, timeout=timeout)
            trace.append({"operation": "rapidwright." + name, "arguments_sha256": digest(arguments), "duration_seconds": time.monotonic() - call_started, "output_sha256": digest(output.encode()), "output_size_bytes": len(output.encode())})
            parsed = json.loads(output)
            if not isinstance(parsed, dict) or parsed.get("error") or parsed.get("status") not in {"success", "already_initialized"}:
                raise RuntimeError(f"rapidwright_{name}_not_successful")
            if name == "optimize_fanout":
                optimization = parsed
        if not intermediate.is_file() or intermediate.stat().st_size == 0:
            raise RuntimeError("rapidwright_intermediate_checkpoint_missing")
        if not optimization or optimization.get("status") != "success" or optimization.get("error"):
            raise RuntimeError("rapidwright_optimize_fanout_not_successful")
    except Exception as exc:
        return {
            "canonical_endpoint": "TOOL_FAILURE", "error": f"rapidwright:{type(exc).__name__}:{exc}",
            "actual_heavy_calls": 0, "actual_light_mutations": len([row for row in trace if row["operation"] == "rapidwright.optimize_fanout"]),
            "rapidwright_trace": trace, "root_unchanged": file_digest(root) == root_before,
            "wall_seconds": time.monotonic() - started,
        }
    finally:
        try:
            await tester.cleanup()
        except Exception:
            pass

    remaining = tuple(compiled.host_steps[1:])
    remainder = replace(
        compiled,
        initial_stage="PLACED",
        host_steps=remaining,
        heavy_calls=sum(row.event_class == "IMPLEMENTATION_HEAVY" for row in remaining),
        light_mutations=sum(row.event_class == "MUTATION_LIGHT" for row in remaining),
    )
    result = await _execute_typed(
        compiled=remainder, registry=registry, root=intermediate, out=out / "vivado",
        python=python, vivado=vivado,
    )
    observed_new_nets = len(optimization.get("new_nets", [])) if optimization else 0
    result["rapidwright_trace"] = trace
    result["rapidwright_result"] = optimization
    result["rapidwright_intermediate"] = {"sha256": file_digest(intermediate), "size_bytes": intermediate.stat().st_size}
    result["actual_light_mutations"] = int(result.get("actual_light_mutations") or 0) + 1
    result["selected_count"] = int(result.get("selected_count") or 0) + 1
    result["transformed_count"] = int(result.get("transformed_count") or 0) + observed_new_nets
    result["root_unchanged"] = file_digest(root) == root_before
    result["wall_seconds"] = time.monotonic() - started
    return result


async def _execute_typed(
    *,
    compiled: CompiledProgram,
    registry: TrialHandleRegistry,
    root: Path,
    out: Path,
    python: Path,
    vivado: Path,
) -> dict[str, Any]:
    macro_indices = [
        index for index, step in enumerate(compiled.host_steps)
        if step.operation == "macro.execute"
    ]
    if macro_indices:
        # The frozen compiler permits selectors before CALL_MACRO, but a real
        # mutation before it cannot be replayed by the production Macro flow
        # without silently changing semantics.  Fail closed for that shape.
        if len(macro_indices) != 1 or macro_indices[0] != 0:
            return {
                "canonical_endpoint": "CAPABILITY_ACTION_REJECTION",
                "error": "embedded_macro_must_precede_mutating_host_steps",
                "actual_heavy_calls": 0,
                "actual_light_mutations": 0,
                "boundary_runtime_exercised": False,
            }
        macro_step = compiled.host_steps[0]
        flow = str(macro_step.private_args["flow_name"])
        macro_result = await _execute_macro(
            root=root, flow=flow, out=out / "macro", python=python,
        )
        macro_trace = {
            "source_construct": "CALL_MACRO",
            "action_key": macro_step.public_args["action_key"],
            "flow_name": flow,
            "runtime_exercised": bool(macro_result.get("actual_heavy_calls")),
            "canonical_endpoint": macro_result.get("canonical_endpoint"),
        }
        if macro_result.get("canonical_endpoint") != "VALID_COMPLETION":
            return macro_result | {
                "embedded_macro_trace": [macro_trace],
                "boundary_runtime_exercised": macro_trace["runtime_exercised"],
            }
        remaining = tuple(compiled.host_steps[1:])
        if not remaining:
            return macro_result | {
                "embedded_macro_trace": [macro_trace],
                "boundary_runtime_exercised": macro_trace["runtime_exercised"],
            }
        macro_child = Path(str(macro_result.get("child_path") or ""))
        if not macro_child.is_file():
            return macro_result | {
                "canonical_endpoint": "INVALID_ARTIFACT",
                "error": "embedded_macro_successor_missing",
                "embedded_macro_trace": [macro_trace],
                "boundary_runtime_exercised": macro_trace["runtime_exercised"],
            }
        remainder = replace(
            compiled,
            initial_stage="ROUTED",
            host_steps=remaining,
            heavy_calls=sum(step.heavy_cost for step in remaining),
            light_mutations=sum(step.light_cost for step in remaining),
        )
        suffix = await _execute_typed(
            compiled=remainder, registry=registry, root=macro_child,
            out=out / "suffix", python=python, vivado=vivado,
        )
        return suffix | {
            "actual_heavy_calls": int(macro_result.get("actual_heavy_calls") or 0)
            + int(suffix.get("actual_heavy_calls") or 0),
            "actual_light_mutations": int(macro_result.get("actual_light_mutations") or 0)
            + int(suffix.get("actual_light_mutations") or 0),
            "embedded_macro_trace": [macro_trace],
            "boundary_runtime_exercised": macro_trace["runtime_exercised"],
            "macro_execution": macro_result,
        }
    if any(step.operation == "rapidwright.optimize_fanout" for step in compiled.host_steps):
        return await _execute_rapidwright_typed(
            compiled=compiled, registry=registry, root=root, out=out,
            python=python, vivado=vivado,
        )
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
            trace_start = len(backend.trace)
            if op in {"vivado.create_pblock", "vivado.wp014c_create_pblock"}:
                region = registry.resolve(str(step.public_args["region"]), expected_type="region")
                bounds = region.attributes["bounds"]
                if re.fullmatch(r"(?:CLOCKREGION_)?X\d+Y\d+", region.canonical_identity):
                    coordinate = region.canonical_identity.removeprefix("CLOCKREGION_")
                    region_name = f"CLOCKREGION_{coordinate}:CLOCKREGION_{coordinate}"
                else:
                    region_name = f"CLOCKREGION_X{bounds[0]}Y{bounds[2]}:CLOCKREGION_X{bounds[1]}Y{bounds[3]}"
                pblock_handle = str(step.public_args.get("pblock") or next(obj.handle for obj in registry.objects(object_type="pblock") if obj.attributes.get("region_handle") == region.handle))
                pblock_id = f"wp014c_pb{index}" if op.startswith("vivado.wp014c") else f"wp013_e0_pb{index}"
                pblock_ids[pblock_handle] = pblock_id
                worker_op = "vivado.wp014c_create_pblock" if op.startswith("vivado.wp014c") else "vivado.wp013_create_pblock"
                await backend.call(worker_op, {"pblock_id": pblock_id, "region_name_b64": b64(region_name)})
                actual["transformed"] += 1
            elif op in {"vivado.assign_pblock", "vivado.wp014c_assign_pblock", "vivado.wp014c_unassign_pblock"}:
                targets = [registry.resolve(handle).canonical_identity for handle in step.public_args["targets"]]
                worker_op = {
                    "vivado.assign_pblock": "vivado.wp013_assign_pblock",
                    "vivado.wp014c_assign_pblock": "vivado.wp014c_assign_pblock",
                    "vivado.wp014c_unassign_pblock": "vivado.wp014c_unassign_pblock",
                }[op]
                await backend.call(worker_op, {"pblock_id": pblock_ids[str(step.public_args["pblock"])], "cell_names_b64": [b64(name) for name in targets]})
                actual["selected"] += len(targets); actual["transformed"] += len(targets)
            elif op == "vivado.wp014c_resize_pblock":
                region = registry.resolve(str(step.public_args["region"]), expected_type="region")
                bounds = region.attributes["bounds"]
                region_name = f"CLOCKREGION_X{bounds[0]}Y{bounds[2]}:CLOCKREGION_X{bounds[1]}Y{bounds[3]}"
                await backend.call("vivado.wp014c_resize_pblock", {"pblock_id": pblock_ids[str(step.public_args["pblock"])], "region_name_b64": b64(region_name), "mode": str(step.public_args["mode"])})
                actual["transformed"] += 1
            elif op in {"vivado.remove_pblock", "vivado.wp014c_delete_pblock"}:
                worker_op = "vivado.wp014c_delete_pblock" if op.startswith("vivado.wp014c") else "vivado.wp013_remove_pblock"
                await backend.call(worker_op, {"pblock_id": pblock_ids[str(step.public_args["pblock"])]})
                actual["transformed"] += 1
            elif op in {"vivado.relocate_hardblock", "vivado.wp014c_relocate_hardblocks"}:
                targets = [registry.resolve(handle, expected_type="hardblock").canonical_identity for handle in step.public_args["targets"]]
                site = registry.resolve(str(step.public_args["destination"]), expected_type="site").canonical_identity
                worker_op = "vivado.wp014c_relocate_hardblocks" if op.startswith("vivado.wp014c") else "vivado.wp013_relocate_hardblocks"
                output = await backend.call(worker_op, {"cell_names_b64": [b64(name) for name in targets], "site_names_b64": [b64(site) for _ in targets]}, timeout=900.0)
                actual["selected"] += len(targets); actual["transformed"] += relocation_effect_count(output, len(targets))
            elif op == "vivado.wp014c_replicate_net":
                net = registry.resolve(str(step.public_args["net"]), expected_type="net").canonical_identity
                await backend.call(op, {"net_name_b64": b64(net)}, timeout=900.0)
                actual["selected"] += 1; actual["transformed"] += 1
            elif op == "vivado.wp014c_route_net":
                net = registry.resolve(str(step.public_args["net"]), expected_type="net").canonical_identity
                await backend.call(op, {"net_name_b64": b64(net), "delay_mode": str(step.public_args["delay_mode"])}, timeout=900.0)
                actual["selected"] += 1; actual["transformed"] += 1
            elif op == "vivado.wp014h_batch_route_nets":
                nets = [registry.resolve(str(handle), expected_type="net").canonical_identity for handle in step.public_args["nets"]]
                await backend.call(
                    op,
                    {"net_names_b64": [b64(net) for net in nets], "delay_mode": str(step.public_args["delay_mode"])},
                    timeout=900.0,
                )
                actual["selected"] += len(nets); actual["transformed"] += len(nets)
            elif op == "vivado.wp014h_apply_allocation":
                relation_objects = registry.objects(object_type="compatibility")
                if len(relation_objects) != 1:
                    raise RuntimeError("wp015b_dispatch_relation_object_missing")
                relation = relation_objects[0].attributes.get("relation")
                allocation = registry.resolve(
                    str(step.public_args["allocation"]), expected_type="allocation",
                )
                if not isinstance(relation, Mapping):
                    raise RuntimeError("wp015b_dispatch_relation_payload_missing")
                validate_allocation(
                    allocation.attributes, relation,
                    root_sha256=registry.root_sha256,
                    stage=str(relation.get("stage")),
                )
                targets = [registry.resolve(str(handle), expected_type="hardblock").canonical_identity for handle in step.public_args["targets"]]
                sites = [registry.resolve(str(handle), expected_type="site").canonical_identity for handle in step.public_args["sites"]]
                target_identity_sha256 = digest(targets)
                dispatch_pairs_sha256 = digest([
                    {"target": target, "site": site}
                    for target, site in zip(targets, sites)
                ])
                worker_arguments = {
                    "cell_names_b64": [b64(name) for name in targets],
                    "site_names_b64": [b64(name) for name in sites],
                    "target_site_pair_hashes": list(step.public_args["target_site_pair_hashes"]),
                    "compatibility_relation_sha256": str(step.public_args["compatibility_relation_sha256"]),
                    "feasibility_query_sha256": str(step.public_args["feasibility_query_sha256"]),
                    "allocation_sha256": str(step.public_args["allocation_sha256"]),
                    "target_identity_sha256": target_identity_sha256,
                    "dispatch_pairs_sha256": dispatch_pairs_sha256,
                }
                output = await backend.call(
                    op, worker_arguments,
                    timeout=900.0,
                )
                moved = relocation_effect_count(output, len(targets))
                allocation_effect_evidence(
                    output, dispatch_pairs_sha256=dispatch_pairs_sha256,
                    target_identity_sha256=target_identity_sha256,
                    compatibility_relation_sha256=worker_arguments["compatibility_relation_sha256"],
                    allocation_sha256=worker_arguments["allocation_sha256"],
                )
                actual["selected"] += len(targets); actual["transformed"] += moved
            elif op == "vivado.wp014c_place_cell":
                cell = registry.resolve(str(step.public_args["cell"]), expected_type="cell").canonical_identity
                site = registry.resolve(str(step.public_args["site"]), expected_type="site").canonical_identity
                await backend.call(op, {"cell_name_b64": b64(cell), "site_name_b64": b64(site)}, timeout=900.0)
                actual["selected"] += 1; actual["transformed"] += 1
            elif op == "vivado.wp014c_unplace_cell":
                cell = registry.resolve(str(step.public_args["cell"]), expected_type="cell").canonical_identity
                await backend.call(op, {"cell_name_b64": b64(cell)}, timeout=900.0)
                actual["selected"] += 1; actual["transformed"] += 1
            elif op == "vivado.wp014c_swap_cells":
                first = registry.resolve(str(step.public_args["first"]), expected_type="cell").canonical_identity
                second = registry.resolve(str(step.public_args["second"]), expected_type="cell").canonical_identity
                await backend.call(op, {"first_cell_b64": b64(first), "second_cell_b64": b64(second)}, timeout=900.0)
                actual["selected"] += 2; actual["transformed"] += 2
            elif op in {"vivado.wp014c_set_cell_loc_fixed", "vivado.wp014c_set_net_dont_touch", "vivado.wp014c_set_pblock_exclude_placement", "vivado.wp014c_set_site_prohibit"}:
                kind = {"vivado.wp014c_set_cell_loc_fixed": "cell", "vivado.wp014c_set_net_dont_touch": "net", "vivado.wp014c_set_pblock_exclude_placement": "pblock", "vivado.wp014c_set_site_prohibit": "site"}[op]
                target = registry.resolve(str(step.public_args["target"]), expected_type=kind).canonical_identity
                if kind == "pblock":
                    target = pblock_ids[str(step.public_args["target"])]
                await backend.call(op, {"target_name_b64": b64(target), "value": bool(step.public_args["value"])}, timeout=900.0)
                actual["selected"] += 1; actual["transformed"] += 1
            elif op == "vivado.unroute":
                await backend.call("vivado.unroute_all", timeout=900.0)
            elif op == "vivado.unplace":
                await backend.call("vivado.unplace_all", timeout=900.0)
            elif op == "vivado.place":
                place_args = {"directive": str(step.public_args.get("directive", "Default"))}
                if step.public_args.get("net_delay_weight") is not None:
                    place_args["net_delay_weight"] = str(step.public_args["net_delay_weight"])
                await backend.call("vivado.place_design", place_args, timeout=900.0)
            elif op == "vivado.route":
                directive = str(step.public_args.get("directive", "Default"))
                await backend.call("vivado.route_design", {"directive": directive}, timeout=900.0)
            elif op == "vivado.phys_opt":
                if step.public_args.get("mode") == "DIRECTIVE":
                    phys_args = {"mode": "DIRECTIVE", "directive": str(step.public_args["directive"])}
                elif step.public_args.get("mode") == "OPTIONS":
                    phys_args = {"mode": "OPTIONS", "options": list(step.public_args["options"])}
                else:
                    phys_args = {"directive": str(step.public_args.get("directive"))}
                await backend.call("vivado.phys_opt_design", phys_args, timeout=900.0)
            target_identity_hash = (
                target_identity_sha256
                if op == "vivado.wp014h_apply_allocation"
                else digest(dict(step.private_args))
            )
            for trace_row in backend.trace[trace_start:]:
                trace_row["source_step_id"] = step.source_step_id
                trace_row["source_construct"] = step.source_construct
                trace_row["consumer_input_slots"] = [
                    {"input_slot": slot, "binding_id": binding}
                    for slot, binding in step.input_bindings
                ]
                trace_row["actual_target_identity_hash"] = target_identity_hash
                if op == "vivado.wp014h_apply_allocation":
                    trace_row.update({
                        "compatibility_relation_sha256": worker_arguments["compatibility_relation_sha256"],
                        "feasibility_query_sha256": worker_arguments["feasibility_query_sha256"],
                        "target_site_pair_hashes": worker_arguments["target_site_pair_hashes"],
                        "allocation_sha256": worker_arguments["allocation_sha256"],
                        "dispatch_pairs_sha256": worker_arguments["dispatch_pairs_sha256"],
                        "pair_membership_revalidated": True,
                    })
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
    write_atomic(out / "private" / "worker-call-outputs.json", backend.raw_call_outputs, private=True)
    root_after = file_digest(root)
    clean_route = bool(route and route_status_clean(route))
    child = {"sha256": file_digest(backend.child), "size_bytes": backend.child.stat().st_size} if backend.child.exists() else None
    endpoint = "VALID_COMPLETION" if error is None and clean_route and child is not None and process and process["cleanup_ok"] else "TOOL_FAILURE"
    observed_heavy = sum(
        row["operation"] in {"vivado.place_design", "vivado.route_design", "vivado.phys_opt_design", "vivado.wp014c_replicate_net", "vivado.wp014c_route_net", "vivado.wp014h_batch_route_nets"}
        for row in backend.trace
    )
    observed_light = sum(
        row["operation"] in {
            "vivado.unroute_all", "vivado.unplace_all", "vivado.wp013_create_pblock",
            "vivado.wp013_assign_pblock", "vivado.wp013_remove_pblock",
            "vivado.wp013_relocate_hardblocks",
            "vivado.wp014c_create_pblock", "vivado.wp014c_resize_pblock",
            "vivado.wp014c_assign_pblock", "vivado.wp014c_unassign_pblock",
            "vivado.wp014c_delete_pblock", "vivado.wp014c_relocate_hardblocks",
            "vivado.wp014c_place_cell", "vivado.wp014c_unplace_cell",
            "vivado.wp014c_swap_cells", "vivado.wp014c_set_cell_loc_fixed",
            "vivado.wp014c_set_net_dont_touch", "vivado.wp014c_set_pblock_exclude_placement",
            "vivado.wp014c_set_site_prohibit",
            "vivado.wp014h_apply_allocation",
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
        "worker_call_outputs_sha256": digest(backend.raw_call_outputs),
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
    provider_admission_value = read_object(args.provider_admission)
    if not provider_admission_value.get("accepted"):
        raise SystemExit("provider_contract_admission_not_accepted")
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
    cost = SharedCost(
        float(bundle["guards"]["openrouter_cost_cap_usd"]),
        spent=float(provider_admission_value["known_cost_usd"]),
    )
    # Four simultaneous Sonnet reservations already consume the full $1 guard
    # once the six provider-admission requests have settled.  Keep model
    # planning below that worst-case reservation envelope while preserving the
    # independently bounded eight-way implementation concurrency.
    planning_semaphore = asyncio.Semaphore(min(args.max_parallel, 4))

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
    remaining_heavy = int(bundle["guards"]["implementation_heavy_call_cap"])
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
            evidence = boundary_evidence(candidate=candidate, execution_started=False, execution=None)
            return row | {"execution_started": False, "actual_heavy_calls": 0, "actual_light_mutations": 0, "execution_allocation": allocation, "boundary_evidence": evidence, "boundary_runtime_exercised": evidence["boundary_runtime_exercised"]}
        if not allocation["dispatch_allowed"]:
            evidence = boundary_evidence(candidate=candidate, execution_started=False, execution=None)
            return row | {"canonical_terminal": "BUDGET_BLOCKED", "execution_started": False, "actual_heavy_calls": 0, "actual_light_mutations": 0, "execution_allocation": allocation, "boundary_evidence": evidence, "boundary_runtime_exercised": evidence["boundary_runtime_exercised"]}
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
            evidence = boundary_evidence(candidate=candidate, execution_started=True, execution=result)
            return row | {"canonical_terminal": result["canonical_endpoint"], "execution_started": True, "execution": result, "actual_heavy_calls": result.get("actual_heavy_calls", 0), "actual_light_mutations": result.get("actual_light_mutations"), "execution_allocation": allocation, "boundary_evidence": evidence, "boundary_runtime_exercised": evidence["boundary_runtime_exercised"]}

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
        "provider_requests_e0": sum(row.get("provider_requests", 0) for row in final_rows),
        "provider_requests_total": provider_admission_value["physical_requests"] + sum(row.get("provider_requests", 0) for row in final_rows),
        "provider_admission_sha256": provider_admission_value["aggregate_sha256"],
        "provider_admission_cost_usd": provider_admission_value["known_cost_usd"],
        "known_openrouter_cost_usd": cost.spent,
        "openrouter_cost_cap_usd": cost.cap,
        "actual_heavy_calls_e0": sum(int(row.get("actual_heavy_calls") or 0) for row in final_rows),
        "wp013a_heavy_cap": bundle["guards"]["implementation_heavy_call_cap"],
        "roots_before": roots_before,
        "roots_after": roots_after,
        "all_roots_unchanged": roots_before == roots_after,
        "repository_before": bundle["repository"],
        "repository_after": repository_after,
        "all_o_hashes_equal_by_root": all(
            len({row.get("observation_sha256") for row in final_rows if row["root_id"] == root_id and row["level"] in LLM_LEVELS}) == 1
            for root_id in bundle["roots"]
        ),
        "all_q_catalogues_equal_by_root": all(
            len({
                digest(row.get("planner_evidence", {}).get("query_catalogue"))
                for row in final_rows
                if row["root_id"] == root_id and row["level"] in LLM_LEVELS
            }) == 1
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
    aggregate["valid_executions_by_level"] = {
        level: sum(
            row["level"] == level and row["canonical_terminal"] == "VALID_COMPLETION"
            for row in final_rows
        )
        for level in LLM_LEVELS
    }
    aggregate["model_commit_or_attributable_terminal"] = {
        model: bool(
            any(
                row.get("model") == model
                and (
                    row.get("planner_evidence", {}).get("valid_commits", 0) >= 1
                    or row.get("canonical_terminal") == "NO_VALID_SUBMISSION"
                )
                for row in final_rows
            )
        )
        for model in MODELS
    }
    aggregate["accepted_for_evidence_review"] = bool(
        aggregate["canonical_terminal_jobs"] == 16
        and aggregate["known_openrouter_cost_usd"] <= aggregate["openrouter_cost_cap_usd"]
        and aggregate["actual_heavy_calls_e0"] <= aggregate["wp013a_heavy_cap"]
        and aggregate["all_roots_unchanged"]
        and aggregate["all_o_hashes_equal_by_root"]
        and aggregate["all_q_catalogues_equal_by_root"]
        and aggregate["precommit_qor_feedback_count"] == 0
        and aggregate["precommit_mutation_count"] == 0
        and aggregate["postcommit_resubmission_count"] == 0
        and all(value >= 1 for value in aggregate["valid_executions_by_level"].values())
        and all(aggregate["model_commit_or_attributable_terminal"].values())
        and bundle["contract_completeness_audit"]["accepted"]
    )
    aggregate["aggregate_sha256"] = digest(aggregate)
    write_atomic(args.out / "e0-aggregate.json", aggregate)
    print(json.dumps({key: aggregate[key] for key in ("accepted_for_evidence_review", "planned_jobs", "canonical_terminal_jobs", "terminal_counts", "provider_requests_e0", "known_openrouter_cost_usd", "actual_heavy_calls_e0", "valid_executions_by_level", "queue_wall_seconds")}))
    return 0 if aggregate["accepted_for_evidence_review"] else 2


def audit(args: argparse.Namespace) -> int:
    aggregate = read_object(args.run / "e0-aggregate.json")
    claimed = aggregate.pop("aggregate_sha256")
    errors: list[str] = []
    if digest(aggregate) != claimed:
        errors.append("aggregate_hash_invalid")
    aggregate["aggregate_sha256"] = claimed
    jobs = aggregate.get("jobs", [])
    if len(jobs) != EXPECTED_JOB_COUNT:
        errors.append("job_count_not_16")
    if sum(row.get("canonical_terminal") in TERMINALS for row in jobs) != 16:
        errors.append("canonical_terminal_count_not_16")
    if aggregate.get("known_openrouter_cost_usd", EXPECTED_OPENROUTER_COST_CAP_USD + 1.0) > EXPECTED_OPENROUTER_COST_CAP_USD:
        errors.append("openrouter_cost_cap_exceeded")
    if aggregate.get("actual_heavy_calls_e0", EXPECTED_HEAVY_CALL_CAP + 1) > EXPECTED_HEAVY_CALL_CAP:
        errors.append("heavy_call_cap_exceeded")
    if not aggregate.get("all_roots_unchanged"):
        errors.append("root_integrity_failed")
    if not aggregate.get("all_o_hashes_equal_by_root"):
        errors.append("o_hash_equality_failed")
    if not aggregate.get("all_q_catalogues_equal_by_root"):
        errors.append("q_catalogue_equality_failed")
    if not all(value >= 1 for value in aggregate.get("valid_executions_by_level", {}).values()):
        errors.append("missing_valid_execution_for_level")
    if not aggregate.get("accepted_for_evidence_review"):
        errors.append("aggregate_acceptance_false")
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
        if evidence and evidence.get("query_requests_used", 0) > 8:
            errors.append("query_budget_exceeded:" + row["job_id"])
        if evidence:
            for event in evidence.get("transcript", []):
                if event.get("turn") == 4 and event.get("kind") not in {
                    "COMMIT", "STOP", "NO_VALID_SUBMISSION", "INVALID"
                } and not any(
                    isinstance(item, Mapping)
                    and item.get("error_code") == "kind_not_allowed_on_final_turn"
                    for item in event.get("feedback", [])
                ):
                    errors.append("final_turn_rule_not_enforced:" + row["job_id"])
                for feedback in event.get("feedback", []):
                    if not isinstance(feedback, Mapping) or not {
                        "error_code", "location", "expected_shape_or_schema_ref",
                        "remaining_turns", "remaining_feedback_rounds", "remaining_query_budget",
                    } <= set(feedback):
                        errors.append("feedback_shape_invalid:" + row["job_id"])
        for turn in row.get("turns", []):
            if int(turn.get("total_input_size_bytes") or 0) > MAX_INPUT_BYTES:
                errors.append("context_byte_ceiling_exceeded:" + row["job_id"])
        if row.get("actual_heavy_calls", 0) > row.get("execution_allocation", {}).get("required_heavy_calls", 0):
            errors.append("predispatch_accounting_exceeded:" + row["job_id"])
    audit_value = {
        "schema_version": "dof.wp013a-e0-v4-independent-audit.v1",
        "aggregate_sha256": claimed,
        "accepted": not errors,
        "errors": sorted(set(errors)),
        "checks": {
            "jobs": len(jobs),
            "canonical_terminals": sum(row.get("canonical_terminal") in TERMINALS for row in jobs),
            "cost_usd": aggregate.get("known_openrouter_cost_usd"),
            "heavy_total": aggregate.get("actual_heavy_calls_e0"),
            "roots_unchanged": aggregate.get("all_roots_unchanged"),
            "o_hashes_equal": aggregate.get("all_o_hashes_equal_by_root"),
            "q_catalogues_equal": aggregate.get("all_q_catalogues_equal_by_root"),
            "valid_executions_by_level": aggregate.get("valid_executions_by_level"),
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
    d = commands.add_parser("provider-admission")
    d.add_argument("--plan", type=Path, required=True)
    d.add_argument("--preflight", type=Path, required=True)
    d.add_argument("--out", type=Path, required=True)
    r = commands.add_parser("run")
    r.add_argument("--bundle", type=Path, required=True)
    r.add_argument("--capture", type=Path, required=True)
    r.add_argument("--preflight", type=Path, required=True)
    r.add_argument("--provider-admission", type=Path, required=True)
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
    if args.command == "provider-admission":
        return asyncio.run(provider_admission(args))
    if args.command == "run":
        return asyncio.run(run(args))
    return audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
