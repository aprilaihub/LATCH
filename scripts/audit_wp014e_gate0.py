#!/usr/bin/env python3
"""Public-safe, read-only WP-014E entry audit.

The audit deliberately records credential presence only.  It does not perform a
provider request, launch a backend, or expose sealed artifact paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


EXPECTED_HEAD = "7bdcc01d5a54b31ac0a80943ee51850e77b8d73d"
EXPECTED_TREE = "06b0b7789a2a60c71602549c04ebd61ac8c2b494"
REQUIRED_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def owned_processes() -> list[dict[str, Any]]:
    markers = (
        "run_wp014e", "wp014e-runs", "validate_dcps.py", "dcp_validation_",
        "run_wp014c_scientific.py", "run_wp014c.py",
    )
    rows = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in {1, os.getpid(), os.getppid()}:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        matched = [marker for marker in markers if marker in command]
        if matched:
            rows.append({"pid": int(entry.name), "markers": matched})
    return sorted(rows, key=lambda row: row["pid"])


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--holdout-roots", type=Path, required=True)
    parser.add_argument("--holdout-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    errors: list[str] = []
    head = git(args.repo, "rev-parse", "HEAD")
    tree = git(args.repo, "rev-parse", "HEAD^{tree}")
    status = git(args.repo, "status", "--porcelain")
    source_tree = git(args.repo, "show", "-s", "--format=%T", EXPECTED_HEAD)
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", EXPECTED_HEAD, head], cwd=args.repo
    ).returncode == 0
    repository = {
        "branch": git(args.repo, "branch", "--show-current"),
        "head": head,
        "tree": tree,
        "expected_source_head": EXPECTED_HEAD,
        "expected_source_tree": EXPECTED_TREE,
        "expected_source_tree_observed": source_tree,
        "source_tree_exact": source_tree == EXPECTED_TREE,
        "source_is_ancestor": ancestor,
        "clean": not status,
    }
    if not repository["source_tree_exact"] or not repository["source_is_ancestor"]:
        errors.append("source_commit_or_tree_mismatch")
    if not repository["clean"]:
        errors.append("worktree_not_clean")

    credential_presence = {name: bool(os.environ.get(name)) for name in REQUIRED_KEYS}
    missing = [name for name, present in credential_presence.items() if not present]
    if missing:
        errors.append("required_credentials_missing")

    roots = read(args.roots)
    root_prefix = Path(str(roots["root_path_prefix"]))
    development = []
    for row in roots["roots"]:
        path = root_prefix / str(row["filename"])
        observed = file_digest(path) if path.is_file() else None
        development.append({
            "root_id": row["root_id"],
            "expected_sha256": row["sha256"],
            "observed_sha256": observed,
            "matches": observed == row["sha256"],
        })
    if not all(row["matches"] for row in development):
        errors.append("development_root_hash_mismatch")

    holdout_paths = read(args.holdout_roots)["roots"]
    holdout_expected = {
        row["benchmark"]: row["artifact"]["sha256"]
        for row in read(args.holdout_audit)["rows"]
    }
    holdouts = []
    for pseudonym, raw_path in sorted(holdout_paths.items()):
        path = Path(str(raw_path))
        observed = file_digest(path) if path.is_file() else None
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

    live = owned_processes()
    if live:
        errors.append("owned_processes_live")

    # These are source-level reproductions of the blockers named by WP-014E.
    action_surface = (args.repo / "optimizer_pipeline/dof/single_submission/action_surface_v3.py").read_text()
    language = (args.repo / "optimizer_pipeline/dof/single_submission/language.py").read_text()
    runtime = (args.repo / "optimizer_pipeline/dof/runtime.py").read_text()
    scorer = (args.repo / "optimizer_pipeline/apps/fpl_contest/official_score.py").read_text()
    blocker_reproduction = {
        "embedded_call_macro_schema_uses_action_key": 'closed_object({"action_key": _ACTION_KEY}' in action_surface,
        "embedded_call_macro_compiler_uses_legacy_macro_field": '_exact_fields(args, {"macro"}, op)' in language,
        "embedded_call_macro_registry_cost_is_fixed_two": 'worker_operation=None, heavy=2, boundary="M"' in action_surface,
        "runtime_has_strict_functional_terminal": "FUNCTIONAL_PASS_200_VECTOR" in runtime,
        "runtime_has_boundary_runtime_exercised": "boundary_runtime_exercised" in runtime,
        "official_scorer_has_byte_identical_hash_guard": (
            "byte_identical" in scorer or "input_sha256 == output_sha256" in scorer
        ),
        "official_scorer_has_frozen_epsilon_fmax": "epsilon_fmax" in scorer,
    }

    core = {
        "schema_version": "dof.wp014e-gate0-entry-audit.v1",
        "accepted_for_execution": not errors,
        "errors": errors,
        "stop_condition": "MISSING_REQUIRED_CREDENTIAL" if missing else None,
        "repository": repository,
        "bundle": {
            "name": args.bundle.name,
            "sha256": file_digest(args.bundle),
            "manifest_hashes_verified_before_audit": True,
        },
        "credential_presence": credential_presence,
        "missing_credentials": missing,
        "secret_values_recorded": False,
        "development_roots": development,
        "sealed_holdouts": holdouts,
        "sealed_holdouts_optimised": False,
        "owned_processes": live,
        "blocker_reproduction": blocker_reproduction,
        "provider_requests": 0,
        "backend_dispatches": 0,
        "openrouter_fallback_substitution": 0,
    }
    value = core | {"audit_sha256": digest(core)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({
        "accepted_for_execution": value["accepted_for_execution"],
        "errors": errors,
        "missing_credentials": missing,
        "audit_sha256": value["audit_sha256"],
    }, sort_keys=True))
    return 0 if value["accepted_for_execution"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
