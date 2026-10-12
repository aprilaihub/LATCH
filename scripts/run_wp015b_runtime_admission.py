#!/usr/bin/env python3
"""Execute bounded final-source WP-015B strict-mechanism candidates.

The runner never calls a model.  Spatial/net candidates come from the accepted
rows of WP-015A; hardblock candidates come only from the exact-relation WP-015B
admission.  Candidate order is frozen before execution and is QoR-independent.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

import run_wp013a_e0 as base  # noqa: E402
import run_wp015_cells as cells  # noqa: E402
from optimizer_pipeline.dof.scientific_evidence import (  # noqa: E402
    boundary_evidence, strict_boundary_certificate,
)
from optimizer_pipeline.dof.single_submission.observation_v3 import CommonQuerySurfaceV3  # noqa: E402
from scripts.run_wp014h_strict_witnesses import mechanism_deltas  # noqa: E402
from scripts.run_wp014i_integrated_preflight import resolve_execution_child  # noqa: E402


FAMILIES = (
    "SPATIAL_GENERATED_GEOMETRY",
    "NONPREFIX_MULTI_NET_BATCH",
    "COUPLED_HARDBLOCK_SITE_ALLOCATION",
)


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def model_family(model: str) -> str:
    if model.startswith("openai/"):
        return "openai_gpt56"
    if model.startswith("deepseek/"):
        return "deepseek_v4"
    return model.split("/", 1)[0]


def candidate_rows(
    legacy_admission: Mapping[str, Any], hardblock_admission: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    if hardblock_admission.get("status") != "PASS":
        raise ValueError("wp015b_gate3_not_passed")
    rows: dict[str, list[dict[str, Any]]] = {family: [] for family in FAMILIES}
    for item in legacy_admission.get("rows", ()):
        family = item.get("strict_mechanism")
        if family in FAMILIES[:2] and item.get("strict_candidate_valid") is True:
            rows[str(family)].append(dict(item) | {"candidate_admission": "wp015a"})
    for item in hardblock_admission.get("rows", ()):
        if (
            item.get("strict_mechanism") == FAMILIES[2]
            and item.get("strict_candidate_valid") is True
        ):
            rows[FAMILIES[2]].append(dict(item) | {"candidate_admission": "wp015b"})
    return rows


def select(
    legacy_admission: Mapping[str, Any], hardblock_admission: Mapping[str, Any],
    order: Mapping[str, Any], *, candidate_index: int, families: tuple[str, ...],
) -> list[dict[str, Any]]:
    available = candidate_rows(legacy_admission, hardblock_admission)
    selected = []
    for family in families:
        valid = {row["model_key"]: row for row in available[family]}
        eligible = [valid[model] for model in order["orders"][family] if model in valid]
        if len(eligible) <= candidate_index:
            raise ValueError("wp015b_runtime_candidate_exhausted:" + family)
        selected.append(eligible[candidate_index])
    return selected


def candidate_source(
    row: Mapping[str, Any], legacy_admission_path: Path, hardblock_admission_path: Path,
) -> Path:
    parent = (
        hardblock_admission_path.parent
        if row["candidate_admission"] == "wp015b"
        else legacy_admission_path.parent
    )
    return parent / "planning" / row["job_id"] / "private" / "committed-source.json"


def replay_saved_queries(
    *, registry: Any, private: Mapping[str, Any], planning_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Recreate the exact planning-time registry additions without tool calls."""

    surface = CommonQuerySurfaceV3(registry)
    replayed = []
    for turn in planning_result.get("turns", ()):
        batch = turn.get("planner_result", {}).get("query_batch")
        if not isinstance(batch, Mapping):
            continue
        for item in batch.get("items", ()):
            request = item["request"]
            name, top_k = str(request["name"]), int(request["top_k"])
            result = surface.materialize(
                query_name=name, top_k=top_k,
                private_rows=private["rows"].get(name, ()),
                tool_call_ids=(base.QUERY_EVIDENCE_TAG,), latency_seconds=0.0,
                missing_reason=private["missing_reasons"].get(name),
            )
            if result.sha256 != item["result_sha256"]:
                raise ValueError("saved_query_replay_hash_mismatch:" + name)
            replayed.append({"name": name, "top_k": top_k, "result_sha256": result.sha256})
    return {
        "query_count": len(replayed), "queries": replayed,
        "precompile_handle_manifest_sha256": registry.public_manifest()["manifest_sha256"],
    }


def compile_saved_candidate(
    *, row: Mapping[str, Any], bundle: Mapping[str, Any], capture: Path,
    candidate: Mapping[str, Any], planning_result_path: Path,
) -> tuple[Any, Any, dict[str, Any]]:
    _observation, private = base._load_trial(bundle, capture, row["root_id"])
    replay = replay_saved_queries(
        registry=private["registry"], private=private,
        planning_result=read(planning_result_path),
    )
    # Compile against the replayed, exact planning-time registry rather than
    # the unmaterialised base capture view.
    from optimizer_pipeline.dof.single_submission.contract_v5 import compile_candidate_v5
    from optimizer_pipeline.dof.single_submission.language import CompileContext
    admitted = tuple(row["admitted_action_keys"])
    compiled = compile_candidate_v5(
        candidate,
        CompileContext(
            private["registry"], "ROUTED", private["predefined_regions"],
            private["geometry_bounds"],
            tuple(key.removeprefix("macro.") for key in admitted),
            fanout_admitted=bool(private.get("fanout_admitted", False)),
            hardblock_compatibility_relation=private.get("hardblock_compatibility_relation"),
        ),
    )
    expected_manifest = read(planning_result_path)["query_handle_manifest"]
    observed_manifest = private["registry"].public_manifest()
    if expected_manifest.get("trial_binding_hash") != observed_manifest.get("trial_binding_hash"):
        raise ValueError("saved_query_trial_binding_mismatch")
    missing = sorted(set(expected_manifest.get("handles", ())) - set(observed_manifest.get("handles", ())))
    replay["postcompile_handle_manifest_sha256"] = observed_manifest["manifest_sha256"]
    replay["expected_handle_manifest_sha256"] = expected_manifest["manifest_sha256"]
    # Registry-wide differences are expected when the final source adds the
    # exact hardblock relation.  Candidate literal references and replayed Q
    # rows are still compiler/hash checked; preserve unrelated removed handles.
    replay["superseded_unreferenced_handles"] = missing
    replay["forward_compatible_added_handles"] = sorted(
        set(observed_manifest.get("handles", ())) - set(expected_manifest.get("handles", ()))
    )
    return private["registry"], compiled, replay


async def run(args: argparse.Namespace) -> int:
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean_for_gate4_runtime")
    cells.configure()
    cells.configure_wp015b_hardblock_compatibility(args.scan)
    bundle = read(args.bundle)
    legacy_admission = read(args.legacy_admission)
    hardblock_admission = read(args.hardblock_admission)
    order = read(args.order)
    families = tuple(args.family or FAMILIES)
    if any(family not in FAMILIES for family in families) or len(set(families)) != len(families):
        raise ValueError("invalid_or_duplicate_gate4_family")
    rows = select(
        legacy_admission, hardblock_admission, order,
        candidate_index=args.candidate_index, families=families,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def one(row: Mapping[str, Any]) -> dict[str, Any]:
        family = str(row["strict_mechanism"])
        source = candidate_source(row, args.legacy_admission, args.hardblock_admission)
        candidate = read(source)
        planning_result_path = source.parent.parent / "planning-result.json"
        case_id = "wp015b-runtime__" + cells.digest({
            "family": family, "model": row["model_key"],
            "candidate_index": args.candidate_index, "source_tree": git("rev-parse", "HEAD^{tree}"),
        })[:24]
        execution_dir = args.output / "cases" / case_id / "execution"
        try:
            registry, compiled, query_replay = compile_saved_candidate(
                row=row, bundle=bundle, capture=args.capture, candidate=candidate,
                planning_result_path=planning_result_path,
            )
        except Exception as exc:
            return dict(row) | {
                "job_id": case_id, "runtime_round": args.candidate_index,
                "selected_language": "G", "canonical_terminal": "CANDIDATE_RECOMPILE_FAILURE",
                "physical_terminal": None, "execution_started": False,
                "compile_error_type": type(exc).__name__, "compile_error": str(exc),
                "actual_heavy_calls": 0, "actual_light_mutations": 0,
                "functional_terminal": None, "committed_candidate": candidate,
                "candidate_sha256": cells.digest(candidate),
                "model_family": model_family(str(row["model_key"])),
            }
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
            "job_id": case_id, "runtime_round": args.candidate_index,
            "selected_language": "G", "canonical_terminal": execution["canonical_endpoint"],
            "physical_terminal": execution["canonical_endpoint"], "execution_started": True,
            "execution": execution, "planned_strict_boundary_certificate": planned,
            "strict_boundary_certificate": strict, "boundary_evidence": causal,
            "actual_heavy_calls": int(execution.get("actual_heavy_calls") or 0),
            "actual_light_mutations": execution.get("actual_light_mutations"),
            "resolved_child_path": str(child) if child else None,
            "resolved_child_sha256": cells.file_sha256(child) if child else None,
            "functional_terminal": (
                "PENDING_V1" if child and execution["canonical_endpoint"] == "VALID_COMPLETION" else None
            ),
            "committed_candidate": candidate, "candidate_sha256": cells.digest(candidate),
            "model_family": model_family(str(row["model_key"])),
            "planning_query_replay": query_replay,
        }

    results = await asyncio.gather(*(one(row) for row in rows))
    source_commit, source_tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    core = {
        "schema_version": "dof.wp015b-runtime-admission-round.v1",
        "development_only": True, "candidate_index": args.candidate_index,
        "families_requested": list(families), "job_count": len(results),
        "terminal_counts": dict(collections.Counter(row["canonical_terminal"] for row in results)),
        "strict_runtime_exercised": sum(
            bool(row["strict_boundary_certificate"].get("strict_boundary_exercised")) for row in results
        ),
        "physical_valid_children": sum(row["functional_terminal"] == "PENDING_V1" for row in results),
        "actual_heavy_calls": sum(row["actual_heavy_calls"] for row in results),
        "provider_requests": 0, "bundle_sha256": bundle["bundle_sha256"],
        "shared_scientific_core_hash": bundle["shared_scientific_core_hash"],
        "scientific_source_commit": source_commit, "scientific_source_tree": source_tree,
        "legacy_admission_sha256": legacy_admission["aggregate_sha256"],
        "hardblock_admission_sha256": hardblock_admission["aggregate_sha256"],
        "jobs": results,
    }
    result = core | {"aggregate_sha256": cells.digest(core)}
    cells.write(args.output / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in (
        "terminal_counts", "strict_runtime_exercised", "physical_valid_children", "actual_heavy_calls",
    )}, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--legacy-admission", type=Path, required=True)
    value.add_argument("--hardblock-admission", type=Path, required=True)
    value.add_argument("--order", type=Path, required=True)
    value.add_argument("--scan", type=Path, required=True)
    value.add_argument("--capture", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--vivado", type=Path, required=True)
    value.add_argument("--candidate-index", type=int, choices=(0, 1), required=True)
    value.add_argument("--family", action="append", choices=FAMILIES)
    value.add_argument("--max-parallel", type=int, default=3)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
