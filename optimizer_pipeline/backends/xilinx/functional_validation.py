"""Shared prepared-workspace validation adapters. No research policy is selected here."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
from optimizer_pipeline.dof.seed_validation import (
    clone_prepared_workspace, semantic_seed_result, canonical_hash, file_sha256,
)
from .owned_process import run_owned


def prepare_workspace(*, golden: Path, revised: Path, output: Path,
                      python: Path, vivado: Path, seeds: list[int], vectors: int,
                      precheck_vectors: int, timeout_seconds: float):
    """Prepare once and retain the existing structural/precheck requirements."""
    repo = Path(__file__).resolve().parents[3]
    validator = repo / "validate_dcps.py"
    if not validator.is_file():
        raise RuntimeError("Functional validation requires the full editable tool checkout")
    output.mkdir(parents=True, exist_ok=False)
    before = [file_sha256(golden), file_sha256(revised)]
    command = [str(python), str(validator), str(golden), str(revised),
               "--vectors", str(vectors), "--precheck-vectors", str(precheck_vectors),
               "--prepare-seed-workspace-only", "--workspace-dir", str(output.resolve())]
    for seed in seeds:
        command.extend(["--seed", hex(seed)])
    env = dict(os.environ, VIVADO_EXEC=str(vivado))
    completed = run_owned(command, cwd=repo, env=env, timeout=timeout_seconds, text=True)
    log = completed.stdout + completed.stderr
    (output / "validator.log").write_text(log)
    if before != [file_sha256(golden), file_sha256(revised)]:
        raise RuntimeError("Validation preparation changed an input checkpoint")
    matches = re.findall(r"Working directory:\s*(\S*dcp_validation_[^\s]+)", log)
    report_path = Path(matches[-1]) / "validation_report.json" if matches else None
    if report_path and not report_path.resolve().is_relative_to(output.resolve()):
        raise RuntimeError("validation_workspace_outside_requested_output")
    if not report_path or not report_path.is_file():
        raise RuntimeError("seed_workspace_preparation_failed")
    report = json.loads(report_path.read_text())
    simulation = report.get("simulation_report") or {}
    if not report.get("phase1_passed") or not report.get("seed_workspace_prepared") or not simulation.get("precheck_passed"):
        # A precheck mismatch is a scientific failure, not an infrastructure
        # success; the caller records the report rather than inventing seeds.
        return {"report": report, "workspace": None}
    if completed.returncode != 0:
        raise RuntimeError("seed_workspace_preparation_process_failed")
    workspace = Path(simulation["prepared_seed_workspace"])
    if not workspace.resolve().is_relative_to(output.resolve()):
        raise RuntimeError("prepared_seed_workspace_outside_requested_output")
    if not workspace.is_dir():
        raise RuntimeError("prepared_seed_workspace_missing")
    (output / "prepared-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return {"report": report, "workspace": workspace}

def run_prepared_seed(
    *, prepared_workspace: Path, output: Path, stimulus_seed: int,
    vectors: int = 200, timeout_seconds: int = 3600, vivado: Path | None = None,
) -> dict[str, Any]:
    from validate_dcps import parse_simulation_output
    started = time.time()
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / "workspace"
    clone_method = clone_prepared_workspace(prepared_workspace, workspace)
    log_path = output / "xsim.log"
    time_path = output / "gnu-time.txt"
    vivado_exec = str(vivado) if vivado is not None else os.environ.get("VIVADO_EXEC")
    xsim = Path(vivado_exec).parent / "xsim" if vivado_exec else Path("xsim")
    if vivado is not None and (not xsim.is_file() or not os.access(xsim, os.X_OK)):
        raise RuntimeError("approved_xsim_executable_unavailable")
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
        result = run_owned(
            command, cwd=workspace, text=True,
            timeout=timeout_seconds, env=os.environ.copy(),
        )
        returncode = result.returncode
        text = result.stdout + result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        chunks = [exc.stdout or b"", exc.stderr or b""]
        text = "".join(c.decode(errors="replace") if isinstance(c, bytes) else c for c in chunks)
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
