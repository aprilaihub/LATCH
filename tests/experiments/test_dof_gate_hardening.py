from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from optimizer_pipeline.core.store import SQLiteRunStore
from optimizer_pipeline.dof import runner as dof_runner
from optimizer_pipeline.dof.catalogues import (
    clean_macro_catalogue,
    strict_l3_catalogue,
)
from optimizer_pipeline.dof.contracts import (
    DesignStage,
    DoFLevel,
    EndpointCode,
    EventClass,
)
from optimizer_pipeline.dof.gate import (
    ACTION_PROPOSAL_SCHEMA_VERSION,
    ActionProposal,
    ActionStepProposal,
    ExperimentActionGate,
    ProposalKind,
)
from optimizer_pipeline.dof.manifests import (
    ACTION_INTERFACE_SCHEMA_VERSION,
    action_interface_schema,
)
from optimizer_pipeline.dof.provenance import RepositoryObservation
from optimizer_pipeline.dof.spec import MaskSpec


DECISION_ID = "decision-current"
_DEFAULT_MASK = object()
CONFIG_ROOT = (
    Path(__file__).resolve().parents[2]
    / "configs"
    / "experiments"
    / "dof_contract"
)


def _catalogue(level: DoFLevel):
    if level in {DoFLevel.L1, DoFLevel.L2}:
        return clean_macro_catalogue(level)
    return strict_l3_catalogue()


def _typed_proposal(
    level: DoFLevel,
    kind: ProposalKind,
    steps: tuple[ActionStepProposal, ...],
    **updates,
) -> ActionProposal:
    catalogue = _catalogue(level)
    values = {
        "schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "proposal-current",
        "decision_id": DECISION_ID,
        "catalogue_version": catalogue.version,
        "catalogue_hash": catalogue.sha256,
        "kind": kind,
        "steps": steps,
    }
    values.update(updates)
    return ActionProposal(**values)


def _evaluate(
    level: DoFLevel,
    payload: ActionProposal | dict,
    *,
    expected_decision_id: str = DECISION_ID,
    max_plan_steps: int = 8,
    mask: object = _DEFAULT_MASK,
):
    catalogue = _catalogue(level)
    bound_mask = (
        MaskSpec.create(version="mask.test.v1")
        if mask is _DEFAULT_MASK
        else mask
    )
    return ExperimentActionGate(max_plan_steps=max_plan_steps).evaluate(
        payload,
        level=level,
        expected_decision_id=expected_decision_id,
        catalogue=catalogue,
        expected_catalogue_hash=catalogue.sha256,
        current_stage=DesignStage.ROUTED,
        budget_remaining={event_class: 20 for event_class in EventClass},
        mask=bound_mask,
    )


def _step(
    step_id: str,
    action_key: str,
    parameters: object | None = None,
) -> ActionStepProposal:
    return ActionStepProposal(
        step_id=step_id,
        action_key=action_key,
        parameters={} if parameters is None else parameters,  # type: ignore[arg-type]
    )


def _assert_no_dispatch(decision, endpoint: EndpointCode, reason: str) -> None:
    assert decision.accepted is False
    assert decision.endpoint is endpoint
    assert decision.gated_steps == ()
    assert decision.required_units == {}
    assert any(reason in item for item in decision.reasons)


def test_direct_typed_l1_action_is_revalidated_against_manifest_kinds() -> None:
    decision = _evaluate(
        DoFLevel.L1,
        _typed_proposal(
            DoFLevel.L1,
            ProposalKind.ACTION,
            (_step("macro", "macro.phys_opt"),),
        ),
    )
    _assert_no_dispatch(
        decision,
        EndpointCode.CAPABILITY_ACTION_REJECTION,
        "proposal_kind_not_allowed:L1:ACTION",
    )


def test_direct_typed_l2_multi_step_action_is_reparsed() -> None:
    decision = _evaluate(
        DoFLevel.L2,
        _typed_proposal(
            DoFLevel.L2,
            ProposalKind.ACTION,
            (
                _step("first", "macro.phys_opt"),
                _step("second", "macro.phys_opt"),
            ),
        ),
    )
    _assert_no_dispatch(
        decision,
        EndpointCode.INVALID_POLICY_OUTPUT,
        "action_requires_exactly_one_step:2",
    )


def test_direct_typed_l3_overlong_plan_is_reparsed() -> None:
    proposal = _typed_proposal(
        DoFLevel.L3,
        ProposalKind.PLAN,
        tuple(
            _step(
                f"step-{index}",
                "manual.phys_opt_design",
                {"directive": "Explore"},
            )
            for index in range(9)
        ),
    )
    decision = _evaluate(DoFLevel.L3, proposal, max_plan_steps=8)
    _assert_no_dispatch(
        decision,
        EndpointCode.INVALID_POLICY_OUTPUT,
        "plan_too_long:9>8",
    )


@pytest.mark.parametrize("max_plan_steps", [1, 7, 9])
def test_gate_cannot_change_declared_plan_limit(max_plan_steps: int) -> None:
    with pytest.raises(ValueError, match="must equal"):
        ExperimentActionGate(max_plan_steps=max_plan_steps)


def test_action_interface_v2_binds_decision_freshness_and_cardinality() -> None:
    assert ACTION_INTERFACE_SCHEMA_VERSION == "dof.action-interface-schema.v2"
    l2 = action_interface_schema(DoFLevel.L2)
    l3 = action_interface_schema(DoFLevel.L3)
    for schema in (l2, l3):
        assert schema["decision_id_must_match_active_decision"] is True
        assert schema["proposal_revalidation"] == "canonical_strict_parser"
        assert schema["max_plan_steps"] == 8
        assert schema["kind_step_cardinality"]["STOP"] == {
            "min_steps": 0,
            "max_steps": 0,
        }
        assert schema["kind_step_cardinality"]["ACTION"] == {
            "min_steps": 1,
            "max_steps": 1,
        }
    assert "PLAN" not in l2["kind_step_cardinality"]
    assert l3["kind_step_cardinality"]["PLAN"] == {
        "min_steps": 1,
        "max_steps": 8,
    }


def test_direct_typed_duplicate_step_ids_are_reparsed() -> None:
    decision = _evaluate(
        DoFLevel.L3,
        _typed_proposal(
            DoFLevel.L3,
            ProposalKind.PLAN,
            (
                _step("same", "manual.phys_opt_design", {"directive": "Explore"}),
                _step("same", "manual.phys_opt_design", {"directive": "Explore"}),
            ),
        ),
    )
    _assert_no_dispatch(
        decision,
        EndpointCode.INVALID_POLICY_OUTPUT,
        "duplicate_step_ids",
    )


def test_direct_typed_malformed_id_is_reparsed() -> None:
    decision = _evaluate(
        DoFLevel.L3,
        _typed_proposal(
            DoFLevel.L3,
            ProposalKind.ACTION,
            (_step("valid-step", "manual.phys_opt_design", {"directive": "Explore"}),),
            proposal_id="not a canonical id",
        ),
    )
    _assert_no_dispatch(
        decision,
        EndpointCode.INVALID_POLICY_OUTPUT,
        "proposal_id_not_canonical_id",
    )


@pytest.mark.parametrize("location", ["envelope", "step"])
def test_mapping_with_non_string_schema_key_is_structured_rejection(
    location: str,
) -> None:
    catalogue = _catalogue(DoFLevel.L2)
    payload: dict = {
        "schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "proposal-current",
        "decision_id": DECISION_ID,
        "catalogue_version": catalogue.version,
        "catalogue_hash": catalogue.sha256,
        "kind": "ACTION",
        "steps": [
            {
                "step_id": "one",
                "action_key": "macro.phys_opt",
                "parameters": {},
            }
        ],
    }
    if location == "envelope":
        payload[1] = "bad"
        reason = "payload_has_non_string_key"
    else:
        payload["steps"][0][1] = "bad"
        reason = "step_0_has_non_string_key"
    decision = _evaluate(DoFLevel.L2, payload)
    _assert_no_dispatch(decision, EndpointCode.INVALID_POLICY_OUTPUT, reason)


def test_stale_typed_decision_id_is_rejected_before_dispatch() -> None:
    stale = _typed_proposal(
        DoFLevel.L2,
        ProposalKind.ACTION,
        (_step("macro", "macro.phys_opt"),),
        decision_id="decision-stale",
    )
    decision = _evaluate(DoFLevel.L2, stale)
    _assert_no_dispatch(
        decision,
        EndpointCode.CAPABILITY_ACTION_REJECTION,
        "proposal_decision_id_mismatch",
    )


def test_runner_stale_policy_response_creates_no_attempt_or_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    empty_hash = hashlib.sha256(b"").hexdigest()
    monkeypatch.setattr(
        dof_runner,
        "observe_repository",
        lambda _anchor: RepositoryObservation(
            observed_at="2026-08-06T12:00:00.000000+00:00",
            repository_root_identity_sha256="1" * 64,
            head_commit="2" * 40,
            head_tree="3" * 40,
            ref_state="branch",
            branch_name="test-branch",
            tracked_tree_dirty=False,
            staged_entry_count=0,
            unstaged_entry_count=0,
            conflicted_entry_count=0,
            assume_unchanged_entry_count=0,
            skip_worktree_entry_count=0,
            tracked_status_sha256=empty_hash,
            index_flag_status_sha256="5" * 64,
            binary_diff_sha256=hashlib.sha256(
                b"cached\0\0working\0"
            ).hexdigest(),
            submodule_status_sha256=empty_hash,
            submodule_count=0,
            nonclean_submodule_count=0,
            untracked_entry_count=0,
            untracked_status_sha256=empty_hash,
            git_version="git version 2.43.0",
        ),
    )
    original = dof_runner._policy_envelope

    def stale_envelope(spec, decision_id):
        envelope = original(spec, decision_id)
        envelope["decision_id"] = "decision-stale"
        return envelope

    monkeypatch.setattr(dof_runner, "_policy_envelope", stale_envelope)
    run_dir = tmp_path / "stale-decision"
    summary = dof_runner.run_mock_experiment(
        CONFIG_ROOT / "03_l2_legal_known.json",
        run_dir,
    )

    assert summary["endpoint"] == EndpointCode.CAPABILITY_ACTION_REJECTION.value
    assert summary["accepted_steps"] == 0
    assert summary["outer_attempt_ids"] == []
    assert summary["reasons"] == ["proposal_decision_id_mismatch"]
    with SQLiteRunStore(run_dir / "run.db", migrate=False) as store:
        assert store.attempts() == ()
        assert store.outcomes() == ()
        assert store.actions() == ()
        assert store.proposals() == ()


@pytest.mark.parametrize(
    ("invalid_mask", "reason"),
    [
        (None, "mask_missing"),
        (
            {
                "version": "mask.test.v1",
                "hash": "a" * 64,
                "hidden_macro_families": [],
                "forbidden_sentinels": [],
                "known_prior_allowlist": [],
            },
            "mask.hash_content_mismatch",
        ),
    ],
)
def test_invalid_or_missing_mask_rejects_even_stop_before_dispatch(
    invalid_mask: object,
    reason: str,
) -> None:
    stop = _typed_proposal(DoFLevel.L2, ProposalKind.STOP, ())
    decision = _evaluate(DoFLevel.L2, stop, mask=invalid_mask)
    _assert_no_dispatch(
        decision,
        EndpointCode.CAPABILITY_ACTION_REJECTION,
        reason,
    )


def test_gate_uses_validated_mask_hidden_families_as_its_only_rule_source() -> None:
    catalogue = _catalogue(DoFLevel.L2)
    action = catalogue.actions[0]
    bound_mask = MaskSpec.create(
        version="mask.hidden-family.v1",
        hidden_macro_families=(action.family,),
    )
    payload = _typed_proposal(
        DoFLevel.L2,
        ProposalKind.ACTION,
        (_step("masked", action.name),),
    )
    decision = _evaluate(DoFLevel.L2, payload, mask=bound_mask)
    _assert_no_dispatch(
        decision,
        EndpointCode.MASK_VIOLATION,
        "masked_action_family",
    )


@pytest.mark.parametrize("level", [DoFLevel.L1, DoFLevel.L2, DoFLevel.L3])
def test_content_valid_unknown_hidden_family_rejects_with_zero_steps(
    level: DoFLevel,
) -> None:
    kind = ProposalKind.PLAN if level in {DoFLevel.L1, DoFLevel.L3} else ProposalKind.ACTION
    action_key = (
        "manual.phys_opt_design"
        if level is DoFLevel.L3
        else "macro.phys_opt"
    )
    parameters = {"directive": "Explore"} if level is DoFLevel.L3 else {}
    mask = MaskSpec.create(
        version="mask.typo-family.v1",
        hidden_macro_families=("retimng",),
    )
    decision = _evaluate(
        level,
        _typed_proposal(
            level,
            kind,
            (_step("one", action_key, parameters),),
        ),
        mask=mask,
    )
    _assert_no_dispatch(
        decision,
        EndpointCode.CAPABILITY_ACTION_REJECTION,
        "mask.hidden_macro_families_unknown:retimng",
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("schema", "unsupported_schema_version"),
        ("non_json", "parameters_not_json"),
        ("non_string_key", "parameters_not_json"),
    ],
)
def test_typed_object_route_preserves_defects_for_the_strict_parser(
    mutation: str,
    reason: str,
) -> None:
    valid = _typed_proposal(
        DoFLevel.L3,
        ProposalKind.ACTION,
        (_step("opt", "manual.phys_opt_design", {"directive": "Explore"}),),
    )
    if mutation == "schema":
        malformed = replace(valid, schema_version="action_proposal.future")
    elif mutation == "non_json":
        malformed = replace(
            valid,
            steps=(
                _step("opt", "manual.phys_opt_design", {"bad": object()}),
            ),
        )
    else:
        malformed = replace(valid, steps=(_step("opt", "manual.phys_opt_design", {1: "bad"}),))

    decision = _evaluate(DoFLevel.L3, malformed)
    _assert_no_dispatch(decision, EndpointCode.INVALID_POLICY_OUTPUT, reason)


@pytest.mark.parametrize(
    ("level", "kind", "steps"),
    [
        (
            DoFLevel.L1,
            ProposalKind.PLAN,
            (_step("macro", "macro.phys_opt"),),
        ),
        (
            DoFLevel.L2,
            ProposalKind.ACTION,
            (_step("macro", "macro.phys_opt"),),
        ),
        (DoFLevel.L2, ProposalKind.STOP, ()),
        (
            DoFLevel.L3,
            ProposalKind.ACTION,
            (_step("opt", "manual.phys_opt_design", {"directive": "Explore"}),),
        ),
        (
            DoFLevel.L3,
            ProposalKind.PLAN,
            (
                _step("opt-1", "manual.phys_opt_design", {"directive": "Explore"}),
                _step("opt-2", "manual.phys_opt_design", {"directive": "Explore"}),
            ),
        ),
        (DoFLevel.L3, ProposalKind.STOP, ()),
    ],
)
def test_manifest_declared_legal_kinds_remain_accepted(
    level: DoFLevel,
    kind: ProposalKind,
    steps: tuple[ActionStepProposal, ...],
) -> None:
    decision = _evaluate(level, _typed_proposal(level, kind, steps))
    assert decision.accepted is True
    if kind is ProposalKind.STOP:
        assert decision.endpoint is EndpointCode.VALID_COMPLETION
        assert decision.gated_steps == ()
    else:
        assert decision.endpoint is None
        assert len(decision.gated_steps) == len(steps)
