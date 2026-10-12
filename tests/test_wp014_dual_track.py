from __future__ import annotations

import pytest

from optimizer_pipeline.dof.single_submission.contract_v3 import M_ACTION_KEYS, contract_for_level
from optimizer_pipeline.dof.single_submission.contract_v4 import (
    contract_for_cell,
    language_condition_error,
    neutral_prompt,
    prompt_for_cell,
    validate_protocol_cell,
)
from optimizer_pipeline.dof.single_submission.observation_v3 import CommonQuerySurfaceV3
from optimizer_pipeline.dof.single_submission.planner import SystemBinding
from optimizer_pipeline.dof.single_submission.planner_v4 import SingleSubmissionSessionV4
from scripts.run_wp013b_e0 import _synthetic_state
from scripts.run_wp013b_e0 import prompt as neutral_prompt_v22
from scripts.run_wp014 import _jobs, _replace_placeholders
from scripts.run_wp014 import _binding as wp014_binding
from scripts.analyze_wp014 import require_track
from scripts.audit_wp014_interface_blocker import audit as audit_interface_blocker
from optimizer_pipeline.dof.openrouter import build_openrouter_outbound


def cell(track: str, condition: str, level: str, regime: str = "FULL_M8"):
    return contract_for_cell(
        study_track=track,
        coverage_regime=regime,
        language_condition=condition,
        level=level,
        admitted_action_keys=M_ACTION_KEYS,
    )


@pytest.mark.parametrize(
    ("level", "languages"),
    [("L2", ["M"]), ("L3", ["M", "P"]), ("L4", ["M", "P", "G"])],
)
def test_neutral_contract_is_exact_v22_contract(level, languages):
    value = cell("N_NEUTRAL", "FREE_CHOICE", level)
    assert value == contract_for_level(level)
    assert value["allowed_languages"] == languages
    # The neutral prompt remains the exact accepted WP-013B bytes.
    assert neutral_prompt(level) == neutral_prompt_v22(level)
    assert prompt_for_cell(study_track="N_NEUTRAL", language_condition="FREE_CHOICE", level=level) == neutral_prompt_v22(level)


def test_conditioned_m_is_a_strict_m_projection():
    value = cell("C_CONDITIONED", "M_ONLY", "C")
    assert value["allowed_languages"] == ["M"]
    assert set(value["examples"]) == {"M"}
    assert "P" not in value and "G" not in value
    assert language_condition_error({"language": "M", "action_key": "macro.phys_opt"}, "M_ONLY") is None
    assert language_condition_error({"language": "P", "steps": []}, "M_ONLY") == "LANGUAGE_CONDITION_NOT_MET"


def test_conditioned_p_has_no_macro_surface_and_requires_p_operation():
    value = cell("C_CONDITIONED", "P_EXCLUSIVE", "C")
    assert value["allowed_languages"] == ["P"]
    assert "M" not in value and "G" not in value
    assert "CALL_MACRO" not in {row["key"] for row in value["P"]["cards"]}
    assert "CALL_MACRO" not in value["P"]["program_schema"]["properties"]["steps"]["items"]["properties"]["op"]["enum"]
    assert language_condition_error({"language": "P", "steps": [{"op": "ROUTE"}]}, "P_EXCLUSIVE") is None
    assert language_condition_error({"language": "P", "steps": [{"op": "CALL_MACRO"}]}, "P_EXCLUSIVE") == "LANGUAGE_CONDITION_NOT_MET"


def test_conditioned_g_requires_g_only_construct_and_has_no_macro_surface():
    value = cell("C_CONDITIONED", "G_EXCLUSIVE", "C")
    assert value["allowed_languages"] == ["G"]
    assert "M" not in value
    nodes = value["G"]["program_schema"]["properties"]["statements"]["items"]["properties"]["node"]["enum"]
    assert "CALL_MACRO" not in nodes
    assert "CALL_MACRO" not in {row["key"] for row in value["P"]["cards"]}
    assert language_condition_error({"language": "G", "statements": [{"node": "ROUTE", "args": {}}]}, "G_EXCLUSIVE") == "LANGUAGE_CONDITION_NOT_MET"
    assert language_condition_error({"language": "G", "statements": [{"node": "SELECT_WHERE", "args": {}}]}, "G_EXCLUSIVE") is None
    assert language_condition_error({"language": "G", "statements": [{"node": "RELOCATE_HARDBLOCK", "args": {"candidate_sites": ["x"], "site_index": 0}}]}, "G_EXCLUSIVE") is None


def test_track_and_condition_cross_product_fails_closed():
    with pytest.raises(ValueError, match="neutral_cell_invalid"):
        validate_protocol_cell(study_track="N_NEUTRAL", coverage_regime="FULL_M8", language_condition="P_EXCLUSIVE", level="L3")
    with pytest.raises(ValueError, match="conditioned_cell_invalid"):
        validate_protocol_cell(study_track="C_CONDITIONED", coverage_regime="FULL_M8", language_condition="FREE_CHOICE", level="C")
    with pytest.raises(ValueError, match="coverage_regime_invalid"):
        validate_protocol_cell(study_track="N_NEUTRAL", coverage_regime="SEALED", language_condition="FREE_CHOICE", level="L2")


def session_for(condition: str):
    level = {"M_ONLY": "L2", "P_EXCLUSIVE": "L3", "G_EXCLUSIVE": "L4"}[condition]
    observation, registry, context, _ = _synthetic_state("wp014-test-" + condition)
    binding = SystemBinding.create(
        observation_hash=observation.sha256,
        root_binding=context.registry.trial_binding_hash,
        catalogue_version="test", catalogue_hash="0" * 64,
        provider="test", model="test", route="test", session_nonce=condition,
    )
    return SingleSubmissionSessionV4(
        level=level, observation=observation, queries=CommonQuerySurfaceV3(registry),
        compile_context=context, binding=binding,
        query_provider=lambda *_args: ((), (), 0.0, "test"),
        language_condition=condition,
    ), context


@pytest.mark.parametrize("condition", ["M_ONLY", "P_EXCLUSIVE", "G_EXCLUSIVE"])
def test_conditioned_examples_pass_same_production_compiler(condition):
    session, context = session_for(condition)
    contract = _replace_placeholders(cell("C_CONDITIONED", condition, "C"), context)
    example_key = {
        "M_ONLY": "M", "P_EXCLUSIVE": "P_EXCLUSIVE",
        "G_EXCLUSIVE": "G_SPATIAL_EXCLUSIVE",
    }[condition]
    example = contract["examples"][example_key]["candidate"]
    result = session.accept({"kind": "COMMIT", "candidate": example})
    assert result.status == "COMMITTED"
    assert result.compiled is not None
    assert result.compiled.language == condition[0]
    assert session.evidence()["precommit_mutation_count"] == 0
    assert session.evidence()["precommit_qor_feedback_count"] == 0


def test_runtime_rejects_condition_violation_before_dispatch():
    session, _ = session_for("G_EXCLUSIVE")
    result = session.accept(
        {"kind": "COMMIT", "candidate": {"language": "G", "statements": [
            {"statement_id": "g1", "node": "ROUTE", "args": {"directive": "Default"}}
        ]}}
    )
    assert result.status == "CONSTRUCTION_INVALID"
    assert result.feedback[0]["error_code"] == "LANGUAGE_CONDITION_NOT_MET"
    assert session.committed is None

    nested_session, _ = session_for("G_EXCLUSIVE")
    nested = nested_session.accept(
        {"kind": "COMMIT", "candidate": {"language": "G", "statements": [
            {"statement_id": "g1", "node": "P_PROGRAM", "args": {"program": {
                "language": "P", "steps": [{"step_id": "s1", "op": "CALL_MACRO", "args": {"action_key": "macro.phys_opt"}}]
            }}},
            {"statement_id": "g2", "node": "CREATE_REGION", "args": {"x_min": 0, "x_max": 0, "y_min": 0, "y_max": 0}, "bind": "region"},
        ]}}
    )
    assert nested.status == "CONSTRUCTION_INVALID"
    assert nested.feedback[0]["error_code"] == "LANGUAGE_CONDITION_NOT_MET"


def test_frozen_repeat1_matrix_is_exact_and_track_separated():
    import json
    from pathlib import Path

    stress = json.loads(Path("configs/wp014/stress-masks-v1.json").read_text())
    jobs = _jobs(stress)
    assert len(jobs) == 162
    assert sum(row["phase"] == "N_K" for row in jobs) == 90
    assert sum(row["phase"] == "S_N" for row in jobs) == 36
    assert sum(row["phase"] == "C_S" for row in jobs) == 36
    assert all(row["language_condition"] == "FREE_CHOICE" for row in jobs if row["study_track"] == "N_NEUTRAL")
    assert all(row["language_condition"] != "FREE_CHOICE" for row in jobs if row["study_track"] == "C_CONDITIONED")


def test_conditioned_rows_cannot_enter_neutral_aggregate():
    with pytest.raises(ValueError, match="track_pooling_forbidden"):
        require_track(
            [{"study_track": "N_NEUTRAL"}, {"study_track": "C_CONDITIONED"}],
            "N_NEUTRAL",
        )


def test_exact_wp014_profile_uses_frozen_65536_byte_ceiling():
    job = {
        "job_id": "test", "study_track": "N_NEUTRAL", "coverage_regime": "FULL_M8",
        "language_condition": "FREE_CHOICE", "agent_level": "L4",
        "mask_evidence_hash": "0" * 64,
    }
    request = wp014_binding(
        job=job, model="openai/gpt-5.6-luna", turn=0,
        context=b"x" * 64_000, prompt=b"y" * 1_536,
        catalogue_hash="1" * 64, reserve=0.05,
    )
    assert build_openrouter_outbound(request).body["model"] == "openai/gpt-5.6-luna"


def test_calibration_interface_audit_fails_closed_on_undefined_phys_opt_combo(tmp_path):
    import json

    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"jobs": [{
        "job_id": "conditioned-p",
        "canonical_terminal": "TOOL_FAILURE",
        "selected_language": "P",
        "turns": [{"planner_result": {
            "status": "COMMITTED",
            "compiled": {"host_steps": [{
                "operation": "vivado.phys_opt",
                "args": {
                    "directive": "AlternateFlowWithRetiming",
                    "options": ["AddRetime"],
                },
            }]},
        }}],
    }]}))
    vivado_help = tmp_path / "vivado.log"
    vivado_help.write_text(
        "Vivado v2025.1\n-net_delay_weight <arg>\n"
        "valid options are\n                       low, medium, high\n"
        "*  AggressiveExplore\n"
        "not compatible with any specific optimization options\n"
    )

    result = audit_interface_blocker(calibration, vivado_help)
    assert result["requires_protocol_owner_decision"] is True
    assert result["primary_queue_must_not_start"] is True
    assert len(result["committed_vivado_incompatible_phys_opt_jobs"]) == 1
    assert result["authoritative_help_checks"]["place_lists_extra_net_delay_high_as_directive"] is False
