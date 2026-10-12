#!/usr/bin/env python3
"""Audit and export the frozen WP-012 K/U/H evidence without rerunning tools."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from optimizer_pipeline.dof.common_observation import build_common_policy_context
from optimizer_pipeline.dof.contracts import DoFLevel
from optimizer_pipeline.dof.e1v2 import (
    E1V2_REPEAT1_MATRIX,
    WP012_EXCLUDED_ACTIONS,
    canonical_bytes,
    content_hash,
    expansion_inheritance_audit,
    protocol_manifest,
)
from optimizer_pipeline.dof.e1v2_job import (
    _condition_runtime,
    validate_e1v2_job_bundle,
)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")


def _write_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    values = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in values for key in row})
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in values:
            writer.writerow(
                {
                    key: (
                        json.dumps(value, sort_keys=True, separators=(",", ":"))
                        if isinstance(value, (dict, list, tuple))
                        else value
                    )
                    for key, value in row.items()
                }
            )


def _verify_hash(value: Mapping[str, Any], field: str) -> bool:
    core = {key: item for key, item in value.items() if key != field}
    return value.get(field) == content_hash(core)


def _aggregate(path: Path) -> dict[str, Any]:
    value = _read(path)
    if not _verify_hash(value, "aggregate_sha256"):
        raise ValueError(f"aggregate hash mismatch: {path}")
    for row in value.get("jobs", ()):
        if not _verify_hash(row, "result_sha256"):
            raise ValueError(f"trajectory hash mismatch: {row.get('job', {}).get('job_id')}")
    return value


def phase_audit(bundle_path: Path, queue_roots: list[Path], output: Path) -> int:
    bundle = validate_e1v2_job_bundle(_read(bundle_path))
    planned = {row["job"]["job_id"] for row in bundle["jobs"]}
    observed: set[str] = set()
    queue_rows = []
    cost_lower = 0.0
    cost_upper = 0.0
    for root in queue_roots:
        aggregate = _aggregate(root / "aggregate.json")
        ledger = _read(root / "controller-ledger.json")
        result = _read(root / "queue-result.json")
        ledger_hash_valid = _verify_hash(ledger, "ledger_sha256")
        result_hash_valid = _verify_hash(result, "queue_result_sha256")
        ids = {row["job"]["job_id"] for row in aggregate["jobs"]}
        overlap = observed & ids
        if overlap:
            raise ValueError("duplicate jobs across queues: " + ",".join(sorted(overlap)))
        observed |= ids
        cost_lower += float(aggregate.get("cost_lower_bound_usd", 0.0))
        cost_upper += float(aggregate.get("cost_upper_bound_usd", 0.0))
        queue_rows.append(
            {
                "queue_root": str(root.resolve()),
                "job_count": aggregate["job_count"],
                "ledger_hash_valid": ledger_hash_valid,
                "queue_result_hash_valid": result_hash_valid,
                "repository_unchanged": result.get("repository_unchanged") is True,
                "cleanup_quiescent": result.get("cleanup_quiescent") is True,
                "stopped": result.get("stopped"),
                "terminal_job_count": result.get("terminal_job_count"),
                "peak_concurrency": result.get("peak_concurrency"),
                "cost_lower_bound_usd": aggregate.get("cost_lower_bound_usd"),
                "cost_upper_bound_usd": aggregate.get("cost_upper_bound_usd"),
            }
        )
    missing = sorted(planned - observed)
    unexpected = sorted(observed - planned)
    cap = float(bundle["protocol"]["budgets"]["aggregate_cost_usd"])
    accepted = bool(
        not missing
        and not unexpected
        and cost_upper <= cap + 1e-12
        and all(
            row["ledger_hash_valid"]
            and row["queue_result_hash_valid"]
            and row["repository_unchanged"]
            and row["cleanup_quiescent"]
            and row["stopped"] is False
            for row in queue_rows
        )
    )
    payload = {
        "schema_version": "dof.wp012-phase-integrity-audit.v1",
        "matrix": bundle["protocol"]["matrix"],
        "bundle_sha256": bundle["bundle_sha256"],
        "protocol_sha256": bundle["protocol"]["protocol_sha256"],
        "planned_job_count": len(planned),
        "observed_job_count": len(observed),
        "missing_jobs": missing,
        "unexpected_jobs": unexpected,
        "cost_lower_bound_usd": cost_lower,
        "cost_upper_bound_usd": cost_upper,
        "phase_cost_cap_usd": cap,
        "queues": queue_rows,
        "accepted": accepted,
    }
    _write(output, payload | {"audit_sha256": content_hash(payload)})
    return 0 if accepted else 2


def known_noop_audit(bundle_path: Path, queue_root: Path, output: Path) -> int:
    bundle = validate_e1v2_job_bundle(_read(bundle_path))
    protocol = protocol_manifest(
        matrix=E1V2_REPEAT1_MATRIX,
        roots=tuple(bundle["protocol"]["roots"]),
    )
    aggregate = _aggregate(queue_root / "aggregate.json")
    specs = {row["job"]["job_id"]: row for row in bundle["jobs"]}
    rows = []
    groups: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    for result in aggregate["jobs"]:
        job = result["job"]
        if job["level"] not in {"L2", "L3", "L4"}:
            continue
        job_id = job["job_id"]
        attempt = next((queue_root / "jobs" / job_id).glob("*--attempt-*/trajectory"))
        observation = _read(attempt / "decision-00" / "observation" / "real-observation.json")
        spec = specs[job_id]
        level = DoFLevel(job["level"])
        default = build_common_policy_context(
            level=level,
            source=observation["source"],
            current_artifact_sha256=observation["current_artifact"]["sha256"],
            decision_index=0,
        )
        regime, catalogue, mask = _condition_runtime(spec, level)
        explicit = build_common_policy_context(
            level=level,
            source=observation["source"],
            current_artifact_sha256=observation["current_artifact"]["sha256"],
            decision_index=0,
            regime=regime,
            catalogue=catalogue,
            mask=mask,
        )
        equal = default.evidence() == explicit.evidence()
        rows.append(
            {
                "job_id": job_id,
                "benchmark": job["benchmark"],
                "model": job["model"],
                "level": job["level"],
                "known_extension_noop": equal,
                "base_observation_hash": explicit.base_observation_hash,
                "common_prompt_core_hash": explicit.common_prompt_core_hash,
                "capability_appendix_hash": explicit.capability_appendix_hash,
                "catalogue_hash": catalogue.sha256,
                "mask_hash": mask.hash,
            }
        )
        groups.setdefault((job["benchmark"], job["model"]), {})[job["level"]] = (
            explicit.base_observation_hash,
            explicit.common_prompt_core_hash,
        )
    group_rows = []
    for (benchmark, model), levels in sorted(groups.items()):
        hashes = list(levels.values())
        group_rows.append(
            {
                "benchmark": benchmark,
                "model": model,
                "levels": sorted(levels),
                "base_and_core_equal_l2_l3_l4": len(levels) == 3 and len(set(hashes)) == 1,
            }
        )
    accepted = bool(
        bundle["protocol"] == protocol
        and len(rows) == 72
        and len(group_rows) == 24
        and all(row["known_extension_noop"] for row in rows)
        and all(row["base_and_core_equal_l2_l3_l4"] for row in group_rows)
        and expansion_inheritance_audit()["accepted"] is True
    )
    payload = {
        "schema_version": "dof.wp012-known-noop-audit.v1",
        "wp011_bundle_sha256": bundle["bundle_sha256"],
        "wp011_protocol_sha256": protocol["protocol_sha256"],
        "trajectory_rows": rows,
        "group_rows": group_rows,
        "accepted": accepted,
    }
    _write(output, payload | {"audit_sha256": content_hash(payload)})
    return 0 if accepted else 2


def _actions(result: Mapping[str, Any]) -> list[str]:
    return [
        action["action_key"]
        for history in result.get("history", ())
        for action in history.get("actions", ())
    ]


def _trajectory_row(result: Mapping[str, Any], regime: str) -> dict[str, Any]:
    job = result["job"]
    root = next(row for row in result["selection_candidates"] if row["source"] == "root")
    best = result["best_valid_selection"]
    root_fmax = float(root["score"]["fmax_mhz"])
    best_fmax = float(best["score"]["fmax_mhz"])
    actions = _actions(result)
    sequence = [
        "manual.unroute_all",
        "manual.unplace_all",
        "manual.place_design",
        "manual.route_design",
    ]
    reconstructed = any(
        actions[index : index + len(sequence)] == sequence
        for index in range(max(0, len(actions) - len(sequence) + 1))
    )
    return {
        "matrix": result["matrix"],
        "regime": regime,
        "job_id": job["job_id"],
        "benchmark": job["benchmark"],
        "model": job.get("model"),
        "level": job["level"],
        "repeat": job["repeat"],
        "endpoint": result["endpoint"],
        "root_fmax_mhz": root_fmax,
        "best_fmax_mhz": best_fmax,
        "delta_fmax_mhz": best_fmax - root_fmax,
        "best_source": best["source"],
        "root_unchanged": result["root_unchanged"],
        "heavy_calls": result["heavy_calls"],
        "light_mutation_calls": result["light_mutation_calls"],
        "model_requests": result["model_requests"],
        "known_cost_usd": result["known_cost_usd"],
        "cost_upper_bound_usd": result["cost_upper_bound_usd"],
        "known_token_total": result["known_token_total"],
        "wall_seconds": result["wall_seconds"],
        "actions": actions,
        "g_use_count": sum(action == "generated.pblock_region" for action in actions),
        "exact_primitive_reconstruction": reconstructed,
    }


def _l1_macro_gains(results: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    gains: dict[str, float] = {}
    for result in results:
        job = result["job"]
        if job["level"] != "L1":
            continue
        root_fmax = float(result["selection_candidates"][0]["score"]["fmax_mhz"])
        decision = next(
            row for row in result["decisions"] if row["action_key"] == "macro.place_route"
        )
        child = decision.get("child_score", {}).get("score", {}).get("fmax_mhz")
        gains[job["benchmark"]] = 0.0 if child is None else float(child) - root_fmax
    return gains


def final_analysis(
    wp011: Path,
    known: list[Path],
    forced: Path,
    holdout: Path,
    output_dir: Path,
) -> int:
    old = _aggregate(wp011)
    known_aggregates = [_aggregate(path) for path in known]
    forced_aggregate = _aggregate(forced)
    holdout_aggregate = _aggregate(holdout)
    results = (
        [(row, "known") for row in old["jobs"]]
        + [(row, "known") for agg in known_aggregates for row in agg["jobs"]]
        + [(row, "forced_unknown") for row in forced_aggregate["jobs"]]
        + [(row, "natural_unknown") for row in holdout_aggregate["jobs"]]
    )
    trajectories = [_trajectory_row(row, regime) for row, regime in results]
    decisions = []
    actions_table = []
    for result, regime in results:
        job = result["job"]
        for index, decision in enumerate(result.get("decisions", ())):
            provider = decision.get("provider", {})
            history = (
                result.get("history", ())[index]
                if index < len(result.get("history", ()))
                else {}
            )
            selected_actions = [
                action.get("action_key") for action in history.get("actions", ())
            ]
            if "action_key" in decision:
                selected_actions = [decision["action_key"]]
            decisions.append(
                {
                    "regime": regime,
                    "job_id": job["job_id"],
                    "benchmark": job["benchmark"],
                    "model": job.get("model"),
                    "level": job["level"],
                    "repeat": job["repeat"],
                    "decision_index": index,
                    "provider_endpoint": provider.get("endpoint"),
                    "provider_accepted": provider.get("provider_accepted"),
                    "gate_accepted": (provider.get("gate") or {}).get("accepted"),
                    "dispatches": decision.get("dispatches"),
                    "selected_actions": selected_actions,
                    "actual_heavy_calls": decision.get("actual_heavy_calls"),
                    "valid": decision.get("valid", history.get("valid")),
                }
            )
            for action_index, action_key in enumerate(selected_actions):
                actions_table.append(
                    {
                        "regime": regime,
                        "job_id": job["job_id"],
                        "benchmark": job["benchmark"],
                        "model": job.get("model"),
                        "level": job["level"],
                        "repeat": job["repeat"],
                        "decision_index": index,
                        "action_index": action_index,
                        "action_key": action_key,
                        "generated": action_key == "generated.pblock_region",
                    }
                )
    keyed = {
        (row["regime"], row["benchmark"], row["model"], row["repeat"], row["level"]): row
        for row in trajectories
        if row["model"] is not None
    }
    contrasts = []
    for regime, benchmark, model, repeat, _level in sorted(keyed):
        if _level != "L2":
            continue
        levels = {
            level: keyed.get((regime, benchmark, model, repeat, level))
            for level in ("L2", "L3", "L4")
        }
        if any(value is None for value in levels.values()):
            continue
        values = {level: float(row["delta_fmax_mhz"]) for level, row in levels.items()}
        contrasts.append(
            {
                "regime": regime,
                "benchmark": benchmark,
                "model": model,
                "repeat": repeat,
                "l3_minus_l2_mhz": values["L3"] - values["L2"],
                "l4_minus_l3_mhz": values["L4"] - values["L3"],
                "l4_minus_l2_mhz": values["L4"] - values["L2"],
            }
        )
    denominators = _l1_macro_gains(old["jobs"])
    recovery = []
    for benchmark, denominator in sorted(denominators.items()):
        eligible = [
            row
            for row in trajectories
            if row["regime"] == "forced_unknown" and row["benchmark"] == benchmark
        ]
        if not eligible:
            continue
        best = max(eligible, key=lambda row: float(row["delta_fmax_mhz"]))
        recovery.append(
            {
                "benchmark": benchmark,
                "known_place_route_gain_mhz": denominator,
                "masked_best_valid_gain_mhz": best["delta_fmax_mhz"],
                "masked_best_job_id": best["job_id"],
                "raw_recovery_ratio": (
                    None if denominator == 0.0 else best["delta_fmax_mhz"] / denominator
                ),
                "any_exact_primitive_reconstruction": any(
                    row["exact_primitive_reconstruction"] for row in eligible
                ),
                "g_use_count": sum(int(row["g_use_count"]) for row in eligible),
            }
        )
    endpoint_counts = {
        endpoint: sum(row["endpoint"] == endpoint for row in trajectories)
        for endpoint in sorted({str(row["endpoint"]) for row in trajectories})
    }
    summary = {
        "schema_version": "dof.wp012-analysis-summary.v1",
        "trajectory_count": len(trajectories),
        "contrast_count": len(contrasts),
        "forced_unknown_recovery_rows": len(recovery),
        "endpoint_counts": endpoint_counts,
        "known_cost_usd": sum(float(row["known_cost_usd"]) for row in trajectories),
        "known_token_total": sum(int(row["known_token_total"]) for row in trajectories),
        "g_use_count": sum(int(row["g_use_count"]) for row in trajectories),
        "exact_primitive_reconstruction_count": sum(
            bool(row["exact_primitive_reconstruction"]) for row in trajectories
        ),
        "no_final_paper_claim_authorized_before_gpt_pro_review": True,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "trajectories.csv", trajectories)
    _write_csv(output_dir / "decisions.csv", decisions)
    _write_csv(output_dir / "actions.csv", actions_table)
    _write_csv(output_dir / "paired-contrasts.csv", contrasts)
    _write_csv(output_dir / "forced-unknown-recovery.csv", recovery)
    _write(
        output_dir / "analysis-summary.json",
        summary | {"summary_sha256": content_hash(summary)},
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    phase = sub.add_parser("phase-audit")
    phase.add_argument("--bundle", type=Path, required=True)
    phase.add_argument("--queue-root", type=Path, action="append", required=True)
    phase.add_argument("--output", type=Path, required=True)
    noop = sub.add_parser("known-noop-audit")
    noop.add_argument("--bundle", type=Path, required=True)
    noop.add_argument("--queue-root", type=Path, required=True)
    noop.add_argument("--output", type=Path, required=True)
    final = sub.add_parser("final")
    final.add_argument("--wp011-aggregate", type=Path, required=True)
    final.add_argument("--known-aggregate", type=Path, action="append", required=True)
    final.add_argument("--forced-aggregate", type=Path, required=True)
    final.add_argument("--holdout-aggregate", type=Path, required=True)
    final.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "phase-audit":
        return phase_audit(args.bundle, args.queue_root, args.output)
    if args.command == "known-noop-audit":
        return known_noop_audit(args.bundle, args.queue_root, args.output)
    if args.command == "final":
        return final_analysis(
            args.wp011_aggregate,
            args.known_aggregate,
            args.forced_aggregate,
            args.holdout_aggregate,
            args.output_dir,
        )
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
