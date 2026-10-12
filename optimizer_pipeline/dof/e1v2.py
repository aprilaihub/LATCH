"""Pure Protocol E1-v2 factors and frozen job inventories."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .catalogues import (
    filtered_nested_capability_audit,
    nested_action_catalogue,
    nested_capability_audit,
)
from .common_observation import (
    COMMON_BASE_SCHEMA,
    COMMON_PROMPT_CORE_VERSION,
)
from .contracts import DoFLevel
from .e1 import (
    E1_BENCHMARKS,
    E1_L1_ACTIONS,
    l1_portfolio_contract,
    scorer_contract,
)
from .expansions import real_action_expansion_manifest_v2
from .openrouter import OPENROUTER_E1_MODELS, OPENROUTER_E1_PROFILES
from .masks import MASK_MANIFEST_SCHEMA_VERSION, create_mask_rules
from .toolcalls import e1v2_tool_call_registry


E1V2_PROTOCOL_VERSION = "dof.protocol-e1.v2.4"
E1V2_JOB_BUNDLE_SCHEMA = "dof.e1v2-job-bundle.v4"
E1V2_BOUND_BUNDLE_SCHEMA = "dof.e1v2-bound-job-bundle.v4"
E1V2_JOB_SPEC_SCHEMA = "dof.e1v2-bound-job-spec.v4"
E1V2_TRAJECTORY_RESULT_SCHEMA = "dof.e1v2-trajectory-result.v4"
E0V2_MATRIX = "E0V2_4"
E1V2_REPEAT1_MATRIX = "E1V2_4_REPEAT1"
WP012_K_MATRIX = "WP012_K_KNOWN_REPEATS_2_3"
WP012_U_MATRIX = "WP012_U_FORCED_UNKNOWN"
WP012_H_MATRIX = "WP012_H_NATURAL_UNKNOWN"
WP012_ORDER_VERSION = "dof.wp012-deterministic-order.v1"
WP012_FORCED_UNKNOWN_BENCHMARKS = (
    "amd_mini_isp",
    "corescore_500_mod",
    "finn_radioml",
    "logicnets_jscl",
    "rosetta_optical-flow",
    "vexriscv_re-place",
)
WP012_HOLDOUTS = ("external_family_holdout_a", "external_family_holdout_b")
WP012_EXCLUDED_ACTIONS = ("macro.place_route",)
WP012_FORCED_UNKNOWN_L1_ACTIONS = tuple(
    action for action in E1_L1_ACTIONS if action not in WP012_EXCLUDED_ACTIONS
)
UNKNOWN_PROVIDER_REQUEST_RESERVE_USD = 0.10
E0V2_DEVELOPMENT_ROOTS = ("vexriscv_re-place", "external_compatibility_a")
E0V2_MODELS = ("openai/gpt-5.6-luna", "anthropic/claude-sonnet-5")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        value if isinstance(value, bytes) else canonical_bytes(value)
    ).hexdigest()


def expansion_inheritance_audit() -> dict[str, Any]:
    registry = e1v2_tool_call_registry()
    manifest = real_action_expansion_manifest_v2(registry)
    definitions = {
        action.action_key: {
            "definition": action.to_dict(),
            "sha256": content_hash(action.to_dict()),
        }
        for action in manifest.actions
    }
    catalogues = {
        level.value: nested_action_catalogue(level)
        for level in (DoFLevel.L2, DoFLevel.L3, DoFLevel.L4)
    }
    sets = {
        level: {action.name for action in catalogue.actions}
        for level, catalogue in catalogues.items()
    }
    inherited = {
        "L2_to_L3": {
            key: definitions[key]["sha256"] for key in sorted(sets["L2"])
        },
        "L2_to_L4": {
            key: definitions[key]["sha256"] for key in sorted(sets["L2"])
        },
        "L3_to_L4": {
            key: definitions[key]["sha256"] for key in sorted(sets["L3"])
        },
    }
    payload = {
        "schema_version": "dof.e1v2-expansion-inheritance-audit.v1",
        "registry_sha256": registry.sha256,
        "expansion_manifest_sha256": manifest.sha256,
        "action_expansions": definitions,
        "inherited_expansion_sha256": inherited,
        "l2_strict_subset_l3": sets["L2"] < sets["L3"],
        "l3_strict_subset_l4": sets["L3"] < sets["L4"],
        "one_definition_per_inherited_action": True,
    }
    payload["accepted"] = (
        payload["l2_strict_subset_l3"]
        and payload["l3_strict_subset_l4"]
        and payload["one_definition_per_inherited_action"]
    )
    return payload | {"audit_sha256": content_hash(payload)}


def e0v2_jobs(
    roots: Sequence[str] = E0V2_DEVELOPMENT_ROOTS,
) -> list[dict[str, Any]]:
    if len(tuple(roots)) != 2 or len(set(roots)) != 2:
        raise ValueError("e0v2_requires_two_distinct_roots")
    jobs: list[dict[str, Any]] = []
    order = 0
    for benchmark_index, benchmark in enumerate(roots):
        for level in ("L0", "L1"):
            jobs.append(
                {
                    "order": order,
                    "job_id": f"{benchmark}__{level.lower()}__e0v2_4",
                    "benchmark": benchmark,
                    "level": level,
                    "model": None,
                    "repeat": 0,
                }
            )
            order += 1
        rotated = E0V2_MODELS[benchmark_index:] + E0V2_MODELS[:benchmark_index]
        for model_index, model in enumerate(rotated):
            levels = ("L2", "L3", "L4") if model_index == 0 else ("L4", "L3", "L2")
            for level in levels:
                jobs.append(
                    {
                        "order": order,
                        "job_id": f"{benchmark}__{level.lower()}__{model.replace('/', '_')}__e0v2_4",
                        "benchmark": benchmark,
                        "level": level,
                        "model": model,
                        "repeat": 0,
                    }
                )
                order += 1
    if len(jobs) != 16 or [item["order"] for item in jobs] != list(range(16)):
        raise RuntimeError("e0v2_job_inventory_invalid")
    return jobs


def e1v2_repeat1_jobs() -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    order = 0
    for benchmark_index, benchmark in enumerate(E1_BENCHMARKS):
        for level in ("L0", "L1"):
            jobs.append(
                {
                    "order": order,
                    "job_id": f"{benchmark}__{level.lower()}__e1v2_4",
                    "benchmark": benchmark,
                    "level": level,
                    "model": None,
                    "repeat": 1,
                }
            )
            order += 1
        rotated = (
            OPENROUTER_E1_MODELS[benchmark_index % 3 :]
            + OPENROUTER_E1_MODELS[: benchmark_index % 3]
        )
        for model_index, model in enumerate(rotated):
            levels = (
                ("L2", "L3", "L4")
                if model_index % 2 == 0
                else ("L4", "L3", "L2")
            )
            for level in levels:
                jobs.append(
                    {
                        "order": order,
                        "job_id": f"{benchmark}__{level.lower()}__{model.replace('/', '_')}__r1_e1v2_4",
                        "benchmark": benchmark,
                        "level": level,
                        "model": model,
                        "repeat": 1,
                    }
                )
                order += 1
    if len(jobs) != 88 or [item["order"] for item in jobs] != list(range(88)):
        raise RuntimeError("e1v2_repeat1_job_inventory_invalid")
    return jobs


def _deterministic_order(
    rows: Sequence[Mapping[str, Any]], *, matrix: str
) -> list[dict[str, Any]]:
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: content_hash(
            {
                "order_version": WP012_ORDER_VERSION,
                "matrix": matrix,
                "job_id": row["job_id"],
            }
        ),
    )
    return [row | {"order": index} for index, row in enumerate(ordered)]


def _agentic_rows(
    *,
    benchmarks: Sequence[str],
    repeats: Sequence[int],
    matrix: str,
    regime: str,
    excluded_action_keys: Sequence[str] = (),
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tag = matrix.lower()
    for benchmark in benchmarks:
        for model in OPENROUTER_E1_MODELS:
            for level in ("L2", "L3", "L4"):
                for repeat in repeats:
                    rows.append(
                        {
                            "job_id": (
                                f"{benchmark}__{level.lower()}__"
                                f"{model.replace('/', '_')}__r{repeat}__{tag}"
                            ),
                            "benchmark": benchmark,
                            "level": level,
                            "model": model,
                            "repeat": repeat,
                            "regime": regime,
                            "excluded_action_keys": sorted(excluded_action_keys),
                        }
                    )
    return rows


def wp012_known_jobs() -> list[dict[str, Any]]:
    """Exact 144-job repeat-2/3 characterised-known inventory."""

    jobs = _deterministic_order(
        _agentic_rows(
            benchmarks=E1_BENCHMARKS,
            repeats=(2, 3),
            matrix=WP012_K_MATRIX,
            regime="known",
        ),
        matrix=WP012_K_MATRIX,
    )
    if len(jobs) != 144:
        raise RuntimeError("wp012_known_job_inventory_invalid")
    return jobs


def wp012_forced_unknown_jobs() -> list[dict[str, Any]]:
    """Exact 174-job pre-registered forced-unknown inventory."""

    rows: list[dict[str, Any]] = []
    tag = WP012_U_MATRIX.lower()
    for benchmark in WP012_FORCED_UNKNOWN_BENCHMARKS:
        common = {
            "benchmark": benchmark,
            "regime": "forced_unknown",
            "excluded_action_keys": list(WP012_EXCLUDED_ACTIONS),
        }
        rows.extend(
            (
                common
                | {
                    "job_id": f"{benchmark}__l0__{tag}",
                    "level": "L0",
                    "model": None,
                    "repeat": 0,
                },
                common
                | {
                    "job_id": f"{benchmark}__l1__{tag}",
                    "level": "L1",
                    "model": None,
                    "repeat": 0,
                    "l1_actions": list(WP012_FORCED_UNKNOWN_L1_ACTIONS),
                },
            )
        )
    rows.extend(
        _agentic_rows(
            benchmarks=WP012_FORCED_UNKNOWN_BENCHMARKS,
            repeats=(1, 2, 3),
            matrix=WP012_U_MATRIX,
            regime="forced_unknown",
            excluded_action_keys=WP012_EXCLUDED_ACTIONS,
        )
    )
    jobs = _deterministic_order(rows, matrix=WP012_U_MATRIX)
    if len(jobs) != 174:
        raise RuntimeError("wp012_forced_unknown_job_inventory_invalid")
    return jobs


def wp012_holdout_jobs() -> list[dict[str, Any]]:
    """Exact 58-job sealed natural-unknown holdout inventory."""

    rows: list[dict[str, Any]] = []
    tag = WP012_H_MATRIX.lower()
    for benchmark in WP012_HOLDOUTS:
        common = {
            "benchmark": benchmark,
            "regime": "natural_unknown",
            "excluded_action_keys": [],
        }
        for level in ("L0", "L1"):
            rows.append(
                common
                | {
                    "job_id": f"{benchmark}__{level.lower()}__{tag}",
                    "level": level,
                    "model": None,
                    "repeat": 0,
                }
            )
    rows.extend(
        _agentic_rows(
            benchmarks=WP012_HOLDOUTS,
            repeats=(1, 2, 3),
            matrix=WP012_H_MATRIX,
            regime="natural_unknown",
        )
    )
    jobs = _deterministic_order(rows, matrix=WP012_H_MATRIX)
    if len(jobs) != 58:
        raise RuntimeError("wp012_holdout_job_inventory_invalid")
    return jobs


def protocol_manifest(*, matrix: str, roots: Sequence[str]) -> dict[str, Any]:
    condition_contract: dict[str, Any] | None = None
    if matrix == E0V2_MATRIX:
        jobs = e0v2_jobs(roots)
        models = E0V2_MODELS
        cost_cap = 5.0
        wall_guard_hours = None
        study_status = "E0V2_4_NON_CLAIM"
    elif matrix == E1V2_REPEAT1_MATRIX:
        if tuple(roots) != E1_BENCHMARKS:
            raise ValueError("e1v2_known_root_order_mismatch")
        jobs = e1v2_repeat1_jobs()
        models = OPENROUTER_E1_MODELS
        cost_cap = 10.0
        wall_guard_hours = 48.0
        study_status = "E1V2_4_CHARACTERISED_KNOWN_REPEAT1"
    elif matrix == WP012_K_MATRIX:
        if tuple(roots) != E1_BENCHMARKS:
            raise ValueError("wp012_known_root_order_mismatch")
        jobs = wp012_known_jobs()
        models = OPENROUTER_E1_MODELS
        cost_cap = 4.0
        wall_guard_hours = 24.0
        study_status = "WP012_CHARACTERISED_KNOWN_REPEATS_2_3"
        condition_contract = {
            "regime": "known",
            "excluded_action_keys": [],
            "order_version": WP012_ORDER_VERSION,
            "mask_schema_version": MASK_MANIFEST_SCHEMA_VERSION,
            "mask": create_mask_rules(
                version="dof.e1v2-known-empty-mask.v1"
            ).to_manifest_dict(),
        }
    elif matrix == WP012_U_MATRIX:
        if tuple(roots) != WP012_FORCED_UNKNOWN_BENCHMARKS:
            raise ValueError("wp012_forced_unknown_root_order_mismatch")
        jobs = wp012_forced_unknown_jobs()
        models = OPENROUTER_E1_MODELS
        cost_cap = 4.5
        wall_guard_hours = 24.0
        study_status = "WP012_PRE_REGISTERED_FORCED_UNKNOWN"
        filtered = filtered_nested_capability_audit(
            excluded_action_keys=WP012_EXCLUDED_ACTIONS
        )
        condition_contract = {
            "regime": "forced_unknown",
            "excluded_action_keys": list(WP012_EXCLUDED_ACTIONS),
            "hidden_macro_families": ["placement_routing"],
            "l1_actions": list(WP012_FORCED_UNKNOWN_L1_ACTIONS),
            "order_version": WP012_ORDER_VERSION,
            "filtered_capability_audit": filtered,
            "mask_schema_version": MASK_MANIFEST_SCHEMA_VERSION,
            "mask_derivation": {
                "version": "dof.wp012-neutral-mask.v1",
                "forbidden_sentinel_rule": "exact_root_benchmark_pseudonym",
                "by_benchmark": {
                    benchmark: create_mask_rules(
                        version="dof.wp012-neutral-mask.v1",
                        hidden_macro_families=("placement_routing",),
                        forbidden_sentinels=(benchmark,),
                    ).to_manifest_dict()
                    for benchmark in WP012_FORCED_UNKNOWN_BENCHMARKS
                },
            },
        }
    elif matrix == WP012_H_MATRIX:
        if tuple(roots) != WP012_HOLDOUTS:
            raise ValueError("wp012_holdout_root_order_mismatch")
        jobs = wp012_holdout_jobs()
        models = OPENROUTER_E1_MODELS
        cost_cap = 2.0
        wall_guard_hours = 12.0
        study_status = "WP012_SEALED_NATURAL_UNKNOWN_HOLDOUT"
        condition_contract = {
            "regime": "natural_unknown",
            "excluded_action_keys": [],
            "neutral_root_pseudonyms": list(WP012_HOLDOUTS),
            "known_priors_permitted": False,
            "order_version": WP012_ORDER_VERSION,
            "mask_schema_version": MASK_MANIFEST_SCHEMA_VERSION,
            "mask": create_mask_rules(
                version="dof.wp012-natural-empty-mask.v1"
            ).to_manifest_dict(),
        }
    else:
        raise ValueError("unknown_e1v2_matrix")
    capability = nested_capability_audit()
    expansion = expansion_inheritance_audit()
    payload = {
        "schema_version": E1V2_JOB_BUNDLE_SCHEMA,
        "protocol_version": E1V2_PROTOCOL_VERSION,
        "matrix": matrix,
        "study_status": study_status,
        "roots": list(roots),
        "models": [
            {
                "model": model,
                "max_tokens": 4096,
                "reasoning_effort": "medium",
                **dict(OPENROUTER_E1_PROFILES[model]),
            }
            for model in models
        ],
        "capability_audit": capability,
        "expansion_audit": expansion,
        "common_observation": {
            "base_schema_version": COMMON_BASE_SCHEMA,
            "prompt_core_version": COMMON_PROMPT_CORE_VERSION,
            "initial_observation_equal_l2_l3_l4": True,
        },
        "l1_portfolio": l1_portfolio_contract(),
        "scorer": scorer_contract(),
        "budgets": {
            "heavy_calls_per_trajectory": 4,
            "model_requests_per_l2_l3_l4_trajectory": 4,
            "heavy_call_timeout_seconds": 900,
            "action_transaction_timeout_seconds": 1200,
            "trajectory_timeout_seconds": 3700,
            "aggregate_cost_usd": cost_cap,
            "unknown_provider_request_reserve_usd": (
                UNKNOWN_PROVIDER_REQUEST_RESERVE_USD
            ),
            "cost_cap_basis": "known_plus_reserved_upper_bound",
            "aggregate_canonical_wall_hours": wall_guard_hours,
            "retry_fallback_repair": 0,
        },
        "scheduler": {
            "unit": "complete_experiment_job",
            "weekend_max_concurrency": 16,
            "weekday_guidance": 8,
            "external_outputs_required": True,
        },
        "provider_transport_terminal": {
            "endpoint": "PROVIDER_TRANSPORT_FAILURE",
            "inference_retry": 0,
            "fallback": 0,
            "repair": 0,
            "maximum_accounting_lookup_requests": 1,
            "action_dispatch_after_terminal": 0,
        },
        "provider_error_policy": {
            "classifier_version": "dof.openrouter-error-classifier.v1",
            "immediate_global_categories": [
                "AUTHENTICATION_OR_KEY",
                "PAYMENT_OR_BUDGET",
                "PERMISSION_OR_WORKSPACE",
                "UNCLASSIFIED_4XX",
            ],
            "job_scoped_rejection_category": "GUARDRAIL_OR_PROMPT_BLOCK",
            "same_route_failure_limit": 3,
            "global_transport_failure_limit": 5,
        },
        "policy_output_terminal": {
            "endpoint": "INVALID_POLICY_OUTPUT",
            "proposal_binding_subtype": "PROPOSAL_BINDING_MISMATCH",
            "provider_transport_accounting_accepted": True,
            "proposal_dispatchable": False,
            "action_dispatch_after_terminal": 0,
            "vivado_mutation_after_terminal": 0,
            "retry_fallback_repair": 0,
            "queue_scope": "JOB_ONLY",
        },
        "nested_process_ownership": {
            "registry_schema": "dof.queue-process-ownership-registry.v1",
            "record_schema": "dof.queue-owned-nested-process.v1",
            "identity": "PID_PROCFS_STARTTIME_PGID_SID_COMMAND_HASH",
            "registration_before_backend": True,
            "exact_pidfd_signalling": True,
            "two_zero_live_audits_required": True,
            "independent_attempt_path_audit": True,
        },
        "jobs": jobs,
        "job_count": len(jobs),
    }
    if condition_contract is not None:
        payload["budgets"]["per_trajectory_provider_cost_cap_usd"] = (
            payload["budgets"]["model_requests_per_l2_l3_l4_trajectory"]
            * payload["budgets"]["unknown_provider_request_reserve_usd"]
        )
        payload["condition_contract"] = condition_contract
        payload["analysis_contract"] = {
            "schema_version": "dof.wp012-analysis-contract.v1",
            "trajectory_qor": "best_valid_delta_fmax_mhz_with_immutable_root_fallback",
            "reliability_reported_separately": True,
            "sampling_unit": "complete_trajectory",
            "iterations_and_actions_not_independent_samples": True,
            "paired_keys": ["benchmark", "model", "repeat"],
            "controlled_contrasts": ["L3-L2", "L4-L3", "L4-L2"],
            "forced_unknown_recovery": {
                "eligible_benchmarks": list(WP012_FORCED_UNKNOWN_BENCHMARKS),
                "numerator": "masked_best_valid_gain_with_root_fallback",
                "denominator": "canonical_wp011_repeat1_l1_macro.place_route_gain",
                "raw_ratio_unclipped": True,
                "exact_primitive_reconstruction_sequence": [
                    "manual.unroute_all",
                    "manual.unplace_all",
                    "manual.place_design",
                    "manual.route_design",
                ],
                "reconstruction_match": "contiguous_flattened_trajectory_action_sequence",
            },
            "endpoint_missingness": "never_silently_drop",
            "no_final_superiority_or_generalisation_claim": True,
        }
    if not capability["accepted"] or not expansion["accepted"]:
        raise RuntimeError("e1v2_nested_capability_invariant_failed")
    return payload | {"protocol_sha256": content_hash(payload)}


__all__ = [
    "E0V2_DEVELOPMENT_ROOTS",
    "E0V2_MATRIX",
    "E0V2_MODELS",
    "E1V2_BOUND_BUNDLE_SCHEMA",
    "E1V2_JOB_BUNDLE_SCHEMA",
    "E1V2_JOB_SPEC_SCHEMA",
    "E1V2_PROTOCOL_VERSION",
    "E1V2_REPEAT1_MATRIX",
    "E1V2_TRAJECTORY_RESULT_SCHEMA",
    "UNKNOWN_PROVIDER_REQUEST_RESERVE_USD",
    "canonical_bytes",
    "content_hash",
    "e0v2_jobs",
    "e1v2_repeat1_jobs",
    "expansion_inheritance_audit",
    "protocol_manifest",
    "WP012_EXCLUDED_ACTIONS",
    "WP012_FORCED_UNKNOWN_BENCHMARKS",
    "WP012_FORCED_UNKNOWN_L1_ACTIONS",
    "WP012_HOLDOUTS",
    "WP012_H_MATRIX",
    "WP012_K_MATRIX",
    "WP012_ORDER_VERSION",
    "WP012_U_MATRIX",
    "wp012_forced_unknown_jobs",
    "wp012_holdout_jobs",
    "wp012_known_jobs",
]
