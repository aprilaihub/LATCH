"""Prepared-workspace, isolated-seed XSim execution for WP-015 V1."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Mapping

from validate_dcps import parse_simulation_output


SEED_RUNNER_VERSION = "dof.wp015-seed-runner.v1"


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_manifest(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rows.append({
                "path": str(path.relative_to(root)), "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            })
        elif path.is_symlink():
            rows.append({"path": str(path.relative_to(root)), "symlink": os.readlink(path)})
    return rows


def clone_prepared_workspace(source: Path, destination: Path) -> str:
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    result = subprocess.run(
        ["cp", "-a", "--reflink=auto", str(source) + "/.", str(destination)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("prepared_workspace_clone_failed:" + result.stderr[-500:])
    return "cp_-a_--reflink=auto"


def semantic_seed_result(report: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "vectors_requested", "stimulus_seed", "stimulus_seed_hex",
        "cycles_simulated", "mismatch_count", "protocol_mismatch_count",
        "result_pass_seen", "result_fail_seen", "simulator_failed",
        "infrastructure_failure", "infrastructure_reason", "returncode", "passed",
    )
    return {key: report.get(key) for key in keys}


def run_prepared_seed(
    *, prepared_workspace: Path, output: Path, stimulus_seed: int,
    vectors: int = 200, timeout_seconds: int = 3600,
) -> dict[str, Any]:
    started = time.time()
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / "workspace"
    clone_method = clone_prepared_workspace(prepared_workspace, workspace)
    log_path = output / "xsim.log"
    time_path = output / "gnu-time.txt"
    vivado_exec = os.environ.get("VIVADO_EXEC")
    xsim = Path(vivado_exec).parent / "xsim" if vivado_exec else Path("xsim")
    if not xsim.is_file() or not os.access(xsim, os.X_OK):
        resolved = shutil.which("xsim")
        if not resolved:
            raise RuntimeError("xsim_executable_unavailable")
        xsim = Path(resolved)
    command = [
        "/usr/bin/time", "-v", "-o", str(time_path), "--",
        str(xsim), "testbench_sim", "-R",
        "--testplusarg", f"NUM_VECTORS={vectors}",
        "--testplusarg", f"TEST_SEED={stimulus_seed:08X}",
    ]
    timed_out = False
    try:
        result = subprocess.run(
            command, cwd=workspace, capture_output=True, text=True,
            timeout=timeout_seconds, env=os.environ.copy(),
        )
        returncode = result.returncode
        text = result.stdout + result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        text = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
    log_path.write_text(text, encoding="utf-8", errors="replace")
    parsed = parse_simulation_output(text, returncode, vectors)
    if timed_out:
        parsed.update({
            "passed": False, "simulator_failed": True,
            "infrastructure_failure": True, "infrastructure_reason": "xsim_seed_timeout",
        })
    resource: dict[str, Any] = {}
    if time_path.is_file():
        for line in time_path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("Maximum resident set size (kbytes): "):
                resource["max_rss_bytes"] = int(stripped.rsplit(": ", 1)[1]) * 1024
            elif stripped.startswith("User time (seconds): "):
                resource["user_seconds"] = float(stripped.rsplit(": ", 1)[1])
            elif stripped.startswith("System time (seconds): "):
                resource["system_seconds"] = float(stripped.rsplit(": ", 1)[1])
    report = {
        "vectors_requested": vectors, "stimulus_seed": stimulus_seed,
        "stimulus_seed_hex": f"0x{stimulus_seed:08X}",
        "cycles_simulated": parsed["cycles_simulated"],
        "mismatch_count": parsed["mismatch_count"],
        "protocol_mismatch_count": parsed["protocol_mismatch_count"],
        "log_file": str(log_path), "log_sha256": file_sha256(log_path),
        "result_pass_seen": parsed["result_pass_seen"],
        "result_fail_seen": parsed["result_fail_seen"],
        "simulator_failed": parsed["simulator_failed"],
        "infrastructure_failure": parsed["infrastructure_failure"],
        "infrastructure_reason": parsed["infrastructure_reason"],
        "returncode": parsed["returncode"], "passed": parsed["passed"],
        "timed_out": timed_out, "wall_seconds": time.time() - started,
        "workspace": str(workspace), "clone_method": clone_method,
        "resource": resource,
    }
    semantic = semantic_seed_result(report)
    report["semantic_result_sha256"] = canonical_hash(semantic)
    (output / "seed-result.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    # Compiled assets remain canonical in the prepared case workspace.  Seed
    # clones are transient isolation sandboxes; retain only logs, resource data
    # and the hash-bound result to avoid multiplying XSim artefacts by 30 in the
    # serial/parallel canary (and by every future scientific child).
    shutil.rmtree(workspace)
    return report


def aggregate_seed_reports(
    *, prepared_report: Mapping[str, Any], seed_reports: list[Mapping[str, Any]],
    expected_seeds: list[int],
) -> dict[str, Any]:
    ordered = sorted(seed_reports, key=lambda row: expected_seeds.index(int(row["stimulus_seed"])))
    exactly_once = [int(row["stimulus_seed"]) for row in ordered] == expected_seeds
    all_passed = exactly_once and all(bool(row["passed"]) for row in ordered)
    simulation = dict(prepared_report["simulation_report"])
    simulation.update({
        "full_run_skipped": False, "full_run_skip_reason": None,
        "stimulus_seeds": expected_seeds, "seed_reports": ordered,
        "seed_reports_complete": exactly_once,
        "total_full_cycles_simulated": sum(int(row["cycles_simulated"]) for row in ordered),
        "cycles_simulated": int(simulation["full_vectors"]) if all_passed else min(
            (int(row["cycles_simulated"]) for row in ordered if not row["passed"]), default=0
        ),
        "mismatch_count": sum(int(row["mismatch_count"]) for row in ordered),
        "protocol_mismatch_count": sum(int(row["protocol_mismatch_count"]) for row in ordered),
        "result_pass_seen": all_passed,
        "result_fail_seen": any(bool(row["result_fail_seen"]) for row in ordered),
        "simulator_failed": any(bool(row["simulator_failed"]) for row in ordered),
        "infrastructure_failure": any(bool(row["infrastructure_failure"]) for row in ordered),
        "infrastructure_reason": next((row["infrastructure_reason"] for row in ordered if row["infrastructure_failure"]), None),
    })
    terminal = (
        "MULTISEED_FUNCTIONAL_PASS" if all_passed else
        "VALIDATOR_INFRASTRUCTURE_FAILURE" if simulation["infrastructure_failure"] else
        "FUNCTIONAL_MISMATCH" if simulation["mismatch_count"] or simulation["protocol_mismatch_count"] else
        "FUNCTIONAL_INCOMPLETE"
    )
    semantic_rows = [semantic_seed_result(row) for row in ordered]
    return {
        "schema_version": "dof.wp015-seed-validation-aggregate.v1",
        "terminal": terminal, "expected_seeds": expected_seeds,
        "all_seeds_present_exactly_once": exactly_once,
        "precheck_report": simulation.get("precheck_report"),
        "simulation_report": simulation,
        "semantic_results": semantic_rows,
        "semantic_result_sha256": canonical_hash(semantic_rows),
    }
