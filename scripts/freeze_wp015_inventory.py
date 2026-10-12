#!/usr/bin/env python3
"""Generate the exact immutable 1,134-cell WP-015 Lean inventory."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
MODELS_PATH = REPO / ".research-flow/source/WP015-Model-and-Provider-Freeze-v1.json"
MATRIX_PATH = REPO / ".research-flow/source/WP015-Lean-Matrix-v1.json"
PORTFOLIO_PATH = REPO / ".research-flow/source/WP015-Benchmark-Portfolio-Freeze-v1.json"
PROTOCOL_ID = "WP015_LEAN_R1_V3"


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def generate(
    *, gate0: Mapping[str, Any], stress: Mapping[str, Any],
    models_value: Mapping[str, Any], matrix: Mapping[str, Any],
    portfolio: Mapping[str, Any], core_hash: str,
) -> list[dict[str, Any]]:
    roots = {row["root_id"]: row for row in gate0["roots"]}
    portfolio_by_root = {row["root_id"]: row for row in portfolio["roots"]}
    if gate0.get("status") != "PASS" or set(roots) != set(portfolio_by_root) or len(roots) != 25:
        raise ValueError("wp015_inventory_root_reconciliation_invalid")
    models = models_value["models"]
    if [row["model_key"] for row in models] != matrix["models"]:
        raise ValueError("wp015_inventory_model_order_mismatch")
    stress_rows = {row["root_id"]: row for row in stress["selected_stress_roots"]}
    if len(stress_rows) != 4 or set(stress_rows) != set(stress["selected_root_ids"]):
        raise ValueError("wp015_inventory_stress_roots_invalid")
    jobs = []

    def append(root_id: str, model: Mapping[str, Any], track: str, condition: str,
               level: str, coverage: str, admitted: list[str], repeat_id: str,
               mask_hash: str) -> None:
        public_condition = condition
        language_condition = {
            "L2": "FREE_CHOICE", "L3": "FREE_CHOICE", "L4": "FREE_CHOICE",
            "M_ONLY": "M_ONLY", "P_BOUNDARY": "P_BOUNDARY",
            "G_STRICT_BOUNDARY": "G_BOUNDARY",
        }[condition]
        agent_level = level if track != "C_CONDITIONED" else "C"
        identity = {
            "protocol_id": PROTOCOL_ID, "repeat_id": repeat_id,
            "root_id": root_id, "model_key": model["model_key"],
            "track": track, "condition": public_condition,
        }
        root = roots[root_id]
        portfolio_row = portfolio_by_root[root_id]
        jobs.append({
            "job_id": "wp015__" + digest(identity)[:32], **identity,
            "root_pseudonym": "design-" + digest({"protocol": matrix["protocol_id"], "root": root_id})[:16],
            "root_dcp": root["dcp"], "root_sha256": root["sha256"],
            "family": portfolio_row["family"], "split": portfolio_row["split"],
            "timing_stratum": portfolio_row["timing_stratum"],
            "resource_class": portfolio_row["resource_class"],
            "provider": model["provider"], "api_model_id": model["api_model_id"],
            "credential_env": model["credential"], "track": track,
            "condition": public_condition, "language_condition": language_condition,
            "level": level, "agent_level": agent_level,
            "coverage_regime": coverage, "admitted_action_keys": admitted,
            "mask_evidence_hash": mask_hash,
            "strict_g_required": condition == "G_STRICT_BOUNDARY",
            "shared_scientific_core_hash": core_hash,
            "scientific_cell_reuse": False,
        })

    m10 = list(stress["m10"])
    full_hash = digest({"coverage_regime": "FULL_M10", "actions": m10})
    for root_id in matrix["roots"]:
        for model in models:
            for level in matrix["neutral_conditions"]:
                append(root_id, model, "N_NEUTRAL", level, level, "FULL_M10", m10,
                       matrix["repeat_id"], full_hash)
            for condition, level in (("M_ONLY", "L2"), ("P_BOUNDARY", "L3"),
                                     ("G_STRICT_BOUNDARY", "L4")):
                append(root_id, model, "C_CONDITIONED", condition, level, "FULL_M10", m10,
                       matrix["repeat_id"], full_hash)
    for root_id in stress["selected_root_ids"]:
        mask = stress_rows[root_id]
        mask_hash = digest(mask)
        for model in models:
            for level in matrix["stress"]["conditions"]:
                append(root_id, model, "S_NEUTRAL_REDUCED", level, level,
                       "REDUCED_M_STRESS", list(mask["retained_action_keys"]),
                       matrix["stress"]["repeat_id"], mask_hash)
    return jobs


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--gate0", type=Path, required=True)
    parser.add_argument("--stress", type=Path, required=True)
    parser.add_argument("--core-hash", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    gate0, stress = read(args.gate0), read(args.stress)
    models_value, matrix, portfolio = read(MODELS_PATH), read(MATRIX_PATH), read(PORTFOLIO_PATH)
    jobs = generate(
        gate0=gate0, stress=stress, models_value=models_value, matrix=matrix,
        portfolio=portfolio, core_hash=args.core_hash,
    )
    if len(jobs) != 1134 or len({row["job_id"] for row in jobs}) != 1134:
        raise ValueError("wp015_inventory_not_exact_1134_unique_jobs")
    if sum(row["track"] != "S_NEUTRAL_REDUCED" for row in jobs) != 1050:
        raise ValueError("wp015_core_inventory_not_1050")
    if sum(row["track"] == "S_NEUTRAL_REDUCED" for row in jobs) != 84:
        raise ValueError("wp015_stress_inventory_not_84")
    counts = collections.Counter((row["track"], row["condition"]) for row in jobs)
    core = {
        "schema_version": "dof.wp015-frozen-inventory.v1",
        "protocol_id": PROTOCOL_ID, "base_protocol_id": matrix["protocol_id"],
        "shared_scientific_core_hash": args.core_hash,
        "repository": {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")},
        "source_hashes": {
            "gate0": file_sha256(args.gate0), "stress": file_sha256(args.stress),
            "models": file_sha256(MODELS_PATH), "matrix": file_sha256(MATRIX_PATH),
            "portfolio": file_sha256(PORTFOLIO_PATH),
        },
        "job_count": len(jobs), "core_job_count": 1050, "stress_job_count": 84,
        "condition_counts": {f"{key[0]}::{key[1]}": value for key, value in sorted(counts.items())},
        "repeat_ids": sorted({row["repeat_id"] for row in jobs}),
        "jobs": jobs, "provider_retries": 0, "provider_fallbacks": 0,
        "response_repairs": 0, "scientific_cell_reuse": 0,
    }
    result = core | {"inventory_sha256": digest(core)}
    write(args.output, result)
    print(json.dumps({
        "job_count": result["job_count"], "core_job_count": 1050,
        "stress_job_count": 84, "condition_counts": result["condition_counts"],
        "inventory_sha256": result["inventory_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
