#!/usr/bin/env python3
"""Audit the bounded three-family WP-015B runtime admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


FAMILIES = {
    "SPATIAL_GENERATED_GEOMETRY",
    "NONPREFIX_MULTI_NET_BATCH",
    "COUPLED_HARDBLOCK_SITE_ALLOCATION",
}


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def model_family(model: str) -> str:
    if model.startswith("openai/"):
        return "openai_gpt56"
    if model.startswith("deepseek/"):
        return "deepseek_v4"
    return model.split("/", 1)[0]


def run(args: argparse.Namespace) -> int:
    value = read(args.aggregate)
    rows = value.get("jobs", [])
    by_family = {row.get("strict_mechanism"): row for row in rows}
    contributors = {model_family(str(row["model_key"])) for row in rows}
    checks = {
        "exact_three_families": set(by_family) == FAMILIES and len(rows) == 3,
        "all_valid_completion": all(row.get("canonical_terminal") == "VALID_COMPLETION" for row in rows),
        "all_runtime_strict": all(
            row.get("strict_boundary_certificate", {}).get("strict_boundary_exercised") is True
            for row in rows
        ),
        "all_nonempty_physical_effect": all(
            int(row.get("execution", {}).get("transformed_count") or 0) > 0 for row in rows
        ),
        "all_five_seed_v1_pass": all(
            row.get("functional_terminal") == "MULTISEED_FUNCTIONAL_PASS"
            and row.get("functional_valid") is True for row in rows
        ),
        "two_model_families": len(contributors) >= 2,
        "bounded_candidates": all(int(row.get("runtime_round") or 0) in {0, 1} for row in rows),
        "provider_requests_zero": int(value.get("provider_requests") or 0) == 0,
        "heavy_call_bound": int(value.get("actual_heavy_calls") or 0) <= 80,
    }
    core = {
        "schema_version": "dof.wp015b-gate4-audit.v1",
        "status": "PASS" if all(checks.values()) else "STOP_SHARED_GATE",
        "source_aggregate_sha256": value["aggregate_sha256"],
        "checks": checks, "families": sorted(FAMILIES),
        "model_families": sorted(contributors),
        "actual_heavy_calls": value.get("actual_heavy_calls"),
        "functional_terminal_counts": value.get("functional_terminal_counts"),
    }
    result = core | {"audit_sha256": digest(core)}
    write(args.output, result)
    print(json.dumps({"status": result["status"], "checks": checks}, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--aggregate", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
