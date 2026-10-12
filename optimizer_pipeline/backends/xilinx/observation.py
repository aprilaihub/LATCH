"""Fresh read-only observation using retained owned workers and probe scripts.

This is a migrated capture, not a historical frozen-bundle replay. No legacy
repository check is disabled or monkeypatched. Requires a full tool checkout.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import platform
import sys
from types import SimpleNamespace
from .owned_process import run_owned
from optimizer_pipeline.scoring import file_sha256

REPO = Path(__file__).resolve().parents[3]


async def measure(root, output, vivado):
    from optimizer_pipeline.dof.processes import OwnedLineProtocolSession, ProcessLimits, build_target_process_environment
    from optimizer_pipeline.dof.vivado_readonly_worker import (READY_LINE, request_bytes,
        terminal_marker, parse_tcl_result, effective_vivado_environment_hash)
    from optimizer_pipeline.dof.preflight import _verify_worker_attestation, _validate_reports
    workspace = output / "measurement-workspace"
    isolated_home = workspace / "private" / "vivado-home"
    isolated_home.mkdir(parents=True, mode=0o700)
    env = build_target_process_environment(os.environ)
    argv = [sys.executable, "-I", str(REPO / "optimizer_pipeline/dof/vivado_readonly_worker.py"),
        "--vivado-exec", str(vivado), "--initialization-timeout-seconds", "120",
        "--run-workspace", str(workspace), "--isolated-home", str(isolated_home)]
    session = OwnedLineProtocolSession(argv, ready_line=READY_LINE,
        close_command=request_bytes("worker.exit", "cleanup", timeout_seconds=10),
        limits=ProcessLimits(initialization_timeout_seconds=120, whole_run_timeout_seconds=1800,
            output_limit_bytes=8 * 1024 * 1024), cwd=workspace, env=env)
    reports = {}
    try:
        await session.start()
        _verify_worker_attestation(session.startup_output.data,
            SimpleNamespace(expected_vivado_version="2025.1", expected_python_version=platform.python_version()),
            expected_environment_hash=effective_vivado_environment_hash(env, isolated_home))
        operations = [("vivado.open_checkpoint", {"dcp_path_b64": base64.b64encode(os.fsencode(root)).decode()})]
        operations += [(name, {}) for name in ("vivado.report_timing_summary", "vivado.report_route_status", "vivado.report_utilization", "vivado.close_design")]
        for index, (operation, arguments) in enumerate(operations):
            token = f"capture-{index}"
            # Retained read-only worker protocol allows at most 600 seconds.
            # This does not change the separate 900-second scoring timeout.
            response = await session.call(request_bytes(operation, token, timeout_seconds=600, arguments=arguments),
                terminal_marker=terminal_marker(token), timeout_seconds=600)
            status, data = parse_tcl_result(token, response.stdout.data)
            if status != 0:
                raise RuntimeError("Read-only observation operation failed: " + operation)
            reports[operation] = data
    finally:
        closed = await session.close()
        if not closed.to_dict(include_output_text=False).get("cleanup_ok"):
            raise RuntimeError("Owned observation worker cleanup failed")
    measured = _validate_reports(reports)
    if not measured["valid"]:
        raise RuntimeError("Read-only observation incomplete")
    return {"root_sha256": file_sha256(root), "timing": measured["timing"],
        "utilization": measured["utilization"],
        "route": measured["route_status"] | {"status": "FULLY_ROUTED"},
        "historical_replay": False}


def census(root):
    """WP014C/WP015 RapidWright WIRE selection, with the same 64-row limit."""
    sys.path.insert(0, str(REPO / "RapidWrightMCP"))
    rw = importlib.import_module("rapidwright_tools")
    initialized = rw.initialize_rapidwright("8G")
    if initialized.get("error") or initialized.get("status") not in {"success", "already_initialized"}:
        raise RuntimeError("rapidwright_census_initialization_failed")
    loaded = rw.read_checkpoint(str(root.resolve()))
    if loaded.get("error") or loaded.get("status") != "success":
        raise RuntimeError("rapidwright_census_read_failed")
    rows = []
    for net in rw._current_design.getNets():
        fanout, kind, name = int(net.getFanOut()), str(net.getType()), str(net.getName())
        if fanout >= 2 and kind == "WIRE" and name not in {"<const0>", "<const1>", "GLOBAL_LOGIC0", "GLOBAL_LOGIC1"}:
            rows.append({"name": name, "fanout": fanout, "net_type": kind})
    rows.sort(key=lambda row: (-row["fanout"], row["name"]))
    return {"root_sha256": file_sha256(root), "nets": rows[:64], "returned": min(64, len(rows)), "available": len(rows)}


def capture(*, root, output, vivado, target_clock, score_timeout):
    if output.exists():
        raise ValueError("Capture output must be a new directory")
    if not (REPO / "scripts/run_wp013_admission.py").is_file():
        raise RuntimeError("Full editable tool checkout required")
    before = file_sha256(root)
    output.mkdir(parents=True, mode=0o700)
    measurement = asyncio.run(measure(root, output, vivado))
    (output / "measurement.json").write_text(json.dumps(measurement, indent=2) + "\n")
    from optimizer_pipeline.scoring.read_only import run_readonly_score
    run_readonly_score(root, vivado_exec=vivado, scorer_script=REPO / "scripts/e1_score_dcp.tcl",
        run_dir=output / "root-score", frozen_target_clock=target_clock, timeout_seconds=score_timeout)
    for mode, destination in (("contract-probe", output / "contract-probe"),
                              ("hardblock-compatibility-probe", output / "hardblock-scan/probes/root")):
        argv = [sys.executable, str(REPO / "scripts/run_wp013_admission.py"), "--mode", mode,
            "--root", str(root), "--out", str(destination), "--python", sys.executable,
            "--vivado", str(vivado), "--omit-vivado-fanout"]
        completed = run_owned(argv, cwd=REPO, timeout=7500, text=True)
        (output / (mode + ".log")).write_text(completed.stdout + completed.stderr)
        summary_path = destination / "admission-summary.json"
        if completed.returncode != 0 or not summary_path.is_file() or json.loads(summary_path.read_text()).get("accepted") is not True:
            raise RuntimeError("Required owned query capture failed: " + mode)
    (output / "hardblock-scan/aggregate.json").write_text(json.dumps({"rows": [{"root_id": "root", "root_sha256": before}]}))
    # Run the JVM in its own bounded child, not inside the planner process.
    completed = run_owned([sys.executable, "-m", "optimizer_pipeline.backends.xilinx.observation", "--census-only", "--input", str(root),
        "--output", str(output / "rapidwright-net-census.json")], cwd=REPO, timeout=900, text=True)
    if completed.returncode != 0:
        raise RuntimeError("Required RapidWright census failed")
    if file_sha256(root) != before:
        raise RuntimeError("Observation changed the input checkpoint")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", type=Path)
    parser.add_argument("--target-clock")
    parser.add_argument("--score-timeout", type=float, default=900)
    parser.add_argument("--census-only", action="store_true")
    args = parser.parse_args()
    if args.census_only:
        if args.output.exists():
            parser.error("Census output exists")
        args.output.write_text(json.dumps(census(args.input.resolve()), indent=2))
    else:
        if not args.vivado or not args.target_clock:
            parser.error("Explicit Vivado and target clock required")
        capture(root=args.input.resolve(), output=args.output.resolve(), vivado=args.vivado.resolve(),
                target_clock=args.target_clock, score_timeout=args.score_timeout)


if __name__ == "__main__":
    main()
