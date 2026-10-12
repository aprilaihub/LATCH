#!/usr/bin/env python3
"""Final WP-014C capture, freeze, calibration, and repeat-1 controller."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
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
import run_wp014 as legacy  # noqa: E402
from optimizer_pipeline.dof.single_submission.action_surface_v3 import M_ACTION_KEYS, registry_sha256  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v5 import (  # noqa: E402
    MAX_INPUT_BYTES, PLANNING_CONTEXT_V22, PROMPT_VERSION_V3,
)
from optimizer_pipeline.dof.single_submission.contract_v6 import (  # noqa: E402
    PLANNER_MESSAGE_V6, PROTOCOL_EPOCH, contract_for_cell,
    contract_hash_for_cell, prompt_for_cell, validate_protocol_cell,
)
from optimizer_pipeline.dof.single_submission.observation_v4 import CommonQuerySurfaceV4  # noqa: E402
from optimizer_pipeline.dof.single_submission.planner_v5 import SingleSubmissionSessionV5  # noqa: E402
from scripts.run_wp014c_admission import rapidwright_net_census  # noqa: E402


SCHEMA = "dof.wp014c-dual-track-repeat1.v3"
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
CONDITIONS = ("M_ONLY", "P_BOUNDARY", "G_BOUNDARY")
ORIGINAL_COMMON_OBSERVATION = base._common_observation


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else base.canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write(path: Path, value: Any, *, private: bool = False) -> None:
    base.write_atomic(path, value, private=private)


def common_observation_v3(plan_root: Mapping[str, Any], capture_root: Path):
    observation, private = ORIGINAL_COMMON_OBSERVATION(plan_root, capture_root)
    census = read(capture_root / "private" / "rapidwright-net-census.json")
    if census.get("root_sha256") != plan_root["sha256"] or not census.get("nets"):
        raise RuntimeError("rapidwright_census_binding_failed:" + str(plan_root["root_id"]))
    private["rows"]["high_fanout_net_details"] = [
        {
            "canonical_identity": row["name"], "handle_type": "net",
            "fanout": int(row["fanout"]), "row_semantics": "rapidwright_nonstatic_wire",
            "source_tool": "rapidwright",
        }
        for row in census["nets"]
    ]
    private["missing_reasons"].pop("high_fanout_net_details", None)
    private["query_semantic_audit"] = base.audit_query_semantics(
        private["rows"], private["missing_reasons"], require_e0_paths=True,
    )
    private["fanout_admitted"] = True
    private["rapidwright_census_sha256"] = census["census_sha256"]
    return observation, private


class ContextSessionV3(SingleSubmissionSessionV5):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        job = legacy.CURRENT_JOB.get()
        if job is None:
            raise RuntimeError("wp014c_job_context_missing")
        kwargs["language_condition"] = job["language_condition"]
        super().__init__(*args, **kwargs)


def runtime_level(condition: str) -> str:
    return {"M_ONLY": "L2", "P_BOUNDARY": "L3", "G_BOUNDARY": "L4"}[condition]


def jobs_v3(stress: Mapping[str, Any]) -> list[dict[str, Any]]:
    full_ids = [row["root_id"] for row in stress["derivation_table"]]
    stress_rows = {row["root_id"]: row for row in stress["selected_stress_roots"]}
    result: list[dict[str, Any]] = []
    full_mask_hash = digest({"coverage_regime": "FULL_M8", "actions": M_ACTION_KEYS})
    for root_id in full_ids:
        for model in MODELS:
            for level in ("L2", "L3", "L4"):
                result.append({
                    "job_id": f"nk__{root_id}__{model.replace('/', '_')}__{level.lower()}",
                    "phase": "N_K", "root_id": root_id, "model": model,
                    "level": level, "agent_level": level, "protocol_epoch": PROTOCOL_EPOCH,
                    "study_track": "N_NEUTRAL", "coverage_regime": "FULL_M8",
                    "language_condition": "FREE_CHOICE", "repeat": 1,
                    "admitted_action_keys": list(M_ACTION_KEYS),
                    "mask_evidence_hash": full_mask_hash,
                })
    for root_id, stress_row in stress_rows.items():
        admitted = list(stress_row["retained_action_keys"])
        mask_hash = digest(stress_row)
        for model in MODELS:
            for level in ("L2", "L3", "L4"):
                result.append({
                    "job_id": f"sn__{root_id}__{model.replace('/', '_')}__{level.lower()}",
                    "phase": "S_N", "root_id": root_id, "model": model,
                    "level": level, "agent_level": level, "protocol_epoch": PROTOCOL_EPOCH,
                    "study_track": "S_NEUTRAL_REDUCED", "coverage_regime": "REDUCED_M_STRESS",
                    "language_condition": "FREE_CHOICE", "repeat": 1,
                    "admitted_action_keys": admitted, "mask_evidence_hash": mask_hash,
                })
            for condition in CONDITIONS:
                result.append({
                    "job_id": f"cs__{root_id}__{model.replace('/', '_')}__{condition.lower()}",
                    "phase": "C_S", "root_id": root_id, "model": model,
                    "level": runtime_level(condition), "agent_level": "C",
                    "protocol_epoch": PROTOCOL_EPOCH, "study_track": "C_CONDITIONED",
                    "coverage_regime": "REDUCED_M_STRESS", "language_condition": condition,
                    "repeat": 1, "admitted_action_keys": admitted,
                    "mask_evidence_hash": mask_hash,
                    "calibration_cell": root_id in {"logicnets_jscl", "corescore_500_mod"},
                })
    return result


def configure() -> None:
    legacy.SCHEMA = SCHEMA
    legacy.MODELS = MODELS
    legacy.MODEL_RESERVES = MODEL_RESERVES
    legacy.CONDITIONS = CONDITIONS
    legacy.PROTOCOL_EPOCH = PROTOCOL_EPOCH
    legacy.PROMPT_VERSION_V22 = PROMPT_VERSION_V3
    legacy.PLANNER_MESSAGE_V4 = PLANNER_MESSAGE_V6
    legacy.PLANNING_CONTEXT_V22 = PLANNING_CONTEXT_V22
    legacy.M_ACTION_KEYS = M_ACTION_KEYS
    legacy.contract_for_cell = contract_for_cell
    legacy.contract_hash_for_cell = contract_hash_for_cell
    legacy.prompt_for_cell = prompt_for_cell
    legacy.validate_protocol_cell = validate_protocol_cell
    legacy.ContextSession = ContextSessionV3
    legacy.CommonQuerySurfaceV3 = CommonQuerySurfaceV4
    legacy._runtime_level = runtime_level
    legacy._jobs = jobs_v3
    legacy.OPENROUTER_COST_CAP_USD = 12.0
    legacy.IMPLEMENTATION_HEAVY_CALL_CAP = 760
    legacy.QUEUE_WALL_SECONDS_CAP = 129600
    legacy.configure_base()
    base.CommonQuerySurfaceV2 = CommonQuerySurfaceV4
    base.SingleSubmissionSessionV2 = ContextSessionV3
    base._common_observation = common_observation_v3
    base.EXPECTED_OPENROUTER_COST_CAP_USD = 12.0
    base.CAPTURE_SCHEMA = "dof.wp014c-observation-capture.v3"
    base.CAPTURE_JOB_SCHEMA = "dof.wp014c-observation-capture-job.v3"
    base.CAPTURE_JOB_PREFIX = "wp014c-observe"
    base.HANDLE_NAMESPACE = "wp014c-handles-v3"
    base.TRIAL_ID = "wp014c-repeat1"
    base.QUERY_EVIDENCE_TAG = "wp014c-frozen-read-only-capture"


async def capture(args: argparse.Namespace) -> int:
    started = time.monotonic()
    # The WP-014C high-fanout source of truth is the direct RapidWright WIRE
    # census appended below.  Do not also launch the superseded Vivado
    # full-design fanout traversal through the legacy compatibility capture.
    args.omit_vivado_fanout = True
    result = await legacy.capture(args)
    if result != 0:
        return result
    roots = legacy._root_specs(args.roots, args.stress)
    census_rows = []
    for root in roots:
        census = await asyncio.to_thread(rapidwright_net_census, Path(root["root_path"]), limit=64)
        write(args.out / root["root_id"] / "private" / "rapidwright-net-census.json", census, private=True)
        census_rows.append({
            "root_id": root["root_id"], "root_sha256": root["sha256"],
            "census_sha256": census["census_sha256"], "returned": census["returned"],
            "available": census["available"], "accepted": census["returned"] > 0,
        })
    previous = read(args.out / "capture-aggregate.json")
    previous.pop("capture_sha256", None)
    previous["schema_version"] = "dof.wp014c-observation-capture.v3"
    previous["rapidwright_census"] = census_rows
    previous["accepted"] = bool(previous.get("accepted") and all(row["accepted"] for row in census_rows))
    previous["queue_wall_seconds"] = time.monotonic() - started
    value = previous | {"capture_sha256": digest(previous)}
    write(args.out / "capture-aggregate.json", value)
    print(json.dumps({
        "accepted": value["accepted"], "roots": value["root_count"],
        "rapidwright_roots": len(census_rows), "queue_wall_seconds": value["queue_wall_seconds"],
    }, sort_keys=True))
    return 0 if value["accepted"] else 2


def freeze(args: argparse.Namespace) -> int:
    result = legacy.freeze(args)
    if result != 0:
        return result
    bundle = read(args.output)
    bundle.pop("bundle_sha256", None)
    bundle["schema_version"] = SCHEMA
    bundle["protocol_epoch"] = PROTOCOL_EPOCH
    bundle["action_surface_registry_sha256"] = registry_sha256()
    bundle["query_surface"] = {
        "version": "dof.single-submission.query-q.v4",
        "operations": 6, "request_items": 8, "session_result_max_bytes": 16384,
    }
    bundle["common_component_hashes"] = {
        path: base.file_digest(REPO / path) for path in (
            "optimizer_pipeline/dof/single_submission/action_surface_v3.py",
            "optimizer_pipeline/dof/single_submission/contract_v5.py",
            "optimizer_pipeline/dof/single_submission/contract_v6.py",
            "optimizer_pipeline/dof/single_submission/observation_v4.py",
            "optimizer_pipeline/dof/single_submission/planner_v5.py",
            "optimizer_pipeline/dof/single_submission/language.py",
            "optimizer_pipeline/dof/vivado_mutation_worker.py",
            "optimizer_pipeline/dof/openrouter.py",
            "scripts/run_wp013a_e0.py", "scripts/run_wp014.py",
            "scripts/run_wp014c.py", "scripts/run_wp014c_scientific.py",
        )
    }
    bundle["gate3_summary_sha256"] = base.file_digest(
        REPO / "configs/wp014c/gate3-production-admission-summary.json"
    )
    bundle["gate4_summary_sha256"] = base.file_digest(
        REPO / "configs/wp014c/gate4-provider-admission-summary.json"
    )
    bundle["guards"] = {
        "openrouter_cost_cap_usd": 12.0, "implementation_heavy_call_cap": 760,
        "queue_wall_seconds": 129600, "max_parallel_weekday": 8,
        "max_parallel_weekend": 16, "provider_retries": 0,
        "provider_fallbacks": 0, "postcommit_resubmissions": 0,
        "planning_input_byte_ceiling": MAX_INPUT_BYTES,
    }
    bundle["repository"] = base.assert_clean_repository()
    value = bundle | {"bundle_sha256": digest(bundle)}
    write(args.output, value)
    print(json.dumps({
        "status": "FROZEN", "stage": args.stage, "jobs": len(value["jobs"]),
        "bundle_sha256": value["bundle_sha256"], "source_commit": value["repository"]["head_commit"],
    }, sort_keys=True))
    return 0


def init_ledger(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise FileExistsError(args.output)
    gate4 = read(REPO / "configs/wp014c/gate4-provider-admission-summary.json")
    capture_value = read(args.capture / "capture-aggregate.json")
    diagnostic = float(gate4["diagnostic_predecessor"]["known_cost_usd"])
    final = float(gate4["known_cost_usd"])
    value = {
        "schema_version": "dof.wp014c-resource-ledger.v1",
        "cost_usd": diagnostic + final,
        "heavy_calls": 0,
        "queue_wall_seconds": float(capture_value["queue_wall_seconds"]),
        "completed_phases": ["GATE4_DIAGNOSTIC", "GATE4_PROVIDER_ADMISSION", "CAPTURE"],
        "cost_components": {"gate4_diagnostic": diagnostic, "gate4_final": final},
    }
    write(args.output, value)
    print(json.dumps(value, sort_keys=True))
    return 0


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
    f = commands.add_parser("freeze")
    f.add_argument("--stage", choices=("PROVISIONAL_CALIBRATION", "FINAL_REPEAT1"), required=True)
    f.add_argument("--roots", type=Path, required=True)
    f.add_argument("--stress", type=Path, required=True)
    f.add_argument("--capture", type=Path, required=True)
    f.add_argument("--admission", type=Path, required=True)
    f.add_argument("--references", type=Path, required=True)
    f.add_argument("--calibration", type=Path)
    f.add_argument("--output", type=Path, required=True)
    l = commands.add_parser("init-ledger")
    l.add_argument("--capture", type=Path, required=True)
    l.add_argument("--output", type=Path, required=True)
    r = commands.add_parser("run")
    r.add_argument("--phase", choices=("CALIBRATION", "N_K", "S_N", "C_S"), required=True)
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
    configure()
    args = parser().parse_args()
    if args.command == "capture":
        return asyncio.run(capture(args))
    if args.command == "freeze":
        return freeze(args)
    if args.command == "init-ledger":
        return init_ledger(args)
    if args.command == "run":
        return asyncio.run(legacy.run_phase(args))
    if args.command == "audit":
        return legacy.audit_phase(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
