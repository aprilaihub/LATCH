#!/usr/bin/env python3
"""Create and apply immutable five-seed V1 plans for WP-015 cell batches."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


SEEDS = [0xC0FFEE, 0x5EED1234, 0x1BADB002, 0xD15EA5E, 0xA11CE55]


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


def plan(args: argparse.Namespace) -> int:
    aggregate, bundle = read(args.aggregate), read(args.bundle)
    if (
        aggregate.get("bundle_sha256") != bundle.get("bundle_sha256")
        or aggregate.get("shared_scientific_core_hash") != bundle.get("shared_scientific_core_hash")
    ):
        raise ValueError("wp015_validation_source_binding_mismatch")
    cases = []
    for row in aggregate["jobs"]:
        if row.get("functional_terminal") != "PENDING_V1":
            continue
        child = Path(row["resolved_child_path"])
        root = bundle["roots"][row["root_id"]]
        if not child.is_file() or file_sha256(child) != row["resolved_child_sha256"]:
            raise ValueError("wp015_validation_child_hash_mismatch:" + row["job_id"])
        cases.append({
            "case_id": "wp015__" + row["job_id"], "job_id": row["job_id"],
            "root_id": row["root_id"],
            "memory_class": "high" if row["resource_class"] in {"LARGE", "VERY_LARGE"} else "normal",
            "mechanism": row.get("selected_language"),
            "golden_dcp": root["root_path"], "revised_dcp": str(child),
            "golden_sha256": root["sha256"], "revised_sha256": row["resolved_child_sha256"],
            "resolved_input_sha256": {
                "golden": root["sha256"], "revised": row["resolved_child_sha256"],
            },
        })
    core = {
        "schema_version": "dof.wp015-cell-validation-plan.v1",
        "source_aggregate_sha256": aggregate["aggregate_sha256"],
        "bundle_sha256": bundle["bundle_sha256"],
        "shared_scientific_core_hash": bundle["shared_scientific_core_hash"],
        "case_count": len(cases), "cases": cases,
        "functional": {"precheck_vectors": 100, "vectors_per_seed": 200, "seeds": SEEDS},
        "limits": {
            "preparation_parallel": 16, "seed_parallel": 16,
            "reserve_fraction": 0.20, "per_case_timeout_seconds": 10800,
            "per_seed_timeout_seconds": 3600,
        },
        "repository": {
            "head": aggregate["scientific_source_commit"],
            "tree": aggregate["scientific_source_tree"],
        },
        "provider_or_llm_calls": 0, "optimization_calls": 0,
        "transfer_or_holdout_accesses": 0,
    }
    result = core | {"plan_sha256": digest(core)}
    write(args.output, result)
    print(json.dumps({"case_count": len(cases), "plan_sha256": result["plan_sha256"]}, sort_keys=True))
    return 0


def apply(args: argparse.Namespace) -> int:
    aggregate, plan_value, validation = read(args.aggregate), read(args.plan), read(args.validation)
    if (
        plan_value.get("source_aggregate_sha256") != aggregate.get("aggregate_sha256")
        or validation.get("plan_sha256") != plan_value.get("plan_sha256")
        or len(validation.get("case_aggregates", {})) != plan_value.get("case_count")
    ):
        raise ValueError("wp015_validation_aggregate_binding_mismatch")
    cases = {row["case_id"]: row for row in plan_value["cases"]}
    by_job = {
        cases[case_id]["job_id"]: item
        for case_id, item in validation["case_aggregates"].items()
    }
    expected = {row["job_id"] for row in aggregate["jobs"] if row.get("functional_terminal") == "PENDING_V1"}
    if set(by_job) != expected:
        raise ValueError("wp015_validation_job_set_mismatch")
    rows = []
    for row in aggregate["jobs"]:
        item = by_job.get(row["job_id"])
        if item is None:
            rows.append(row)
            continue
        terminal = item["terminal"]
        rows.append(dict(row) | {
            "functional_terminal": terminal,
            "functional_valid": terminal == "MULTISEED_FUNCTIONAL_PASS",
            "functional_validation_case_sha256": item.get("semantic_result_sha256"),
            "functional_all_seeds_present_exactly_once": item.get("all_seeds_present_exactly_once"),
        })
    core = {
        **{key: value for key, value in aggregate.items() if key not in {"jobs", "aggregate_sha256"}},
        "schema_version": "dof.wp015-functional-cell-aggregate.v1",
        "source_physical_aggregate_sha256": aggregate["aggregate_sha256"],
        "validation_plan_sha256": plan_value["plan_sha256"],
        "validation_aggregate_sha256": validation["aggregate_sha256"],
        "functional_terminal_counts": dict(sorted(collections.Counter(
            str(row.get("functional_terminal")) for row in rows if row.get("functional_terminal")
        ).items())),
        "functional_valid_children": sum(row.get("functional_valid") is True for row in rows),
        "jobs": rows,
    }
    result = core | {"aggregate_sha256": digest(core)}
    write(args.output, result)
    print(json.dumps({
        "job_count": len(rows), "functional_terminal_counts": result["functional_terminal_counts"],
        "functional_valid_children": result["functional_valid_children"],
        "aggregate_sha256": result["aggregate_sha256"],
    }, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    commands = value.add_subparsers(dest="command", required=True)
    p = commands.add_parser("plan", allow_abbrev=False)
    p.add_argument("--aggregate", type=Path, required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(function=plan)
    a = commands.add_parser("apply", allow_abbrev=False)
    a.add_argument("--aggregate", type=Path, required=True)
    a.add_argument("--plan", type=Path, required=True)
    a.add_argument("--validation", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    a.set_defaults(function=apply)
    return value


if __name__ == "__main__":
    parsed = parser().parse_args()
    raise SystemExit(parsed.function(parsed))
