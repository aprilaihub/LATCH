#!/usr/bin/env python3
"""Revalidate an immutable synthetic-timing cohort after a shared validator repair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.run_wp014f_validation_recon import file_sha256, object_sha256  # noqa: E402
from scripts.run_wp014i_validation_canary import run_memory_aware, write  # noqa: E402


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def resolve(config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = Path(config["source_run"])
    summary = read(source / "synthetic-timing-recon-summary.json")
    cases = []
    for candidate_id in summary["cohort"]:
        result = read(source / "final" / candidate_id / "final-result.json")
        if result.get("terminal") != "SYNTHETIC_V0_SCORE_READY":
            continue
        dcp = Path(result["final_dcp"])
        sha = result["final_dcp_sha256"]
        if not dcp.is_file() or file_sha256(dcp) != sha:
            raise ValueError("synthetic_revalidation_input_hash_mismatch:" + candidate_id)
        cases.append({
            "case_id": "synthetic_revalidation__" + candidate_id,
            "root_id": candidate_id,
            "memory_class": "high" if dcp.stat().st_size >= 100 * 1024**2 else "normal",
            "mechanism": "golden_self_synthetic_timing_after_validator_repair",
            "golden_dcp": str(dcp),
            "revised_dcp": str(dcp),
            "golden_sha256": sha,
            "revised_sha256": sha,
            "resolved_input_sha256": {"golden": sha, "revised": sha},
            "sealed_holdout_accesses": 0,
            "source_certificate_sha256": object_sha256(
                result["synthetic_timing_certificate"]
            ),
        })
    if not cases:
        raise ValueError("synthetic_revalidation_has_no_v0_ready_cases")
    return summary, cases


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
    limits = config["limits"]
    if any(limits[key] for key in (
        "provider_or_llm_calls", "optimization_calls", "holdout_optimization_calls"
    )):
        raise ValueError("synthetic_revalidation_firewall_not_zero")
    summary, cases = resolve(config)
    plan = read(REPO / "configs/wp014f/validation-recon-v1.json")
    plan["boundaries"]["per_case_timeout_seconds"] = int(
        limits["per_case_timeout_seconds"]
    )
    args.output.mkdir(parents=True)
    write(args.output / "resolved-plan.json", {
        "schema_version": "dof.wp014i-synthetic-revalidation-resolved.v1",
        "identity": config["identity"],
        "config_sha256": file_sha256(args.config),
        "source_summary_sha256": summary["summary_sha256"],
        "repository": {
            "head": git("rev-parse", "HEAD"),
            "tree": git("rev-parse", "HEAD^{tree}"),
        },
        "reason": config["reason"],
        "cases": cases,
    })
    aggregate = run_memory_aware(cases, plan, args.output / "cases", config)
    core = {
        **aggregate,
        "schema_version": "dof.wp014i-synthetic-revalidation-result.v1",
        "identity": config["identity"],
        "reason": config["reason"],
        "source_summary_sha256": summary["summary_sha256"],
        "supersedes_validation_only": config["supersedes_validation_only"],
        "reconstruction_rerun": False,
        "timing_contract_changed": False,
    }
    core.pop("aggregate_sha256", None)
    result = core | {"aggregate_sha256": object_sha256(core)}
    write(args.output / "aggregate.json", result)
    print(json.dumps({
        "executed_cases": result["executed_cases"],
        "terminal_counts": result["terminal_counts"],
        "aggregate_sha256": result["aggregate_sha256"],
    }, sort_keys=True))
    return 0 if (
        result["executed_cases"] == len(cases)
        and result["all_inputs_unchanged"]
        and not result["owned_process_survivors"]
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
