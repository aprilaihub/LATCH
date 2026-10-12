from pathlib import Path

from optimizer_pipeline.models import ExecutionResult
from optimizer_pipeline.validators import OutputValidator


def test_output_validator_marks_route_design_failure_invalid(tmp_path: Path):
    run_dir = tmp_path / "node" / "executor_run"
    run_dir.mkdir(parents=True)
    output = tmp_path / "node" / "output.dcp"
    output.write_text("fake dcp")
    summary = run_dir / "run_summary.json"
    summary.write_text("{}\n")
    (run_dir.parent / "terminal.log").write_text(
        "ERROR: [Common 17-39] 'route_design' failed due to earlier errors.\n"
        "# of nets with routing errors: 1\n"
    )
    result = ExecutionResult(
        success=True,
        flow="lut_cone",
        input_dcp="input.dcp",
        output_dcp=str(output),
        run_dir=str(run_dir),
        summary_path=str(summary),
        exit_code=0,
    )
    validation = OutputValidator().validate(result)
    assert validation.valid is False
    assert validation.status == "INVALID"
    assert any("route" in err.lower() for err in validation.errors)


def test_output_validator_marks_rapidwright_edif_fallback_warning(tmp_path: Path):
    run_dir = tmp_path / "node" / "executor_run"
    run_dir.mkdir(parents=True)
    output = tmp_path / "node" / "output.dcp"
    output.write_text("fake dcp")
    summary = run_dir / "run_summary.json"
    summary.write_text("{}\n")
    (run_dir.parent / "rapidwright-mcp.log").write_text(
        "Unable to find a readable EDIF file for the DCP.\n"
        "Failed to auto-generate an EDIF file because Couldn't find vivado on PATH.\n"
    )
    result = ExecutionResult(
        success=True,
        flow="auto_pblock",
        input_dcp="input.dcp",
        output_dcp=str(output),
        run_dir=str(run_dir),
        summary_path=str(summary),
        exit_code=0,
    )
    validation = OutputValidator().validate(result)
    assert validation.valid is True
    assert validation.status == "FALLBACK_USED"
    assert validation.fallback_used is True
    assert validation.warnings


def test_output_validator_does_not_treat_vivado_tcl_as_path_fallback(tmp_path: Path):
    run_dir = tmp_path / "node" / "executor_run"
    run_dir.mkdir(parents=True)
    output = tmp_path / "node" / "output.dcp"
    output.write_text("fake dcp")
    summary = run_dir / "run_summary.json"
    summary.write_text("{}\n")
    (run_dir.parent / "vivado.log").write_text(
        "INFO: [Vivado_Tcl 4-241] Physical synthesis in post route mode\n"
        "WARNING: [Timing 38-242] A property is not set on this timing path\n"
    )
    result = ExecutionResult(
        success=True,
        flow="manual_action",
        input_dcp="input.dcp",
        output_dcp=str(output),
        run_dir=str(run_dir),
        summary_path=str(summary),
        exit_code=0,
    )

    validation = OutputValidator().validate(result)

    assert validation.status == "CLEAN"
    assert validation.fallback_used is False


def test_output_validator_accepts_in_memory_metrics_without_persisted_summary(tmp_path: Path):
    run_dir = tmp_path / "node" / "executor_run"
    run_dir.mkdir(parents=True)
    output = tmp_path / "node" / "output.dcp"
    output.write_text("fake dcp")
    result = ExecutionResult(
        success=True,
        flow="phys_opt",
        input_dcp="input.dcp",
        output_dcp=str(output),
        run_dir=str(run_dir),
        summary_path=str(run_dir / "run_summary.json"),
        metrics={"final_fmax_mhz": 411.0, "final_wns_ns": -0.12},
        exit_code=0,
    )

    validation = OutputValidator().validate(result)

    assert validation.valid is True
    assert validation.evidence["artifact_checks"] == {
        "executor_success": True,
        "output_dcp_exists": True,
        "summary_exists": False,
        "in_memory_metrics_available": True,
    }
