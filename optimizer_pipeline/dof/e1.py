"""Frozen Protocol E1-v1 factors, scorer, history, and job inventory.

This module is intentionally pure: importing it cannot start a provider or a
backend.  It defines the pre-result contract that real E1 collection consumes.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from optimizer_pipeline.analysis_schema import calculate_fmax_mhz

from .catalogues import clean_macro_catalogue, strict_l3_catalogue
from .contracts import DesignStage, DoFLevel
from .expansions import expand_real_action
from .openrouter import OPENROUTER_E1_MODELS, OPENROUTER_E1_PROFILES


E1_PROTOCOL_VERSION = "dof.protocol-e1.v1"
E1_SCORER_VERSION = "dof.achievable-period-fmax.v1"
E1_HISTORY_SCHEMA = "dof.action-aware-history.v1"
E1_JOB_BUNDLE_SCHEMA = "dof.e1-job-bundle.v1"

E1_REFERENCE_BASELINES_MHZ: Mapping[str, float] = {
    "amd_mini_isp": 307.13,
    "boom_soc": 48.24,
    "corescore_500_mod": 344.23,
    "finn_radioml": 284.90,
    "logicnets_jscl": 403.55,
    "rosetta_digit-recognition": 366.97,
    "rosetta_optical-flow": 324.89,
    "vexriscv_re-place": 310.17,
}
E1_BENCHMARKS = tuple(E1_REFERENCE_BASELINES_MHZ)
E1_REPEAT_SUBSET = (
    "vexriscv_re-place",
    "logicnets_jscl",
    "corescore_500_mod",
    "rosetta_optical-flow",
)
E1_L1_ACTIONS = (
    "macro.phys_opt",
    "macro.retime",
    "macro.place_route",
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        value if isinstance(value, bytes) else _canonical_bytes(value)
    ).hexdigest()


def scorer_contract() -> dict[str, Any]:
    payload = {
        "schema_version": "dof.e1-scorer-contract.v1",
        "scorer_version": E1_SCORER_VERSION,
        "formula": "fmax_mhz=1000/(target_period_ns-wns_ns)",
        "implementation": "optimizer_pipeline.analysis_schema.calculate_fmax_mhz",
        "target_clock_rule": (
            "clk_fpl26contest_if_present_else_endpoint_clock_of_worst_setup_path"
        ),
        "target_clock_frozen_per_benchmark": True,
        "root_reference_tolerance": "max(1.0_mhz,1_percent)",
        "selection_order": [
            "valid_and_fully_routed",
            "highest_fmax_mhz",
            "within_0.01_mhz_higher_wns_ns",
            "lower_actual_heavy_runtime_seconds",
            "earliest_completed_candidate",
        ],
    }
    return payload | {"scorer_sha256": _hash(payload)}


def score_timing(
    *,
    target_clock: str,
    target_period_ns: float,
    wns_ns: float,
    tns_ns: float,
) -> dict[str, Any]:
    if not isinstance(target_clock, str) or not target_clock:
        raise ValueError("target_clock_missing")
    values = (target_period_ns, wns_ns, tns_ns)
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        for value in values
    ):
        raise ValueError("scorer_nonfinite_input")
    if float(target_period_ns) <= 0:
        raise ValueError("target_period_not_positive")
    fmax = calculate_fmax_mhz(wns_ns, target_period_ns)
    if fmax is None:
        raise ValueError("achievable_period_not_positive")
    contract = scorer_contract()
    return {
        "schema_version": "dof.e1-timing-score.v1",
        "target_clock": target_clock,
        "target_period_ns": float(target_period_ns),
        "wns_ns": float(wns_ns),
        "tns_ns": float(tns_ns),
        "achievable_period_ns": float(target_period_ns) - float(wns_ns),
        "fmax_mhz": fmax,
        "scorer_version": E1_SCORER_VERSION,
        "scorer_sha256": contract["scorer_sha256"],
    }


def root_reference_check(benchmark: str, score: Mapping[str, Any]) -> dict[str, Any]:
    reference = E1_REFERENCE_BASELINES_MHZ[benchmark]
    observed = float(score["fmax_mhz"])
    discrepancy = abs(observed - reference)
    tolerance = max(1.0, reference * 0.01)
    return {
        "benchmark": benchmark,
        "reference_fmax_mhz": reference,
        "derived_fmax_mhz": observed,
        "absolute_discrepancy_mhz": discrepancy,
        "tolerance_mhz": tolerance,
        "accepted": discrepancy <= tolerance,
        "blocker": None if discrepancy <= tolerance else "SCORER_OR_ROOT_IDENTITY_BLOCKER",
    }


def action_aware_history_row(
    *,
    decision_index: int,
    actions: Iterable[Mapping[str, Any]],
    parent_qor: Mapping[str, Any],
    child_qor: Mapping[str, Any],
    valid: bool,
    endpoint: str,
    actual_heavy_calls: int,
) -> dict[str, Any]:
    canonical_actions = [
        {
            "action_key": str(action["action_key"]),
            "parameters": json.loads(_canonical_bytes(dict(action.get("parameters", {})))),
        }
        for action in actions
    ]
    if not canonical_actions or any(
        not action["action_key"] or action["action_key"].startswith("plan:")
        for action in canonical_actions
    ):
        raise ValueError("canonical_action_sequence_required")
    parent = {name: parent_qor.get(name) for name in ("fmax_mhz", "wns_ns")}
    child = {name: child_qor.get(name) for name in ("fmax_mhz", "wns_ns")}
    delta = {
        name: (
            None
            if not isinstance(parent[name], (int, float))
            or not isinstance(child[name], (int, float))
            else float(child[name]) - float(parent[name])
        )
        for name in ("fmax_mhz", "wns_ns")
    }
    return {
        "decision_index": decision_index,
        "actions": canonical_actions,
        "parent_qor": parent,
        "child_qor": child,
        "delta_qor": delta,
        "valid": bool(valid),
        "endpoint": endpoint,
        "actual_heavy_calls": actual_heavy_calls,
    }


def repeated_action_diagnostic(
    history: Iterable[Mapping[str, Any]],
    actions: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare only canonical action keys/parameters, never proposal identity."""

    current = [
        {
            "action_key": str(action["action_key"]),
            "parameters": json.loads(_canonical_bytes(dict(action.get("parameters", {})))),
        }
        for action in actions
    ]
    signature = _hash(current)
    prior = [
        _hash(
            [
                {
                    "action_key": str(action["action_key"]),
                    "parameters": json.loads(
                        _canonical_bytes(dict(action.get("parameters", {})))
                    ),
                }
                for action in row.get("actions", ())
            ]
        )
        for row in history
    ]
    matches = [index for index, prior_signature in enumerate(prior) if prior_signature == signature]
    return {
        "schema_version": "dof.e1-repeated-action-diagnostic.v1",
        "canonical_action_signature_sha256": signature,
        "repeated": bool(matches),
        "matching_prior_decision_indices": matches,
    }


def l1_portfolio_contract() -> dict[str, Any]:
    catalogue = clean_macro_catalogue(DoFLevel.L1)
    arms = []
    total = 0
    for action_key in E1_L1_ACTIONS:
        expanded = expand_real_action(
            DoFLevel.L1,
            action_key,
            {},
            current_stage=DesignStage.ROUTED,
        )
        heavy = expanded.heavy_call_count
        total += heavy
        arms.append(
            {
                "action_key": action_key,
                "parameters": {},
                "root_sibling": True,
                "implementation_heavy_calls": heavy,
                "expansion_sha256": _hash(expanded.to_dict()),
            }
        )
    if total != 4:
        raise RuntimeError("e1_l1_portfolio_not_four_heavy_calls")
    payload = {
        "schema_version": "dof.e1-l1-portfolio.v1",
        "catalogue_version": catalogue.version,
        "catalogue_sha256": catalogue.sha256,
        "arms": arms,
        "total_implementation_heavy_calls": total,
        "selection_scorer_sha256": scorer_contract()["scorer_sha256"],
    }
    return payload | {"portfolio_sha256": _hash(payload)}


def e1_jobs() -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    order = 0
    # Repeat 1 is benchmark-paired and balanced locally across level/model.
    for benchmark_index, benchmark in enumerate(E1_BENCHMARKS):
        for level in ("L0", "L1"):
            jobs.append(
                {
                    "order": order,
                    "job_id": f"{benchmark}__{level.lower()}",
                    "benchmark": benchmark,
                    "level": level,
                    "model": None,
                    "repeat": 1,
                }
            )
            order += 1
        rotated = OPENROUTER_E1_MODELS[benchmark_index % 3 :] + OPENROUTER_E1_MODELS[: benchmark_index % 3]
        for model_index, model in enumerate(rotated):
            levels = ("L2", "L3") if model_index % 2 == 0 else ("L3", "L2")
            for level in levels:
                jobs.append(
                    {
                        "order": order,
                        "job_id": f"{benchmark}__{level.lower()}__{model.replace('/', '_')}__r1",
                        "benchmark": benchmark,
                        "level": level,
                        "model": model,
                        "repeat": 1,
                    }
                )
                order += 1
    for repeat in (2, 3):
        for benchmark_index, benchmark in enumerate(E1_BENCHMARKS):
            rotated = OPENROUTER_E1_MODELS[benchmark_index % 3 :] + OPENROUTER_E1_MODELS[: benchmark_index % 3]
            for model_index, model in enumerate(rotated):
                levels = ("L3", "L2") if (benchmark_index + model_index + repeat) % 2 else ("L2", "L3")
                for level in levels:
                    jobs.append(
                        {
                            "order": order,
                            "job_id": f"{benchmark}__{level.lower()}__{model.replace('/', '_')}__r{repeat}",
                            "benchmark": benchmark,
                            "level": level,
                            "model": model,
                            "repeat": repeat,
                        }
                    )
                    order += 1
    if len(jobs) != 160 or [job["order"] for job in jobs] != list(range(160)):
        raise RuntimeError("e1_job_inventory_not_160")
    return jobs


def e1_protocol_manifest() -> dict[str, Any]:
    jobs = e1_jobs()
    payload = {
        "schema_version": E1_JOB_BUNDLE_SCHEMA,
        "protocol_version": E1_PROTOCOL_VERSION,
        "benchmarks": [
            {"benchmark": name, "reference_fmax_mhz": value}
            for name, value in E1_REFERENCE_BASELINES_MHZ.items()
        ],
        "models": [
            {
                "model": model,
                "reasoning_effort": "medium",
                **dict(OPENROUTER_E1_PROFILES[model]),
            }
            for model in OPENROUTER_E1_MODELS
        ],
        "catalogues": {
            "L1_L2": {
                "version": clean_macro_catalogue(DoFLevel.L1).version,
                "sha256": clean_macro_catalogue(DoFLevel.L1).sha256,
            },
            "L3": {
                "version": strict_l3_catalogue().version,
                "sha256": strict_l3_catalogue().sha256,
            },
        },
        "l1_portfolio": l1_portfolio_contract(),
        "scorer": scorer_contract(),
        "budgets": {
            "heavy_calls_per_trajectory": 4,
            "model_requests_per_l2_l3_trajectory": 4,
            "heavy_call_timeout_seconds": 900,
            "action_transaction_timeout_seconds": 1200,
            "trajectory_timeout_seconds": 3600,
            "aggregate_machine_hours": 36,
            "aggregate_cost_usd": 10,
            "aggregate_physical_model_requests": 576,
        },
        "runtime_only_repeat_subset": list(E1_REPEAT_SUBSET),
        "jobs": jobs,
        "job_count": len(jobs),
    }
    return payload | {"bundle_sha256": _hash(payload)}


__all__ = [
    "E1_BENCHMARKS",
    "E1_HISTORY_SCHEMA",
    "E1_JOB_BUNDLE_SCHEMA",
    "E1_L1_ACTIONS",
    "E1_PROTOCOL_VERSION",
    "E1_REFERENCE_BASELINES_MHZ",
    "E1_REPEAT_SUBSET",
    "E1_SCORER_VERSION",
    "action_aware_history_row",
    "e1_jobs",
    "e1_protocol_manifest",
    "l1_portfolio_contract",
    "root_reference_check",
    "repeated_action_diagnostic",
    "score_timing",
    "scorer_contract",
]
