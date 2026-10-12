#!/usr/bin/env python3
"""WP-014F bounded layered-validation reconnaissance over immutable DCP pairs."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
WORKDIR_RE = re.compile(r"Working directory:\s*(\S*dcp_validation_[^\s]+)")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def git_snapshot() -> dict[str, Any]:
    def command(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()
    return {
        "branch": command("branch", "--show-current"),
        "head": command("rev-parse", "HEAD"),
        "tree": command("rev-parse", "HEAD^{tree}"),
        "clean": not command("status", "--porcelain"),
    }


def resolve_cases(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for source, raw_path in plan["source_plans"].items():
        source_plan = read_json(Path(raw_path))
        sources[source] = {
            str(case.get("case_id") or case.get("job_id")): case
            for case in source_plan["cases"]
        }
    resolved = []
    for selection in plan["cases"]:
        if selection["source"] == "direct":
            case = dict(selection)
        else:
            source_case = sources[selection["source"]][selection["source_case_id"]]
            case = dict(source_case)
            case.update(selection)
        case.pop("source_case_id", None)
        for required in ("case_id", "golden_dcp", "revised_dcp", "golden_sha256", "revised_sha256"):
            if not case.get(required):
                raise ValueError(f"missing_{required}:{selection['case_id']}")
        golden, revised = Path(case["golden_dcp"]), Path(case["revised_dcp"])
        if not golden.is_file() or not revised.is_file():
            raise FileNotFoundError(f"frozen_input_missing:{case['case_id']}")
        observed = {"golden": file_sha256(golden), "revised": file_sha256(revised)}
        expected = {"golden": case["golden_sha256"], "revised": case["revised_sha256"]}
        if observed != expected:
            raise ValueError(f"frozen_input_hash_mismatch:{case['case_id']}")
        case["resolved_input_sha256"] = observed
        resolved.append(case)
    if len(resolved) != 16 or len({case["case_id"] for case in resolved}) != 16:
        raise ValueError("frozen_panel_must_have_16_unique_cases")
    return resolved


def process_group_pids(pgid: int) -> list[int]:
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.getpgid(int(entry.name)) == pgid:
                result.append(int(entry.name))
        except (ProcessLookupError, PermissionError):
            pass
    return result


def process_group_resources(pgid: int) -> dict[str, int]:
    """Return aggregate live RSS/swap for one owned process group."""
    rss_kib = 0
    swap_kib = 0
    pids = process_group_pids(pgid)
    for pid in pids:
        try:
            fields = {}
            for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key] = value.strip()
            rss_kib += int(fields.get("VmRSS", "0 kB").split()[0])
            swap_kib += int(fields.get("VmSwap", "0 kB").split()[0])
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue
    return {
        "processes": len(pids),
        "rss_bytes": rss_kib * 1024,
        "swap_bytes": swap_kib * 1024,
    }


def system_memory_resources(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    fields = {}
    for line in path.read_text().splitlines():
        key, value = line.split(":", 1)
        fields[key] = int(value.strip().split()[0]) * 1024
    swap_total = fields.get("SwapTotal", 0)
    swap_free = fields.get("SwapFree", 0)
    return {
        "memory_total_bytes": fields.get("MemTotal", 0),
        "memory_available_bytes": fields.get("MemAvailable", 0),
        "swap_total_bytes": swap_total,
        "swap_used_bytes": max(0, swap_total - swap_free),
    }


def terminate_group(pgid: int, grace: float = 20.0) -> dict[str, Any]:
    events = []
    try:
        os.killpg(pgid, signal.SIGTERM)
        events.append("SIGTERM")
    except ProcessLookupError:
        return {"events": events, "audit_1": [], "audit_2": []}
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and process_group_pids(pgid):
        time.sleep(0.5)
    if process_group_pids(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
            events.append("SIGKILL")
        except ProcessLookupError:
            pass
    time.sleep(1)
    audit_1 = process_group_pids(pgid)
    time.sleep(1)
    return {"events": events, "audit_1": audit_1, "audit_2": process_group_pids(pgid)}


def directory_bytes(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def parse_time_file(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not path.is_file():
        return result
    fields = {
        "User time (seconds)": ("user_seconds", float),
        "System time (seconds)": ("system_seconds", float),
        "Maximum resident set size (kbytes)": ("max_rss_bytes", lambda value: int(value) * 1024),
        "File system inputs": ("filesystem_inputs", int),
        "File system outputs": ("filesystem_outputs", int),
    }
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        for prefix, (key, converter) in fields.items():
            marker = prefix + ": "
            if stripped.startswith(marker):
                try:
                    result[key] = converter(stripped[len(marker):])
                except ValueError:
                    pass
    return result


def classify(
    report: Mapping[str, Any] | None,
    returncode: int,
    timed_out: bool,
    mode: str,
    log_text: str = "",
) -> str:
    prefix = "TIMING_" if mode == "timing" else ""
    if timed_out:
        return prefix + "VALIDATOR_TIMEOUT"
    if report is not None and report.get("validator_incompatibility"):
        return str(report["validator_incompatibility"])
    if report is None or report.get("infrastructure_failure"):
        return prefix + "VALIDATOR_INFRASTRUCTURE_FAILURE"
    if mode == "timing" and "Unable to annotate SDF delays in the design" in log_text:
        return "TIMING_SIM_UNSUPPORTED"
    if report.get("phase2_skipped"):
        return prefix + "VALIDATOR_UNSUPPORTED"
    simulation = report.get("simulation_report") or {}
    mismatch = int(simulation.get("mismatch_count") or 0)
    protocol = int(simulation.get("protocol_mismatch_count") or 0)
    if mismatch or protocol or simulation.get("result_fail_seen"):
        return "TIMING_BEHAVIOR_MISMATCH" if mode == "timing" else "FUNCTIONAL_MISMATCH"
    if report.get("phase2_passed") and report.get("overall_passed"):
        if mode == "multiseed":
            expected = report.get("stimulus_seeds") or []
            observed = simulation.get("seed_reports") or []
            if len(observed) == len(expected) and all(item.get("passed") for item in observed):
                return "MULTISEED_FUNCTIONAL_PASS"
            return "FUNCTIONAL_INCOMPLETE"
        if mode == "timing":
            return "TIMING_SIM_PASS"
        return "DIRECTED_FUNCTIONAL_PASS"
    if returncode not in (0, None) and not (mismatch or protocol or simulation.get("result_fail_seen")):
        return prefix + "VALIDATOR_INFRASTRUCTURE_FAILURE"
    return prefix + "FUNCTIONAL_INCOMPLETE"


def validator_command(case: Mapping[str, Any], plan: Mapping[str, Any], mode: str, smoke: bool, time_path: Path) -> list[str]:
    functional = plan["functional"]
    vectors = 20 if smoke else int(functional["vectors_per_seed"])
    precheck = 0 if smoke else int(functional["precheck_vectors"])
    command = [
        "/usr/bin/time", "-v", "-o", str(time_path), "--",
        sys.executable, str(REPO / "validate_dcps.py"),
        str(case["golden_dcp"]), str(case["revised_dcp"]),
        "--vectors", str(vectors), "--precheck-vectors", str(precheck),
    ]
    seeds = functional["seeds"] if mode == "multiseed" and not smoke else [functional["seeds"][0]]
    for seed in seeds:
        command.extend(["--seed", hex(int(seed))])
    if mode == "directed":
        command.extend(["--stimulus-profile", "directed"])
    if mode == "timing":
        command.extend(["--simulation-mode", "timing"])
    return command


def run_case(case: Mapping[str, Any], plan: Mapping[str, Any], output: Path, mode: str, smoke: bool = False) -> dict[str, Any]:
    case_dir = output / str(case["case_id"])
    case_dir.mkdir(parents=True, exist_ok=False)
    log_path, time_path = case_dir / "validator.log", case_dir / "gnu-time.txt"
    before = {
        "golden": file_sha256(Path(case["golden_dcp"])),
        "revised": file_sha256(Path(case["revised_dcp"])),
    }
    if before != case["resolved_input_sha256"]:
        raise ValueError(f"input_hash_changed_before_run:{case['case_id']}")
    command = validator_command(case, plan, mode, smoke, time_path)
    timeout = int(plan["boundaries"]["per_case_timeout_seconds"])
    started, timed_out = time.time(), False
    telemetry = {
        "sample_count": 0,
        "peak_process_group_rss_bytes": 0,
        "peak_process_group_swap_bytes": 0,
        "peak_process_group_processes": 0,
        "minimum_system_memory_available_bytes": None,
        "peak_system_swap_used_bytes": 0,
        "case_directory_bytes_before": directory_bytes(case_dir),
    }
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, env=os.environ.copy(),
        )
        pgid = process.pid
        while process.poll() is None:
            group = process_group_resources(pgid)
            system = system_memory_resources()
            telemetry["sample_count"] += 1
            telemetry["peak_process_group_rss_bytes"] = max(
                telemetry["peak_process_group_rss_bytes"], group["rss_bytes"]
            )
            telemetry["peak_process_group_swap_bytes"] = max(
                telemetry["peak_process_group_swap_bytes"], group["swap_bytes"]
            )
            telemetry["peak_process_group_processes"] = max(
                telemetry["peak_process_group_processes"], group["processes"]
            )
            current_available = system["memory_available_bytes"]
            prior_available = telemetry["minimum_system_memory_available_bytes"]
            telemetry["minimum_system_memory_available_bytes"] = (
                current_available if prior_available is None
                else min(prior_available, current_available)
            )
            telemetry["peak_system_swap_used_bytes"] = max(
                telemetry["peak_system_swap_used_bytes"], system["swap_used_bytes"]
            )
            if time.time() - started >= timeout:
                timed_out = True
                break
            time.sleep(2)
        cleanup = terminate_group(pgid) if timed_out else {"events": [], "audit_1": [], "audit_2": []}
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cleanup = terminate_group(pgid, 0)
            returncode = process.wait(timeout=10)
        if not timed_out:
            time.sleep(1)
            survivors = process_group_pids(pgid)
            cleanup = terminate_group(pgid) if survivors else cleanup
            time.sleep(1)
            cleanup["audit_2"] = process_group_pids(pgid)
    text = log_path.read_text(errors="replace")
    matches = WORKDIR_RE.findall(text)
    validation_dir = Path(matches[-1]) if matches else None
    report_path = validation_dir / "validation_report.json" if validation_dir else None
    report = read_json(report_path) if report_path and report_path.is_file() else None
    after = {
        "golden": file_sha256(Path(case["golden_dcp"])),
        "revised": file_sha256(Path(case["revised_dcp"])),
    }
    telemetry["case_directory_bytes_after"] = directory_bytes(case_dir)
    telemetry["case_directory_bytes_delta"] = (
        telemetry["case_directory_bytes_after"]
        - telemetry["case_directory_bytes_before"]
    )
    core = {
        "schema_version": "dof.wp014f-validation-case.v1",
        "case": dict(case), "mode": mode, "smoke": smoke,
        "started_epoch_seconds": started, "ended_epoch_seconds": time.time(),
        "wall_seconds": time.time() - started, "returncode": returncode,
        "timed_out": timed_out, "terminal": classify(report, returncode, timed_out, mode, text),
        "validator_report": report,
        "validator_directory": str(validation_dir) if validation_dir else None,
        "validator_artifact_bytes": directory_bytes(validation_dir),
        "resource": parse_time_file(time_path) | {"process_group_telemetry": telemetry},
        "cleanup": cleanup,
        "input_sha256_before": before, "input_sha256_after": after,
        "log_sha256": file_sha256(log_path),
        "provider_or_llm_calls": 0, "new_optimization_jobs": 0,
        "sealed_holdout_accesses": int(case.get("sealed_holdout_accesses", 0)),
    }
    result = core | {"result_sha256": object_sha256(core)}
    write_json(case_dir / "result.json", result)
    return result


def run_cases(cases: list[dict[str, Any]], plan: Mapping[str, Any], output: Path, mode: str, workers: int) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_case, case, plan, output, mode) for case in cases]
        results = [future.result() for future in futures]
    counts = collections.Counter(result["terminal"] for result in results)
    core = {
        "schema_version": "dof.wp014f-validation-aggregate.v1",
        "mode": mode, "planned_cases": len(cases), "executed_cases": len(results),
        "workers": workers, "started_epoch_seconds": started, "ended_epoch_seconds": time.time(),
        "queue_wall_seconds": time.time() - started,
        "summed_case_wall_seconds": sum(result["wall_seconds"] for result in results),
        "terminal_counts": dict(sorted(counts.items())), "results": results,
        "all_inputs_unchanged": all(result["input_sha256_before"] == result["input_sha256_after"] for result in results),
        "owned_process_survivors": [result["case"]["case_id"] for result in results if result["cleanup"].get("audit_2")],
        "provider_or_llm_calls": 0, "new_optimization_jobs": 0,
        "sealed_holdout_accesses": sum(int(result.get("sealed_holdout_accesses", 0)) for result in results),
    }
    aggregate = core | {"aggregate_sha256": object_sha256(core)}
    write_json(output / "aggregate.json", aggregate)
    return aggregate


def formal_discovery(plan: Mapping[str, Any]) -> dict[str, Any]:
    accepted = list(plan["formal"]["accepted_tools"])
    path_hits = {name: shutil.which(name) for name in accepted}
    module_text = ""
    try:
        module_text = subprocess.run(
            ["bash", "-lc", "module -t avail 2>&1"], capture_output=True,
            text=True, timeout=120, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    module_hits = sorted({
        line.strip() for line in module_text.splitlines()
        if any(token in line.lower() for token in ("questa", "onespin", "formal", "mentor", "siemens", "modelsim"))
    })
    fixed_hits = []
    for root in (Path("/opt"), Path("/tools"), Path("/usr/local"), Path("/cad"), Path("/eda")):
        if not root.is_dir():
            continue
        for name in accepted:
            try:
                command = ["find", str(root), "-maxdepth", "6", "-type", "f", "-name", name, "-perm", "-111", "-print"]
                value = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
                fixed_hits.extend(line for line in value.stdout.splitlines() if line)
            except subprocess.TimeoutExpired:
                pass
    discovered = sorted({path for path in path_hits.values() if path} | set(fixed_hits))
    terminal = "FORMAL_NOT_RUN_TOOL_UNAVAILABLE" if not discovered and not module_hits else "FORMAL_TOOL_DISCOVERED_ADAPTER_REQUIRED"
    core = {
        "schema_version": "dof.wp014f-formal-discovery.v1",
        "accepted_tools": accepted, "path_hits": path_hits,
        "module_hits": module_hits, "fixed_root_executable_hits": discovered,
        "license_probe_performed": False,
        "license_probe_reason": "no_accepted_executable" if not discovered else "adapter_required_before_license_probe",
        "terminal": terminal,
        "xilinx_lmutil_not_used_to_infer_third_party_license": True,
    }
    return core | {"discovery_sha256": object_sha256(core)}


def recommendations(aggregates: Mapping[str, Mapping[str, Any]], discovery: Mapping[str, Any]) -> dict[str, Any]:
    multi = aggregates.get("multiseed", {})
    terminals = multi.get("terminal_counts", {})
    completed = int(terminals.get("MULTISEED_FUNCTIONAL_PASS", 0)) + int(terminals.get("FUNCTIONAL_MISMATCH", 0))
    multi_gate = "GO_CANDIDATE" if completed >= 15 and not multi.get("owned_process_survivors") else "REFRAME"
    timing = aggregates.get("timing", {})
    timing_explainable = sum(int(count) for terminal, count in timing.get("terminal_counts", {}).items() if terminal not in {"TIMING_VALIDATOR_INFRASTRUCTURE_FAILURE", "TIMING_VALIDATOR_TIMEOUT"})
    timing_gate = "GO_SUPPORTED_SUBSET_CANDIDATE" if timing_explainable >= 5 else "REFRAME"
    formal_gate = "DO_NOT_INTEGRATE_TOOL_UNAVAILABLE" if discovery["terminal"] == "FORMAL_NOT_RUN_TOOL_UNAVAILABLE" else "NEEDS_ADAPTER_REVIEW"
    median_case = None
    walls = sorted(float(item["wall_seconds"]) for item in multi.get("results", []))
    if walls:
        median_case = statistics.median(walls)
    projections = {}
    if median_case is not None:
        for count in (500, 702, 1000):
            projections[str(count)] = {
                "summed_case_hours": median_case * count / 3600,
                "idealized_16_way_wall_hours": median_case * count / 16 / 3600,
            }
    return {
        "multiseed_v1": multi_gate, "timing_v2": timing_gate, "formal_v3": formal_gate,
        "wp015_eligibility_changed": False, "human_protocol_freeze_required": True,
        "projections": projections,
    }


def write_report(output: Path, plan: Mapping[str, Any], resolved: list[dict[str, Any]], aggregates: Mapping[str, Mapping[str, Any]], discovery: Mapping[str, Any]) -> dict[str, Any]:
    rec = recommendations(aggregates, discovery)
    summary_core = {
        "schema_version": "dof.wp014f-summary.v1",
        "wp014e_bundle_sha256": plan["wp014e_bundle"]["sha256"],
        "cases": len(resolved), "designs": len({case.get("root_id") for case in resolved}),
        "mechanisms": sorted({case["mechanism"] for case in resolved}),
        "aggregates": {name: {
            "planned_cases": value.get("planned_cases"),
            "executed_cases": value.get("executed_cases"),
            "terminal_counts": value.get("terminal_counts"),
            "queue_wall_seconds": value.get("queue_wall_seconds"),
            "summed_case_wall_seconds": value.get("summed_case_wall_seconds"),
            "aggregate_sha256": value.get("aggregate_sha256"),
        } for name, value in aggregates.items()},
        "formal": discovery, "recommendations": rec,
        "provider_or_llm_calls": 0, "new_optimization_jobs": 0,
        "sealed_holdout_accesses": 0, "wp015_started": False,
        "status": "NEEDS_PRO_EVIDENCE_REVIEW", "next_actor": "GPT_PRO",
    }
    summary = summary_core | {"summary_sha256": object_sha256(summary_core)}
    write_json(output / "wp014f-summary.json", summary)
    lines = [
        "# WP-014F Layered Validation Feasibility Reconnaissance", "",
        "## Executive result", "",
        "- This work used only existing development root/child DCPs; no LLM/provider calls or optimization jobs were made.",
        f"- Frozen panel: {len(resolved)} pairs across {summary['designs']} designs.",
        f"- Multi-seed recommendation: `{rec['multiseed_v1']}`.",
        f"- Timing-simulation recommendation: `{rec['timing_v2']}`.",
        f"- Formal-equivalence recommendation: `{rec['formal_v3']}`.",
        "- These validation results do not change the WP-015 eligibility rule.", "",
        "## Observed terminals", "",
    ]
    for name, aggregate in aggregates.items():
        lines.append(f"- `{name}`: `{json.dumps(aggregate.get('terminal_counts', {}), sort_keys=True)}`; queue wall {aggregate.get('queue_wall_seconds', 0):.1f}s.")
    lines.extend([
        "", "## Methodological interpretation", "",
        "- A sampled simulation mismatch is a child-level scientific result, not validator infrastructure failure.",
        "- Timing simulation is additional delay-aware evidence and does not replace STA.",
        "- Formal unsupported/inconclusive is not functional invalidity; only a reproducible counterexample may be called FORMAL_FAIL.",
        "- Directed mode is a deterministic boundary-pattern profile layered on the existing audited reactive interface logic; it is not a formal or exhaustive design-aware proof.",
        "", "## Protocol firewall", "",
        "- Provider/LLM calls: 0", "- New optimization jobs: 0", "- Sealed holdout accesses: 0", "- WP-015 started: no", "",
        "## Interpretation boundary", "", "Reconnaissance does not change the historical WP-015 validation protocol.",
    ])
    (output / "WP-014F-report.md").write_text("\n".join(lines) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=16)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if not 1 <= args.jobs <= 16:
        raise ValueError("jobs_must_be_between_1_and_16")
    plan = read_json(args.plan)
    if args.jobs != int(plan["boundaries"]["maximum_parallel_vivado_cases"]):
        raise ValueError("wp014f_requires_frozen_16_way_case_parallelism")
    if file_sha256(Path(plan["wp014e_bundle"]["path"])) != plan["wp014e_bundle"]["sha256"]:
        raise ValueError("wp014e_bundle_hash_mismatch")
    snapshot = git_snapshot()
    if not snapshot["clean"]:
        raise RuntimeError("repository_not_clean")
    vivado, rapidwright = os.environ.get("VIVADO_EXEC"), os.environ.get("RAPIDWRIGHT_PATH")
    if not vivado or not Path(vivado).is_file() or not os.access(vivado, os.X_OK):
        raise RuntimeError("VIVADO_EXEC_not_executable")
    if not rapidwright or not Path(rapidwright).is_dir():
        raise RuntimeError("RAPIDWRIGHT_PATH_not_directory")

    resolved = resolve_cases(plan)
    args.output.mkdir(parents=True)
    write_json(args.output / "resolved-plan.json", {
        "schema_version": "dof.wp014f-resolved-plan.v1", "source_plan_sha256": file_sha256(args.plan),
        "repository": snapshot, "cases": resolved,
    })
    discovery = formal_discovery(plan)
    write_json(args.output / "formal-discovery.json", discovery)

    control = next(case for case in resolved if case["case_id"] == "control__amd_self")
    smoke_dir = args.output / "smoke"
    smoke_dir.mkdir()
    smoke = run_case(control, plan, smoke_dir, "multiseed", smoke=True)
    write_json(smoke_dir / "aggregate.json", {"result": smoke, "terminal": smoke["terminal"]})
    if smoke["terminal"] != "MULTISEED_FUNCTIONAL_PASS":
        summary = write_report(args.output, plan, resolved, {}, discovery)
        summary["stopped_reason"] = "self_control_smoke_failed"
        write_json(args.output / "wp014f-summary.json", summary)
        return 2

    aggregates: dict[str, dict[str, Any]] = {}
    aggregates["multiseed"] = run_cases(resolved, plan, args.output / "multiseed", "multiseed", args.jobs)
    directed_ids = set(plan["directed_case_ids"])
    directed = [case for case in resolved if case["case_id"] in directed_ids]
    aggregates["directed"] = run_cases(directed, plan, args.output / "directed", "directed", min(args.jobs, len(directed)))

    timing_ids = set(plan["timing_case_ids"])
    timing = [case for case in resolved if case["case_id"] in timing_ids]
    timing_control = next(case for case in timing if case["case_id"] == "control__amd_self")
    timing_root = args.output / "timing"
    timing_root.mkdir()
    control_aggregate = run_cases([timing_control], plan, timing_root / "control", "timing", 1)
    if control_aggregate["terminal_counts"].get("TIMING_SIM_PASS") == 1:
        remaining = [case for case in timing if case is not timing_control]
        rest = run_cases(remaining, plan, timing_root / "remaining", "timing", min(args.jobs, len(remaining)))
        merged_results = control_aggregate["results"] + rest["results"]
        counts = collections.Counter(item["terminal"] for item in merged_results)
        core = {
            "schema_version": "dof.wp014f-validation-aggregate.v1", "mode": "timing",
            "planned_cases": len(timing), "executed_cases": len(merged_results), "workers": args.jobs,
            "queue_wall_seconds": control_aggregate["queue_wall_seconds"] + rest["queue_wall_seconds"],
            "summed_case_wall_seconds": sum(item["wall_seconds"] for item in merged_results),
            "terminal_counts": dict(sorted(counts.items())), "results": merged_results,
            "all_inputs_unchanged": all(item["input_sha256_before"] == item["input_sha256_after"] for item in merged_results),
            "owned_process_survivors": [item["case"]["case_id"] for item in merged_results if item["cleanup"].get("audit_2")],
            "provider_or_llm_calls": 0, "new_optimization_jobs": 0, "sealed_holdout_accesses": 0,
        }
        aggregates["timing"] = core | {"aggregate_sha256": object_sha256(core)}
    else:
        control_aggregate["planned_cases"] = len(timing)
        control_aggregate["not_executed_cases"] = [
            {"case_id": case["case_id"], "reason": "timing_self_control_not_passed"}
            for case in timing if case is not timing_control
        ]
        aggregates["timing"] = control_aggregate
    write_json(timing_root / "aggregate.json", aggregates["timing"])

    summary = write_report(args.output, plan, resolved, aggregates, discovery)
    write_json(args.output / "environment.json", {
        "python": sys.executable, "python_version": sys.version.split()[0],
        "vivado_version": subprocess.check_output([vivado, "-version"], text=True, stderr=subprocess.STDOUT).splitlines()[:4],
        "rapidwright_present": True, "repository": git_snapshot(),
        "provider_environment_values_recorded": False,
    })
    print(json.dumps({"status": summary["status"], "recommendations": summary["recommendations"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
