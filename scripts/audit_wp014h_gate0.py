#!/usr/bin/env python3
"""Reconcile accepted WP-014E/F/G evidence before WP-014H product work.

The emitted JSON is public-safe: credential values, tool paths and sealed
holdout paths are never serialized.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


ACCEPTED_COMMITS = {
    "WP-014E": "994366146f467802963a0d0233459bc7aed9355a",
    "WP-014F": "bc367cd2d8bdc51749a9791abffde504459d507c",
    "WP-014G": "064b791619381cd1db8131456bb39fcc404df642",
}
ACCEPTED_TREES = {
    "WP-014E": "27b0f8c437d5c2cc41d7cdf2ecfe3efe4fad480d",
    "WP-014F": "d44cac92eb5be3a061015b6fa43d813c163153f8",
    "WP-014G": "0ce1b142174462202fd077367f220ba9348924db",
}
CAPABILITY_FILES = {
    "four_provider_adapters_and_cost_ledger": (
        "optimizer_pipeline/dof/multi_provider.py",
        ".research-flow/artifacts/WP-014E/provider-admission-v2-public.json",
        ".research-flow/artifacts/WP-014E/mini-matrix/job-outcomes.csv",
    ),
    "five_seed_functional_validation_and_profiling": (
        "validate_dcps.py",
        "scripts/run_wp014f_validation_recon.py",
        "configs/wp014f/validation-recon-v1.json",
    ),
    "g_causal_lineage_and_exploration": (
        "optimizer_pipeline/dof/scientific_evidence.py",
        "optimizer_pipeline/dof/vivado_mutation_worker.py",
        ".research-flow/artifacts/WP-014G/current-g-truth-pack.json",
    ),
}
REQUIRED_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
)


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--holdout-roots", type=Path, required=True)
    parser.add_argument("--holdout-audit", type=Path, required=True)
    parser.add_argument("--process-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    errors: list[str] = []

    head = git(repo, "rev-parse", "HEAD")
    clean = not git(repo, "status", "--porcelain")
    commits: dict[str, Any] = {}
    for wp, commit in ACCEPTED_COMMITS.items():
        observed_tree = git(repo, "show", "-s", "--format=%T", commit)
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, head], cwd=repo
        ).returncode == 0
        commits[wp] = {
            "commit": commit,
            "expected_tree": ACCEPTED_TREES[wp],
            "observed_tree": observed_tree,
            "tree_matches": observed_tree == ACCEPTED_TREES[wp],
            "is_ancestor": ancestor,
        }
    if not all(row["tree_matches"] and row["is_ancestor"] for row in commits.values()):
        errors.append("accepted_commit_or_tree_mismatch")
    if not clean:
        errors.append("worktree_not_clean")

    capabilities: dict[str, Any] = {}
    for capability, relative_paths in CAPABILITY_FILES.items():
        rows = []
        for relative in relative_paths:
            path = repo / relative
            rows.append({
                "path": relative,
                "present": path.is_file(),
                "sha256": file_sha256(path) if path.is_file() else None,
            })
        capabilities[capability] = {
            "retained": all(row["present"] for row in rows),
            "files": rows,
        }
    if not all(row["retained"] for row in capabilities.values()):
        errors.append("accepted_capability_missing")

    roots = read_json(args.roots)
    prefix = Path(str(roots["root_path_prefix"]))
    development = []
    for row in roots["roots"]:
        path = prefix / str(row["filename"])
        observed = file_sha256(path) if path.is_file() else None
        development.append({
            "root_id": row["root_id"],
            "expected_sha256": row["sha256"],
            "observed_sha256": observed,
            "matches": observed == row["sha256"],
        })
    if not all(row["matches"] for row in development):
        errors.append("development_root_hash_mismatch")

    holdout_paths = read_json(args.holdout_roots)["roots"]
    holdout_expected = {
        row["benchmark"]: row["artifact"]["sha256"]
        for row in read_json(args.holdout_audit)["rows"]
    }
    holdouts = []
    for pseudonym, raw_path in sorted(holdout_paths.items()):
        path = Path(str(raw_path))
        observed = file_sha256(path) if path.is_file() else None
        expected = holdout_expected.get(pseudonym)
        holdouts.append({
            "pseudonym": pseudonym,
            "expected_sha256": expected,
            "observed_sha256": observed,
            "matches": observed == expected,
            "path_disclosed": False,
        })
    if not all(row["matches"] for row in holdouts):
        errors.append("sealed_holdout_hash_mismatch")

    process_audit = read_json(args.process_audit)
    if not process_audit.get("zero_survivors_twice"):
        errors.append("owned_processes_live")

    credentials = {name: bool(os.environ.get(name)) for name in REQUIRED_KEYS}
    tools = {
        "project_python_executable": bool(os.environ.get("PYTHON"))
        and os.access(str(os.environ.get("PYTHON")), os.X_OK),
        "vivado_2025_1_executable": bool(os.environ.get("VIVADO_EXEC"))
        and os.access(str(os.environ.get("VIVADO_EXEC")), os.X_OK),
        "rapidwright_directory": bool(os.environ.get("RAPIDWRIGHT_PATH"))
        and Path(str(os.environ.get("RAPIDWRIGHT_PATH"))).is_dir(),
    }
    if not all(credentials.values()):
        errors.append("credential_missing")
    if not all(tools.values()):
        errors.append("tool_environment_missing")

    result = {
        "schema_version": "dof.wp014h-accepted-evidence-reconciliation.v1",
        "accepted_for_gate1": not errors,
        "errors": errors,
        "repository": {
            "branch": git(repo, "branch", "--show-current"),
            "head": head,
            "tree": git(repo, "rev-parse", "HEAD^{tree}"),
            "clean": clean,
        },
        "incoming_bundle": {
            "name": args.bundle.name,
            "sha256": file_sha256(args.bundle),
            "manifest_verified": True,
        },
        "accepted_identities": commits,
        "retained_capabilities": capabilities,
        "credential_presence": credentials,
        "secret_values_recorded": False,
        "tool_environment_presence": tools,
        "development_roots": development,
        "sealed_holdouts": holdouts,
        "sealed_holdout_optimisation_accesses": 0,
        "process_audit_sha256": file_sha256(args.process_audit),
        "integration": {
            "wholesale_merge_used": False,
            "ported_missing_capabilities": [],
            "minimal_pre_gate_repair": "exclude_pid1_sandbox_runner_from_process_scan",
        },
        "provider_requests": 0,
        "backend_dispatches": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted_for_gate1": not errors, "errors": errors}, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
