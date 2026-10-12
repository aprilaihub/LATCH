#!/usr/bin/env python3
"""Derive an immutable WP-015 physical aggregate after child-path reconciliation.

The source aggregate and per-cell records remain untouched.  This command only
accepts contract-owned child locations whose bytes match the hash recorded by
the completed execution summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from run_wp014i_integrated_preflight import resolve_execution_child


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def git(*args: str) -> str:
    repository = Path(__file__).resolve().parents[1]
    return subprocess.check_output(["git", *args], cwd=repository, text=True).strip()


def reconcile(args: argparse.Namespace) -> int:
    if args.output.exists():
        raise FileExistsError(args.output)
    aggregate = read(args.aggregate)
    source_hash = aggregate.get("aggregate_sha256")
    source_core = {key: value for key, value in aggregate.items() if key != "aggregate_sha256"}
    if source_hash != digest(source_core):
        raise ValueError("wp015_source_aggregate_hash_mismatch")

    rows: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []
    for source_row in aggregate["jobs"]:
        row = dict(source_row)
        if row.get("canonical_terminal") != "VALID_COMPLETION":
            rows.append(row)
            continue
        existing = row.get("resolved_child_path")
        if existing:
            child = Path(str(existing))
            if not child.is_file() or file_sha256(child) != row.get("resolved_child_sha256"):
                raise ValueError("wp015_existing_child_hash_mismatch:" + row["job_id"])
            rows.append(row)
            continue
        if row.get("functional_terminal") is not None or row.get("resolved_child_sha256") is not None:
            raise ValueError("wp015_partial_child_projection:" + row["job_id"])

        execution_path = args.run_root / "jobs" / row["job_id"] / "execution" / "execution-summary.json"
        execution = read(execution_path)
        if execution.get("canonical_endpoint") != "VALID_COMPLETION":
            raise ValueError("wp015_execution_not_valid:" + row["job_id"])
        child = resolve_execution_child(execution, execution_path.parent)
        if child is None:
            raise ValueError("wp015_valid_child_unresolved:" + row["job_id"])
        child_hash = file_sha256(child)
        expected = execution.get("child", {}).get("sha256")
        if child_hash != expected:
            raise ValueError("wp015_reconciled_child_hash_mismatch:" + row["job_id"])
        row.update({
            "resolved_child_path": str(child),
            "resolved_child_sha256": child_hash,
            "functional_terminal": "PENDING_V1",
        })
        reconciliations.append({
            "job_id": row["job_id"],
            "reason": "contract_owned_suffix_vivado_child_path_omitted_by_resolver",
            "execution_summary_path": str(execution_path),
            "execution_summary_sha256": file_sha256(execution_path),
            "resolved_child_path": str(child),
            "resolved_child_sha256": child_hash,
        })
        rows.append(row)

    if len(reconciliations) != args.expected_count:
        raise ValueError(
            f"wp015_reconciliation_count_mismatch:{len(reconciliations)}!={args.expected_count}"
        )
    pending = sum(row.get("functional_terminal") == "PENDING_V1" for row in rows)
    valid = sum(row.get("canonical_terminal") == "VALID_COMPLETION" for row in rows)
    if pending != valid:
        raise ValueError(f"wp015_valid_child_coverage_incomplete:{pending}!={valid}")

    core = dict(source_core)
    core.update({
        "schema_version": "dof.wp015-reconciled-cell-aggregate.v1",
        "source_physical_aggregate_sha256": source_hash,
        "physical_valid_children": pending,
        "jobs": rows,
        "child_path_reconciliation": {
            "schema_version": "dof.wp015-child-path-reconciliation.v1",
            "count": len(reconciliations),
            "rows": reconciliations,
            "repository": {
                "head": git("rev-parse", "HEAD"),
                "tree": git("rev-parse", "HEAD^{tree}"),
            },
            "provider_or_llm_calls": 0,
            "optimization_or_backend_calls": 0,
            "source_records_modified": False,
        },
    })
    result = core | {"aggregate_sha256": digest(core)}
    write(args.output, result)
    print(json.dumps({
        "reconciled_count": len(reconciliations),
        "physical_valid_children": pending,
        "source_physical_aggregate_sha256": source_hash,
        "aggregate_sha256": result["aggregate_sha256"],
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--aggregate", type=Path, required=True)
    value.add_argument("--run-root", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--expected-count", type=int, required=True)
    return value


if __name__ == "__main__":
    raise SystemExit(reconcile(parser().parse_args()))
