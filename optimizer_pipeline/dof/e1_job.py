"""Serial, bounded real trajectory runner for frozen Protocol E1-v1 jobs."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Mapping

from .artifacts import fingerprint_file
from .contracts import DoFLevel
from .e0_job import (
    _gated_step_rows,
    _hash,
    _write_json,
    capture_real_observation,
    causal_evidence_for_dispatch,
    run_bound_provider_decision,
)
from .e1 import (
    E1_BENCHMARKS,
    E1_L1_ACTIONS,
    action_aware_history_row,
    e1_protocol_manifest,
    repeated_action_diagnostic,
)
from .e1_audit import run_readonly_score
from .gate import ProposalKind
from .mutation_worker_binding import verified_fake_attestation_evidence_sha256
from .provenance import observe_repository
from .real_mutation import (
    build_e0_dispatch_authorization,
    execute_e0_real_action,
    execute_e0_real_gated_plan,
    freeze_e0_real_action_plan,
    freeze_e0_real_gated_plan,
    validate_e0_dispatch_authorization,
    write_e0_frozen_plan,
    write_e0_gated_plan,
)


E1_BOUND_BUNDLE_SCHEMA = "dof.e1-bound-job-bundle.v1"
E1_JOB_SPEC_SCHEMA = "dof.e1-bound-job-spec.v1"
E1_TRAJECTORY_RESULT_SCHEMA = "dof.e1-trajectory-result.v1"


def freeze_e1_job_bundle(
    roots: Mapping[str, str | Path],
    root_audit: Mapping[str, Any],
    provider_discovery: Mapping[str, Any],
    *,
    repository_anchor: str | Path,
    fake_startup_attestation: str | Path,
) -> dict[str, Any]:
    protocol = e1_protocol_manifest()
    repository = observe_repository(repository_anchor)
    if repository.tracked_tree_dirty:
        raise RuntimeError("tracked_product_tree_not_clean")
    if tuple(roots) != E1_BENCHMARKS:
        raise ValueError("e1_root_order_or_set_mismatch")
    if root_audit.get("all_accepted") is not True:
        raise RuntimeError("SCORER_OR_ROOT_IDENTITY_BLOCKER")
    if provider_discovery.get("all_profiles_available") is not True:
        raise RuntimeError("MODEL_AVAILABILITY_BLOCKER")
    audit_rows = {row["benchmark"]: row for row in root_audit.get("rows", ())}
    if set(audit_rows) != set(E1_BENCHMARKS):
        raise ValueError("e1_root_audit_set_mismatch")
    attestation_sha = verified_fake_attestation_evidence_sha256(
        fake_startup_attestation
    )
    root_rows: dict[str, Any] = {}
    for benchmark in E1_BENCHMARKS:
        fingerprint = fingerprint_file(Path(roots[benchmark]).resolve())
        audited = audit_rows[benchmark]
        if audited["artifact"] != {
            "sha256": fingerprint.sha256,
            "size_bytes": fingerprint.size_bytes,
        }:
            raise RuntimeError("e1_root_audit_identity_mismatch")
        root_rows[benchmark] = {
            "pseudonym": benchmark,
            "sha256": fingerprint.sha256,
            "size_bytes": fingerprint.size_bytes,
            "score": audited["score"],
            "reference_check": audited["reference_check"],
        }
    frozen = {
        "protocol_bundle_sha256": protocol["bundle_sha256"],
        "repository": {
            "branch_name": repository.branch_name,
            "head_commit": repository.head_commit,
            "head_tree": repository.head_tree,
            "binary_diff_sha256": repository.binary_diff_sha256,
        },
        "root_audit_sha256": root_audit["audit_sha256"],
        "provider_discovery_sha256": _hash(provider_discovery),
        "fake_startup_attestation_evidence_sha256": attestation_sha,
        "roots": root_rows,
    }
    freeze_sha = _hash(frozen)
    specs = []
    for job in protocol["jobs"]:
        core = {
            "schema_version": E1_JOB_SPEC_SCHEMA,
            "study_status": "E1_KNOWN_SET_DESCRIPTIVE",
            "job": job,
            "root": root_rows[job["benchmark"]],
            "protocol_bundle_sha256": protocol["bundle_sha256"],
            "freeze_sha256": freeze_sha,
            "budgets": {
                "heavy_calls": 0 if job["level"] == "L0" else 4,
                "model_requests": 4 if job["level"] in {"L2", "L3"} else 0,
                "trajectory_wall_seconds": 3600,
                "provider_cost_usd": 10.0,
            },
        }
        specs.append(core | {"job_spec_sha256": _hash(core)})
    payload = {
        "schema_version": E1_BOUND_BUNDLE_SCHEMA,
        "protocol": protocol,
        "freeze": frozen,
        "freeze_sha256": freeze_sha,
        "jobs": specs,
    }
    return payload | {"bundle_sha256": _hash(payload)}


def validate_e1_job_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(dict(bundle)))
    if value.get("schema_version") != E1_BOUND_BUNDLE_SCHEMA:
        raise ValueError("e1_bundle_schema_mismatch")
    if value.get("bundle_sha256") != _hash(
        {key: item for key, item in value.items() if key != "bundle_sha256"}
    ):
        raise ValueError("e1_bundle_hash_mismatch")
    if value.get("protocol", {}).get("bundle_sha256") != e1_protocol_manifest()[
        "bundle_sha256"
    ]:
        raise ValueError("e1_protocol_binding_mismatch")
    if len(value.get("jobs", ())) != 160:
        raise ValueError("e1_job_count_mismatch")
    for spec in value["jobs"]:
        if spec.get("schema_version") != E1_JOB_SPEC_SCHEMA or spec.get(
            "job_spec_sha256"
        ) != _hash({key: item for key, item in spec.items() if key != "job_spec_sha256"}):
            raise ValueError("e1_job_spec_invalid")
    return value


def _l1_causal(
    spec: Mapping[str, Any], observation: Mapping[str, Any], action_key: str
) -> dict[str, Any]:
    payload = {
        "schema_version": "dof.e1-causal-decision-evidence.v1",
        "policy_source": "deterministic_l1_root_sibling_portfolio",
        "job_spec_sha256": spec["job_spec_sha256"],
        "current_artifact": observation["current_artifact"],
        "observation_sha256": observation["observation_sha256"],
        "context_sha256": None,
        "decision_id": "decision-" + _hash((spec["job_spec_sha256"], action_key))[:20],
        "request_contract_hash": None,
        "raw_response_sha256": None,
        "proposal_sha256": _hash({"action_key": action_key, "parameters": {}}),
        "gate_decision_sha256": _hash(
            {"accepted": True, "policy": "deterministic_l1_root_sibling_portfolio"}
        ),
    }
    return payload | {"causal_evidence_sha256": _hash(payload)}


def _choose_best(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in candidates if row.get("valid") is True]
    if not eligible:
        raise RuntimeError("e1_no_valid_selection_candidate")
    highest = max(float(row["score"]["fmax_mhz"]) for row in eligible)
    for row in eligible:
        row["within_primary_tolerance"] = highest - float(row["score"]["fmax_mhz"]) <= 0.01
    near = [row for row in eligible if row["within_primary_tolerance"]]
    return max(
        near,
        key=lambda row: (
            float(row["score"]["wns_ns"]),
            -float(row.get("heavy_runtime_seconds", 0.0)),
            -int(row.get("completion_order", 0)),
        ),
    )


def _reconcile_l1_actual_heavy_calls(
    decisions: list[dict[str, Any]], heavy_used: int
) -> str | None:
    """Validate the L1 cap without inventing calls after a failed arm.

    Four is the admitted portfolio ceiling. A failed multi-call arm can
    legitimately short-circuit before every declared call starts; that actual
    shortfall is evidence, not an accounting mismatch.
    """

    if heavy_used > 4:
        raise RuntimeError("e1_l1_actual_heavy_call_count_mismatch")
    if heavy_used == 4:
        return None
    if len(decisions) != len(E1_L1_ACTIONS) or all(
        decision.get("valid") is True for decision in decisions
    ):
        raise RuntimeError("e1_l1_actual_heavy_call_count_mismatch")
    return "l1_declared_heavy_calls_short_circuited_by_arm_failure"


def _score_child(
    child: Path,
    spec: Mapping[str, Any],
    score_dir: Path,
    *,
    vivado_exec: str | Path,
    scorer_script: str | Path,
) -> dict[str, Any]:
    root_score = spec["root"]["score"]
    result = run_readonly_score(
        child,
        vivado_exec=vivado_exec,
        scorer_script=scorer_script,
        run_dir=score_dir,
        frozen_target_clock=root_score["target_clock"],
    )
    if result["score"]["target_period_ns"] != root_score["target_period_ns"]:
        raise RuntimeError("e1_frozen_target_period_drift")
    return result


def execute_real_e1_job(
    spec: Mapping[str, Any],
    root_dcp: str | Path,
    output_dir: str | Path,
    *,
    vivado_exec: str | Path,
    python_exec: str | Path,
    repository_anchor: str | Path,
    scorer_script: str | Path,
    fake_startup_attestation_sha256: str,
    remaining_program_cost_usd: float,
    credential: str | None = None,
) -> dict[str, Any]:
    start = time.monotonic()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    level = DoFLevel(spec["job"]["level"])
    root = fingerprint_file(Path(root_dcp).resolve())
    if (root.sha256, root.size_bytes) != (
        spec["root"]["sha256"],
        spec["root"]["size_bytes"],
    ):
        raise RuntimeError("ROOT_IDENTITY_BLOCKER")
    root_candidate = {
        "source": "root",
        "artifact": {"sha256": root.sha256, "size_bytes": root.size_bytes},
        "score": spec["root"]["score"],
        "valid": True,
        "heavy_runtime_seconds": 0.0,
        "completion_order": 0,
    }
    candidates = [root_candidate]
    decisions: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    heavy_used = 0
    model_requests = 0
    provider_cost = 0.0
    endpoint = "VALID_COMPLETION"
    reasons: list[str] = []
    if level is DoFLevel.L0:
        pass
    elif level is DoFLevel.L1:
        observation = capture_real_observation(
            spec,
            root_dcp,
            output / "root-observation",
            decision_index=0,
            history=[],
            heavy_remaining=4,
            model_requests_remaining=0,
            vivado_exec=vivado_exec,
            python_exec=python_exec,
            repository_anchor=repository_anchor,
            frozen_timing_score=spec["root"]["score"],
            e1_protocol=True,
        )
        for completion_order, action_key in enumerate(E1_L1_ACTIONS, start=1):
            arm_dir = output / "l1-arms" / action_key.replace(".", "-")
            run_dir = arm_dir / "action-run"
            causal = _l1_causal(spec, observation, action_key)
            plan = freeze_e0_real_action_plan(
                root_dcp,
                root_pseudonym=spec["job"]["benchmark"],
                level=level,
                action_key=action_key,
                parameters={},
                run_id="e1-l1-" + _hash((spec["job_spec_sha256"], action_key))[:20],
                repository_anchor=repository_anchor,
                python_exec=python_exec,
                vivado_exec=vivado_exec,
                run_dir=run_dir,
                fake_startup_attestation_sha256=fake_startup_attestation_sha256,
                protocol="e1",
            )
            plan_path = arm_dir / "materialized-plan.json"
            write_e0_frozen_plan(plan, plan_path)
            authorization = build_e0_dispatch_authorization(plan, causal)
            _write_json(arm_dir / "causal-evidence.json", causal)
            _write_json(arm_dir / "dispatch-authorization.json", authorization)
            for step_auth in authorization["step_authorizations"]:
                _write_json(
                    arm_dir / "step-authorizations" / f"{step_auth['step']['step_index']:03d}.json",
                    step_auth,
                )
            arm_start = time.monotonic()
            mutation = execute_e0_real_action(
                plan,
                root_dcp,
                run_dir,
                dispatch_authorization=authorization,
                causal_evidence=causal,
                vivado_exec=vivado_exec,
                python_exec=python_exec,
                repository_anchor=repository_anchor,
            )
            arm_wall = time.monotonic() - arm_start
            heavy = int(mutation.get("real_mutation_invocations", 0))
            heavy_used += heavy
            transaction = mutation.get("transaction") or {}
            valid = bool(mutation.get("endpoint_success") and transaction.get("valid"))
            row: dict[str, Any] = {
                "action_key": action_key,
                "causal_evidence_sha256": causal["causal_evidence_sha256"],
                "authorization_sha256": authorization["authorization_sha256"],
                "action_result": mutation,
                "actual_heavy_calls": heavy,
                "wall_seconds": arm_wall,
                "valid": valid,
            }
            if valid:
                child = run_dir / "artifacts" / "sha256" / f"{transaction['sha256']}.dcp"
                scored = _score_child(
                    child,
                    spec,
                    arm_dir / "child-score",
                    vivado_exec=vivado_exec,
                    scorer_script=scorer_script,
                )
                row["child_score"] = scored
                candidates.append(
                    {
                        "source": action_key,
                        "artifact": scored["artifact"],
                        "score": scored["score"],
                        "valid": True,
                        "heavy_runtime_seconds": arm_wall,
                        "completion_order": completion_order,
                    }
                )
            decisions.append(row)
        shortfall_reason = _reconcile_l1_actual_heavy_calls(decisions, heavy_used)
        if shortfall_reason is not None:
            reasons.append(shortfall_reason)
    else:
        current_dcp = Path(root_dcp).resolve()
        current_score = spec["root"]["score"]
        for decision_index in range(4):
            if time.monotonic() - start >= 3600:
                endpoint = "TRAJECTORY_TIMEOUT"
                reasons.append("trajectory_wall_budget_exhausted")
                break
            decision_root = output / f"decision-{decision_index:02d}"
            observation = capture_real_observation(
                spec,
                current_dcp,
                decision_root / "observation",
                decision_index=decision_index,
                history=history,
                heavy_remaining=max(0, 4 - heavy_used),
                model_requests_remaining=max(0, 4 - model_requests),
                vivado_exec=vivado_exec,
                python_exec=python_exec,
                repository_anchor=repository_anchor,
                frozen_timing_score=current_score,
                e1_protocol=True,
            )
            bound = run_bound_provider_decision(
                spec,
                observation,
                decision_root / "provider",
                decision_index=decision_index,
                heavy_remaining=max(0, 4 - heavy_used),
                remaining_cost_usd=max(
                    0.0, remaining_program_cost_usd - provider_cost
                ),
                credential=credential,
            )
            model_requests += int(bound.result["physical_requests"])
            provider_cost += float(bound.result.get("cost_usd") or 0.0)
            decision_row: dict[str, Any] = {
                "provider": dict(bound.result),
                "dispatches": 0,
            }
            decisions.append(decision_row)
            if not bound.decision.accepted:
                endpoint = bound.decision.endpoint
                reasons.extend(bound.decision.reasons)
                break
            proposal = bound.decision.bound_proposal.proposal
            if proposal.kind is ProposalKind.STOP:
                endpoint = "POLICY_STOP"
                reasons.append("provider_returned_stop")
                break
            if bound.gate is None or not bound.gate.accepted or not bound.gate.gated_steps:
                endpoint = "CAPABILITY_ACTION_REJECTION"
                reasons.extend(() if bound.gate is None else bound.gate.reasons)
                break
            causal = causal_evidence_for_dispatch(spec, observation, bound)
            canonical_actions = [
                {
                    "action_key": step.action_key,
                    "parameters": dict(step.parameters),
                }
                for step in bound.gate.gated_steps
            ]
            decision_row["repeated_action_diagnostic"] = repeated_action_diagnostic(
                history, canonical_actions
            )
            plan_dir = decision_root / "dispatch"
            run_dir = decision_root / "action-run"
            plan = freeze_e0_real_gated_plan(
                current_dcp,
                root_pseudonym=spec["job"]["benchmark"],
                level=level,
                decision_id=bound.binding.decision_id,
                proposal_id=proposal.proposal_id,
                proposal_hash=proposal.proposal_hash,
                gate_decision_sha256=_hash(bound.gate.to_dict()),
                gated_steps=_gated_step_rows(bound.gate),
                causal_evidence_sha256=causal["causal_evidence_sha256"],
                run_id="e1-action-" + _hash(
                    (spec["job_spec_sha256"], decision_index, proposal.proposal_hash)
                )[:20],
                repository_anchor=repository_anchor,
                python_exec=python_exec,
                vivado_exec=vivado_exec,
                run_dir=run_dir,
                fake_startup_attestation_sha256=fake_startup_attestation_sha256,
                protocol="e1",
            )
            write_e0_gated_plan(plan, plan_dir / "materialized-plan.json")
            authorization = build_e0_dispatch_authorization(plan, causal)
            _write_json(plan_dir / "causal-evidence.json", causal)
            _write_json(plan_dir / "dispatch-authorization.json", authorization)
            for step_auth in authorization["step_authorizations"]:
                _write_json(
                    plan_dir / "step-authorizations" / f"{step_auth['step']['step_index']:03d}.json",
                    step_auth,
                )
            validate_e0_dispatch_authorization(plan, authorization, causal)
            action_start = time.monotonic()
            mutation = execute_e0_real_gated_plan(
                plan,
                current_dcp,
                run_dir,
                dispatch_authorization=authorization,
                causal_evidence=causal,
                vivado_exec=vivado_exec,
                python_exec=python_exec,
                repository_anchor=repository_anchor,
            )
            action_wall = time.monotonic() - action_start
            heavy = int(mutation.get("real_mutation_invocations", 0))
            heavy_used += heavy
            decision_row.update(
                {
                    "dispatches": int(bool(mutation.get("backend_dispatches"))),
                    "causal_evidence_sha256": causal["causal_evidence_sha256"],
                    "authorization_sha256": authorization["authorization_sha256"],
                    "action_result": mutation,
                    "actual_heavy_calls": heavy,
                    "action_wall_seconds": action_wall,
                }
            )
            transaction = mutation.get("transaction") or {}
            valid = bool(mutation.get("endpoint_success") and transaction.get("valid"))
            child_score = None
            if valid:
                child = run_dir / "artifacts" / "sha256" / f"{transaction['sha256']}.dcp"
                child_score = _score_child(
                    child,
                    spec,
                    decision_root / "child-score",
                    vivado_exec=vivado_exec,
                    scorer_script=scorer_script,
                )
                decision_row["child_score"] = child_score
            history.append(
                action_aware_history_row(
                    decision_index=decision_index,
                    actions=canonical_actions,
                    parent_qor=current_score,
                    child_qor={} if child_score is None else child_score["score"],
                    valid=valid,
                    endpoint=mutation.get("endpoint", "UNKNOWN"),
                    actual_heavy_calls=heavy,
                )
            )
            if not valid:
                endpoint = mutation.get("endpoint", "TOOL_EXECUTION_FAILURE")
                reasons.extend(mutation.get("reasons", []))
                break
            assert child_score is not None
            current_dcp = child
            current_score = child_score["score"]
            candidates.append(
                {
                    "source": f"decision-{decision_index:02d}",
                    "artifact": child_score["artifact"],
                    "score": child_score["score"],
                    "valid": True,
                    "heavy_runtime_seconds": action_wall,
                    "completion_order": decision_index + 1,
                }
            )
            if heavy_used >= 4 or model_requests >= 4:
                reasons.append("trajectory_budget_exhausted")
                break
    best = _choose_best(candidates)
    result = {
        "schema_version": E1_TRAJECTORY_RESULT_SCHEMA,
        "study_status": "E1_KNOWN_SET_DESCRIPTIVE",
        "job": spec["job"],
        "job_spec_sha256": spec["job_spec_sha256"],
        "endpoint": endpoint,
        "reasons": reasons,
        "decisions": decisions,
        "history": history,
        "model_requests": model_requests,
        "provider_cost_usd": provider_cost,
        "heavy_calls": heavy_used,
        "selection_candidates": candidates,
        "best_valid_selection": best,
        "wall_seconds": time.monotonic() - start,
    }
    result = result | {"result_sha256": _hash(result)}
    _write_json(output / "trajectory-result.json", result)
    return result


__all__ = [
    "execute_real_e1_job",
    "freeze_e1_job_bundle",
    "validate_e1_job_bundle",
]
