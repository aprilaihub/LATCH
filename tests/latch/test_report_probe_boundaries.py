"""Inert Tcl and injected process results; no EDA or model calls."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from experiments.ablation.runtime import real_analysis_provider as path_probe
from experiments.ablation.runtime.analysis import report_probe_backends as study_backends
from experiments.ablation.runtime.analysis.controller import _fresh_probe_satisfied as study_fresh
from optimizer_pipeline.backends.xilinx.tcl import tcl_word
from optimizer_pipeline.capabilities.analysis import report_probe_backends as shared_backends
from optimizer_pipeline.capabilities.analysis.controller import _fresh_probe_satisfied as shared_fresh


TIMING_REPORT = """WNS(ns) -0.5
TNS(ns) -1.0
Data Path Delay: 4.0
logic 1.0
route 3.0
Logic Levels: 2
"""
BACKENDS = [shared_backends, study_backends]
PATH_NAMES = [
    "foo bar.dcp",
    "evil}; set ::injected 1; #.dcp",
    "[set ::injected 1] $injected.dcp",
    'quotes " braces { } and backslash \\.dcp',
    "line\nbreak\tand café.dcp",
]


def evaluate_fixed_tcl(script):
    """Run Tcl syntax with inert commands and record their exact arguments."""
    tkinter = pytest.importorskip("tkinter")
    interpreter = tkinter.Tcl()
    interpreter.eval("set ::injected 0")
    seen = []
    commands = ["open_checkpoint", "report_timing_summary", "report_timing",
                "report_utilization", "report_high_fanout_nets", "report_route_status",
                "close_design", "exit"]
    for name in commands:
        def record(*args, name=name):
            seen.append((name, args))
            return ""
        interpreter.createcommand(name, record)
    interpreter.eval(script)
    assert interpreter.eval("set ::injected") == "0"
    return seen


def context_for(tmp_path, name="input.dcp"):
    dcp = tmp_path / name
    dcp.write_text("dummy checkpoint text")
    return SimpleNamespace(run_dir=tmp_path / ("reports " + name), node=SimpleNamespace(
        node_id="node_000_root", input_dcp=str(dcp), output_dcp=None))


@pytest.mark.parametrize("name", PATH_NAMES + ["", "\\\n"])
def test_tcl_literal_round_trips_without_substitution(name):
    seen = evaluate_fixed_tcl("open_checkpoint " + tcl_word(name))
    assert seen == [("open_checkpoint", (name,))]


@pytest.mark.parametrize("backends", BACKENDS, ids=["shared", "study"])
@pytest.mark.parametrize("name", PATH_NAMES)
def test_local_backend_preserves_input_and_report_path_arguments(tmp_path, monkeypatch, backends, name):
    context = context_for(tmp_path, name)
    scripts = []

    def fake_owned(argv, *, cwd, timeout, text):
        assert argv[0] == "inert-fixture-vivado"
        assert timeout == 12 and text is True
        script = Path(argv[argv.index("-source") + 1])
        scripts.append(script)
        assert script.parent == Path(cwd) and script.is_absolute()
        seen = evaluate_fixed_tcl(script.read_text())
        assert seen[0] == ("open_checkpoint", (str(Path(context.node.input_dcp).resolve()),))
        report = script.with_suffix(".rpt")
        reports = [args for command, args in seen if command.startswith("report_")]
        assert len(reports) == 2
        assert all(args[-2:] == ("-file", str(report)) for args in reports)
        report.write_text(TIMING_REPORT)
        return SimpleNamespace(returncode=0, stdout="fixture stdout", stderr="fixture stderr")

    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", fake_owned)
    result = backends.LocalVivadoReportBackend(vivado_bin="inert-fixture-vivado", timeout_sec=12).report_timing_summary(args={}, context=context)
    assert len(scripts) == 1
    assert result["available"] is True and result["fresh_vivado_report"] is True
    assert result["critical_path_features"]["route_delay_ratio"] == 0.75
    assert "fixture stderr" in Path(result["log_path"]).read_text()


@pytest.mark.parametrize("name", PATH_NAMES)
def test_path_probe_preserves_input_and_report_path_arguments(tmp_path, monkeypatch, name):
    context = context_for(tmp_path, name)

    def fake_owned(argv, *, cwd, timeout, text):
        assert timeout == 1800 and text is True
        script = Path(argv[argv.index("-source") + 1])
        seen = evaluate_fixed_tcl(script.read_text())
        assert seen[0] == ("open_checkpoint", (str(Path(context.node.input_dcp).resolve()),))
        report = script.with_suffix(".rpt")
        reports = [args for command, args in seen if command.startswith("report_")]
        assert len(reports) == 4
        assert all(args[-2:] == ("-file", str(report)) for args in reports)
        report.write_text(TIMING_REPORT)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", fake_owned)
    result = path_probe._probe_path_features(Path(context.node.input_dcp), context.run_dir)
    assert result["critical_path_features"]["route_delay_ratio"] == 0.75
    status = json.loads((context.run_dir / "path_probe" / "probe-status.json").read_text())
    assert status["status"] == "MEASURED" and status["exit_code"] == 0


@pytest.mark.parametrize("backends", BACKENDS, ids=["shared", "study"])
@pytest.mark.parametrize("exit_code,report_text", [(7, TIMING_REPORT), (0, "")])
def test_failed_or_empty_local_report_cannot_satisfy_fresh_evidence(tmp_path, monkeypatch, backends, exit_code, report_text):
    context = context_for(tmp_path)

    def fake_owned(argv, **kwargs):
        Path(kwargs["cwd"], "timing_summary.rpt").write_text(report_text)
        return SimpleNamespace(returncode=exit_code, stdout="partial run", stderr="failure details")

    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", fake_owned)
    result = backends.LocalVivadoReportBackend().report_timing_summary(args={}, context=context)
    assert result["available"] is False and result["fresh_vivado_report"] is False
    assert result["status"] == "INFRASTRUCTURE_FAILURE" and result["exit_code"] == exit_code
    assert "critical_path_features" not in result and "parsed" not in result
    assert Path(result["report_path"]).read_text() == report_text
    assert result["preview"] == report_text
    call = SimpleNamespace(tool_name="vivado_report_timing_summary", output=result, success=True, timed_out=False)
    assert not shared_fresh(call.tool_name, [call])
    assert not study_fresh(call.tool_name, [call])


@pytest.mark.parametrize("owned", [False, True])
def test_partial_path_report_nonzero_is_infrastructure_failure_for_all_callers(tmp_path, monkeypatch, owned):
    def fake_owned(argv, **kwargs):
        Path(kwargs["cwd"], "probe.rpt").write_text(TIMING_REPORT)
        return SimpleNamespace(returncode=9, stdout="partial run", stderr="failure details")

    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", fake_owned)
    assert path_probe._probe_path_features(tmp_path / "input.dcp", tmp_path, owned=owned) == {}
    status = json.loads((tmp_path / "path_probe" / "probe-status.json").read_text())
    assert status["status"] == "INFRASTRUCTURE_FAILURE" and status["exit_code"] == 9
    assert status["report_available"] is True
    assert Path(status["report_path"]).read_text() == TIMING_REPORT


@pytest.mark.parametrize("backends", BACKENDS, ids=["shared", "study"])
def test_repeated_local_probe_cannot_reuse_previous_report(tmp_path, monkeypatch, backends):
    context = context_for(tmp_path)
    calls = []

    def fake_owned(argv, **kwargs):
        calls.append(Path(kwargs["cwd"]))
        if len(calls) == 1:
            (calls[-1] / "timing_summary.rpt").write_text(TIMING_REPORT)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", fake_owned)
    backend = backends.LocalVivadoReportBackend()
    first = backend.report_timing_summary(args={}, context=context)
    second = backend.report_timing_summary(args={}, context=context)
    assert first["available"] is True and second["available"] is False
    assert second["fresh_vivado_report"] is False and second["report_path"] is None
    assert calls[0] != calls[1]
    assert Path(first["report_path"]).read_text() == TIMING_REPORT


def test_repeated_path_probe_cannot_reuse_previous_report(tmp_path, monkeypatch):
    calls = []

    def fake_owned(argv, **kwargs):
        calls.append(Path(kwargs["cwd"]))
        if len(calls) == 1:
            (calls[-1] / "probe.rpt").write_text(TIMING_REPORT)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", fake_owned)
    assert path_probe._probe_path_features(tmp_path / "input.dcp", tmp_path)
    assert path_probe._probe_path_features(tmp_path / "input.dcp", tmp_path) == {}
    assert calls[0] != calls[1]
    assert (calls[0] / "probe.rpt").read_text() == TIMING_REPORT
    status = json.loads((tmp_path / "path_probe" / "probe-status.json").read_text())
    assert status["status"] == "INFRASTRUCTURE_FAILURE" and status["report_path"] is None


@pytest.mark.parametrize("backends", BACKENDS, ids=["shared", "study"])
def test_local_runner_exception_cannot_be_fresh(tmp_path, monkeypatch, backends):
    def fake_owned(*args, **kwargs):
        raise RuntimeError("owned cleanup failed")

    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", fake_owned)
    result = backends.LocalVivadoReportBackend().report_timing_summary(args={}, context=context_for(tmp_path))
    assert result["status"] == "INFRASTRUCTURE_FAILURE"
    assert result["available"] is False and result["fresh_vivado_report"] is False


def test_path_probe_status_does_not_copy_arbitrary_exception_text(tmp_path, monkeypatch):
    def fake_owned(*args, **kwargs):
        raise RuntimeError("local diagnostic exception details")

    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", fake_owned)
    assert path_probe._probe_path_features(tmp_path / "input.dcp", tmp_path) == {}
    status = json.loads((tmp_path / "path_probe" / "probe-status.json").read_text())
    assert status == {"status": "INFRASTRUCTURE_FAILURE", "error_type": "RuntimeError"}


def test_ordinary_async_provider_runs_probe_outside_event_loop(tmp_path, monkeypatch):
    caller_thread = threading.get_ident()
    calls = []

    class Tester:
        def __init__(self, **kwargs):
            pass

        async def start_servers(self):
            pass

        async def analyze_design_for_flow_selection(self, input_dcp):
            return {"timing": "unchanged fixture"}

        async def cleanup(self):
            calls.append("cleanup")

    def fake_probe(input_dcp, run_dir):
        assert threading.get_ident() != caller_thread
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append("probe")
        return {"critical_path_features": {"route_delay_ratio": 0.75}}

    monkeypatch.setattr(path_probe, "_probe_path_features", fake_probe)
    provider = path_probe.build_real_analysis_provider(run_dir=tmp_path,
        config=SimpleNamespace(acceptance_owned_process=False), optimizer_test_cls=Tester)
    result = asyncio.run(provider(tmp_path / "input.dcp", None))
    assert result["timing"] == "unchanged fixture"
    assert result["critical_path_features"]["route_delay_ratio"] == 0.75
    assert calls == ["probe", "cleanup"]
