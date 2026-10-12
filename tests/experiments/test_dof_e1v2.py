from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from optimizer_pipeline.dof.catalogues import (
    nested_action_catalogue,
    nested_capability_audit,
)
from optimizer_pipeline.dof.common_observation import (
    build_common_policy_context,
    common_observation_equality_audit,
)
from optimizer_pipeline.dof.contracts import DesignStage, DoFLevel, EventClass
from optimizer_pipeline.dof.e1v2 import (
    E0V2_MATRIX,
    e0v2_jobs,
    e1v2_repeat1_jobs,
    expansion_inheritance_audit,
    protocol_manifest,
)
from optimizer_pipeline.dof.gate import ACTION_PROPOSAL_SCHEMA_VERSION, ActionGate
from optimizer_pipeline.dof.generated import (
    GENERATED_ACTION_KEY,
    GeneratedProgramError,
    generated_program_evidence,
    validate_generated_program,
)
from optimizer_pipeline.dof.masks import create_mask_rules
from optimizer_pipeline.dof.artifacts import RootFingerprint
from optimizer_pipeline.dof import real_mutation
from optimizer_pipeline.dof.real_mutation import (
    freeze_e0_real_gated_plan,
    validate_e0_real_gated_plan,
)
from optimizer_pipeline.dof.vivado_mutation_worker import (
    WorkerProtocolError,
    build_fixed_tcl,
    parse_request_line,
    WORKER_REQUEST_SCHEMA,
)
from scripts.run_wp007 import _absolute_preserving_symlink


def _source(history: list[dict] | None = None) -> dict:
    return {
        "condition": {"pseudonym": "condition-opaque"},
        "state": {
            "pseudonym": "state-job-l2-model-hidden",
            "lineage_pseudonym": "lineage-job-l2-model-hidden",
            "stage": "ROUTED",
            "measurements": {
                "timing": {
                    "wns_ns": -0.125,
                    "tns_ns": -1.5,
                    "failing_endpoints": 7,
                    "clock_period_ns": 2.5,
                    "estimated_fmax_mhz": 380.0,
                },
                "route": {"status": "fully routed", "unrouted_nets": 0},
            },
        },
        "history": history or [],
        "budget_remaining": {
            "IMPLEMENTATION_HEAVY": 4,
            "MUTATION_LIGHT": 8,
            "READ_ONLY": 8,
            "LLM_REQUEST": 4,
            "WALL_TIME_SECONDS": 3700.0,
        },
        "validation": {"status": "CLEAN", "errors": [], "warnings": []},
        "stop_conditions": ["budget exhausted"],
        "known_priors": {},
    }


def _generated_parameters() -> dict:
    return {
        "pblock_id": "g_policy_region",
        "selector_kind": "sequential_cells",
        "cell_limit": 16,
        "slice_x0": 0,
        "slice_y0": 0,
        "slice_x1": 12,
        "slice_y1": 99,
    }


def _proposal(level: DoFLevel, action_key: str, parameters: dict) -> dict:
    catalogue = nested_action_catalogue(level)
    return {
        "schema_version": ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": "proposal-e1v2",
        "decision_id": "decision-e1v2",
        "catalogue_version": catalogue.version,
        "catalogue_hash": catalogue.sha256,
        "kind": "ACTION",
        "steps": [
            {
                "step_id": "step-0",
                "action_key": action_key,
                "parameters": parameters,
            }
        ],
    }


def _evaluate(level: DoFLevel, action_key: str, parameters: dict):
    catalogue = nested_action_catalogue(level)
    return ActionGate().evaluate(
        _proposal(level, action_key, parameters),
        level=level,
        expected_decision_id="decision-e1v2",
        catalogue=catalogue,
        expected_catalogue_hash=catalogue.sha256,
        current_stage=DesignStage.ROUTED,
        budget_remaining={event: 8 for event in EventClass},
        mask=create_mask_rules(version="dof.e1v2-test-mask.v1"),
        capability_contract="e1v2",
    )


def _worker_request(parameters: dict) -> bytes:
    return json.dumps(
        {
            "schema_version": WORKER_REQUEST_SCHEMA,
            "operation_id": "vivado.generated_pblock_region",
            "call_token": "call-generated",
            "timeout_seconds": 120.0,
            "arguments": parameters,
        },
        separators=(",", ":"),
    ).encode()


def test_exact_e0_and_e1v2_job_inventories() -> None:
    e0 = e0v2_jobs()
    e1 = e1v2_repeat1_jobs()
    assert len(e0) == 16
    assert len(e1) == 88
    assert len({row["job_id"] for row in e0 + e1}) == 104
    assert sum(row["level"] in {"L2", "L3", "L4"} for row in e0) == 12
    assert sum(row["level"] in {"L2", "L3", "L4"} for row in e1) == 72
    assert protocol_manifest(matrix=E0V2_MATRIX, roots=("a", "b"))["job_count"] == 16


def test_queue_python_path_preserves_virtualenv_symlink(tmp_path: Path) -> None:
    target = tmp_path / "python-target"
    target.write_text("")
    launcher = tmp_path / "venv-python"
    launcher.symlink_to(target)

    observed = _absolute_preserving_symlink(launcher)

    assert observed == str(launcher)
    assert observed != str(target.resolve())


def test_nested_capability_and_expansion_invariants() -> None:
    expected = {
        "L2": {"macro.phys_opt", "macro.retime", "macro.place_route"},
        "L3": {
            "macro.phys_opt",
            "macro.retime",
            "macro.place_route",
            "manual.unroute_all",
            "manual.unplace_all",
            "manual.place_design",
            "manual.route_design",
            "manual.phys_opt_design",
        },
    }
    expected["L4"] = expected["L3"] | {GENERATED_ACTION_KEY}
    for level in (DoFLevel.L2, DoFLevel.L3, DoFLevel.L4):
        assert {row.name for row in nested_action_catalogue(level).actions} == expected[level.value]
    capability = nested_capability_audit()
    expansion = expansion_inheritance_audit()
    assert capability["accepted"] is True
    assert expansion["accepted"] is True
    assert capability["inherited_action_definitions_identical"] is True


def test_common_observation_and_prompt_core_are_byte_identical() -> None:
    history = [
        {
            "decision_index": 0,
            "actions": [
                {"action_key": "macro.phys_opt", "parameters": {}},
                {
                    "action_key": "manual.phys_opt_design",
                    "parameters": {"directive": "Explore"},
                },
            ],
            "parent_qor": {"fmax_mhz": 370.0, "wns_ns": -0.2},
            "child_qor": {"fmax_mhz": 380.0, "wns_ns": -0.125},
            "delta_qor": {"fmax_mhz": 10.0, "wns_ns": 0.075},
            "endpoint": "VALID_COMPLETION",
            "valid": True,
            "actual_heavy_calls": 2,
        }
    ]
    audit = common_observation_equality_audit(
        source=_source(history), current_artifact_sha256="a" * 64, decision_index=1
    )
    assert audit["accepted"] is True
    assert audit["base_bytes_equal"] is True
    assert audit["common_prompt_core_bytes_equal"] is True
    assert audit["capability_appendices_distinct"] is True
    contexts = [
        build_common_policy_context(
            level=level,
            source=_source(history),
            current_artifact_sha256="a" * 64,
            decision_index=1,
        )
        for level in (DoFLevel.L2, DoFLevel.L3, DoFLevel.L4)
    ]
    base = contexts[0].base_observation_bytes.decode()
    assert "state-job-l2-model-hidden" not in base
    assert "lineage-job-l2-model-hidden" not in base
    assert len({row.base_observation_bytes for row in contexts}) == 1


@pytest.mark.parametrize(
    "level,action_key,parameters",
    [
        (DoFLevel.L3, "macro.phys_opt", {}),
        (DoFLevel.L4, "macro.retime", {}),
        (DoFLevel.L4, "manual.phys_opt_design", {"directive": "Explore"}),
        (DoFLevel.L4, GENERATED_ACTION_KEY, _generated_parameters()),
    ],
)
def test_e1v2_gate_accepts_inherited_and_generated_actions(
    level: DoFLevel, action_key: str, parameters: dict
) -> None:
    decision = _evaluate(level, action_key, parameters)
    assert decision.accepted is True, decision.reasons
    assert decision.gated_steps[0].action_key == action_key


def test_generated_intervention_is_typed_and_compiled_without_raw_tcl() -> None:
    parameters = _generated_parameters()
    canonical = validate_generated_program(parameters)
    evidence = generated_program_evidence(parameters)
    assert canonical == parameters
    assert evidence["containment"] == {
        "raw_tcl": False,
        "shell": False,
        "network": False,
        "filesystem": False,
        "checkpoint_write": False,
        "hidden_heavy_call": False,
    }
    tcl = build_fixed_tcl(parse_request_line(_worker_request(parameters)))
    assert "create_pblock g_policy_region" in tcl
    assert "DOF_GENERATED_PBLOCK_V1" in tcl
    assert "run_tcl" not in tcl
    assert "source " not in tcl
    assert "write_checkpoint" not in tcl


def test_e1v2_generated_plan_freezes_and_revalidates_with_v2_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        real_mutation,
        "observe_repository",
        lambda _path: SimpleNamespace(
            head_commit="1" * 40,
            head_tree="2" * 40,
            binary_diff_sha256="3" * 64,
            tracked_tree_dirty=False,
            branch_name="phase0-policy-evidence-replan",
        ),
    )
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
    plan = freeze_e0_real_gated_plan(
        root,
        root_pseudonym="development-root",
        level=DoFLevel.L4,
        decision_id="decision-generated",
        proposal_id="proposal-generated",
        proposal_hash="b" * 64,
        gate_decision_sha256="c" * 64,
        gated_steps=[
            {
                "step_index": 0,
                "step_id": "generated-step",
                "action_key": GENERATED_ACTION_KEY,
                "parameters": _generated_parameters(),
                "stage_before": "ROUTED",
                "stage_after": "ROUTED",
            }
        ],
        causal_evidence_sha256="d" * 64,
        run_id="e1v2-generated-freeze",
        repository_anchor=tmp_path,
        python_exec=sys.executable,
        vivado_exec=vivado,
        run_dir=tmp_path / "run",
        fake_startup_attestation_sha256="e" * 64,
        protocol="e1v2",
        scientific_selection_eligible=False,
    )
    assert validate_e0_real_gated_plan(plan) == plan
    assert plan["schema_version"] == "dof.e1v2-real-gated-plan.v1"
    assert plan["selection_policy"] == "root_only_technical_admission"
    assert plan["technical_non_scientific"] is True
    assert plan["action"]["model_requests"] == 0


@pytest.mark.parametrize(
    "update",
    [
        {"pblock_id": "g_ok;exit"},
        {"selector_kind": "[exec sh]"},
        {"cell_limit": 65},
        {"slice_x0": -1},
        {"slice_y1": 10000},
        {"tcl": "exit"},
    ],
)
def test_generated_intervention_rejects_escape_shapes(update: dict) -> None:
    parameters = _generated_parameters() | update
    with pytest.raises(GeneratedProgramError):
        validate_generated_program(parameters)
    with pytest.raises(WorkerProtocolError):
        parse_request_line(_worker_request(parameters))
