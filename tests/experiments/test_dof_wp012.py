from __future__ import annotations

from types import SimpleNamespace

import pytest

from optimizer_pipeline.dof.catalogues import (
    filtered_nested_action_catalogue,
    filtered_nested_capability_audit,
    nested_action_catalogue,
)
from optimizer_pipeline.dof.common_observation import build_common_policy_context
from optimizer_pipeline.dof.contracts import DesignStage, DoFLevel, EventClass, UnknownRegime
from optimizer_pipeline.dof.e1 import E1_BENCHMARKS
from optimizer_pipeline.dof.e1v2 import (
    E1V2_REPEAT1_MATRIX,
    WP012_EXCLUDED_ACTIONS,
    WP012_FORCED_UNKNOWN_BENCHMARKS,
    WP012_FORCED_UNKNOWN_L1_ACTIONS,
    WP012_HOLDOUTS,
    WP012_H_MATRIX,
    WP012_K_MATRIX,
    WP012_U_MATRIX,
    protocol_manifest,
    wp012_forced_unknown_jobs,
    wp012_holdout_jobs,
    wp012_known_jobs,
)
from optimizer_pipeline.dof.e1v2_job import (
    _condition_runtime,
    freeze_e1v2_job_bundle,
    validate_e1v2_job_bundle,
)
from optimizer_pipeline.dof.generated import GENERATED_ACTION_KEY
from optimizer_pipeline.dof.gate import ACTION_PROPOSAL_SCHEMA_VERSION, ActionGate
from optimizer_pipeline.dof.openrouter import OPENROUTER_E1_MODELS


def _source(benchmark: str = "amd_mini_isp") -> dict:
    return {
        "condition": {"pseudonym": "condition-" + benchmark},
        "state": {
            "pseudonym": "state-" + benchmark,
            "lineage_pseudonym": "lineage-" + benchmark,
            "stage": "ROUTED",
            "measurements": {
                "timing": {"wns_ns": -0.1, "estimated_fmax_mhz": 300.0},
                "route": {"status": "fully routed", "unrouted_nets": 0},
            },
        },
        "history": [],
        "budget_remaining": {
            "IMPLEMENTATION_HEAVY": 4,
            "MUTATION_LIGHT": 3,
            "READ_ONLY": 20,
            "LLM_REQUEST": 4,
            "WALL_TIME_SECONDS": 3700.0,
        },
        "validation": {"status": "CLEAN", "errors": [], "warnings": []},
        "stop_conditions": ["budget exhausted"],
        "known_priors": {"known_winner": "macro.place_route"},
    }


def _spec(*, regime: str, benchmark: str, excluded: tuple[str, ...] = ()) -> dict:
    return {
        "job": {
            "level": "L3",
            "regime": regime,
            "benchmark": benchmark,
            "excluded_action_keys": list(excluded),
        }
    }


def test_wp011_repeat1_manifest_is_byte_stable() -> None:
    manifest = protocol_manifest(matrix=E1V2_REPEAT1_MATRIX, roots=E1_BENCHMARKS)
    assert manifest["protocol_sha256"] == (
        "e065b4e20eec254b5a96c93f897852e920a1367a34a98ca8e87c3524b3d24e56"
    )
    assert "condition_contract" not in manifest


def test_wp012_exact_inventories_and_deterministic_order() -> None:
    cases = (
        (wp012_known_jobs, 144),
        (wp012_forced_unknown_jobs, 174),
        (wp012_holdout_jobs, 58),
    )
    for factory, expected in cases:
        first = factory()
        assert first == factory()
        assert len(first) == expected
        assert [row["order"] for row in first] == list(range(expected))
        assert len({row["job_id"] for row in first}) == expected
    known = wp012_known_jobs()
    assert {row["repeat"] for row in known} == {2, 3}
    assert {row["level"] for row in known} == {"L2", "L3", "L4"}
    forced = wp012_forced_unknown_jobs()
    assert sum(row["level"] == "L0" for row in forced) == 6
    assert sum(row["level"] == "L1" for row in forced) == 6
    assert sum(row["level"] in {"L2", "L3", "L4"} for row in forced) == 162
    holdout = wp012_holdout_jobs()
    assert sum(row["level"] in {"L0", "L1"} for row in holdout) == 4
    assert sum(row["level"] in {"L2", "L3", "L4"} for row in holdout) == 54


def test_filtered_catalogue_preserves_nested_p_g_and_action_bytes() -> None:
    audit = filtered_nested_capability_audit(
        excluded_action_keys=WP012_EXCLUDED_ACTIONS
    )
    assert audit["accepted"] is True
    for level in (DoFLevel.L2, DoFLevel.L3, DoFLevel.L4):
        known = nested_action_catalogue(level)
        filtered = filtered_nested_action_catalogue(
            level, excluded_action_keys=WP012_EXCLUDED_ACTIONS
        )
        assert filtered.action("macro.place_route") is None
        for action in filtered.actions:
            assert action.to_dict() == known.action(action.name).to_dict()
    l3 = filtered_nested_action_catalogue(
        DoFLevel.L3, excluded_action_keys=WP012_EXCLUDED_ACTIONS
    )
    assert {
        "manual.unroute_all",
        "manual.unplace_all",
        "manual.place_design",
        "manual.route_design",
    } <= {action.name for action in l3.actions}
    l4 = filtered_nested_action_catalogue(
        DoFLevel.L4, excluded_action_keys=WP012_EXCLUDED_ACTIONS
    )
    assert l4.action(GENERATED_ACTION_KEY) is not None


def test_known_condition_extension_is_a_noop() -> None:
    source = _source("opaque")
    source["known_priors"] = {}
    default = build_common_policy_context(
        level=DoFLevel.L3,
        source=source,
        current_artifact_sha256="a" * 64,
        decision_index=0,
    )
    regime, catalogue, mask = _condition_runtime(
        _spec(regime="known", benchmark="opaque"), DoFLevel.L3
    )
    explicit = build_common_policy_context(
        level=DoFLevel.L3,
        source=source,
        current_artifact_sha256="a" * 64,
        decision_index=0,
        regime=regime,
        catalogue=catalogue,
        mask=mask,
    )
    assert regime is UnknownRegime.KNOWN
    assert explicit.evidence() == default.evidence()
    assert catalogue.canonical_bytes() == nested_action_catalogue(DoFLevel.L3).canonical_bytes()


def test_forced_unknown_context_has_no_mask_identity_or_known_winner_leakage() -> None:
    benchmark = "amd_mini_isp"
    regime, catalogue, mask = _condition_runtime(
        _spec(
            regime="forced_unknown",
            benchmark=benchmark,
            excluded=WP012_EXCLUDED_ACTIONS,
        ),
        DoFLevel.L3,
    )
    context = build_common_policy_context(
        level=DoFLevel.L3,
        source=_source(benchmark),
        current_artifact_sha256="b" * 64,
        decision_index=0,
        regime=regime,
        catalogue=catalogue,
        mask=mask,
    )
    visible = context.provider_context_bytes.decode() + context.provider_prompt_bytes.decode()
    for forbidden in (
        benchmark,
        mask.version,
        "macro.place_route",
        "placement_routing",
        "known_winner",
    ):
        assert forbidden not in visible
    assert "manual.place_design" in visible
    assert "manual.route_design" in visible


def test_natural_unknown_uses_full_catalogue_without_known_priors() -> None:
    benchmark = WP012_HOLDOUTS[0]
    regime, catalogue, mask = _condition_runtime(
        _spec(regime="natural_unknown", benchmark=benchmark), DoFLevel.L4
    )
    context = build_common_policy_context(
        level=DoFLevel.L4,
        source=_source(benchmark),
        current_artifact_sha256="c" * 64,
        decision_index=0,
        regime=regime,
        catalogue=catalogue,
        mask=mask,
    )
    assert catalogue.canonical_bytes() == nested_action_catalogue(DoFLevel.L4).canonical_bytes()
    assert "known_priors" not in context.base_observation
    assert context.base_observation["condition"]["pseudonym"] == "condition-" + benchmark


@pytest.mark.parametrize(
    "matrix,roots,expected",
    [
        (WP012_K_MATRIX, E1_BENCHMARKS, 144),
        (WP012_U_MATRIX, WP012_FORCED_UNKNOWN_BENCHMARKS, 174),
        (WP012_H_MATRIX, WP012_HOLDOUTS, 58),
    ],
)
def test_wp012_bundle_freeze_and_validation(
    matrix: str,
    roots: tuple[str, ...],
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from optimizer_pipeline.dof import e1v2_job

    root_paths = {name: f"/{name}.dcp" for name in roots}
    rows = []
    for index, name in enumerate(roots):
        rows.append(
            {
                "benchmark": name,
                "artifact": {"sha256": f"{index + 1:064x}", "size_bytes": 100 + index},
                "score": {"fmax_mhz": 100.0 + index, "wns_ns": -0.1},
                "validation_sha256": f"{index + 101:064x}",
            }
        )
    identities = {
        root_paths[name]: SimpleNamespace(
            sha256=row["artifact"]["sha256"],
            size_bytes=row["artifact"]["size_bytes"],
        )
        for name, row in zip(roots, rows)
    }
    monkeypatch.setattr(
        e1v2_job,
        "observe_repository",
        lambda _path: SimpleNamespace(
            tracked_tree_dirty=False,
            branch_name="test",
            head_commit="1" * 40,
            head_tree="2" * 40,
            binary_diff_sha256="3" * 64,
        ),
    )
    monkeypatch.setattr(
        e1v2_job, "fingerprint_file", lambda path: identities[str(path)]
    )
    monkeypatch.setattr(
        e1v2_job, "verified_fake_attestation_evidence_sha256", lambda _path: "4" * 64
    )
    discovery = {
        "all_profiles_available": True,
        "models": {
            "required_models": {model: True for model in OPENROUTER_E1_MODELS}
        },
        "endpoints": {"models": {model: {} for model in OPENROUTER_E1_MODELS}},
        "unknown_cost_reserve_verification": {
            "all_models_accepted": True,
            "verification_sha256": "5" * 64,
        },
    }
    root_audit = {
        "all_accepted": True,
        "audit_sha256": "6" * 64,
        "rows": rows,
    }
    bundle = freeze_e1v2_job_bundle(
        root_paths,
        root_audit,
        discovery,
        matrix=matrix,
        repository_anchor="/repo",
        fake_startup_attestation="/attestation.json",
    )
    assert len(bundle["jobs"]) == expected
    assert {
        row["budgets"]["provider_cost_usd"]
        for row in bundle["jobs"]
        if row["job"]["model"] is not None
    } == {0.4}
    if matrix == WP012_U_MATRIX:
        assert {
            row["budgets"]["heavy_calls"]
            for row in bundle["jobs"]
            if row["job"]["level"] == "L1"
        } == {2}
    assert validate_e1v2_job_bundle(bundle) == bundle


def test_forced_unknown_l1_portfolio_removes_only_place_route() -> None:
    assert WP012_FORCED_UNKNOWN_L1_ACTIONS == ("macro.phys_opt", "macro.retime")
    jobs = [row for row in wp012_forced_unknown_jobs() if row["level"] == "L1"]
    assert all(tuple(row["l1_actions"]) == WP012_FORCED_UNKNOWN_L1_ACTIONS for row in jobs)
    assert {row["benchmark"] for row in jobs} == set(WP012_FORCED_UNKNOWN_BENCHMARKS)


def test_forced_unknown_gate_has_zero_dispatchable_steps_for_excluded_macro() -> None:
    _regime, catalogue, mask = _condition_runtime(
        _spec(
            regime="forced_unknown",
            benchmark="amd_mini_isp",
            excluded=WP012_EXCLUDED_ACTIONS,
        ),
        DoFLevel.L3,
    )
    decision = ActionGate().evaluate(
        {
            "schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
            "proposal_id": "masked-proposal",
            "decision_id": "masked-decision",
            "catalogue_version": catalogue.version,
            "catalogue_hash": catalogue.sha256,
            "kind": "ACTION",
            "steps": [
                {
                    "step_id": "step-0",
                    "action_key": "macro.place_route",
                    "parameters": {},
                }
            ],
        },
        level=DoFLevel.L3,
        expected_decision_id="masked-decision",
        catalogue=catalogue,
        expected_catalogue_hash=catalogue.sha256,
        current_stage=DesignStage.ROUTED,
        budget_remaining={event: 8 for event in EventClass},
        mask=mask,
        capability_contract="e1v2",
    )
    assert decision.accepted is False
    assert decision.gated_steps == ()
