#!/usr/bin/env python3
"""Build public-safe, deterministic WP-012-01 diagnostic tables.

This script deliberately consumes only bound aggregate/result evidence.  It does
not read provider request/response envelopes or implementation run directories.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


G_FIELDS = (
    "g_mentioned",
    "g_proposed",
    "g_gate_admitted",
    "g_executed",
    "g_valid_child",
    "g_followed_by_implementation",
    "g_scored_outcome",
)


def _empty_funnel() -> dict[str, int]:
    return {"jobs": 0, **{field: 0 for field in G_FIELDS}}


def _increment_funnel(target: dict[str, int], utilisation: dict[str, Any]) -> None:
    target["jobs"] += 1
    for field in G_FIELDS:
        target[field] += int(bool(utilisation.get(field, False)))


def _provider_utilisation(row: dict[str, Any]) -> dict[str, Any]:
    diagnostic = row.get("g_diagnostic") or {}
    return {
        "g_mentioned": diagnostic.get("g_mentioned", False),
        "g_proposed": diagnostic.get("g_proposed", False),
        "g_gate_admitted": diagnostic.get("g_gate_admitted", False),
        "g_executed": False,
        "g_valid_child": False,
        "g_followed_by_implementation": False,
        "g_scored_outcome": False,
    }


def _decision_reasons(job: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for decision in job.get("decisions") or []:
        provider = decision.get("provider") or {}
        gate = provider.get("gate") or {}
        reasons.extend(str(item) for item in gate.get("reasons") or [] if item != "accepted")
        subtype = provider.get("policy_output_subtype")
        if subtype:
            reasons.append(f"policy_output:{subtype}")
    if not reasons:
        reasons.extend(str(item) for item in job.get("reasons") or [] if item != "accepted")
    return sorted(set(reasons))


def _best_score(job: dict[str, Any]) -> dict[str, Any]:
    return ((job.get("best_valid_selection") or {}).get("score") or {})


def analyse(
    provider: dict[str, Any],
    real: dict[str, Any],
    queue: dict[str, Any],
    accounting: list[tuple[str, str, dict[str, Any]]],
) -> dict[str, Any]:
    provider_rows = provider.get("rows") or []
    real_jobs = real.get("jobs") or []
    if len(provider_rows) != 15:
        raise ValueError(f"provider matrix incomplete: {len(provider_rows)}/15")
    if len(real_jobs) != 30 or real.get("canonical") != 30:
        raise ValueError(f"real matrix incomplete/non-canonical: {len(real_jobs)}/30")
    if queue.get("terminal_job_count") != 30 or queue.get("stopped"):
        raise ValueError("real queue did not end with 30 clean terminal jobs")

    provider_by_cue: dict[str, dict[str, int]] = defaultdict(_empty_funnel)
    provider_endpoint_counts: Counter[str] = Counter()
    provider_invalid: Counter[tuple[str, str, str]] = Counter()
    g_proposal_rows: list[dict[str, Any]] = []
    route_mismatches: list[str] = []
    for row in provider_rows:
        job = row["job"]
        cue = job["cue"]
        _increment_funnel(provider_by_cue[cue], _provider_utilisation(row))
        provider_endpoint_counts[str(row.get("provider_endpoint"))] += 1
        route = row.get("route") or {}
        if (
            not row.get("route_admitted")
            or route.get("requested_model") != route.get("returned_model")
            or route.get("fallback_allowed") is not False
        ):
            route_mismatches.append(job["job_id"])
        reasons = [str(item) for item in (row.get("gate") or {}).get("reasons") or []]
        if not reasons and row.get("provider_endpoint") == "INVALID_POLICY_OUTPUT":
            reasons = ["INVALID_POLICY_OUTPUT"]
        for reason in reasons:
            if reason != "accepted":
                provider_invalid[(cue, job["model"], reason)] += 1
        for step in ((row.get("gate") or {}).get("proposal") or {}).get("steps") or []:
            if step.get("action_key") != "generated.pblock_region":
                continue
            parameters = step.get("parameters") or {}
            g_proposal_rows.append(
                {
                    "phase": "provider_only",
                    "job_id": job["job_id"],
                    "root": "vexriscv_re-place",
                    "model": job["model"],
                    "cue": cue,
                    "decision_index": 0,
                    **{name: parameters.get(name) for name in (
                        "pblock_id", "selector_kind", "cell_limit", "slice_x0",
                        "slice_y0", "slice_x1", "slice_y1",
                    )},
                    "gate_admitted": bool((row.get("gate") or {}).get("accepted")),
                    "rejection_categories": "|".join(reason for reason in reasons if reason != "accepted"),
                }
            )

    real_funnels: dict[tuple[str, str], dict[str, int]] = defaultdict(_empty_funnel)
    real_rows: list[dict[str, Any]] = []
    real_invalid: Counter[tuple[str, str, str, str]] = Counter()
    real_endpoint_counts: Counter[str] = Counter()
    admitted_sequences: list[dict[str, Any]] = []
    for job in real_jobs:
        spec = job["job"]
        utilisation = (job.get("diagnostic") or {}).get("utilisation") or {}
        for kind, value in (
            ("cue", spec["cue"]),
            ("model", spec["model"]),
            ("root", spec["benchmark"]),
        ):
            _increment_funnel(real_funnels[(kind, value)], utilisation)
        reasons = _decision_reasons(job)
        for reason in reasons or ([job["endpoint"]] if job["endpoint"] != "VALID_COMPLETION" else []):
            real_invalid[(spec["cue"], spec["model"], spec["benchmark"], reason)] += 1
        for decision_index, decision in enumerate(job.get("decisions") or []):
            gate = (decision.get("provider") or {}).get("gate") or {}
            gate_reasons = [str(item) for item in gate.get("reasons") or [] if item != "accepted"]
            for step in (gate.get("proposal") or {}).get("steps") or []:
                if step.get("action_key") != "generated.pblock_region":
                    continue
                parameters = step.get("parameters") or {}
                g_proposal_rows.append(
                    {
                        "phase": "real",
                        "job_id": spec["job_id"],
                        "root": spec["benchmark"],
                        "model": spec["model"],
                        "cue": spec["cue"],
                        "decision_index": decision_index,
                        **{name: parameters.get(name) for name in (
                            "pblock_id", "selector_kind", "cell_limit", "slice_x0",
                            "slice_y0", "slice_x1", "slice_y1",
                        )},
                        "gate_admitted": bool(gate.get("accepted")),
                        "rejection_categories": "|".join(gate_reasons),
                    }
                )
        real_endpoint_counts[str(job["endpoint"])] += 1
        score = _best_score(job)
        real_rows.append(
            {
                "job_id": spec["job_id"],
                "root": spec["benchmark"],
                "model": spec["model"],
                "cue": spec["cue"],
                "endpoint": job["endpoint"],
                "model_requests": job.get("model_requests", 0),
                "heavy_calls": job.get("heavy_calls", 0),
                "light_mutation_calls": job.get("light_mutation_calls", 0),
                "known_tokens": job.get("known_token_total", 0),
                "known_cost_usd": job.get("known_cost_usd", 0),
                "wall_seconds": job.get("wall_seconds"),
                **{field: bool(utilisation.get(field, False)) for field in G_FIELDS},
                "first_g_decision_index": utilisation.get("first_g_decision_index"),
                "instruction_complied": utilisation.get("instruction_complied"),
                "rejection_categories": "|".join(reasons),
                "best_fmax_mhz": score.get("fmax_mhz"),
                "best_wns_ns": score.get("wns_ns"),
                "root_unchanged": job.get("root_unchanged"),
            }
        )
        if utilisation.get("g_gate_admitted") or utilisation.get("g_executed"):
            admitted_sequences.append(
                {
                    "job_id": spec["job_id"],
                    "root": spec["benchmark"],
                    "model": spec["model"],
                    "cue": spec["cue"],
                    "utilisation": utilisation,
                }
            )

    accounting_rows: list[dict[str, Any]] = []
    all_cost = 0.0
    all_reserved = 0.0
    for label, evidence_class, item in accounting:
        known = float(item.get("known_cost_usd") or 0.0)
        reserved = float(item.get("reserved_unknown_cost_usd") or 0.0)
        all_cost += known
        all_reserved += reserved
        accounting_rows.append(
            {
                "label": label,
                "evidence_class": evidence_class,
                "completed": item.get("completed", item.get("canonical")),
                "known_cost_usd": known,
                "reserved_unknown_cost_usd": reserved,
                "known_token_total": item.get("known_token_total", 0),
            }
        )

    provider_table = [
        {"cue": cue, **values}
        for cue, values in sorted(provider_by_cue.items())
    ]
    real_funnel_table = [
        {"stratum_type": kind, "stratum": value, **values}
        for (kind, value), values in sorted(real_funnels.items())
    ]
    invalid_rows = [
        {
            "phase": "provider_only",
            "cue": cue,
            "model": model,
            "root": "vexriscv_re-place",
            "category": reason,
            "count": count,
        }
        for (cue, model, reason), count in sorted(provider_invalid.items())
    ] + [
        {
            "phase": "real",
            "cue": cue,
            "model": model,
            "root": root,
            "category": reason,
            "count": count,
        }
        for (cue, model, root, reason), count in sorted(real_invalid.items())
    ]

    real_g_total = _empty_funnel()
    for job in real_jobs:
        _increment_funnel(real_g_total, (job.get("diagnostic") or {}).get("utilisation") or {})
    qos_status = (
        "NOT_ESTIMABLE_NO_GATE_ADMITTED_G"
        if not real_g_total["g_gate_admitted"]
        else "DESCRIPTIVE_ONLY"
    )
    summary = {
        "schema_version": "dof.wp012-01-public-analysis.v1",
        "protocol": "dof.g-diagnostic.v1",
        "provider_only": {
            "jobs": len(provider_rows),
            "endpoint_counts": dict(sorted(provider_endpoint_counts.items())),
            "by_cue": provider_table,
            "zero_backend_dispatch": all(bool(row.get("zero_backend_dispatch")) for row in provider_rows),
        },
        "real": {
            "jobs": len(real_jobs),
            "endpoint_counts": dict(sorted(real_endpoint_counts.items())),
            "g_funnel": real_g_total,
            "heavy_calls": real.get("heavy_calls"),
            "light_mutation_calls": real.get("light_mutation_calls"),
            "peak_concurrency": queue.get("peak_concurrency"),
            "cleanup_quiescent": queue.get("cleanup_quiescent"),
            "repository_unchanged": queue.get("repository_unchanged"),
        },
        "provider_contract": {
            "route_mismatch_count": len(route_mismatches),
            "route_mismatch_job_ids": route_mismatches,
            "retry_fallback_repair_observed": any(
                (row.get("retry") or 0) or (row.get("fallback") or 0) or (row.get("repair") or 0)
                for row in provider_rows
            ),
        },
        "qor_after_g": {
            "status": qos_status,
            "g_only_observation_count": 0,
            "g_plus_implementation_observation_count": 0,
            "claim_boundary": "No inference about G execution value is supported without a gate-admitted G action.",
        },
        "accounting": {
            "rows": accounting_rows,
            "all_wp_known_cost_usd": all_cost,
            "all_wp_reserved_unknown_cost_usd": all_reserved,
            "hard_cap_usd": 4.0,
            "within_hard_cap": all_cost + all_reserved <= 4.0,
        },
        "sealed_holdouts_accessed_for_optimisation": False,
        "primary_aggregate_eligible": False,
        "admitted_or_executed_g_sequence_count": len(admitted_sequences),
    }
    summary["analysis_sha256"] = hashlib.sha256(_canonical_bytes(summary)).hexdigest()
    return {
        "summary": summary,
        "provider_table": provider_table,
        "real_funnel_table": real_funnel_table,
        "real_rows": sorted(real_rows, key=lambda row: row["job_id"]),
        "invalid_rows": invalid_rows,
        "g_proposal_rows": sorted(
            g_proposal_rows,
            key=lambda row: (row["phase"], row["job_id"], row["decision_index"]),
        ),
        "admitted_sequences": admitted_sequences,
    }


def _parse_accounting(items: list[str]) -> list[tuple[str, str, dict[str, Any]]]:
    result: list[tuple[str, str, dict[str, Any]]] = []
    for item in items:
        parts = item.split("=", 2)
        if len(parts) != 3:
            raise ValueError("--accounting must be LABEL=CLASS=PATH")
        label, evidence_class, path = parts
        result.append((label, evidence_class, _read(Path(path))))
    return result


def _parse_probe_directories(items: list[str]) -> list[tuple[str, str, dict[str, Any]]]:
    result: list[tuple[str, str, dict[str, Any]]] = []
    for item in items:
        parts = item.split("=", 2)
        if len(parts) != 3:
            raise ValueError("--accounting-probe-dir must be LABEL=CLASS=DIR")
        label, evidence_class, directory = parts
        rows = [_read(path) for path in sorted(Path(directory).glob("jobs/*/probe-result.json"))]
        result.append(
            (
                label,
                evidence_class,
                {
                    "completed": len(rows),
                    "known_cost_usd": sum(float(row.get("known_cost_usd") or 0.0) for row in rows),
                    "reserved_unknown_cost_usd": sum(float(row.get("reserved_unknown_cost_usd") or 0.0) for row in rows),
                    "known_token_total": sum(int(row.get("known_token_total") or 0) for row in rows),
                },
            )
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-aggregate", type=Path, required=True)
    parser.add_argument("--real-aggregate", type=Path, required=True)
    parser.add_argument("--queue-result", type=Path, required=True)
    parser.add_argument("--accounting", action="append", default=[], metavar="LABEL=CLASS=PATH")
    parser.add_argument("--accounting-probe-dir", action="append", default=[], metavar="LABEL=CLASS=DIR")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    outputs = analyse(
        _read(args.provider_aggregate),
        _read(args.real_aggregate),
        _read(args.queue_result),
        _parse_accounting(args.accounting) + _parse_probe_directories(args.accounting_probe_dir),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(args.output_dir / "wp012-01-summary.json", outputs["summary"])
    _write_csv(
        args.output_dir / "provider-probe-funnel.csv",
        ["cue", "jobs", *G_FIELDS],
        outputs["provider_table"],
    )
    _write_csv(
        args.output_dir / "real-g-funnel.csv",
        ["stratum_type", "stratum", "jobs", *G_FIELDS],
        outputs["real_funnel_table"],
    )
    _write_csv(
        args.output_dir / "model-cue-root-outcomes.csv",
        [
            "job_id", "root", "model", "cue", "endpoint", "model_requests",
            "heavy_calls", "light_mutation_calls", "known_tokens", "known_cost_usd",
            "wall_seconds", *G_FIELDS, "first_g_decision_index", "instruction_complied",
            "rejection_categories", "best_fmax_mhz", "best_wns_ns", "root_unchanged",
        ],
        outputs["real_rows"],
    )
    _write_csv(
        args.output_dir / "invalid-noncompliance-categories.csv",
        ["phase", "cue", "model", "root", "category", "count"],
        outputs["invalid_rows"],
    )
    _write_csv(
        args.output_dir / "g-proposals.csv",
        [
            "phase", "job_id", "root", "model", "cue", "decision_index", "pblock_id",
            "selector_kind", "cell_limit", "slice_x0", "slice_y0", "slice_x1", "slice_y1",
            "gate_admitted", "rejection_categories",
        ],
        outputs["g_proposal_rows"],
    )
    _write_json(
        args.output_dir / "g-admitted-executed-action-sequences.json",
        {
            "schema_version": "dof.wp012-01-g-action-sequences.v1",
            "count": len(outputs["admitted_sequences"]),
            "rows": outputs["admitted_sequences"],
        },
    )
    _write_json(args.output_dir / "qor-after-g.json", outputs["summary"]["qor_after_g"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
