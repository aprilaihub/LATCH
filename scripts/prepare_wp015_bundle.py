#!/usr/bin/env python3
"""Capture and freeze model-visible O/Q evidence for the 25 WP-015 roots."""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
TIMING_CONTRACT = REPO / "configs/wp015/benchmark-timing-contract-v1.json"
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp013a_e0 as base  # noqa: E402
import run_wp014c_scientific as wp014c  # noqa: E402
from optimizer_pipeline.dof.single_submission.action_surface_v3 import registry_sha256  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v6 import (  # noqa: E402
    PROTOCOL_EPOCH, contract_hash_for_cell, prompt_for_cell, validate_protocol_cell,
)


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any, *, private: bool = False) -> None:
    base.write_atomic(path, value, private=private)


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else base.canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    return base.file_digest(path)


def configure_exact_hardblock_scan(scan_dir: Path) -> None:
    audit = read(scan_dir / "gate1-audit.json")
    aggregate = read(scan_dir / "aggregate.json")
    if audit.get("status") != "PASS" or aggregate.get("status") != "PASS":
        raise ValueError("wp015b_gate1_scan_not_accepted")
    if audit.get("scan_aggregate_sha256") != aggregate.get("aggregate_sha256"):
        raise ValueError("wp015b_gate1_audit_binding_mismatch")
    base.HARDBLOCK_COMPATIBILITY_SCAN_DIR = scan_dir.resolve(strict=True)
    base.REQUIRE_EXACT_HARDBLOCK_COMPATIBILITY = True


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def roots_from_gate0(
    path: Path, timing_contract_path: Path = TIMING_CONTRACT,
) -> list[dict[str, Any]]:
    value = read(path)
    if value.get("status") != "PASS" or len(value.get("roots", [])) != 25:
        raise ValueError("wp015_gate0_roots_not_complete")
    timing_value = read(timing_contract_path)
    timing_rows = timing_value.get("roots", [])
    timing_by_id = {row["root_id"]: row for row in timing_rows}
    gate0_ids = {row["root_id"] for row in value["roots"]}
    if (
        timing_value.get("schema_version") != "dof.wp015-benchmark-timing-contract.v1"
        or len(timing_rows) != 25 or len(timing_by_id) != 25
        or set(timing_by_id) != gate0_ids
    ):
        raise ValueError("wp015_timing_contract_root_set_invalid")
    rows = []
    for row in value["roots"]:
        timing = timing_by_id[row["root_id"]]
        if (
            timing.get("timing_stratum") != row.get("timing_stratum")
            or timing.get("target_clock") != row.get("target_clock")
            or not isinstance(timing.get("target_period_ns"), (int, float))
            or float(timing["target_period_ns"]) <= 0
            or not timing.get("target_period_evidence")
            or not timing.get("evidence_sha256")
        ):
            raise ValueError("wp015_timing_contract_row_invalid:" + row["root_id"])
        evidence_path = Path(timing["evidence_path"])
        if not evidence_path.is_absolute():
            evidence_path = REPO / evidence_path
        if not evidence_path.is_file() or file_sha256(evidence_path) != timing["evidence_sha256"]:
            raise ValueError("wp015_timing_contract_evidence_invalid:" + row["root_id"])
        root = {
            "root_id": row["root_id"], "root_path": row["dcp"],
            "sha256": row["sha256"], "family": row["family"],
            "split": row["split"], "timing_stratum": row["timing_stratum"],
            "resource_class": row["resource_class"],
            "target_clock": timing["target_clock"],
            "target_period_ns": float(timing["target_period_ns"]),
            "period_ns": float(timing["target_period_ns"]),
            "target_period_evidence": timing["target_period_evidence"],
            "timing_contract_evidence_path": timing["evidence_path"],
            "timing_contract_evidence_sha256": timing["evidence_sha256"],
        }
        if not Path(root["root_path"]).is_file() or file_sha256(Path(root["root_path"])) != root["sha256"]:
            raise ValueError("wp015_capture_root_hash_mismatch:" + root["root_id"])
        rows.append(root)
    return rows


async def capture(args: argparse.Namespace) -> int:
    if args.output.exists() and not args.resume:
        raise FileExistsError(args.output)
    base.assert_clean_repository()
    roots = roots_from_gate0(args.gate0)
    if not 1 <= args.max_parallel <= 8:
        raise ValueError("weekday_capture_parallelism_must_be_1_to_8")
    args.output.mkdir(parents=True, exist_ok=args.resume)
    semaphore = asyncio.Semaphore(args.max_parallel)
    started = time.monotonic()

    async def one(root: Mapping[str, Any]) -> dict[str, Any]:
        async with semaphore:
            root_path = Path(root["root_path"])
            root_dir = args.output / root["root_id"]
            prior_path = root_dir / "capture-summary.json"
            if args.resume and prior_path.is_file():
                prior = read(prior_path)
                observation_path = root_dir / "vivado-observation" / "real-observation.json"
                probe_path = root_dir / "contract-probe" / "admission-summary.json"
                census_path = root_dir / "private" / "rapidwright-net-census.json"
                if all(path.is_file() for path in (observation_path, probe_path, census_path)):
                    observation, summary, census = read(observation_path), read(probe_path), read(census_path)
                    if (
                        prior.get("accepted") is True
                        and prior.get("root_sha256") == root["sha256"]
                        and prior.get("observation_sha256") == observation.get("observation_sha256")
                        and prior.get("probe_summary_sha256") == digest(summary)
                        and prior.get("rapidwright_census_sha256") == census.get("census_sha256")
                        and census.get("root_sha256") == root["sha256"]
                    ):
                        return prior | {"batch_reused": True}
            if root_dir.exists():
                interrupted = args.output / "interrupted-captures" / root["root_id"]
                interrupted.parent.mkdir(parents=True, exist_ok=True)
                if interrupted.exists():
                    interrupted = interrupted.with_name(
                        interrupted.name + "-" + str(int(time.time()))
                    )
                shutil.move(str(root_dir), str(interrupted))
            try:
                observation = await asyncio.to_thread(
                    base.capture_real_observation,
                    base._probe_job(root["root_id"], root["sha256"]), root_path,
                    root_dir / "vivado-observation", decision_index=0, history=[],
                    heavy_remaining=4, model_requests_remaining=4,
                    vivado_exec=args.vivado, python_exec=args.python,
                    repository_anchor=REPO, e1_protocol=True,
                )
                command = (
                    str(args.python), str(REPO / "scripts/run_wp013_admission.py"),
                    "--mode", "contract-probe", "--root", str(root_path),
                    "--out", str(root_dir / "contract-probe"), "--python", str(args.python),
                    "--vivado", str(args.vivado), "--omit-vivado-fanout",
                )
                completed = await asyncio.to_thread(
                    subprocess.run, command, cwd=REPO, text=True, capture_output=True,
                    timeout=7200,
                )
                summary_path = root_dir / "contract-probe" / "admission-summary.json"
                summary = read(summary_path) if summary_path.is_file() else None
                census_path = root_dir / "private" / "rapidwright-net-census.json"
                census_command = (
                    str(args.python), str(REPO / "scripts/run_wp015_rapidwright_census.py"),
                    "--root", str(root_path), "--output", str(census_path), "--limit", "64",
                )
                census_completed = await asyncio.to_thread(
                    subprocess.run, census_command, cwd=REPO, text=True,
                    capture_output=True, timeout=900,
                )
                census = read(census_path) if census_path.is_file() else {}
                if census_path.is_file():
                    census_path.chmod(0o600)
                accepted = bool(
                    observation.get("origin") == "real_read_only_vivado"
                    and completed.returncode == 0 and summary and summary.get("accepted")
                    and census_completed.returncode == 0
                    and census.get("root_sha256") == root["sha256"] and census.get("nets")
                )
                result = {
                    "root_id": root["root_id"], "root_sha256": root["sha256"],
                    "observation_sha256": observation["observation_sha256"],
                    "probe_returncode": completed.returncode,
                    "probe_stdout_sha256": digest(completed.stdout),
                    "probe_stderr_sha256": digest(completed.stderr),
                    "probe_summary_sha256": digest(summary) if summary else None,
                    "rapidwright_census_sha256": census.get("census_sha256"),
                    "rapidwright_returncode": census_completed.returncode,
                    "rapidwright_stdout_sha256": digest(census_completed.stdout),
                    "rapidwright_stderr_sha256": digest(census_completed.stderr),
                    "rapidwright_nets": census.get("returned"), "accepted": accepted,
                    "batch_reused": False,
                }
            except Exception as exc:
                result = {
                    "root_id": root["root_id"], "root_sha256": root["sha256"],
                    "accepted": False, "batch_reused": False,
                    "failure_type": type(exc).__name__, "failure_message": str(exc),
                }
            write(root_dir / "capture-summary.json", result)
            return result

    rows = await asyncio.gather(*(one(root) for root in roots))
    core = {
        "schema_version": "dof.wp015-observation-capture.v1",
        "repository": {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")},
        "gate0_sha256": file_sha256(args.gate0), "root_count": len(rows),
        "accepted_count": sum(row["accepted"] for row in rows),
        "accepted": len(rows) == 25 and all(row["accepted"] for row in rows),
        "reused_count": sum(bool(row.get("batch_reused")) for row in rows),
        "failure_counts": dict(sorted(collections.Counter(
            str(row.get("failure_type")) for row in rows if not row["accepted"]
        ).items())),
        "queue_wall_seconds": time.monotonic() - started,
        "provider_or_llm_calls": 0, "optimization_calls": 0,
        "rows": rows,
    }
    result = core | {"capture_sha256": digest(core)}
    write(args.output / "capture-aggregate.json", result)
    print(json.dumps({key: result[key] for key in (
        "accepted", "root_count", "accepted_count", "queue_wall_seconds"
    )}, sort_keys=True))
    return 0 if result["accepted"] else 2


def freeze(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise FileExistsError(args.output)
    repository = base.assert_clean_repository()
    capture_value = read(args.capture / "capture-aggregate.json")
    inventory = read(args.inventory)
    core = read(args.core)
    environment = read(args.environment)
    roots_list = roots_from_gate0(args.gate0)
    if (
        not capture_value.get("accepted") or capture_value.get("root_count") != 25
        or inventory.get("job_count") != 1134
        or inventory.get("shared_scientific_core_hash") != core.get("shared_scientific_core_hash")
        or repository["head_commit"] != core.get("scientific_source_commit")
        or repository["head_tree"] != core.get("scientific_source_tree")
        or environment.get("repository_clean") is not True
        or environment.get("repository", {}).get("head") != [repository["head_commit"]]
        or environment.get("repository", {}).get("tree") != [repository["head_tree"]]
    ):
        raise ValueError("wp015_bundle_freeze_prerequisite_invalid")
    roots = {row["root_id"]: row for row in roots_list}
    observations: dict[str, Any] = {}
    query_audits: dict[str, Any] = {}
    for root_id, root in roots.items():
        observation, private = base._common_observation(root, args.capture / root_id)
        if not private["query_semantic_audit"]["accepted"]:
            raise ValueError("wp015_query_semantic_audit_failed:" + root_id)
        observations[root_id] = {
            "payload": dict(observation.payload),
            "canonical_utf8": observation.canonical_bytes.decode(),
            "sha256": observation.sha256,
            "real_observation_sha256": private["real_observation_sha256"],
            "query_private_rows_sha256": digest(private["rows"]),
            "handle_manifest": private["registry"].public_manifest(),
            "predefined_regions": list(private["predefined_regions"]),
            "geometry_bounds": list(private["geometry_bounds"]),
            "rapidwright_census_sha256": private["rapidwright_census_sha256"],
        }
        query_audits[root_id] = private["query_semantic_audit"]
    contracts = {}
    for job in inventory["jobs"]:
        validate_protocol_cell(
            study_track=job["track"], coverage_regime=job["coverage_regime"],
            language_condition=job["language_condition"], level=job["agent_level"],
        )
        contracts[job["job_id"]] = contract_hash_for_cell(
            study_track=job["track"], coverage_regime=job["coverage_regime"],
            language_condition=job["language_condition"], level=job["agent_level"],
            admitted_action_keys=tuple(job["admitted_action_keys"]),
        )
    counts = collections.Counter(job["track"] for job in inventory["jobs"])
    value_core = {
        "schema_version": "dof.wp015-scientific-bundle.v1",
        "protocol_epoch": PROTOCOL_EPOCH, "protocol_id": "WP015_LEAN_R1_V3",
        "base_protocol_id": "WP015_LEAN_R1",
        "repository": repository, "shared_scientific_core_hash": core["shared_scientific_core_hash"],
        "roots": roots, "observations": observations, "query_audits": query_audits,
        "inventory_path": str(args.inventory), "inventory_sha256": inventory["inventory_sha256"],
        "environment_manifest_sha256": environment["manifest_sha256"],
        "jobs": inventory["jobs"], "job_counts": dict(sorted(counts.items())),
        "contract_hash_by_job": contracts,
        "prompt_hashes": {
            "N_L2": digest(prompt_for_cell(study_track="N_NEUTRAL", language_condition="FREE_CHOICE", level="L2")),
            "N_L3": digest(prompt_for_cell(study_track="N_NEUTRAL", language_condition="FREE_CHOICE", level="L3")),
            "N_L4": digest(prompt_for_cell(study_track="N_NEUTRAL", language_condition="FREE_CHOICE", level="L4")),
            "C_M": digest(prompt_for_cell(study_track="C_CONDITIONED", language_condition="M_ONLY", level="C")),
            "C_P": digest(prompt_for_cell(study_track="C_CONDITIONED", language_condition="P_BOUNDARY", level="C")),
            "C_G": digest(prompt_for_cell(study_track="C_CONDITIONED", language_condition="G_BOUNDARY", level="C")),
        },
        "action_surface_registry_sha256": registry_sha256(),
        "timing_contract_path": str(TIMING_CONTRACT.relative_to(REPO)),
        "timing_contract_sha256": file_sha256(TIMING_CONTRACT),
        "capture_sha256": capture_value["capture_sha256"],
        "provider_retries": 0, "provider_fallbacks": 0, "response_repairs": 0,
        "postcommit_resubmissions": 0, "scientific_cell_reuse": 0,
    }
    value = value_core | {"bundle_sha256": digest(value_core)}
    write(args.output, value)
    print(json.dumps({
        "roots": len(roots), "jobs": len(value["jobs"]),
        "inventory_sha256": value["inventory_sha256"], "bundle_sha256": value["bundle_sha256"],
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    commands = value.add_subparsers(dest="command", required=True)
    c = commands.add_parser("capture", allow_abbrev=False)
    c.add_argument("--gate0", type=Path, required=True)
    c.add_argument("--output", type=Path, required=True)
    c.add_argument("--python", type=Path, required=True)
    c.add_argument("--vivado", type=Path, required=True)
    c.add_argument("--max-parallel", type=int, default=8)
    c.add_argument("--resume", action="store_true")
    c.add_argument("--hardblock-scan", type=Path, required=True)
    c.set_defaults(function=capture)
    f = commands.add_parser("freeze", allow_abbrev=False)
    f.add_argument("--gate0", type=Path, required=True)
    f.add_argument("--capture", type=Path, required=True)
    f.add_argument("--inventory", type=Path, required=True)
    f.add_argument("--core", type=Path, required=True)
    f.add_argument("--environment", type=Path, required=True)
    f.add_argument("--output", type=Path, required=True)
    f.add_argument("--hardblock-scan", type=Path, required=True)
    f.set_defaults(function=freeze)
    return value


def main() -> int:
    wp014c.configure()
    args = parser().parse_args()
    configure_exact_hardblock_scan(args.hardblock_scan)
    result = args.function(args)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


if __name__ == "__main__":
    raise SystemExit(main())
