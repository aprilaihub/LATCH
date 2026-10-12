from __future__ import annotations

import hashlib
import asyncio
from pathlib import Path

import pytest

import scripts.run_wp013a_e0 as runtime
import scripts.run_wp014e as wp014e
import scripts.audit_wp014e_processes as process_audit

from optimizer_pipeline.apps.fpl_contest.official_score import OfficialContestTracer
from optimizer_pipeline.dof.scientific_evidence import (
    STRICT_FUNCTIONAL_PASS,
    boundary_evidence,
    canonical_scientific_outcome,
    classify_functional_validation,
    freeze_epsilon_fmax,
    score_fmax_delta,
)
from optimizer_pipeline.dof.single_submission.action_surface_v3 import P_OPERATION_REGISTRY
from optimizer_pipeline.dof.single_submission.contract_v5 import compile_candidate_v5
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry
from optimizer_pipeline.dof.single_submission.language import CompileContext, compile_macro
from optimizer_pipeline.dof.single_submission.macro_authority import (
    M10_ACTION_KEYS as M_ACTION_KEYS,
    M10_HEAVY_CALLS as M_HEAVY_CALLS,
    macro_definition,
)


def context() -> CompileContext:
    registry = TrialHandleRegistry(
        trial_id="wp014e-correctness",
        root_sha256=hashlib.sha256(b"root").hexdigest(),
        secret=hashlib.sha256(b"secret").digest(),
    )
    return CompileContext(
        registry, "ROUTED",
        admitted_macros=tuple(key.removeprefix("macro.") for key in M_ACTION_KEYS),
        fanout_admitted=True,
        action_surface_version="v3",
    )


def functional_report() -> dict:
    return {
        "phase1_passed": True,
        "phase2_passed": True,
        "phase2_skipped": False,
        "infrastructure_failure": False,
        "num_vectors": 200,
        "simulation_report": {
            "cycles_simulated": 200,
            "mismatch_count": 0,
            "protocol_mismatch_count": 0,
            "result_fail_seen": False,
        },
    }


def test_one_macro_authority_binds_public_cost_and_compilers() -> None:
    cost_map = P_OPERATION_REGISTRY["CALL_MACRO"]["heavy_call_cost_by_action"]
    assert tuple(cost_map) == M_ACTION_KEYS
    assert cost_map == dict(M_HEAVY_CALLS)
    for action_key in M_ACTION_KEYS:
        direct = compile_macro(action_key.removeprefix("macro."), context())
        embedded = compile_candidate_v5(
            {"language": "P", "steps": [
                {"step_id": "s1", "op": "CALL_MACRO", "args": {"action_key": action_key}},
            ]},
            context(),
        )
        definition = macro_definition(action_key)
        assert direct.host_steps == embedded.host_steps
        assert direct.heavy_calls == embedded.heavy_calls == definition.heavy_call_cost
        assert direct.host_steps[0].operation == "macro.execute"
        assert direct.host_steps[0].public_args["flow_name"] == definition.flow_name


def test_embedded_macro_dispatches_the_production_macro_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    compiled = compile_candidate_v5(
        {"language": "P", "steps": [
            {"step_id": "s1", "op": "CALL_MACRO", "args": {"action_key": "macro.retime"}},
        ]},
        context(),
    )
    calls: list[tuple[Path, str]] = []

    async def fake_macro(*, root: Path, flow: str, out: Path, python: Path) -> dict:
        calls.append((root, flow))
        return {
            "canonical_endpoint": "VALID_COMPLETION",
            "actual_heavy_calls": 3,
            "actual_light_mutations": 0,
        }

    monkeypatch.setattr(runtime, "_execute_macro", fake_macro)
    result = asyncio.run(
        runtime._execute_typed(
            compiled=compiled,
            registry=context().registry,
            root=tmp_path / "root.dcp",
            out=tmp_path / "run",
            python=Path("python"),
            vivado=Path("vivado"),
        )
    )
    assert calls == [(tmp_path / "root.dcp", "retime")]
    assert result["canonical_endpoint"] == "VALID_COMPLETION"
    assert result["boundary_runtime_exercised"] is True


def test_same_dcp_hash_forces_exact_zero_and_epsilon_is_frozen() -> None:
    frozen = freeze_epsilon_fmax([400.0, 400.02, 399.98])
    assert frozen["epsilon_fmax"] == pytest.approx(0.02)
    score = score_fmax_delta(
        root_fmax_mhz=400.0,
        child_fmax_mhz=410.0,
        root_sha256="a" * 64,
        child_sha256="a" * 64,
        epsilon_fmax=frozen["epsilon_fmax"],
    )
    assert score["measurement_delta_fmax_mhz"] == 10.0
    assert score["delta_fmax_mhz"] == 0.0
    assert score["byte_identical_child"] is True


def test_official_trace_records_byte_identity_guard(tmp_path: Path) -> None:
    root = tmp_path / "root.dcp"
    child = tmp_path / "child.dcp"
    root.write_bytes(b"same checkpoint")
    child.write_bytes(root.read_bytes())
    tracer = OfficialContestTracer(tmp_path, "research-dev", False, "none")
    row = tracer._base_row(
        node_id="n", transition_id="t", parent_node_id="p",
        optimizer_name="test", flow_sequence=[], input_dcp=str(root),
        output_dcp=str(child), fmax_input_mhz=400.0,
        fmax_output_mhz=401.0, wns_ns=0.0, tns_ns=0.0,
        runtime_seconds=1.0, openrouter_cost_usd=0.0,
        valid_for_scoring=True, produced_output_dcp=True,
        pure_qor_score=None, pure_qor_components={}, rejection_reason=None,
    )
    assert row["byte_identical_child"] is True
    assert row["measurement_delta_fmax_mhz"] == 1.0
    assert row["delta_fmax_mhz"] == 0.0


def test_strict_functional_terminal_and_root_fallback() -> None:
    terminal = classify_functional_validation(
        functional_report(), returncode=0, timed_out=False,
    )
    assert terminal == STRICT_FUNCTIONAL_PASS
    delta = score_fmax_delta(
        root_fmax_mhz=400.0, child_fmax_mhz=405.0,
        root_sha256="a" * 64, child_sha256="b" * 64, epsilon_fmax=0.01,
    )
    passed = canonical_scientific_outcome(
        physical_endpoint="VALID_COMPLETION",
        functional_terminal=terminal,
        delta=delta,
    )
    failed = canonical_scientific_outcome(
        physical_endpoint="VALID_COMPLETION",
        functional_terminal="FUNCTIONAL_MISMATCH",
        delta=delta,
    )
    assert passed["valid_child_conditional_delta_fmax_mhz"] == 5.0
    assert passed["root_fallback_deployment_delta_fmax_mhz"] == 5.0
    assert failed["valid_child_conditional_delta_fmax_mhz"] is None
    assert failed["root_fallback_deployment_delta_fmax_mhz"] == 0.0


def test_boundary_requires_runtime_evidence_not_only_declared_language() -> None:
    candidate = {"language": "P", "steps": [
        {"step_id": "s1", "op": "UNROUTE", "args": {}},
        {"step_id": "s2", "op": "ROUTE", "args": {"directive": "Default"}},
    ]}
    before = boundary_evidence(
        candidate=candidate, execution_started=False, execution=None,
    )
    after = boundary_evidence(
        candidate=candidate,
        execution_started=True,
        execution={"call_trace": [
            {"operation": "vivado.unroute_all"},
            {"operation": "vivado.route_design"},
        ]},
    )
    assert before["compiled_boundary"] == "P"
    assert before["boundary_runtime_exercised"] is False
    assert after["boundary_runtime_exercised"] is True


def test_incomplete_functional_report_never_passes() -> None:
    report = functional_report()
    report["simulation_report"]["cycles_simulated"] = 199
    assert classify_functional_validation(
        report, returncode=0, timed_out=False,
    ) == "FUNCTIONAL_INCOMPLETE"


def test_authorised_request_cap_counts_both_gate3_admissions() -> None:
    assert wp014e.PHYSICAL_MODEL_REQUEST_CAP == 950
    assert wp014e.PRIOR_PROVIDER_ONLY_REQUESTS == 28
    assert wp014e.IMPLEMENTATION_HEAVY_CALL_CAP == 600


def test_backend_resume_cli_has_no_provider_or_planning_inputs() -> None:
    args = wp014e.parser().parse_args([
        "resume-backend", "--matrix", "matrix.json",
        "--planning-stop", "planning-stop.json",
        "--amendment", "resume.json", "--capture", "capture",
        "--out", "backend", "--python", "python", "--vivado", "vivado",
    ])
    assert args.command == "resume-backend"
    assert not hasattr(args, "bundle")
    assert not hasattr(args, "admission")
    assert not hasattr(args, "kimi_endpoint")


def test_verified_object_rejects_postfreeze_mutation(tmp_path: Path) -> None:
    path = tmp_path / "frozen.json"
    core = {"cell": "PROVIDER_FAILURE", "retry": 0}
    wp014e.write(path, core | {"aggregate_sha256": wp014e.digest(core)})
    assert wp014e._verified_object(path, "aggregate_sha256")["retry"] == 0
    value = wp014e.read(path)
    value["retry"] = 1
    wp014e.write(path, value)
    with pytest.raises(SystemExit, match="aggregate_sha256_invalid"):
        wp014e._verified_object(path, "aggregate_sha256")


def test_child_path_prefers_hash_bound_canonical_copy(tmp_path: Path) -> None:
    canonical = tmp_path / "published.dcp"
    duplicate = tmp_path / "execution" / "job" / "nested" / "copy.dcp"
    duplicate.parent.mkdir(parents=True)
    canonical.write_bytes(b"same-child")
    duplicate.write_bytes(canonical.read_bytes())
    sha = hashlib.sha256(canonical.read_bytes()).hexdigest()
    row = {
        "job_id": "job",
        "execution": {"child_path": str(canonical), "child": {"sha256": sha}},
    }
    assert wp014e._child_path(row, tmp_path) == canonical


def test_process_audit_excludes_license_daemon_but_not_vivado_worker() -> None:
    license_command = "/opt/Xilinx/Vivado/latest/bin/unwrapped/lnx64.o/lmgrd -c Xilinx.lic"
    assert process_audit.matched_markers("lmgrd", license_command) == []
    assert process_audit.matched_markers(
        "vivado", "/opt/Xilinx/Vivado/2025.1/bin/vivado -mode batch"
    ) == ["vivado"]
