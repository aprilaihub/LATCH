from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import sys

import pytest

from optimizer_pipeline.dof.catalogues import clean_macro_catalogue
from optimizer_pipeline.dof.contracts import DoFLevel, UnknownRegime
from optimizer_pipeline.dof.e1 import (
    E1_BENCHMARKS,
    E1_L1_ACTIONS,
    action_aware_history_row,
    e1_jobs,
    e1_protocol_manifest,
    l1_portfolio_contract,
    root_reference_check,
    repeated_action_diagnostic,
    score_timing,
)
from optimizer_pipeline.dof.e1_job import _reconcile_l1_actual_heavy_calls
from optimizer_pipeline.dof.masks import create_mask_rules
from optimizer_pipeline.dof import preflight as preflight_module
from optimizer_pipeline.dof.preflight import freeze_e1_observation_preflight_plan
from optimizer_pipeline.dof.projector import DecisionContextProjector
from optimizer_pipeline.dof import real_mutation
from optimizer_pipeline.dof.artifacts import RootFingerprint
from optimizer_pipeline.dof.real_mutation import (
    RealMutationPlanError,
    freeze_e0_real_gated_plan,
    validate_e0_real_gated_plan,
)


def _source(history: list[dict]) -> dict:
    return {
        "condition": {"pseudonym": "condition-opaque"},
        "state": {
            "pseudonym": "state-opaque",
            "lineage_pseudonym": "lineage-opaque",
            "stage": "ROUTED",
            "measurements": {
                "timing": {
                    "wns_ns": -0.882,
                    "tns_ns": -670.031,
                    "failing_endpoints": 1937,
                    "clock_period_ns": 3.0,
                    "estimated_fmax_mhz": 257.599,
                },
                "route": {"status": "fully routed", "unrouted_nets": 0},
            },
        },
        "history": history,
        "budget_remaining": {
            "IMPLEMENTATION_HEAVY": 3,
            "MUTATION_LIGHT": 3,
            "READ_ONLY": 20,
            "LLM_REQUEST": 3,
            "WALL_TIME_SECONDS": 1200.0,
        },
        "validation": {"status": "CLEAN", "errors": [], "warnings": []},
        "stop_conditions": ["budget exhausted"],
        "known_priors": {},
    }


def test_action_history_exposes_canonical_actions_not_proposal_identity() -> None:
    history = action_aware_history_row(
        decision_index=0,
        actions=[{"action_key": "macro.phys_opt", "parameters": {}}],
        parent_qor={"fmax_mhz": 310.17, "wns_ns": -1.654},
        child_qor={"fmax_mhz": 407.8, "wns_ns": -0.882},
        valid=True,
        endpoint="VALID_COMPLETION",
        actual_heavy_calls=1,
    )
    projected = DecisionContextProjector().project(
        level=DoFLevel.L2,
        regime=UnknownRegime.KNOWN,
        source=_source([history]),
        catalogue=clean_macro_catalogue(DoFLevel.L2),
        mask=create_mask_rules(version="dof.e1-test-mask.v1"),
    )
    visible = projected.to_dict()["context"]["history"][0]
    assert visible["actions"] == [{"action_key": "macro.phys_opt", "parameters": {}}]
    assert visible["parent_qor"]["fmax_mhz"] == 310.17
    assert "proposal" not in projected.serialized
    assert projected.context_hash == __import__("hashlib").sha256(projected.serialized.encode()).hexdigest()


def test_plan_identifier_is_rejected_as_history_action() -> None:
    with pytest.raises(ValueError, match="canonical_action_sequence_required"):
        action_aware_history_row(
            decision_index=0,
            actions=[{"action_key": "plan:proposal-123", "parameters": {}}],
            parent_qor={"fmax_mhz": 1.0, "wns_ns": -1.0},
            child_qor={"fmax_mhz": 1.0, "wns_ns": -1.0},
            valid=True,
            endpoint="VALID_COMPLETION",
            actual_heavy_calls=1,
        )


def test_repeated_action_diagnostic_uses_canonical_actions_only() -> None:
    actions = [{"action_key": "macro.retime", "parameters": {}}]
    history = [
        action_aware_history_row(
            decision_index=0,
            actions=actions,
            parent_qor={"fmax_mhz": 300.0, "wns_ns": -1.0},
            child_qor={"fmax_mhz": 301.0, "wns_ns": -0.9},
            valid=True,
            endpoint="VALID_COMPLETION",
            actual_heavy_calls=1,
        )
    ]
    first = repeated_action_diagnostic(history, actions)
    renamed = repeated_action_diagnostic(
        history,
        [{"action_key": "macro.phys_opt", "parameters": {}}],
    )
    assert first["repeated"] is True
    assert first["matching_prior_decision_indices"] == [0]
    assert renamed["repeated"] is False
    assert "proposal" not in json.dumps(first)


def test_scorer_reuses_achievable_period_semantics_and_reference_gate() -> None:
    score = score_timing(
        target_clock="clk_fpl26contest",
        target_period_ns=1.569,
        wns_ns=-1.654,
        tns_ns=-671.139,
    )
    assert score["achievable_period_ns"] == pytest.approx(3.223)
    assert score["fmax_mhz"] == pytest.approx(310.2699)
    assert root_reference_check("vexriscv_re-place", score)["accepted"] is True


def test_l1_is_three_root_siblings_and_exactly_four_heavy_calls() -> None:
    portfolio = l1_portfolio_contract()
    assert tuple(arm["action_key"] for arm in portfolio["arms"]) == E1_L1_ACTIONS
    assert all(arm["root_sibling"] for arm in portfolio["arms"])
    assert [arm["implementation_heavy_calls"] for arm in portfolio["arms"]] == [1, 1, 2]
    assert portfolio["total_implementation_heavy_calls"] == 4


def test_l1_actual_heavy_shortfall_is_retained_only_after_arm_failure() -> None:
    complete = [{"valid": True}, {"valid": True}, {"valid": True}]
    partial = [{"valid": False}, {"valid": False}, {"valid": False}]

    assert _reconcile_l1_actual_heavy_calls(complete, 4) is None
    assert (
        _reconcile_l1_actual_heavy_calls(partial, 3)
        == "l1_declared_heavy_calls_short_circuited_by_arm_failure"
    )
    with pytest.raises(RuntimeError, match="actual_heavy_call_count_mismatch"):
        _reconcile_l1_actual_heavy_calls(complete, 3)
    with pytest.raises(RuntimeError, match="actual_heavy_call_count_mismatch"):
        _reconcile_l1_actual_heavy_calls(partial, 5)


def test_e1_inventory_is_exactly_160_and_deterministic() -> None:
    first = e1_jobs()
    second = e1_jobs()
    assert first == second
    assert len(first) == 160
    assert len({row["job_id"] for row in first}) == 160
    assert {row["benchmark"] for row in first} == set(E1_BENCHMARKS)
    manifest = e1_protocol_manifest()
    assert manifest["job_count"] == 160
    assert json.loads(json.dumps(manifest, sort_keys=True))["bundle_sha256"] == manifest["bundle_sha256"]


@dataclass(frozen=True)
class _Repository:
    head_commit: str = "1" * 40
    head_tree: str = "2" * 40
    binary_diff_sha256: str = "3" * 64
    tracked_tree_dirty: bool = False
    branch_name: str = "phase0-policy-evidence-replan"


def test_e1_observation_timeout_contract_uses_gate2_runtime_bounds(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(preflight_module, "observe_repository", lambda _path: _Repository())
    monkeypatch.setattr(
        preflight_module,
        "fingerprint_file",
        lambda _path: RootFingerprint("a" * 64, 1234),
    )
    root = tmp_path / "root.dcp"
    root.write_bytes(b"root")
    plan = freeze_e1_observation_preflight_plan(
        root,
        dcp_pseudonym="characterised-root",
        run_id="e1-observation-test",
        repository_anchor=tmp_path,
    )

    assert plan.preflight_id == "e1-decision-readonly"
    assert plan.whole_run_timeout_seconds == 600.0
    assert plan.call_timeouts_seconds["vivado.report_timing_summary"] == 300.0
    assert plan.call_timeouts_seconds["vivado.open_checkpoint"] == 300.0


def test_e1_gated_plan_admits_exactly_four_heavy_calls_but_e0_does_not(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(real_mutation, "observe_repository", lambda _path: _Repository())
    monkeypatch.setattr(
        real_mutation,
        "fingerprint_file",
        lambda _path: RootFingerprint("a" * 64, 1234),
    )
    root = tmp_path / "root.dcp"
    root.write_bytes(b"root")
    vivado = tmp_path / "Vivado" / "2025.1" / "bin" / "vivado"
    vivado.parent.mkdir(parents=True)
    vivado.write_text("#!/bin/sh\nexit 0\n")
    vivado.chmod(0o700)
    steps = [
        {
            "step_index": index,
            "step_id": f"step-{index}",
            "action_key": "manual.phys_opt_design",
            "parameters": {"directive": "Explore"},
            "stage_before": "ROUTED",
            "stage_after": "ROUTED",
        }
        for index in range(4)
    ]
    common = dict(
        root_dcp=root,
        root_pseudonym="root",
        level=DoFLevel.L3,
        decision_id="decision-four-heavy",
        proposal_id="proposal-four-heavy",
        proposal_hash="b" * 64,
        gate_decision_sha256="c" * 64,
        gated_steps=steps,
        causal_evidence_sha256="d" * 64,
        run_id="e1-four-heavy",
        repository_anchor=tmp_path,
        python_exec=sys.executable,
        vivado_exec=vivado,
        run_dir=tmp_path / "run",
        fake_startup_attestation_sha256="e" * 64,
    )
    with pytest.raises(RealMutationPlanError, match="e0_heavy_call_cap_exceeded"):
        freeze_e0_real_gated_plan(**common)
    plan = freeze_e0_real_gated_plan(**common, protocol="e1")
    assert validate_e0_real_gated_plan(plan) == plan
    assert plan["selection_policy"] == "e1_valid_routed_fmax_external_scorer"
