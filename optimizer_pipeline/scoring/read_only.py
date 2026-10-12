"""Shared read-only timing scorer, retaining the historical E1 measurement schema."""
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from optimizer_pipeline.dof.artifacts import fingerprint_file
from optimizer_pipeline.dof.e1 import score_timing
from optimizer_pipeline.dof.e1_audit import _marker, _section, _hash, E1_READONLY_SCORE_SCHEMA
from optimizer_pipeline.dof.preflight import _parse_route, _parse_timing
from optimizer_pipeline.backends.xilinx.owned_process import run_owned

def run_readonly_score(
    dcp_path: str | Path,
    *,
    vivado_exec: str | Path,
    scorer_script: str | Path,
    run_dir: str | Path,
    frozen_target_clock: str | None = None,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    dcp = Path(dcp_path).resolve()
    script = Path(scorer_script).resolve()
    output = Path(run_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = fingerprint_file(dcp)
    argv = [
        str(Path(vivado_exec).resolve()),
        "-mode",
        "batch",
        "-nolog",
        "-nojournal",
        "-notrace",
        "-source",
        str(script),
        "-tclargs",
        str(dcp),
    ]
    if frozen_target_clock is not None:
        argv.append(frozen_target_clock)
    start = time.monotonic()
    completed = run_owned(
        argv,
        cwd=output,
        timeout=timeout_seconds,
    )
    wall = time.monotonic() - start
    after = fingerprint_file(dcp)
    if before != after:
        raise RuntimeError("e1_root_modified_by_readonly_scorer")
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    (output / "vivado.stdout").write_bytes(completed.stdout)
    (output / "vivado.stderr").write_bytes(completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"e1_readonly_scorer_failed:{completed.returncode}")
    target_clock = _marker(stdout, "DOF_E1_TARGET_CLOCK")
    target_rule = _marker(stdout, "DOF_E1_TARGET_RULE")
    period = float(_marker(stdout, "DOF_E1_TARGET_PERIOD_NS"))
    target_wns = float(_marker(stdout, "DOF_E1_TARGET_WNS_NS"))
    timing_text = _section(stdout, "DOF_E1_TIMING_SUMMARY")
    route_text = _section(stdout, "DOF_E1_ROUTE_STATUS")
    timing = _parse_timing(timing_text)
    route = _parse_route(route_text)
    if timing["tns_ns"] is None or route["is_fully_routed"] is not True:
        raise RuntimeError("e1_readonly_score_reports_invalid")
    score = score_timing(
        target_clock=target_clock,
        target_period_ns=period,
        wns_ns=target_wns,
        tns_ns=timing["tns_ns"],
    )
    payload = {
        "schema_version": E1_READONLY_SCORE_SCHEMA,
        "artifact": {"sha256": before.sha256, "size_bytes": before.size_bytes},
        "target_clock_selection_rule": target_rule,
        "score": score,
        "route": route,
        "failing_endpoints": timing["failing_endpoints"],
        "wall_seconds": wall,
        "process": {
            "returncode": completed.returncode,
            "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
        },
    }
    result = payload | {"result_sha256": _hash(payload)}
    (output / "score.json").write_bytes(json.dumps(result, sort_keys=True, indent=2).encode() + b"\n")
    return result
