"""Offline Tcl >=8.6 diagnostics, optionally using LATCH_TEST_TCLSH.

Use an available tclsh or Python's embedded Tcl. No Vivado, checkpoint or
model is involved.
"""

import base64
import json
import os
import shutil
import subprocess
import sys

import pytest

from optimizer_pipeline.dof.vivado_mutation_worker import (
    STATUS_PREFIX,
    WORKER_REQUEST_SCHEMA,
    build_fixed_tcl,
    parse_request_line,
    parse_tcl_result,
)


@pytest.fixture(scope="module")
def tcl_command():
    explicit = os.environ.get("LATCH_TEST_TCLSH")
    candidates = [explicit] if explicit else ["tclsh8.6", "tclsh8.7", "tclsh9.0", "tclsh"]
    unavailable = []
    commands = []
    for candidate in candidates:
        interpreter = shutil.which(candidate)
        if interpreter is None:
            unavailable.append(f"{candidate}: not found")
            continue
        commands.append([interpreter])
    if not explicit:
        commands.append([
            sys.executable, "-I", "-B", "-c",
            "import sys, tkinter; tkinter.Tcl().eval(sys.stdin.read())",
        ])
    for command in commands:
        label = command[0] if len(command) == 1 else "Python tkinter.Tcl"
        try:
            probe = subprocess.run(
                command,
                input="puts [info patchlevel]\nputs [expr {[package vcompare [info patchlevel] 8.6] >= 0}]\n",
                text=True, capture_output=True, check=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            unavailable.append(f"{label}: {type(exc).__name__}")
            continue
        version = probe.stdout.splitlines()
        if len(version) == 2 and version[1] == "1":
            return command
        unavailable.append(f"{label}: Tcl {version[0] if version else 'unknown'}")
    reason = (
        "Offline diagnostics require Tcl >=8.6 for binary encode base64; "
        + "; ".join(unavailable)
        + ". Set LATCH_TEST_TCLSH to an available interpreter."
    )
    if explicit:
        pytest.fail("Invalid LATCH_TEST_TCLSH override: " + reason)
    pytest.skip(reason)


def _command():
    return build_fixed_tcl(parse_request_line(json.dumps({
        "schema_version": WORKER_REQUEST_SCHEMA,
        "operation_id": "vivado.phys_opt_design",
        "call_token": "diagnostic-001",
        "timeout_seconds": 10.0,
        "arguments": {"directive": "Explore"},
    }).encode()))


@pytest.mark.parametrize("fails", [False, True])
def test_tcl_diagnostic_preserves_single_operation_and_status(fails, tcl_command):
    outcome = "error {bounded synthetic failure}" if fails else "return {}"
    script = (
        "set calls 0\n"
        "proc phys_opt_design {args} {incr ::calls; " + outcome + "}\n"
        + _command() + "\nputs stderr $calls\n"
    )
    result = subprocess.run(tcl_command, input=script.encode(), capture_output=True,
                            check=True, timeout=5)
    assert result.stderr.strip() == b"1"
    status, output = parse_tcl_result("diagnostic-001", result.stdout)
    assert status == int(fails)
    prefix = b"DOF_VIVADO_MUTATION_TCL_ERROR_V1:diagnostic-001:"
    rows = [line[len(prefix):] for line in output.splitlines()
            if line.startswith(prefix)]
    assert len(rows) == int(fails)
    if fails:
        assert base64.b64decode(rows[0]) == b"bounded synthetic failure"


def test_legacy_failure_output_remains_accepted():
    status, output = parse_tcl_result(
        "diagnostic-001", f"{STATUS_PREFIX}diagnostic-001:1\n".encode())
    assert status == 1
    assert output == b""


def test_diagnostic_does_not_interpolate_result_as_tcl(tcl_command):
    message = "do not run [incr ::injected] ; café"
    script = (
        "set calls 0\nset injected 0\n"
        "proc phys_opt_design {args} {incr ::calls; error {" + message + "}}\n"
        + _command() + "\nputs stderr $injected\n"
    )
    result = subprocess.run(tcl_command, input=script.encode(), capture_output=True,
                            check=True, timeout=5)
    assert result.stderr.strip() == b"0"
    status, output = parse_tcl_result("diagnostic-001", result.stdout)
    assert status == 1
    prefix = b"DOF_VIVADO_MUTATION_TCL_ERROR_V1:diagnostic-001:"
    value = next(line[len(prefix):] for line in output.splitlines()
                 if line.startswith(prefix))
    assert base64.b64decode(value).decode() == message
