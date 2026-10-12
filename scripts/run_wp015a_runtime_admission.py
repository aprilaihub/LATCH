#!/usr/bin/env python3
"""Execute one bounded round of model-generated strict-G candidates."""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
from pathlib import Path
import sys
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp013a_e0 as base  # noqa: E402
import run_wp014c_scientific as wp014c  # noqa: E402
import run_wp015_cells as cells  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import (  # noqa: E402
    boundary_evidence, strict_boundary_certificate,
)
from scripts.run_wp014h_strict_witnesses import mechanism_deltas  # noqa: E402
from scripts.run_wp014i_integrated_preflight import resolve_execution_child  # noqa: E402


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def select(
    admission: Mapping[str, Any], order: Mapping[str, Any], *, candidate_index: int,
) -> list[dict[str, Any]]:
    selected = []
    for family, models in order["orders"].items():
        valid = {
            row["model_key"]: row for row in admission["rows"]
            if row["strict_mechanism"] == family and row["strict_candidate_valid"]
        }
        eligible = [valid[model] for model in models if model in valid]
        if len(eligible) <= candidate_index:
            raise ValueError("wp015a_runtime_candidate_exhausted:" + family)
        selected.append(dict(eligible[candidate_index]))
    return selected


async def run(args: argparse.Namespace) -> int:
    wp014c.configure()
    bundle, admission, order = read(args.bundle), read(args.admission), read(args.order)
    if admission.get("status") != "PASS":
        raise ValueError("wp015a_gate2_not_passed")
    rows = select(admission, order, candidate_index=args.candidate_index)
    args.output.mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def one(row: Mapping[str, Any]) -> dict[str, Any]:
        family = row["strict_mechanism"]
        source = args.admission.parent / "planning" / row["job_id"] / "private" / "committed-source.json"
        candidate = read(source)
        registry, compiled = cells._compile_saved_candidate(row, bundle, args.capture, candidate)
        case_id = "wp015a-runtime__" + cells.digest({"family": family, "model": row["model_key"], "round": args.candidate_index})[:24]
        execution_dir = args.output / "cases" / case_id / "execution"
        async with semaphore:
            execution = await base._execute_typed(
                compiled=compiled, registry=registry,
                root=Path(bundle["roots"][row["root_id"]]["root_path"]),
                out=execution_dir, python=args.python, vivado=args.vivado,
            )
        planned = strict_boundary_certificate(candidate=candidate, execution_started=False, execution=None)
        witness_id = cells.STRICT_WITNESS_IDS[family]
        execution["binding_records"] = [dict(item) for item in compiled.binding_records]
        execution["mechanism_state_deltas"] = mechanism_deltas(execution_dir, witness_id)
        cells.write(execution_dir / "execution-summary.json", execution)
        strict = strict_boundary_certificate(candidate=candidate, execution_started=True, execution=execution)
        causal = boundary_evidence(candidate=candidate, execution_started=True, execution=execution)
        child = resolve_execution_child(execution, execution_dir)
        return dict(row) | {
            "job_id": case_id,
            "runtime_round": args.candidate_index,
            "selected_language": "G",
            "canonical_terminal": execution["canonical_endpoint"],
            "physical_terminal": execution["canonical_endpoint"],
            "execution_started": True,
            "execution": execution,
            "planned_strict_boundary_certificate": planned,
            "strict_boundary_certificate": strict,
            "boundary_evidence": causal,
            "actual_heavy_calls": int(execution.get("actual_heavy_calls") or 0),
            "actual_light_mutations": execution.get("actual_light_mutations"),
            "resolved_child_path": str(child) if child else None,
            "resolved_child_sha256": cells.file_sha256(child) if child else None,
            "functional_terminal": "PENDING_V1" if child and execution["canonical_endpoint"] == "VALID_COMPLETION" else None,
            "committed_candidate": candidate,
            "candidate_sha256": cells.digest(candidate),
        }

    results = await asyncio.gather(*(one(row) for row in rows))
    core = {
        "schema_version": "dof.wp015a-runtime-admission-round.v1",
        "development_only": True,
        "candidate_index": args.candidate_index,
        "job_count": len(results),
        "terminal_counts": dict(collections.Counter(row["canonical_terminal"] for row in results)),
        "strict_runtime_exercised": sum(bool(row["strict_boundary_certificate"].get("strict_boundary_exercised")) for row in results),
        "physical_valid_children": sum(row["functional_terminal"] == "PENDING_V1" for row in results),
        "actual_heavy_calls": sum(row["actual_heavy_calls"] for row in results),
        "provider_requests": 0,
        "bundle_sha256": bundle["bundle_sha256"],
        "shared_scientific_core_hash": bundle["shared_scientific_core_hash"],
        "scientific_source_commit": bundle["repository"]["head_commit"],
        "scientific_source_tree": bundle["repository"]["head_tree"],
        "jobs": results,
    }
    result = core | {"aggregate_sha256": cells.digest(core)}
    cells.write(args.output / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in ("terminal_counts", "strict_runtime_exercised", "physical_valid_children", "actual_heavy_calls")}, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--admission", type=Path, required=True)
    value.add_argument("--order", type=Path, required=True)
    value.add_argument("--capture", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--vivado", type=Path, required=True)
    value.add_argument("--candidate-index", type=int, choices=(0, 1), required=True)
    value.add_argument("--max-parallel", type=int, default=3)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
