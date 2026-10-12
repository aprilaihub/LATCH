#!/usr/bin/env python3
"""Generate the deterministic WP-004A fake-host acceptance matrix."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.dof.artifacts import ArtifactFailurePoint
from optimizer_pipeline.dof.catalogues import strict_l3_catalogue
from optimizer_pipeline.dof.contracts import DesignStage, DoFLevel
from optimizer_pipeline.dof.technical import (
    run_fake_composed_l3_mutation,
    run_fake_technical_mutation,
)


MATRIX_SCHEMA = "dof.wp004a-fake-host-matrix.v2"


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    level: DoFLevel
    action_key: str
    parameters: dict[str, Any]
    initial_stage: DesignStage
    options: dict[str, Any]
    expected_endpoint: str
    runner: str = "single_action"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _positive_cases() -> list[Case]:
    cases: list[Case] = []
    for level in (DoFLevel.L1, DoFLevel.L2):
        for action in ("macro.phys_opt", "macro.retime", "macro.place_route"):
            cases.append(
                Case(
                    f"positive-{level.value.lower()}-{_slug(action)}",
                    level,
                    action,
                    {},
                    DesignStage.ROUTED,
                    {},
                    "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC",
                )
            )
    initial_stage = {
        "manual.unroute_all": DesignStage.ROUTED,
        "manual.unplace_all": DesignStage.PLACED,
        "manual.place_design": DesignStage.UNPLACED,
        "manual.route_design": DesignStage.PLACED,
        "manual.phys_opt_design": DesignStage.ROUTED,
    }
    for action in strict_l3_catalogue().actions:
        schema = action.to_dict()["parameters_schema"]
        directives = schema["properties"].get("directive", {}).get("enum", [None])
        for directive in directives:
            parameters = {} if directive is None else {"directive": directive}
            suffix = action.name if directive is None else f"{action.name}-{directive}"
            cases.append(
                Case(
                    f"positive-l3-{_slug(suffix)}",
                    DoFLevel.L3,
                    action.name,
                    parameters,
                    initial_stage[action.name],
                    {},
                    "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC",
                )
            )
    return cases


def _negative_cases() -> list[Case]:
    base = dict(
        level=DoFLevel.L3,
        action_key="manual.phys_opt_design",
        parameters={"directive": "Explore"},
        initial_stage=DesignStage.ROUTED,
    )
    cases = [
        Case(
            f"negative-child-{mode}",
            DoFLevel.L1,
            "macro.phys_opt",
            {},
            DesignStage.ROUTED,
            {"child_mode": mode},
            endpoint,
        )
        for mode, endpoint in (
            ("missing", "TECHNICAL_MUTATION_NO_VALID_CHILD"),
            ("truncated", "INVALID_ARTIFACT"),
            ("invalid", "INVALID_ARTIFACT"),
            ("tampered", "EVIDENCE_INTEGRITY_FAILURE"),
        )
    ]
    for point in (
        ArtifactFailurePoint.AFTER_BACKEND_BEFORE_ARTIFACT,
        ArtifactFailurePoint.AFTER_TEMP_WRITE_BEFORE_VALIDATION,
        ArtifactFailurePoint.AFTER_VALIDATION_BEFORE_REGISTRATION,
        ArtifactFailurePoint.AFTER_REGISTRATION_BEFORE_CANONICAL_FACTS,
    ):
        cases.append(
            Case(
                f"negative-crash-{point.value}",
                options={"fail_at": point},
                expected_endpoint="ARTIFACT_TRANSACTION_INTERRUPTED",
                **base,
            )
        )
    cases.extend(
        (
            Case(
                "negative-extra-heavy",
                DoFLevel.L1,
                "macro.place_route",
                {},
                DesignStage.ROUTED,
                {"inject_extra_operation": "vivado.phys_opt_design"},
                "BACKEND_CONTRACT_MISMATCH",
            ),
            Case(
                "negative-unknown-operation",
                DoFLevel.L1,
                "macro.phys_opt",
                {},
                DesignStage.ROUTED,
                {"inject_extra_operation": "hidden.raw_tcl"},
                "BACKEND_CONTRACT_MISMATCH",
            ),
            Case(
                "negative-tool-failure",
                DoFLevel.L1,
                "macro.phys_opt",
                {},
                DesignStage.ROUTED,
                {"fail_operation": "vivado.phys_opt_design"},
                "TOOL_EXECUTION_FAILURE",
            ),
            Case(
                "negative-root-drift",
                DoFLevel.L1,
                "macro.phys_opt",
                {},
                DesignStage.ROUTED,
                {"mutate_root": True},
                "ROOT_ARTIFACT_MUTATION",
            ),
        )
    )
    return cases


def _composed_l3_cases() -> list[Case]:
    common = {
        "level": DoFLevel.L3,
        "action_key": "technical.composed_l3_plan",
        "parameters": {},
        "initial_stage": DesignStage.ROUTED,
        "runner": "composed_l3",
    }
    return [
        Case(
            "wp004a-positive-composed-l3-production-path",
            options={},
            expected_endpoint="TECHNICAL_MUTATION_VALID_NONSCIENTIFIC",
            **common,
        ),
        Case(
            "wp004a-negative-composed-l3-third-step-failure",
            options={"fail_step_index": 2},
            expected_endpoint="TOOL_EXECUTION_FAILURE",
            **common,
        ),
    ]


def _composed_case_passed(case: Case, result: dict[str, Any]) -> bool:
    plan = result.get("technical_plan") or {}
    common = (
        result["endpoint"] == case.expected_endpoint
        and plan.get("proposal_id") == "wp004a-composed-l3-plan"
        and plan.get("decision_id") == "wp004a-composed-l3-decision"
        and str(plan.get("call_plan_id", "")).startswith(
            "wp004a-technical-composed-l3:"
        )
        and plan.get("gate_accepted") is True
        and plan.get("jit_step_expansion") is True
        and plan.get("parser_binding_hash")
        and plan.get("gate_binding_hash")
        and plan.get("gate_decision_hash")
        and result["root_unchanged"] is True
        and result["root_is_sole_selection"] is True
        and result["artifact_lifecycle"]["safe"] is True
        and result["canonical_schema_version"] == 2
        and result["sqlite_quick_check"] == "ok"
        and result["foreign_key_errors"] == 0
    )
    if not common:
        return False
    statuses = [item["status"] for item in plan["step_lifecycle"]]
    if case.options.get("fail_step_index") is None:
        return bool(
            result["endpoint_success"] is True
            and plan["stage_progression"]
            == ["ROUTED", "PLACED", "UNPLACED", "PLACED", "ROUTED", "ROUTED"]
            and plan["actual_light_started"] == 2
            and plan["actual_heavy_started"] == 3
            and statuses == ["SUCCEEDED"] * 5
            and plan["observed_mutation_operations"]
            == [
                "vivado.unroute_all",
                "vivado.unplace_all",
                "vivado.place_design",
                "vivado.route_design",
                "vivado.phys_opt_design",
            ]
            and plan["no_later_step_dispatched"] is True
            and plan["undispatched_step_ids"] == []
            and result["child_selection_eligible"] is False
            and result["canonical_counts"]["states"] == 2
        )
    return bool(
        result["endpoint_success"] is False
        and plan["stage_progression"] == ["ROUTED", "PLACED", "UNPLACED"]
        and plan["actual_light_started"] == 2
        and plan["actual_heavy_started"] == 1
        and statuses == ["SUCCEEDED", "SUCCEEDED", "FAILED"]
        and plan["failed_step_index"] == 2
        and plan["observed_mutation_operations"]
        == [
            "vivado.unroute_all",
            "vivado.unplace_all",
            "vivado.place_design",
        ]
        and plan["no_later_step_dispatched"] is True
        and plan["undispatched_step_ids"] == ["step-route", "step-phys-opt"]
        and result["transaction"] is None
        and result["child_selection_eligible"] is None
        and result["canonical_counts"]["states"] == 1
    )


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key
        not in {
            "budget",
            "call_reconciliation",
            "transaction",
        }
    } | {
        "budget": result["budget"],
        "call_reconciliation_ok": result["call_reconciliation"]["ok"],
        "observed_started_operations": result["call_reconciliation"][
            "observed_started_operations"
        ],
        "transaction": result["transaction"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)

    rows: list[dict[str, Any]] = []
    for case in _positive_cases() + _negative_cases() + _composed_l3_cases():
        options = dict(case.options)
        if isinstance(options.get("fail_at"), ArtifactFailurePoint):
            options["fail_at"] = options["fail_at"]
        if case.runner == "composed_l3":
            result = run_fake_composed_l3_mutation(
                output / "cases" / case.case_id,
                **options,
            )
            passed = _composed_case_passed(case, result)
        else:
            result = run_fake_technical_mutation(
                output / "cases" / case.case_id,
                level=case.level,
                action_key=case.action_key,
                parameters=case.parameters,
                initial_stage=case.initial_stage,
                **options,
            )
            passed = result["endpoint"] == case.expected_endpoint
        rows.append(
            {
                "case_id": case.case_id,
                "runner": case.runner,
                "expected_endpoint": case.expected_endpoint,
                "passed": passed,
                "result": _public_result(result),
            }
        )

    payload = {
        "schema_version": MATRIX_SCHEMA,
        "technical_non_scientific": True,
        "case_count": len(rows),
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "all_passed": all(bool(row["passed"]) for row in rows),
        "composed_l3_summary": {
            row["case_id"]: {
                "passed": row["passed"],
                "endpoint": row["result"]["endpoint"],
                "proposal_id": row["result"]["technical_plan"]["proposal_id"],
                "decision_id": row["result"]["technical_plan"]["decision_id"],
                "call_plan_id": row["result"]["technical_plan"]["call_plan_id"],
                "parser_binding_hash": row["result"]["technical_plan"][
                    "parser_binding_hash"
                ],
                "gate_binding_hash": row["result"]["technical_plan"][
                    "gate_binding_hash"
                ],
                "decision_binding_hash": row["result"]["technical_plan"][
                    "decision_binding_hash"
                ],
                "stage_progression": row["result"]["technical_plan"][
                    "stage_progression"
                ],
                "step_statuses": [
                    item["status"]
                    for item in row["result"]["technical_plan"]["step_lifecycle"]
                ],
                "actual_light_started": row["result"]["technical_plan"][
                    "actual_light_started"
                ],
                "actual_heavy_started": row["result"]["technical_plan"][
                    "actual_heavy_started"
                ],
                "observed_mutation_operations": row["result"]["technical_plan"][
                    "observed_mutation_operations"
                ],
                "undispatched_step_ids": row["result"]["technical_plan"][
                    "undispatched_step_ids"
                ],
                "failed_step_index": row["result"]["technical_plan"][
                    "failed_step_index"
                ],
                "failed_step_id": row["result"]["technical_plan"][
                    "failed_step_id"
                ],
                "failed_action_key": row["result"]["technical_plan"][
                    "failed_action_key"
                ],
                "root_unchanged": row["result"]["root_unchanged"],
                "root_is_sole_selection": row["result"][
                    "root_is_sole_selection"
                ],
                "child_selection_eligible": row["result"][
                    "child_selection_eligible"
                ],
            }
            for row in rows
            if row["runner"] == "composed_l3"
        },
        "cases": rows,
        "companion_negative_test_selectors": [
            "tests/experiments/test_dof_expansions.py",
            "tests/experiments/test_dof_toolcalls.py::test_failed_timeout_and_cancelled_calls_have_terminal_rows_and_are_counted",
            "tests/experiments/test_dof_processes.py",
            "tests/experiments/test_dof_artifacts.py::test_crash_points_preserve_schema_integrity_and_never_select_partial_child",
            "tests/experiments/test_dof_gate_hardening.py::test_stale_typed_decision_id_is_rejected_before_dispatch",
            "tests/experiments/test_dof_provider.py::test_provider_model_fallback_and_stale_context_binding_fail_before_dispatch",
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    (output / "fake-host-matrix.json").write_bytes(encoded)
    (output / "fake-host-matrix.sha256").write_text(
        hashlib.sha256(encoded).hexdigest() + "  fake-host-matrix.json\n",
        encoding="ascii",
    )
    print(json.dumps({key: payload[key] for key in ("case_count", "passed_count", "all_passed")}))
    return 0 if payload["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
