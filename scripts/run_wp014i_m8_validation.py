#!/usr/bin/env python3
"""Run fresh five-seed validation for the immutable 10xM8 reference panel."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.run_wp014f_validation_recon import file_sha256, object_sha256  # noqa: E402
from scripts.run_wp014i_validation_canary import run_memory_aware, write  # noqa: E402


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def resolve(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows_path = Path(config["panel_rows"])
    if file_sha256(rows_path) != config["panel_rows_sha256"]:
        raise ValueError("m8_panel_rows_hash_mismatch")
    panel_rows = read(rows_path)
    manifest = read(REPO / config["development_roots_manifest"])
    root_prefix = Path(manifest["root_path_prefix"])
    roots = {
        row["root_id"]: {
            **row, "path": root_prefix / row["filename"]
        } for row in manifest["roots"]
    }
    cases = []
    for row in panel_rows:
        if row.get("canonical_endpoint") != "VALID_COMPLETION":
            continue
        root = roots[row["root_id"]]
        root_path = Path(root["path"])
        if not root_path.is_file() or file_sha256(root_path) != root["sha256"]:
            raise ValueError("m8_root_hash_mismatch:" + row["root_id"])
        outputs = sorted((Path(config["panel_root"]) / "jobs" / row["job_id"] / "outputs").glob("*.dcp"))
        if len(outputs) != 1:
            raise ValueError("m8_child_not_unique:" + row["job_id"])
        child = outputs[0]
        child_sha = file_sha256(child)
        cases.append({
            "case_id": "m8__" + row["job_id"],
            "root_id": row["root_id"],
            "memory_class": "normal",
            "mechanism": row["flow_name"],
            "golden_dcp": str(root_path),
            "revised_dcp": str(child),
            "golden_sha256": root["sha256"],
            "revised_sha256": child_sha,
            "resolved_input_sha256": {"golden": root["sha256"], "revised": child_sha},
            "sealed_holdout_accesses": 0,
            "physical_result": {
                "final_fmax_mhz": row["final_fmax_mhz"],
                "initial_fmax_mhz": row["initial_fmax_mhz"],
                "fmax_improvement_mhz": row["fmax_improvement_mhz"],
                "canonical_endpoint": row["canonical_endpoint"],
            },
        })
    if len(cases) != 80 or len({row["case_id"] for row in cases}) != 80:
        raise ValueError("m8_reference_requires_exactly_80_valid_children")
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    config = read(args.config)
    if config["limits"]["provider_or_llm_calls"] or config["limits"]["optimization_calls"]:
        raise ValueError("m8_validation_firewall_not_zero")
    cases = resolve(config)
    plan = read(REPO / "configs/wp014f/validation-recon-v1.json")
    plan["boundaries"]["per_case_timeout_seconds"] = int(
        config["limits"]["per_case_timeout_seconds"]
    )
    args.output.mkdir(parents=True)
    write(args.output / "resolved-plan.json", {
        "schema_version": "dof.wp014i-m8-validation-resolved.v1",
        "config_sha256": file_sha256(args.config),
        "repository": {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")},
        "cases": cases,
    })
    aggregate = run_memory_aware(cases, plan, args.output / "cases", config)
    write(args.output / "cases" / "aggregate.json", aggregate)
    core = {
        "schema_version": "dof.wp014i-m8-validation-summary.v1",
        "identity": config["identity"],
        "planned_cases": 80,
        "executed_cases": aggregate["executed_cases"],
        "terminal_counts": aggregate["terminal_counts"],
        "queue_wall_seconds": aggregate["queue_wall_seconds"],
        "all_inputs_unchanged": aggregate["all_inputs_unchanged"],
        "owned_process_survivors": aggregate["owned_process_survivors"],
        "aggregate_sha256": aggregate["aggregate_sha256"],
        "provider_or_llm_calls": 0,
        "optimization_calls": 0,
    }
    summary = core | {"summary_sha256": object_sha256(core)}
    write(args.output / "summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0 if (
        summary["executed_cases"] == 80
        and not summary["owned_process_survivors"]
        and summary["all_inputs_unchanged"]
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
