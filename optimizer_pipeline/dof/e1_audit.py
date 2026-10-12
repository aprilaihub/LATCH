"""Read-only root scoring and freeze helpers for Protocol E1-v1."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Mapping

from .artifacts import fingerprint_file
from .e1 import (
    E1_BENCHMARKS,
    e1_protocol_manifest,
    root_reference_check,
    score_timing,
)
from .preflight import _parse_route, _parse_timing


E1_READONLY_SCORE_SCHEMA = "dof.e1-readonly-score.v1"
E1_ROOT_AUDIT_SCHEMA = "dof.e1-root-scorer-audit.v1"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        value if isinstance(value, bytes) else _canonical_bytes(value)
    ).hexdigest()


def _marker(text: str, name: str) -> str:
    prefix = name + ":"
    rows = [line[len(prefix) :].strip() for line in text.splitlines() if line.startswith(prefix)]
    if len(rows) != 1 or not rows[0]:
        raise RuntimeError("e1_scorer_marker_invalid:" + name)
    return rows[0]


def _section(text: str, name: str) -> str:
    begin = name + "_BEGIN"
    end = name + "_END"
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == begin]
    stops = [index for index, line in enumerate(lines) if line.strip() == end]
    if len(starts) != 1 or len(stops) != 1 or stops[0] <= starts[0]:
        raise RuntimeError("e1_scorer_section_invalid:" + name)
    return "\n".join(lines[starts[0] + 1 : stops[0]])


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
    completed = subprocess.run(
        argv,
        cwd=output,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
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


def audit_e1_roots(
    roots: Mapping[str, str | Path],
    *,
    vivado_exec: str | Path,
    scorer_script: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    if tuple(roots) != E1_BENCHMARKS:
        raise ValueError("e1_root_order_or_set_mismatch")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    observations: dict[str, Any] = {}
    for benchmark in E1_BENCHMARKS:
        observed = run_readonly_score(
            roots[benchmark],
            vivado_exec=vivado_exec,
            scorer_script=scorer_script,
            run_dir=output / benchmark,
        )
        observations[benchmark] = observed
    result = assemble_e1_root_audit(observations)
    (output / "root-scorer-audit.json").write_bytes(
        json.dumps(result, sort_keys=True, indent=2).encode() + b"\n"
    )
    return result


def assemble_e1_root_audit(
    observations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Assemble aggregate audit evidence from immutable per-root score rows."""

    if tuple(observations) != E1_BENCHMARKS:
        raise ValueError("e1_score_observation_order_or_set_mismatch")
    rows = []
    for benchmark in E1_BENCHMARKS:
        observed = observations[benchmark]
        reference = root_reference_check(benchmark, observed["score"])
        rows.append(
            {
                "benchmark": benchmark,
                "artifact": observed["artifact"],
                "target_clock_selection_rule": observed["target_clock_selection_rule"],
                "score": observed["score"],
                "route": observed["route"],
                "reference_check": reference,
                "wall_seconds": observed["wall_seconds"],
            }
        )
    payload = {
        "schema_version": E1_ROOT_AUDIT_SCHEMA,
        "protocol_bundle_sha256": e1_protocol_manifest()["bundle_sha256"],
        "root_count": len(rows),
        "all_accepted": all(row["reference_check"]["accepted"] for row in rows),
        "rows": rows,
    }
    result = payload | {"audit_sha256": _hash(payload)}
    return result


__all__ = ["assemble_e1_root_audit", "audit_e1_roots", "run_readonly_score"]
