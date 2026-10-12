"""Inert MCP responses and file fixtures only: no EDA processes or network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimizer_pipeline.backends.xilinx.legacy import (
    FPGAOptimizerTest,
    MCPToolCallError,
    _checkpoint_signature,
    _require_written_checkpoint,
)


def response(text=None, **fields):
    return SimpleNamespace(content=[] if text is None else [SimpleNamespace(text=text)], **fields)


def invoke(tmp_path, backend, result, tool_name="fixture"):
    tester = FPGAOptimizerTest(run_dir=tmp_path)

    async def call_tool(name, arguments):
        return result

    setattr(tester, f"{backend}_session", SimpleNamespace(call_tool=call_tool))
    return asyncio.run(getattr(tester, f"call_{backend}_tool")(tool_name, {}))


@pytest.mark.parametrize("backend", ["vivado", "rapidwright"])
@pytest.mark.parametrize("result", [
    response("arbitrary payload", isError=True),
    response('{"error": "could not write"}'),
    response('{"status": "error", "message": "could not initialize"}'),
    response(structuredContent={"error": "could not read"}),
    response("Error: tool process terminated"),
    {"content": [{"type": "text", "text": '{"error": "could not write"}'}]},
])
def test_explicit_tool_errors_propagate_without_copying_payload(tmp_path, backend, result):
    with pytest.raises(MCPToolCallError) as caught:
        invoke(tmp_path, backend, result)
    assert caught.value.tool_name == "fixture"
    assert "could not" not in str(caught.value)
    assert "arbitrary payload" not in str(caught.value)


@pytest.mark.parametrize("backend", ["vivado", "rapidwright"])
@pytest.mark.parametrize("text", [
    "Opened checkpoint.\nCompleted normally.",
    '{"status": "success", "error": null, "bytes_written": 123}',
    '{"status": "success", "optimized_count": 1, "results": [{"status": "error", "error": "bad pin"}]}',
    '{"status": "success", "optimized_count": 0, "results": [{"status": "error", "message": "all pins rejected"}]}',
    "Report contains the word ERROR: as a column description.",
    "WARNING: [Vivado_Tcl 4-1061] diagnostic only",
    "INFO: previous ERROR: [Vivado_Tcl 4-1061] quoted by a successful query",
])
def test_success_text_and_nested_pin_failures_remain_compatible(tmp_path, backend, text):
    assert invoke(tmp_path, backend, response(text, isError=False)) == text


def test_legacy_mapping_carrier_and_empty_output(tmp_path):
    assert invoke(tmp_path, "rapidwright", {"content": [{"type": "text", "text": "success"}]}) == "success"
    assert invoke(tmp_path, "vivado", response()) == "(no output)"


@pytest.mark.parametrize("backend", ["vivado", "rapidwright"])
def test_nontext_content_and_structured_only_success_keep_legacy_returns(tmp_path, backend):
    assert invoke(tmp_path, backend, SimpleNamespace(content=[SimpleNamespace(type="image")])) == ""
    assert invoke(tmp_path, backend, response(structuredContent={"status": "success"})) == "(no output)"


@pytest.mark.parametrize("backend", ["vivado", "rapidwright"])
def test_multiple_text_blocks_keep_order_but_any_explicit_error_stops(tmp_path, backend):
    success = SimpleNamespace(content=[SimpleNamespace(text="first"), SimpleNamespace(text="second")])
    assert invoke(tmp_path, backend, success) == "first\nsecond"
    failure = SimpleNamespace(content=[SimpleNamespace(text="first"), SimpleNamespace(text='{"error": "failed"}')])
    with pytest.raises(MCPToolCallError):
        invoke(tmp_path, backend, failure)


@pytest.mark.parametrize("backend", ["vivado", "rapidwright"])
def test_json_array_error_entries_are_not_global_tool_errors(tmp_path, backend):
    text = '[{"pin": "lut/I0", "error": "local failure"}]'
    assert invoke(tmp_path, backend, response(text)) == text


def test_explicit_vivado_numbered_command_error_is_not_success(tmp_path):
    with pytest.raises(MCPToolCallError, match=r"Vivado \[Vivado_Tcl 4-1061\]"):
        invoke(tmp_path, "vivado", response("Operation complete.\nERROR: [Vivado_Tcl 4-1061] option rejected"))


@pytest.mark.parametrize("tool_name,text,reason", [
    ("not_registered", "Unknown tool: not_registered", "server unknown tool response"),
    ("get_wns", "ERROR: clock not found", "target clock not found"),
])
@pytest.mark.parametrize("mapping", [False, True])
def test_known_complete_vivado_server_error_responses(tmp_path, tool_name, text, reason, mapping):
    result = {"content": [{"type": "text", "text": text}]} if mapping else response(text)
    with pytest.raises(MCPToolCallError) as caught:
        invoke(tmp_path, "vivado", result, tool_name)
    assert caught.value.reason == reason


@pytest.mark.parametrize("tool_name,text", [
    ("get_wns", "INFO: previous ERROR: clock not found was handled"),
    ("get_wns", "Report quotes ERROR: clock not found as an example"),
    ("get_wns", "Unknown tool: other_name"),
    ("get_wns", "INFO: Unknown tool: get_wns"),
    ("get_wns", "ERROR: clock not found\n0.125"),
    ("run_tcl", "ERROR: clock not found"),
])
def test_known_error_words_inside_success_or_user_tcl_are_not_matched(tmp_path, tool_name, text):
    assert invoke(tmp_path, "vivado", response(text), tool_name) == text


@pytest.mark.parametrize("backend", ["vivado", "rapidwright"])
@pytest.mark.parametrize("error_type", [asyncio.TimeoutError, asyncio.CancelledError])
def test_timeout_and_cancellation_are_preserved(tmp_path, backend, error_type):
    tester = FPGAOptimizerTest(run_dir=tmp_path)

    async def call_tool(name, arguments):
        raise error_type()

    setattr(tester, f"{backend}_session", SimpleNamespace(call_tool=call_tool))
    with pytest.raises(error_type):
        asyncio.run(getattr(tester, f"call_{backend}_tool")("fixture", {}))


def test_output_check_rejects_missing_empty_directory_and_stale_file(tmp_path):
    output = tmp_path / "candidate.dcp"
    with pytest.raises(MCPToolCallError, match="not created"):
        _require_written_checkpoint(output, None, "RapidWright")
    output.touch()
    with pytest.raises(MCPToolCallError, match="nonempty regular"):
        _require_written_checkpoint(output, None, "RapidWright")
    with pytest.raises(MCPToolCallError, match="nonempty regular"):
        _require_written_checkpoint(tmp_path, None, "RapidWright")
    output.write_bytes(b"old candidate")
    before = _checkpoint_signature(output)
    with pytest.raises(MCPToolCallError, match="not observably updated"):
        _require_written_checkpoint(output, before, "RapidWright")
    output.write_bytes(b"new candidate with changed size")
    _require_written_checkpoint(output, before, "RapidWright")


def run_mock_lut_flow(tmp_path, rw_write="ok", final_write="ok", pin_results="partial"):
    tester = FPGAOptimizerTest(run_dir=tmp_path / "run")
    events = []
    output = tmp_path / "final.dcp"
    if rw_write == "stale":
        (tester.run_dir / "lut_cone_optimized.dcp").write_bytes(b"old intermediate")
    if final_write == "stale":
        output.write_bytes(b"old final")

    async def constant(*args, **kwargs):
        return 0.0

    async def clock():
        return 1.5

    tester.fetch_clock_period = clock
    tester.get_wns_for_target_clock = constant
    tester.save_analysis_context = lambda *args, **kwargs: None

    async def vivado_call(name, arguments):
        events.append(("vivado", name))
        if name == "extract_critical_path_pins":
            return response(json.dumps([["lut/I0", "lut/I1"]]))
        if name == "write_checkpoint":
            if final_write == "error":
                return response("Error: checkpoint write failed")
            if final_write == "ok":
                Path(arguments["dcp_path"]).write_bytes(b"final mock fixture")
            if final_write == "empty":
                Path(arguments["dcp_path"]).touch()
        return response("Mock completed.")

    async def rapidwright_call(name, arguments):
        events.append(("rapidwright", name))
        if name == "optimize_lut_input_cone":
            if pin_results == "error":
                return response('{"error": "global optimizer failure"}')
            return response(json.dumps({
                "status": "success", "optimized_count": int(pin_results == "partial"),
                "total_pins": 2,
                "results": [{"pin": "lut/I0", "status": "error", "message": "local rejection"}],
            }))
        if name == "write_checkpoint":
            if rw_write == "error":
                return response('{"error": "checkpoint write failed"}')
            if rw_write == "ok":
                Path(arguments["dcp_path"]).write_bytes(b"intermediate mock fixture")
            if rw_write == "empty":
                Path(arguments["dcp_path"]).touch()
        return response('{"status": "success"}')

    tester.vivado_session = SimpleNamespace(call_tool=vivado_call)
    tester.rapidwright_session = SimpleNamespace(call_tool=rapidwright_call)
    success = asyncio.run(tester.run_test_lut_cone(tmp_path / "input.dcp", output))
    return success, events


@pytest.mark.parametrize("mode", ["error", "missing", "empty", "stale"])
def test_lut_cone_stops_before_opening_failed_intermediate(tmp_path, mode):
    success, events = run_mock_lut_flow(tmp_path, rw_write=mode)
    assert success is False
    assert events.count(("vivado", "open_checkpoint")) == 1
    assert ("vivado", "route_design") not in events
    assert ("vivado", "write_checkpoint") not in events


@pytest.mark.parametrize("mode", ["error", "missing", "empty", "stale"])
def test_lut_cone_failed_final_write_cannot_return_true(tmp_path, mode):
    success, events = run_mock_lut_flow(tmp_path, final_write=mode)
    assert success is False
    assert ("vivado", "write_checkpoint") in events


@pytest.mark.parametrize("pin_results", ["partial", "none"])
def test_lut_cone_local_pin_failures_preserve_route_and_write_through(tmp_path, pin_results):
    success, events = run_mock_lut_flow(tmp_path, pin_results=pin_results)
    assert success is True
    assert ("vivado", "route_design") in events
    assert ("vivado", "write_checkpoint") in events


def test_lut_cone_global_optimizer_error_stops_before_checkpoint(tmp_path):
    success, events = run_mock_lut_flow(tmp_path, pin_results="error")
    assert success is False
    assert ("rapidwright", "write_checkpoint") not in events
