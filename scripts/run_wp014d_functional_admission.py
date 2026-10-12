#!/usr/bin/env python3
"""Bounded functional-validation cost admission over frozen WP-014C children."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
WORKDIR_RE = re.compile(r"Working directory:\s*(\S*dcp_validation_[^\s]+)")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        result[key] = int(value.strip().split()[0]) * 1024
    return result


def process_group_rows(pgid: int) -> list[dict[str, int]]:
    rows = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if os.getpgid(pid) != pgid:
                continue
            status: dict[str, str] = {}
            for line in (entry / "status").read_text().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
            stat = (entry / "stat").read_text().split()
            rows.append({
                "pid": pid,
                "rss_bytes": int(status.get("VmRSS", "0 kB").split()[0]) * 1024,
                "threads": int(status.get("Threads", "0")),
                "cpu_ticks": int(stat[13]) + int(stat[14]),
            })
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError):
            continue
    return rows


def terminate_group(pgid: int, grace_seconds: float) -> dict[str, Any]:
    events = []
    try:
        os.killpg(pgid, signal.SIGTERM)
        events.append("SIGTERM")
    except ProcessLookupError:
        return {"events": events, "survivors_after": []}
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and process_group_rows(pgid):
        time.sleep(0.5)
    if process_group_rows(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
            events.append("SIGKILL")
        except ProcessLookupError:
            pass
    time.sleep(1)
    first = [row["pid"] for row in process_group_rows(pgid)]
    time.sleep(1)
    second = [row["pid"] for row in process_group_rows(pgid)]
    return {"events": events, "survivors_after_first_audit": first, "survivors_after": second}


def parse_gnu_time(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not path.is_file():
        return result
    mapping = {
        "User time (seconds)": ("user_cpu_seconds", float),
        "System time (seconds)": ("system_cpu_seconds", float),
        "Percent of CPU this job got": ("cpu_percent_reported", str),
        "Elapsed (wall clock) time": ("elapsed_reported", str),
        "Maximum resident set size (kbytes)": ("maximum_single_process_rss_bytes", lambda value: int(value) * 1024),
        "File system inputs": ("filesystem_inputs", int),
        "File system outputs": ("filesystem_outputs", int),
        "Major (requiring I/O) page faults": ("major_page_faults", int),
        "Minor (reclaiming a frame) page faults": ("minor_page_faults", int),
    }
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        for prefix, (key, converter) in mapping.items():
            marker = prefix + ": "
            if stripped.startswith(marker):
                try:
                    result[key] = converter(stripped[len(marker):])
                except ValueError:
                    result[key] = stripped[len(marker):]
    return result


def directory_bytes(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def classify(report: Mapping[str, Any] | None, returncode: int | None, timed_out: bool) -> str:
    if timed_out:
        return "TIMEOUT"
    if report is None:
        return "INFRASTRUCTURE_FAILURE"
    if report.get("phase2_skipped"):
        return "VALIDATOR_UNSUPPORTED_SKIPPED"
    simulation = report.get("simulation_report") or {}
    if report.get("infrastructure_failure"):
        return "INFRASTRUCTURE_FAILURE"
    if not report.get("phase1_passed"):
        return "STRUCTURAL_GATE_REJECTION"
    mismatch = int(simulation.get("mismatch_count") or 0)
    protocol = int(simulation.get("protocol_mismatch_count") or 0)
    if report.get("phase2_passed") and simulation.get("cycles_simulated") == report.get("num_vectors") and mismatch == 0 and protocol == 0:
        return "FUNCTIONAL_PASS"
    if mismatch > 0 or protocol > 0 or simulation.get("result_fail_seen"):
        return "FUNCTIONAL_MISMATCH"
    if returncode not in (0, None):
        return "INFRASTRUCTURE_FAILURE"
    return "FUNCTIONAL_INCOMPLETE"


def git_snapshot() -> dict[str, Any]:
    def command(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()
    return {
        "branch": command("branch", "--show-current"),
        "head": command("rev-parse", "HEAD"),
        "tree": command("rev-parse", "HEAD^{tree}"),
        "tracked_and_untracked_clean": not command("status", "--porcelain"),
    }


def run_case(case: Mapping[str, Any], plan: Mapping[str, Any], output: Path) -> dict[str, Any]:
    case_dir = output / "cases" / str(case["case_id"])
    case_dir.mkdir(parents=True, exist_ok=False)
    log_path, time_path = case_dir / "validator.log", case_dir / "gnu-time.txt"
    golden, revised = Path(str(case["golden_dcp"])), Path(str(case["revised_dcp"]))
    before = {"golden": file_digest(golden), "revised": file_digest(revised)}
    if before != {"golden": case["golden_sha256"], "revised": case["revised_sha256"]}:
        raise ValueError(f"input_hash_mismatch:{case['case_id']}")
    cfg = plan["validator"]
    timeout_seconds = int(cfg["large_timeout_seconds"] if case["timeout_class"] == "large" else cfg["ordinary_timeout_seconds"])
    command = [
        "/usr/bin/time", "-v", "-o", str(time_path), "--",
        sys.executable, str(REPO / "validate_dcps.py"), str(golden), str(revised),
        "--vectors", str(cfg["vectors"]), "--precheck-vectors", str(cfg["precheck_vectors"]),
    ]
    memory_before, started = meminfo(), time.time()
    peak_rss, peak_processes, peak_threads, max_cpu_ticks = 0, 0, 0, 0
    timed_out = False
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
            text=True, start_new_session=True, env=os.environ.copy(),
        )
        pgid = process.pid
        while process.poll() is None:
            rows = process_group_rows(pgid)
            peak_rss = max(peak_rss, sum(row["rss_bytes"] for row in rows))
            peak_processes = max(peak_processes, len(rows))
            peak_threads = max(peak_threads, sum(row["threads"] for row in rows))
            max_cpu_ticks = max(max_cpu_ticks, sum(row["cpu_ticks"] for row in rows))
            if time.time() - started >= timeout_seconds:
                timed_out = True
                break
            time.sleep(float(cfg["sample_interval_seconds"]))
        cleanup = terminate_group(pgid, float(cfg["term_grace_seconds"])) if timed_out else {"events": [], "survivors_after": []}
        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cleanup = terminate_group(pgid, 0)
            returncode = process.wait(timeout=5)
        if not timed_out:
            time.sleep(1)
            first = [row["pid"] for row in process_group_rows(pgid)]
            if first:
                cleanup = terminate_group(pgid, float(cfg["term_grace_seconds"]))
                cleanup["unexpected_survivors_after_parent_exit"] = first
            else:
                time.sleep(1)
                cleanup = {
                    "events": [],
                    "survivors_after_first_audit": [],
                    "survivors_after": [row["pid"] for row in process_group_rows(pgid)],
                }
    ended, memory_after = time.time(), meminfo()
    text = log_path.read_text(errors="replace")
    matches = WORKDIR_RE.findall(text)
    validation_dir = Path(matches[-1]) if matches else None
    report_path = validation_dir / "validation_report.json" if validation_dir else None
    report = read(report_path) if report_path and report_path.is_file() else None
    result_core = {
        "schema_version": "dof.wp014d-functional-case.v1",
        "case": dict(case),
        "command": {"python": sys.executable, "vectors": cfg["vectors"], "precheck_vectors": cfg["precheck_vectors"], "timeout_seconds": timeout_seconds},
        "started_epoch_seconds": started, "ended_epoch_seconds": ended, "wall_seconds": ended - started,
        "returncode": returncode, "timed_out": timed_out,
        "strict_classification": classify(report, returncode, timed_out),
        "validator_report": report,
        "validator_directory": str(validation_dir) if validation_dir else None,
        "validator_artifact_bytes": directory_bytes(validation_dir),
        "resource": parse_gnu_time(time_path) | {
            "sampled_peak_process_group_rss_bytes": peak_rss,
            "sampled_peak_process_count": peak_processes,
            "sampled_peak_thread_count": peak_threads,
            "sampled_max_live_cpu_ticks": max_cpu_ticks,
            "memory_available_before_bytes": memory_before.get("MemAvailable"),
            "memory_available_after_bytes": memory_after.get("MemAvailable"),
            "swap_free_before_bytes": memory_before.get("SwapFree"),
            "swap_free_after_bytes": memory_after.get("SwapFree"),
            "swap_growth_bytes": max(0, int(memory_before.get("SwapFree", 0)) - int(memory_after.get("SwapFree", 0))),
        },
        "cleanup": cleanup,
        "input_sha256_before": before,
        "input_sha256_after": {"golden": file_digest(golden), "revised": file_digest(revised)},
        "log_sha256": file_digest(log_path),
    }
    result = result_core | {"result_sha256": digest(result_core)}
    (case_dir / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return result


def resource_stop(result: Mapping[str, Any], plan: Mapping[str, Any]) -> str | None:
    cfg, resource = plan["validator"], result["resource"]
    if int(resource.get("sampled_peak_process_group_rss_bytes") or 0) > int(cfg["single_case_peak_rss_stop_bytes"]):
        return "single_case_peak_rss_stop"
    if int(resource.get("swap_growth_bytes") or 0) > int(cfg["single_case_swap_growth_stop_bytes"]):
        return "single_case_swap_growth_stop"
    if int(result.get("validator_artifact_bytes") or 0) > int(cfg["artifact_bytes_stop"]):
        return "single_case_artifact_stop"
    if result["input_sha256_before"] != result["input_sha256_after"]:
        return "input_hash_changed"
    if result.get("cleanup", {}).get("survivors_after"):
        return "owned_process_survivor"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True)
    plan = read(args.plan)
    cases = {case["case_id"]: case for case in plan["cases"]}
    if set(cases) != {case for wave in plan["waves"] for case in wave["case_ids"]}:
        raise ValueError("wave_case_matrix_mismatch")
    if max(int(wave["max_workers"]) for wave in plan["waves"]) > int(plan["boundaries"]["maximum_parallel_validators"]):
        raise ValueError("parallelism_cap_exceeded")
    repo_before = git_snapshot()
    if not repo_before["tracked_and_untracked_clean"]:
        raise RuntimeError("repository_not_clean")
    vivado = os.environ.get("VIVADO_EXEC")
    rapidwright = os.environ.get("RAPIDWRIGHT_PATH")
    if not vivado or not Path(vivado).is_file() or not os.access(vivado, os.X_OK):
        raise RuntimeError("VIVADO_EXEC_not_executable")
    if not rapidwright or not Path(rapidwright).is_dir():
        raise RuntimeError("RAPIDWRIGHT_PATH_not_directory")
    vivado_version = subprocess.check_output([vivado, "-version"], text=True, stderr=subprocess.STDOUT).splitlines()[:4]
    environment = {
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "vivado_version_lines": vivado_version,
        "rapidwright_path_present": True,
    }
    results: list[dict[str, Any]] = []
    stopped_reason = None
    run_started = time.time()
    for wave in plan["waves"]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(wave["max_workers"])) as pool:
            futures = [pool.submit(run_case, cases[case_id], plan, args.output) for case_id in wave["case_ids"]]
            wave_results = [future.result() for future in futures]
        results.extend(wave_results)
        if (
            plan.get("boundaries", {}).get("self_control_required", True)
            and int(wave["wave"]) == 0
            and wave_results[0]["strict_classification"] != "FUNCTIONAL_PASS"
        ):
            stopped_reason = "self_control_not_strict_functional_pass"
        for result in wave_results:
            stopped_reason = stopped_reason or resource_stop(result, plan)
        if stopped_reason:
            break
    repo_after = git_snapshot()
    counts = collections.Counter(result["strict_classification"] for result in results)
    core = {
        "schema_version": "dof.wp014d-functional-admission-aggregate.v1",
        "plan_sha256": file_digest(args.plan),
        "started_epoch_seconds": run_started, "ended_epoch_seconds": time.time(),
        "planned_cases": len(cases), "executed_cases": len(results),
        "stopped_reason": stopped_reason,
        "classification_counts": dict(sorted(counts.items())),
        "language_counts": dict(sorted(collections.Counter(result["case"]["language"] for result in results).items())),
        "total_case_wall_seconds": sum(float(result["wall_seconds"]) for result in results),
        "aggregate_validator_artifact_bytes": sum(int(result.get("validator_artifact_bytes") or 0) for result in results),
        "maximum_sampled_peak_process_group_rss_bytes": max((int(result["resource"].get("sampled_peak_process_group_rss_bytes") or 0) for result in results), default=0),
        "results": results,
        "repository_before": repo_before, "repository_after": repo_after,
        "environment": environment,
        "all_inputs_unchanged": all(result["input_sha256_before"] == result["input_sha256_after"] for result in results),
        "owned_process_survivors": [result["case"]["case_id"] for result in results if result.get("cleanup", {}).get("survivors_after")],
        "openrouter_calls": 0, "new_scientific_jobs": 0, "sealed_holdout_accesses": 0,
    }
    value = core | {"aggregate_sha256": digest(core)}
    (args.output / "aggregate.json").write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({key: value[key] for key in ("planned_cases", "executed_cases", "stopped_reason", "classification_counts", "aggregate_sha256")}, sort_keys=True))
    return 0 if stopped_reason is None and len(results) == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
