import os
import importlib.util
import sys
from pathlib import Path

from validate_dcps import ensure_vivado_on_path, parse_simulation_output


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_run_flow_matrix_module():
    spec = importlib.util.spec_from_file_location(
        "run_flow_matrix",
        REPO_ROOT / "scripts" / "run_flow_matrix.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ensure_vivado_on_path_uses_vivado_exec(tmp_path: Path) -> None:
    vivado_bin = tmp_path / "Vivado" / "bin"
    vivado_bin.mkdir(parents=True)
    vivado = vivado_bin / "vivado"
    vivado.write_text("#!/bin/sh\n")

    env = {
        "PATH": "/usr/bin",
        "VIVADO_EXEC": str(vivado),
    }

    updated = ensure_vivado_on_path(env)

    assert updated["PATH"].split(os.pathsep)[0] == str(vivado_bin.resolve())


def test_ensure_vivado_on_path_is_idempotent(tmp_path: Path) -> None:
    vivado_bin = tmp_path / "Vivado" / "bin"
    vivado_bin.mkdir(parents=True)
    vivado = vivado_bin / "vivado"
    vivado.write_text("#!/bin/sh\n")
    existing_path = f"{vivado_bin.resolve()}{os.pathsep}/usr/bin"

    updated = ensure_vivado_on_path({
        "PATH": existing_path,
        "VIVADO_PATH": str(vivado),
    })

    assert updated["PATH"] == existing_path


def test_parse_simulation_output_pass_has_no_infrastructure_failure() -> None:
    parsed = parse_simulation_output(
        "\n".join([
            "Cycles simulated: 2",
            "Mismatches found: 0",
            "Protocol mismatches found: 0",
            "Result: PASS",
        ]),
        0,
        2,
    )

    assert parsed["passed"] is True
    assert parsed["infrastructure_failure"] is False
    assert parsed["infrastructure_reason"] is None


def test_parse_simulation_output_mismatch_stays_logical_fail_even_with_fatal_text() -> None:
    parsed = parse_simulation_output(
        "\n".join([
            "MISMATCH at cycle 1: out golden=0 revised=1",
            "FATAL: simulator emitted an additional diagnostic",
            "Cycles simulated: 1",
            "Result: FAIL",
        ]),
        1,
        1,
    )

    assert parsed["passed"] is False
    assert parsed["mismatch_count"] > 0
    assert parsed["simulator_failed"] is True
    assert parsed["infrastructure_failure"] is False


def test_parse_simulation_output_protocol_mismatch_stays_logical_fail() -> None:
    parsed = parse_simulation_output(
        "\n".join([
            "PROTOCOL MISMATCH at cycle 1: valid golden=0 revised=1",
            "XSIM 43-9999: simulator emitted an additional diagnostic",
            "Cycles simulated: 1",
        ]),
        1,
        1,
    )

    assert parsed["passed"] is False
    assert parsed["protocol_mismatch_count"] > 0
    assert parsed["simulator_failed"] is True
    assert parsed["infrastructure_failure"] is False


def test_parse_simulation_output_missing_result_marker_is_infrastructure_failure() -> None:
    parsed = parse_simulation_output("Cycles simulated: 2", 0, 2)

    assert parsed["passed"] is False
    assert parsed["infrastructure_failure"] is True
    assert parsed["infrastructure_reason"] == "testbench_completion_marker_missing"


def test_parse_simulation_output_simulator_fatal_without_mismatch_is_infrastructure_failure() -> None:
    parsed = parse_simulation_output(
        "\n".join([
            "FATAL: simulator failed before the testbench completed",
            "Cycles simulated: 0",
        ]),
        1,
        2,
    )

    assert parsed["passed"] is False
    assert parsed["infrastructure_failure"] is True
    assert parsed["infrastructure_reason"] == "simulator_failed"


def test_run_flow_matrix_classifies_infrastructure_failures() -> None:
    module = load_run_flow_matrix_module()

    assert module.classify_validation_status(2, {}) == "infrastructure_failed"
    assert module.classify_validation_status(1, {"infrastructure_failure": True}) == "infrastructure_failed"


def test_run_flow_matrix_classifies_structural_only_skip_as_pass() -> None:
    module = load_run_flow_matrix_module()

    report = {
        "overall_passed": True,
        "phase2_skipped": True,
        "infrastructure_failure": False,
    }
    assert module.classify_validation_status(0, report) == "passed"
