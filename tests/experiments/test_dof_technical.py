from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from optimizer_pipeline.dof.artifacts import ArtifactFailurePoint
from optimizer_pipeline.dof.catalogues import strict_l3_catalogue
from optimizer_pipeline.dof.contracts import DesignStage, DoFLevel
from optimizer_pipeline.dof.technical import (
    run_fake_composed_l3_mutation,
    run_fake_technical_mutation,
)
from optimizer_pipeline.dof.toolcalls import ToolCallContractError


_L3_INITIAL_STAGE = {
    "manual.unroute_all": DesignStage.ROUTED,
    "manual.unplace_all": DesignStage.PLACED,
    "manual.place_design": DesignStage.UNPLACED,
    "manual.route_design": DesignStage.PLACED,
    "manual.phys_opt_design": DesignStage.ROUTED,
}


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _positive_cases() -> list[tuple[DoFLevel, str, dict, DesignStage]]:
    cases = [
        (level, action, {}, DesignStage.ROUTED)
        for level in (DoFLevel.L1, DoFLevel.L2)
        for action in ("macro.phys_opt", "macro.retime", "macro.place_route")
    ]
    for action in strict_l3_catalogue().actions:
        schema = action.to_dict()["parameters_schema"]
        directives = schema["properties"].get("directive", {}).get("enum", [None])
        for directive in directives:
            parameters = {} if directive is None else {"directive": directive}
            cases.append(
                (
                    DoFLevel.L3,
                    action.name,
                    parameters,
                    _L3_INITIAL_STAGE[action.name],
                )
            )
    return cases


@pytest.mark.parametrize(
    "level,action_key,parameters,initial_stage",
    _positive_cases(),
)
def test_fake_host_positive_matrix_registers_only_nonselected_children(
    tmp_path: Path,
    level: DoFLevel,
    action_key: str,
    parameters: dict,
    initial_stage: DesignStage,
) -> None:
    result = run_fake_technical_mutation(
        tmp_path / (level.value + "-" + action_key.replace(".", "-")),
        level=level,
        action_key=action_key,
        parameters=parameters,
        initial_stage=initial_stage,
    )

    assert result["endpoint"] == "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC"
    assert result["call_reconciliation"]["ok"] is True
    assert result["root_unchanged"] is True
    assert result["root_is_sole_selection"] is True
    assert result["child_selection_eligible"] is False
    assert result["artifact_lifecycle"]["safe"] is True
    assert result["sqlite_quick_check"] == "ok"
    assert result["foreign_key_errors"] == 0
    assert result["canonical_counts"] == {
        "actions": 1,
        "attempts": 1,
        "outcomes": 1,
        "states": 2,
        "selections": 1,
    }
    rows = [
        json.loads(line)
        for line in (
            tmp_path
            / (level.value + "-" + action_key.replace(".", "-"))
            / "evidence/backend_calls.jsonl"
        ).read_text().splitlines()
    ]
    call_rows = [row for row in rows if "status" in row]
    assert any(row["status"] == "SUCCEEDED" for row in call_rows)
    assert all(
        row["execution_binding"] == {"runtime_binding_id": result["spec_hash"]}
        for row in call_rows
    )


@pytest.mark.parametrize(
    "child_mode,endpoint",
    [
        ("missing", "TECHNICAL_MUTATION_NO_VALID_CHILD"),
        ("truncated", "INVALID_ARTIFACT"),
        ("invalid", "INVALID_ARTIFACT"),
        ("tampered", "EVIDENCE_INTEGRITY_FAILURE"),
    ],
)
def test_invalid_fake_children_never_become_states_or_selections(
    tmp_path: Path,
    child_mode: str,
    endpoint: str,
) -> None:
    result = run_fake_technical_mutation(
        tmp_path / child_mode,
        level=DoFLevel.L1,
        action_key="macro.phys_opt",
        parameters={},
        initial_stage=DesignStage.ROUTED,
        child_mode=child_mode,
    )

    assert result["endpoint"] == endpoint
    assert result["endpoint_success"] is False
    assert result["root_is_sole_selection"] is True
    assert result["child_selection_eligible"] is None
    assert result["canonical_counts"]["states"] == 1
    assert result["canonical_counts"]["selections"] == 1
    assert result["canonical_counts"]["outcomes"] == 1
    assert result["artifact_lifecycle"]["safe"] is True


@pytest.mark.parametrize(
    "failure_point",
    [
        ArtifactFailurePoint.AFTER_BACKEND_BEFORE_ARTIFACT,
        ArtifactFailurePoint.AFTER_TEMP_WRITE_BEFORE_VALIDATION,
        ArtifactFailurePoint.AFTER_VALIDATION_BEFORE_REGISTRATION,
        ArtifactFailurePoint.AFTER_REGISTRATION_BEFORE_CANONICAL_FACTS,
    ],
)
def test_fake_host_artifact_interruptions_are_typed_and_never_select(
    tmp_path: Path,
    failure_point: ArtifactFailurePoint,
) -> None:
    result = run_fake_technical_mutation(
        tmp_path / failure_point.value,
        level=DoFLevel.L3,
        action_key="manual.phys_opt_design",
        parameters={"directive": "Explore"},
        initial_stage=DesignStage.ROUTED,
        fail_at=failure_point,
    )

    assert result["endpoint"] == "ARTIFACT_TRANSACTION_INTERRUPTED"
    assert result["root_is_sole_selection"] is True
    assert result["canonical_counts"]["selections"] == 1
    assert result["artifact_lifecycle"]["safe"] is True
    assert result["foreign_key_errors"] == 0


def test_extra_heavy_unknown_failure_root_drift_and_selection_fail_closed(
    tmp_path: Path,
) -> None:
    extra = run_fake_technical_mutation(
        tmp_path / "extra-heavy",
        level=DoFLevel.L1,
        action_key="macro.place_route",
        parameters={},
        initial_stage=DesignStage.ROUTED,
        inject_extra_operation="vivado.phys_opt_design",
    )
    assert extra["endpoint"] == "BACKEND_CONTRACT_MISMATCH"
    assert extra["budget"]["used"]["IMPLEMENTATION_HEAVY"] == 0

    unknown = run_fake_technical_mutation(
        tmp_path / "unknown",
        level=DoFLevel.L1,
        action_key="macro.phys_opt",
        parameters={},
        initial_stage=DesignStage.ROUTED,
        inject_extra_operation="hidden.raw_tcl",
    )
    assert unknown["endpoint"] == "BACKEND_CONTRACT_MISMATCH"
    assert unknown["call_reconciliation"]["observed_started_operations"][-1] == "vivado.report_route_status"

    failed = run_fake_technical_mutation(
        tmp_path / "host-failure",
        level=DoFLevel.L1,
        action_key="macro.phys_opt",
        parameters={},
        initial_stage=DesignStage.ROUTED,
        fail_operation="vivado.phys_opt_design",
    )
    assert failed["endpoint"] == "TOOL_EXECUTION_FAILURE"
    assert failed["budget"]["used"]["IMPLEMENTATION_HEAVY"] == 1

    drift = run_fake_technical_mutation(
        tmp_path / "root-drift",
        level=DoFLevel.L1,
        action_key="macro.phys_opt",
        parameters={},
        initial_stage=DesignStage.ROUTED,
        mutate_root=True,
    )
    assert drift["endpoint"] == "ROOT_ARTIFACT_MUTATION"
    assert drift["root_unchanged"] is False
    assert drift["root_is_sole_selection"] is True

    with pytest.raises(Exception, match="technical child cannot be selection eligible"):
        run_fake_technical_mutation(
            tmp_path / "selection",
            level=DoFLevel.L1,
            action_key="macro.phys_opt",
            parameters={},
            initial_stage=DesignStage.ROUTED,
            selection_eligible=True,
        )


def test_illegal_stage_and_directive_fail_before_observed_host_entry(
    tmp_path: Path,
) -> None:
    with pytest.raises(ToolCallContractError, match="stage_precondition"):
        run_fake_technical_mutation(
            tmp_path / "stage",
            level=DoFLevel.L3,
            action_key="manual.route_design",
            parameters={"directive": "Default"},
            initial_stage=DesignStage.ROUTED,
        )
    with pytest.raises(ToolCallContractError, match="directive_not_allowed"):
        run_fake_technical_mutation(
            tmp_path / "directive",
            level=DoFLevel.L3,
            action_key="manual.phys_opt_design",
            parameters={"directive": "AggressiveExplore"},
            initial_stage=DesignStage.ROUTED,
        )
    assert not list(tmp_path.rglob("backend_calls.jsonl"))


def test_composed_l3_plan_is_strictly_bound_jit_expanded_and_registered(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "wp004a-composed-positive"
    result = run_fake_composed_l3_mutation(run_dir)

    assert result["endpoint"] == "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC"
    assert result["call_reconciliation"]["ok"] is True
    assert result["root_unchanged"] is True
    assert result["root_is_sole_selection"] is True
    assert result["child_selection_eligible"] is False
    assert result["canonical_schema_version"] == 2
    assert result["canonical_counts"] == {
        "actions": 1,
        "attempts": 1,
        "outcomes": 1,
        "states": 2,
        "selections": 1,
    }

    plan = result["technical_plan"]
    assert plan["proposal_id"] == "wp004a-composed-l3-plan"
    assert plan["decision_id"] == "wp004a-composed-l3-decision"
    assert plan["call_plan_id"].startswith("wp004a-technical-composed-l3:")
    assert plan["gate_accepted"] is True
    assert plan["jit_step_expansion"] is True
    assert plan["planned_step_count"] == 5
    assert plan["expanded_step_count"] == 5
    assert plan["stage_progression"] == [
        "ROUTED",
        "PLACED",
        "UNPLACED",
        "PLACED",
        "ROUTED",
        "ROUTED",
    ]
    assert [item["status"] for item in plan["step_lifecycle"]] == [
        "SUCCEEDED"
    ] * 5
    assert plan["actual_light_started"] == 2
    assert plan["actual_heavy_started"] == 3
    assert plan["observed_mutation_operations"] == [
        "vivado.unroute_all",
        "vivado.unplace_all",
        "vivado.place_design",
        "vivado.route_design",
        "vivado.phys_opt_design",
    ]
    assert plan["no_later_step_dispatched"] is True
    assert plan["undispatched_step_ids"] == []

    binding = json.loads(
        (run_dir / "evidence/technical_decision_binding.json").read_text()
    )
    assert binding["gate_accepted"] is True
    assert binding["fallback_used"] is False
    assert binding["decision_id"] == "wp004a-composed-l3-decision"
    assert binding["parser_binding_hash"] == _canonical_hash(
        binding["parser_binding"]
    )
    assert binding["gate_binding_hash"] == _canonical_hash(binding["gate_binding"])
    assert binding["gate_decision_hash"] == _canonical_hash(
        binding["gate_decision"]
    )

    step_rows = [
        json.loads(line)
        for line in (run_dir / "evidence/technical_plan_steps.jsonl")
        .read_text()
        .splitlines()
    ]
    expanded_rows = [
        row for row in step_rows if row["record_type"] == "PLAN_STEP_EXPANDED"
    ]
    terminal_rows = [
        row for row in step_rows if row["record_type"] == "PLAN_STEP_TERMINAL"
    ]
    assert [row["prior_terminal_success_count"] for row in expanded_rows] == list(
        range(5)
    )
    assert all(row["jit_after_prior_success"] is True for row in expanded_rows)
    assert [row["status"] for row in terminal_rows] == ["SUCCEEDED"] * 5
    assert {
        "technical_decision_binding",
        "technical_plan_step_trace",
    }.issubset(result["registered_evidence_roles"])


def test_composed_l3_third_step_failure_stops_expansion_and_dispatch(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "wp004a-composed-third-step-failure"
    result = run_fake_composed_l3_mutation(run_dir, fail_step_index=2)

    assert result["endpoint"] == "TOOL_EXECUTION_FAILURE"
    assert result["endpoint_success"] is False
    assert result["reasons"] == [
        "technical_plan_step_failed:2:step-place:manual.place_design"
    ]
    assert result["root_unchanged"] is True
    assert result["root_is_sole_selection"] is True
    assert result["child_selection_eligible"] is None
    assert result["transaction"] is None
    assert result["artifact_lifecycle"]["safe"] is True
    assert result["canonical_schema_version"] == 2
    assert result["canonical_counts"] == {
        "actions": 1,
        "attempts": 1,
        "outcomes": 1,
        "states": 1,
        "selections": 1,
    }

    plan = result["technical_plan"]
    assert plan["stage_progression"] == ["ROUTED", "PLACED", "UNPLACED"]
    assert [item["status"] for item in plan["step_lifecycle"]] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
    ]
    assert plan["failed_step_index"] == 2
    assert plan["failed_step_id"] == "step-place"
    assert plan["failed_action_key"] == "manual.place_design"
    assert plan["actual_light_started"] == 2
    assert plan["actual_heavy_started"] == 1
    assert plan["observed_mutation_operations"] == [
        "vivado.unroute_all",
        "vivado.unplace_all",
        "vivado.place_design",
    ]
    assert plan["no_later_step_dispatched"] is True
    assert plan["undispatched_step_ids"] == ["step-route", "step-phys-opt"]

    step_rows = [
        json.loads(line)
        for line in (run_dir / "evidence/technical_plan_steps.jsonl")
        .read_text()
        .splitlines()
    ]
    assert sum(row["record_type"] == "PLAN_STEP_EXPANDED" for row in step_rows) == 3
    assert not any(row.get("step_index") in {3, 4} for row in step_rows)

    backend_rows = [
        json.loads(line)
        for line in (run_dir / "evidence/backend_calls.jsonl")
        .read_text()
        .splitlines()
    ]
    mutation_starts = [
        row["operation_id"]
        for row in backend_rows
        if row["record_type"] == "HOST_CALL_START"
        and row["event_class"] in {"MUTATION_LIGHT", "IMPLEMENTATION_HEAVY"}
    ]
    assert mutation_starts == [
        "vivado.unroute_all",
        "vivado.unplace_all",
        "vivado.place_design",
    ]
    assert "vivado.route_design" not in mutation_starts
    assert "vivado.phys_opt_design" not in mutation_starts


def test_generated_fake_matrix_contains_both_composed_l3_cases(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    output = tmp_path / "generated-matrix"
    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts/run_wp004_fake_matrix.py"),
            "--output-dir",
            str(output),
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads((output / "fake-host-matrix.json").read_text())
    assert payload["case_count"] >= 29
    assert payload["passed_count"] == payload["case_count"]
    assert payload["all_passed"] is True
    summary = payload["composed_l3_summary"]
    assert set(summary) == {
        "wp004a-positive-composed-l3-production-path",
        "wp004a-negative-composed-l3-third-step-failure",
    }
    assert summary["wp004a-positive-composed-l3-production-path"][
        "stage_progression"
    ] == ["ROUTED", "PLACED", "UNPLACED", "PLACED", "ROUTED", "ROUTED"]
    failed = summary["wp004a-negative-composed-l3-third-step-failure"]
    assert failed["failed_step_index"] == 2
    assert failed["observed_mutation_operations"] == [
        "vivado.unroute_all",
        "vivado.unplace_all",
        "vivado.place_design",
    ]
    assert failed["undispatched_step_ids"] == ["step-route", "step-phys-opt"]
