#!/usr/bin/env python3
"""Bounded FPGA'24 synthetic-timing feasibility reconnaissance.

This command never dispatches an optimisation action or a model request.  It
routes at most four official FPGA'24 physical netlists, discovers clock
topology on unconstrained DCPs, compares two preregistered period policies and
runs V0 plus golden=self V1 only on the bounded promoted cohort.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tarfile
import time
from typing import Any, Mapping

try:
    from scripts.run_wp014f_validation_recon import file_sha256, object_sha256, run_cases
except ModuleNotFoundError:
    from run_wp014f_validation_recon import file_sha256, object_sha256, run_cases


REPO = Path(__file__).resolve().parents[1]
CLOCKING_REF = re.compile(r"(MMCM|PLL)", re.IGNORECASE)
ALLOWED_BUFFER_REF = re.compile(r"^(BUFG|BUFH|BUFR)", re.IGNORECASE)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def unescape(value: str) -> str:
    return value.replace("%0D", "\r").replace("%0A", "\n").replace("%7C", "|").replace("%25", "%")


def parse_kv(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    candidates = []
    for line in path.read_text(errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "candidate":
            parts = value.split("|", 5)
            if len(parts) not in (5, 6):
                raise ValueError("invalid_topology_candidate_row")
            candidate = {
                "net": unescape(parts[0]),
                "sink_clock_pins": int(parts[1]),
                "source_ports": [unescape(item) for item in parts[2].split(",") if item],
                "drivers": [unescape(item) for item in parts[3].split(",") if item],
                "clocking_cells": [unescape(item) for item in parts[4].split(",") if item],
            }
            if len(parts) == 6:
                candidate["sink_domain_id"] = unescape(parts[5])
            candidates.append(candidate)
        else:
            result[key] = value
    result["candidates"] = candidates
    return result


def evaluate_topology(probe: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    clock_pins = int(probe.get("clock_pins", 0))
    candidates = list(probe.get("candidates", []))
    grouped: dict[str, list[Mapping[str, Any]]] = collections.defaultdict(list)
    for row in candidates:
        # Legacy V1 rows deliberately remain independent.  Only V2 rows whose
        # Tcl producer proved exact sink-set equality share a domain id.
        domain_id = row.get("sink_domain_id")
        key = f"exact:{domain_id}" if domain_id else f"legacy:{row['net']}"
        grouped[key].append(row)
    domains = []
    for key, members in grouped.items():
        sink_counts = {int(row["sink_clock_pins"]) for row in members}
        if len(sink_counts) != 1:
            return {
                "terminal": "UNSTABLE_CLOCK_IDENTITY", "eligible": False,
                "reason": "exact_domain_members_disagree_on_sink_count", "domain_key": key,
            }
        source_members = [row for row in members if len(row["source_ports"]) == 1]
        representative = sorted(source_members or members, key=lambda row: row["net"])[0]
        merged = dict(representative)
        merged["sink_clock_pins"] = sink_counts.pop()
        merged["equivalent_source_candidates"] = sorted(row["net"] for row in members)
        merged["domain_member_count"] = len(members)
        merged["domain_aggregation"] = "EXACT_SINK_SET" if representative.get("sink_domain_id") else "LEGACY_INDEPENDENT"
        merged["domain_clocking_cells"] = sorted({
            cell for row in members for cell in row.get("clocking_cells", [])
        })
        domains.append(merged)
    ordered = sorted(domains, key=lambda row: (-int(row["sink_clock_pins"]), row["net"]))
    if not ordered or clock_pins == 0:
        return {"terminal": "NO_CLOCK_CANDIDATE", "eligible": False}
    first = ordered[0]
    dominant = int(first["sink_clock_pins"]) / clock_pins
    second = int(ordered[1]["sink_clock_pins"]) / clock_pins if len(ordered) > 1 else 0.0
    common = {"dominant_fraction": dominant, "second_domain_fraction": second, "dominant_candidate": first}
    if int(first["sink_clock_pins"]) < int(policy["minimum_clock_sinks"]):
        return common | {"terminal": "CLOCK_SINK_COUNT_TOO_SMALL", "eligible": False}
    if dominant < float(policy["minimum_dominant_fraction"]) or second > float(policy["maximum_second_domain_fraction"]):
        return common | {"terminal": "AMBIGUOUS_MULTICLOCK", "eligible": False}
    if policy["reject_unresolved_mmcm_or_pll"] and first["domain_clocking_cells"]:
        return common | {"terminal": "GENERATED_CLOCK_UNRESOLVED", "eligible": False}
    if len(first["source_ports"]) == 1:
        target = {"kind": "port", "name": first["source_ports"][0]}
    else:
        buffer_drivers = []
        for driver in first["drivers"]:
            pin, _, ref = driver.partition("@")
            if ALLOWED_BUFFER_REF.match(ref):
                buffer_drivers.append({"kind": "pin", "name": pin})
        if len(first["source_ports"]) > 1 or len(buffer_drivers) != 1:
            return common | {"terminal": "UNSTABLE_CLOCK_IDENTITY", "eligible": False}
        target = buffer_drivers[0]
    return common | {"terminal": "TOPOLOGY_ELIGIBLE", "eligible": True, "target": target}


def topologies_stable(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    keys = ("part", "sequential_cells", "clock_pins", "candidates")
    return all(first.get(key) == second.get(key) for key in keys)


def root_specific_period(dcrit_ns: float, margin: float, quantum_ns: float) -> float:
    if not math.isfinite(dcrit_ns) or dcrit_ns <= 0 or margin <= 0 or quantum_ns <= 0:
        raise ValueError("invalid_period_inputs")
    period = round(math.ceil((margin * dcrit_ns - 1e-12) / quantum_ns) * quantum_ns, 9)
    if margin < 1 and period >= dcrit_ns:
        period = round(period - quantum_ns, 9)
    if period <= 0:
        raise ValueError("invalid_quantized_period")
    return period


def choose_period_policy(probes: list[Mapping[str, Any]], policy: Mapping[str, Any], minimum_coverage: float) -> str:
    if policy.get("selection") == "root_specific_frozen_5pct_tightening":
        return "root_specific"
    fixed = float(policy["fixed_period_ns"])
    return "fixed" if probes and all(
        float(policy["fixed_ratio_min"]) <= float(row["dcrit_ns"]) / fixed <= float(policy["fixed_ratio_max"])
        and float(row["target_timed_fraction"]) >= minimum_coverage
        and int(row["no_clock_pins"]) == 0
        for row in probes
    ) else "root_specific"


def process_group_pids(pgid: int) -> list[int]:
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.getpgid(int(entry.name)) == pgid:
                pids.append(int(entry.name))
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def terminate_group(pgid: int) -> list[str]:
    events = []
    for sig, label, wait in ((signal.SIGTERM, "SIGTERM", 10), (signal.SIGKILL, "SIGKILL", 2)):
        if not process_group_pids(pgid):
            break
        try:
            os.killpg(pgid, sig)
            events.append(label)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + wait
        while process_group_pids(pgid) and time.monotonic() < deadline:
            time.sleep(0.25)
    return events


def run_command(command: list[str], *, cwd: Path, log: Path, timeout: int, env: Mapping[str, str]) -> dict[str, Any]:
    started = time.time()
    with log.open("w") as stream:
        process = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True, env=dict(env))
        pgid = process.pid
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
            cleanup = []
        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup = terminate_group(pgid)
            returncode = process.wait(timeout=30)
    time.sleep(0.5)
    survivors = process_group_pids(pgid)
    if survivors:
        cleanup.extend(terminate_group(pgid))
    return {
        "command": command,
        "returncode": returncode,
        "timed_out": timed_out,
        "wall_seconds": time.time() - started,
        "cleanup_events": cleanup,
        "survivors": process_group_pids(pgid),
        "log_sha256": file_sha256(log),
    }


def vivado_batch(vivado: str, script: Path, arguments: list[str], *, cwd: Path, log: Path, timeout: int, env: Mapping[str, str]) -> dict[str, Any]:
    return run_command(
        [vivado, "-mode", "batch", "-nolog", "-nojournal", "-source", str(script), "-tclargs", *arguments],
        cwd=cwd, log=log, timeout=timeout, env=env,
    )


def extract_source(archive: Path, candidate_id: str, output: Path) -> dict[str, Path]:
    output.mkdir(parents=True, exist_ok=False)
    names = [f"{candidate_id}.netlist", f"{candidate_id}_unrouted.phys"]
    with tarfile.open(archive, "r:gz") as source:
        available = set(source.getnames())
        if not set(names).issubset(available):
            raise FileNotFoundError(f"archive_members_missing:{candidate_id}")
        for name in names:
            member = source.getmember(name)
            member.name = Path(name).name
            source.extract(member, output)
    return {"netlist": output / names[0], "unrouted": output / names[1]}


def classpath(contest: Path, env: Mapping[str, str], output: Path) -> str:
    completed = run_command(
        [str(contest / "gradlew"), "-quiet", "--offline", "runtimeClasspath"],
        cwd=contest, log=output, timeout=600, env=env,
    )
    if completed["returncode"] != 0:
        raise RuntimeError("gradle_runtime_classpath_failed")
    lines = [line for line in output.read_text(errors="replace").splitlines() if line and not line.startswith("Picked up")]
    if not lines:
        raise RuntimeError("empty_runtime_classpath")
    return lines[-1] + ":" + str(contest / "build" / "classes" / "java" / "main")


def reconstruct(contest: Path, netlist: Path, phys: Path, xdc: Path, dcp: Path, env: Mapping[str, str], timeout: int) -> dict[str, Any]:
    return run_command(
        [str(contest / "RapidWright" / "bin" / "rapidwright"), "PhysicalNetlistToDcp", str(netlist), str(phys), str(xdc), str(dcp)],
        cwd=dcp.parent, log=dcp.with_suffix(".reconstruct.log"), timeout=timeout, env=env,
    )


def route_candidate(candidate: Mapping[str, Any], *, archive: Path, contest: Path, cp: str, output: Path, env: Mapping[str, str], timeout: int) -> dict[str, Any]:
    candidate_id = str(candidate["candidate_id"])
    case_dir = output / "routing" / candidate_id
    paths = extract_source(archive, candidate_id, case_dir)
    routed = case_dir / f"{candidate_id}_rwroute.phys"
    route = run_command(
        ["java", "-Xms2g", "-Xmx32736m", "-cp", cp, "com.xilinx.fpga24_routing_contest.PartialRouterPhysNetlist", str(paths["unrouted"]), str(routed)],
        cwd=case_dir, log=case_dir / "route.log", timeout=timeout, env=env,
    )
    check = None
    if route["returncode"] == 0 and routed.is_file():
        check = run_command(
            ["java", "-Xms2g", "-Xmx32736m", "-cp", cp, "com.xilinx.fpga24_routing_contest.CheckPhysNetlist", str(paths["netlist"]), str(routed), str(paths["unrouted"])],
            cwd=case_dir, log=case_dir / "check.log", timeout=min(timeout, 1800), env=env,
        )
    empty_xdc = case_dir / "empty.xdc"
    empty_xdc.write_text("# Intentionally empty discovery constraint set.\n")
    discovery = case_dir / f"{candidate_id}_discovery.dcp"
    conversion = None
    if check and check["returncode"] == 0:
        conversion = reconstruct(contest, paths["netlist"], routed, empty_xdc, discovery, env, 1800)
    terminal = "ROUTE_TIMEOUT" if route["timed_out"] else "ROUTE_FAILURE"
    if route["returncode"] == 0 and (not check or check["returncode"] != 0):
        terminal = "CHECK_PHYS_FAILURE"
    if check and check["returncode"] == 0 and (not conversion or conversion["returncode"] != 0 or not discovery.is_file()):
        terminal = "DCP_RECONSTRUCTION_FAILURE"
    if conversion and conversion["returncode"] == 0 and discovery.is_file():
        terminal = "ROUTED_DISCOVERY_DCP_READY"
    result = {
        "candidate": dict(candidate), "terminal": terminal,
        "route": route, "check": check, "conversion": conversion,
        "source_paths": {key: str(value) for key, value in paths.items()},
        "source_sha256": {key: file_sha256(value) for key, value in paths.items()},
        "routed_phys": str(routed) if routed.is_file() else None,
        "routed_phys_sha256": file_sha256(routed) if routed.is_file() else None,
        "discovery_dcp": str(discovery) if discovery.is_file() else None,
        "discovery_dcp_sha256": file_sha256(discovery) if discovery.is_file() else None,
        "provider_or_llm_calls": 0, "optimization_calls": 0,
    }
    write_json(case_dir / "routing-result.json", result)
    return result


def reuse_routing_candidate(candidate: Mapping[str, Any], *, evidence: Path, contest: Path, cp: str, output: Path, env: Mapping[str, str], timeout: int) -> dict[str, Any]:
    """Revalidate a completed route without consuming another routing attempt."""
    candidate_id = str(candidate["candidate_id"])
    prior_path = evidence / "routing" / candidate_id / "routing-result.json"
    prior = json.loads(prior_path.read_text())
    if prior["candidate"]["candidate_id"] != candidate_id or prior["route"]["returncode"] != 0:
        raise ValueError(f"invalid_prior_route:{candidate_id}")
    paths = {key: Path(value) for key, value in prior["source_paths"].items()}
    routed = Path(prior["routed_phys"])
    for key, path in paths.items():
        if file_sha256(path) != prior["source_sha256"][key]:
            raise ValueError(f"prior_source_hash_mismatch:{candidate_id}:{key}")
    if file_sha256(routed) != prior["routed_phys_sha256"]:
        raise ValueError(f"prior_routed_hash_mismatch:{candidate_id}")

    case_dir = output / "routing" / candidate_id
    case_dir.mkdir(parents=True, exist_ok=False)
    check = run_command(
        ["java", "-Xms2g", "-Xmx32736m", "-cp", cp, "com.xilinx.fpga24_routing_contest.CheckPhysNetlist", str(paths["netlist"]), str(routed), str(paths["unrouted"])],
        cwd=case_dir, log=case_dir / "check.log", timeout=min(timeout, 1800), env=env,
    )
    empty_xdc = case_dir / "empty.xdc"
    empty_xdc.write_text("# Intentionally empty discovery constraint set.\n")
    discovery = case_dir / f"{candidate_id}_discovery.dcp"
    conversion = reconstruct(contest, paths["netlist"], routed, empty_xdc, discovery, env, 1800) if check["returncode"] == 0 else None
    terminal = "CHECK_PHYS_FAILURE"
    if check["returncode"] == 0 and (not conversion or conversion["returncode"] != 0 or not discovery.is_file()):
        terminal = "DCP_RECONSTRUCTION_FAILURE"
    if conversion and conversion["returncode"] == 0 and discovery.is_file():
        terminal = "ROUTED_DISCOVERY_DCP_READY"
    result = {
        "candidate": dict(candidate), "terminal": terminal, "route": prior["route"],
        "prior_evidence": str(prior_path), "prior_evidence_sha256": file_sha256(prior_path),
        "routing_reexecuted": False, "check": check, "conversion": conversion,
        "source_paths": {key: str(value) for key, value in paths.items()},
        "source_sha256": prior["source_sha256"], "routed_phys": str(routed),
        "routed_phys_sha256": prior["routed_phys_sha256"],
        "discovery_dcp": str(discovery) if discovery.is_file() else None,
        "discovery_dcp_sha256": file_sha256(discovery) if discovery.is_file() else None,
        "provider_or_llm_calls": 0, "optimization_calls": 0,
    }
    write_json(case_dir / "routing-result.json", result)
    return result


def topology_probe(candidate_id: str, dcp: Path, output: Path, vivado: str, env: Mapping[str, str], timeout: int, policy: Mapping[str, Any]) -> dict[str, Any]:
    case_dir = output / "topology" / candidate_id
    case_dir.mkdir(parents=True, exist_ok=False)
    before = file_sha256(dcp)
    probes = []
    executions = []
    for index in (1, 2):
        probe_path = case_dir / f"probe-{index}.txt"
        execution = vivado_batch(vivado, REPO / "scripts" / "wp014h_synthetic_clock_topology.tcl", [str(dcp), str(probe_path)], cwd=case_dir, log=case_dir / f"probe-{index}.log", timeout=timeout, env=env)
        executions.append(execution)
        probes.append(parse_kv(probe_path) if execution["returncode"] == 0 and probe_path.is_file() else {})
    stable = topologies_stable(probes[0], probes[1])
    evaluation = evaluate_topology(probes[0], policy) if probes[0] else {"terminal": "TOPOLOGY_PROBE_FAILURE", "eligible": False}
    if not stable and policy["require_stable_double_probe"]:
        evaluation = evaluation | {"terminal": "UNSTABLE_CLOCK_IDENTITY", "eligible": False}
    result = {
        "candidate_id": candidate_id, "dcp": str(dcp), "dcp_sha256": file_sha256(dcp),
        "executions": executions, "probes": probes, "stable": stable, "evaluation": evaluation,
        "input_sha256_before": before, "input_sha256_after": file_sha256(dcp),
        "input_unchanged": before == file_sha256(dcp),
    }
    write_json(case_dir / "topology-result.json", result)
    return result


def timing_probe(candidate_id: str, dcp: Path, target: Mapping[str, str], period: float, output: Path, vivado: str, env: Mapping[str, str], timeout: int, label: str) -> dict[str, Any]:
    case_dir = output / "timing" / candidate_id
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / f"{label}.txt"
    execution = vivado_batch(vivado, REPO / "scripts" / "wp014h_synthetic_timing_probe.tcl", [str(dcp), target["kind"], target["name"], f"{period:.9f}", str(path)], cwd=case_dir, log=case_dir / f"{label}.log", timeout=timeout, env=env)
    parsed = parse_kv(path) if execution["returncode"] == 0 and path.is_file() else {}
    clock_pins = int(parsed.get("clock_pins", 0))
    timed = int(parsed.get("timed_clock_pins", 0))
    timed_fraction = timed / clock_pins if clock_pins else 0.0
    target_timed = int(parsed.get("target_timed_clock_pins", 0))
    target_timed_fraction = target_timed / clock_pins if clock_pins else 0.0
    try:
        wns = float(parsed["wns_ns"])
    except (KeyError, ValueError):
        wns = None
    try:
        dcrit = float(parsed["data_path_delay_ns"])
    except (KeyError, ValueError):
        dcrit = None
    result = {
        "candidate_id": candidate_id, "label": label, "period_ns": period,
        "target": dict(target), "execution": execution, "probe": parsed,
        "timed_fraction": timed_fraction, "target_timed_fraction": target_timed_fraction,
        "no_clock_pins": int(parsed.get("no_clock_pins", 0)), "wns_ns": wns, "dcrit_ns": dcrit,
        "terminal": "TIMING_PROBE_PASS" if execution["returncode"] == 0 and dcrit and dcrit > 0 else "TIMING_PROBE_FAILURE",
    }
    write_json(case_dir / f"{label}.json", result)
    return result


def source_target_options(evaluation: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return only source identities proven to share the selected exact sink set."""
    options = []
    for encoded in evaluation["dominant_candidate"].get("equivalent_source_candidates", []):
        kind, separator, name = str(encoded).partition(":")
        if separator and kind in {"port", "pin"} and name:
            options.append({"kind": kind, "name": name})
    if not options:
        options.append(dict(evaluation["target"]))
    return sorted(options, key=lambda row: (row["kind"], row["name"]))


def choose_source_binding(probes: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Select by target-clock reach only; never use timing quality or QoR."""
    usable = [row for row in probes if row["terminal"] == "TIMING_PROBE_PASS"]
    if not usable:
        return None
    return sorted(
        usable,
        key=lambda row: (
            -float(row["target_timed_fraction"]),
            row["target"]["kind"],
            row["target"]["name"],
        ),
    )[0]


def xdc_text(target: Mapping[str, str], period: float) -> str:
    name = target["name"]
    if any(char in name for char in "{}\n\r"):
        raise ValueError("unsafe_tcl_target_name")
    getter = "get_ports" if target["kind"] == "port" else "get_pins"
    return (
        "# EXPERIMENT_DEFINED_SYNTHETIC_TIMING_CONTRACT\n"
        "# Not source-supported; report in a separate timing stratum.\n"
        f"create_clock -name wp014h_synth_clock -period {period:.3f} [{getter} {{{name}}}]\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--routing-evidence", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    config = json.loads(args.config.read_text())
    limits, topology_policy, period_policy = config["limits"], config["topology_policy"], config["period_policy"]
    if limits["provider_or_llm_calls"] or limits["optimization_calls"] or limits["holdout_optimization_calls"]:
        raise ValueError("reconnaissance_firewall_not_zero")
    vivado = os.environ.get("VIVADO_EXEC")
    if not vivado or not Path(vivado).is_file() or not os.access(vivado, os.X_OK):
        raise RuntimeError("VIVADO_EXEC_not_executable")
    if not os.environ.get("RAPIDWRIGHT_PATH") or not Path(os.environ["RAPIDWRIGHT_PATH"]).is_dir():
        raise RuntimeError("RAPIDWRIGHT_PATH_not_directory")
    archive = Path(config["assets"]["evaluation_archive"])
    contest = Path(config["assets"]["contest_repository"])
    if file_sha256(archive) != config["assets"]["evaluation_archive_sha256"]:
        raise ValueError("evaluation_archive_hash_mismatch")
    args.output.mkdir(parents=True)
    started = time.time()
    env = os.environ.copy()
    env["RAPIDWRIGHT_PATH"] = str(contest / "RapidWright")
    vivado_wrapper = REPO / "tools" / "wp014h" / "bin"
    env["PATH"] = str(vivado_wrapper) + os.pathsep + env.get("PATH", "")
    cp = classpath(contest, env, args.output / "gradle-classpath.log")

    controls = {}
    for row in config["controls"]:
        dcp = Path(row["dcp"])
        if file_sha256(dcp) != row["sha256"]:
            raise ValueError(f"control_hash_mismatch:{row['candidate_id']}")
        controls[row["candidate_id"]] = {"metadata": row, "dcp": dcp}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            cid: pool.submit(topology_probe, cid, value["dcp"], args.output, vivado, env, limits["vivado_probe_timeout_seconds"], topology_policy)
            for cid, value in controls.items()
        }
        control_topology = {cid: future.result() for cid, future in futures.items()}

    candidates = config["priority_candidates"]
    route_results = []
    first_batch = candidates[:3]
    if args.routing_evidence:
        available = []
        for row in candidates:
            prior = args.routing_evidence / "routing" / row["candidate_id"] / "routing-result.json"
            if prior.is_file() and json.loads(prior.read_text()).get("route", {}).get("returncode") == 0:
                available.append(row)
        if not available:
            raise ValueError("no_prior_routing_evidence")
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(limits["route_workers"], len(available))) as pool:
            route_results.extend(pool.map(lambda row: reuse_routing_candidate(row, evidence=args.routing_evidence, contest=contest, cp=cp, output=args.output, env=env, timeout=limits["route_timeout_seconds"]), available))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(limits["route_workers"], len(first_batch))) as pool:
            route_results.extend(pool.map(lambda row: route_candidate(row, archive=archive, contest=contest, cp=cp, output=args.output, env=env, timeout=limits["route_timeout_seconds"]), first_batch))
    priority_topology = {}
    for row in route_results:
        if row["terminal"] == "ROUTED_DISCOVERY_DCP_READY":
            candidate_id = row["candidate"]["candidate_id"]
            priority_topology[candidate_id] = topology_probe(candidate_id, Path(row["discovery_dcp"]), args.output, vivado, env, limits["vivado_probe_timeout_seconds"], topology_policy)
    eligible_count = sum(result["evaluation"]["eligible"] for result in priority_topology.values())
    if not args.routing_evidence and eligible_count < limits["promoted_priority_roots"] and len(candidates) > 3:
        reserve = route_candidate(candidates[3], archive=archive, contest=contest, cp=cp, output=args.output, env=env, timeout=limits["route_timeout_seconds"])
        route_results.append(reserve)
        if reserve["terminal"] == "ROUTED_DISCOVERY_DCP_READY":
            candidate_id = reserve["candidate"]["candidate_id"]
            priority_topology[candidate_id] = topology_probe(candidate_id, Path(reserve["discovery_dcp"]), args.output, vivado, env, limits["vivado_probe_timeout_seconds"], topology_policy)
    if len(route_results) > limits["routing_attempts"]:
        raise RuntimeError("routing_attempt_cap_exceeded")

    promoted_ids = [row["candidate_id"] for row in candidates if row["candidate_id"] in priority_topology and priority_topology[row["candidate_id"]]["evaluation"]["eligible"]][:limits["promoted_priority_roots"]]
    cohort = promoted_ids[:]
    positive = next(row for row in config["controls"] if row["role"] == "positive_control")
    positive_topology = control_topology[positive["candidate_id"]]
    if positive_topology["evaluation"]["eligible"]:
        cohort.append(positive["candidate_id"])
    cohort = cohort[:limits["final_v1_cases"]]

    route_by_id = {row["candidate"]["candidate_id"]: row for row in route_results}
    dcp_by_id = {cid: Path(route_by_id[cid]["discovery_dcp"]) for cid in promoted_ids}
    dcp_by_id.update({positive["candidate_id"]: Path(positive["dcp"])})
    topology_by_id = priority_topology | control_topology
    baseline_probes = []
    binding_probes = {}
    for cid in cohort:
        evaluation = topology_by_id[cid]["evaluation"]
        case_probes = [
            timing_probe(
                cid, dcp_by_id[cid], target, float(period_policy["probe_period_ns"]),
                args.output, vivado, env, limits["vivado_probe_timeout_seconds"],
                f"baseline-10ns-source-{index:02d}",
            )
            for index, target in enumerate(source_target_options(evaluation))
        ]
        binding_probes[cid] = case_probes
        selected = choose_source_binding(case_probes)
        if selected:
            baseline_probes.append(selected)
    usable_probes = [
        row for row in baseline_probes
        if row["target_timed_fraction"] >= topology_policy["minimum_final_timed_endpoint_fraction"]
    ]
    selected_policy = choose_period_policy(usable_probes, period_policy, float(topology_policy["minimum_final_timed_endpoint_fraction"]))

    final_records = []
    probe_by_id = {row["candidate_id"]: row for row in usable_probes}
    for cid in cohort:
        if cid not in probe_by_id:
            continue
        probe = probe_by_id[cid]
        period = float(period_policy["fixed_period_ns"]) if selected_policy == "fixed" else root_specific_period(float(probe["dcrit_ns"]), float(period_policy["root_specific_margin"]), float(period_policy["root_specific_quantum_ns"]))
        case_dir = args.output / "final" / cid
        case_dir.mkdir(parents=True, exist_ok=False)
        target = probe["target"]
        xdc = case_dir / "synthetic.xdc"
        xdc.write_text(xdc_text(target, period))
        if cid in route_by_id:
            source_paths = route_by_id[cid]["source_paths"]
            netlist, phys = Path(source_paths["netlist"]), Path(route_by_id[cid]["routed_phys"])
        else:
            netlist = contest / f"{cid}.netlist"
            phys = contest / f"{cid}_rwroute.phys"
        final_dcp = case_dir / f"{cid}_synthetic.dcp"
        conversion = reconstruct(contest, netlist, phys, xdc, final_dcp, env, limits["vivado_probe_timeout_seconds"])
        final_probe = None
        v0 = None
        scorer = None
        if conversion["returncode"] == 0 and final_dcp.is_file():
            final_probe = timing_probe(cid, final_dcp, target, period, args.output, vivado, env, limits["vivado_probe_timeout_seconds"], "final-period")
            v0 = vivado_batch(vivado, REPO / "scripts" / "audit_holdout_dcp.tcl", [str(final_dcp), "wp014h_synth_clock", f"{period:.9f}"], cwd=case_dir, log=case_dir / "v0.log", timeout=limits["vivado_probe_timeout_seconds"], env=env)
            scorer = vivado_batch(vivado, REPO / "scripts" / "e1_score_dcp.tcl", [str(final_dcp), "wp014h_synth_clock"], cwd=case_dir, log=case_dir / "scorer.log", timeout=limits["vivado_probe_timeout_seconds"], env=env)
        coverage_ok = bool(final_probe and final_probe["target_timed_fraction"] >= topology_policy["minimum_final_timed_endpoint_fraction"] and final_probe["no_clock_pins"] == 0)
        terminal = "FINAL_RECONSTRUCTION_FAILURE"
        if final_dcp.is_file() and not coverage_ok:
            terminal = "FINAL_TIMING_COVERAGE_FAILURE"
        if final_dcp.is_file() and coverage_ok and v0 and v0["returncode"] == 0 and scorer and scorer["returncode"] == 0:
            terminal = "SYNTHETIC_V0_SCORE_READY"
        record = {
            "candidate_id": cid, "selected_policy": selected_policy, "period_ns": period,
            "target": target, "baseline_probe": probe, "xdc": str(xdc), "xdc_sha256": file_sha256(xdc),
            "final_dcp": str(final_dcp) if final_dcp.is_file() else None,
            "final_dcp_sha256": file_sha256(final_dcp) if final_dcp.is_file() else None,
            "conversion": conversion, "final_probe": final_probe, "v0": v0, "scorer": scorer,
            "terminal": terminal, "provider_or_llm_calls": 0, "optimization_calls": 0,
        }
        if final_probe:
            part = str(topology_by_id[cid]["probes"][0].get("part", ""))
            speed_match = re.search(r"(-\d+(?:[a-z])?(?:-[a-z])?)$", part, re.IGNORECASE)
            record["synthetic_timing_certificate"] = {
                "schema_version": "dof.synthetic-timing-certificate.v1",
                "clock_target": target,
                "dominant_fraction": topology_by_id[cid]["evaluation"]["dominant_fraction"],
                "second_domain_fraction": topology_by_id[cid]["evaluation"]["second_domain_fraction"],
                "baseline_path_startpoint": probe["probe"].get("path_startpoint"),
                "baseline_path_endpoint": probe["probe"].get("path_endpoint"),
                "baseline_datapath_delay_ns": probe["dcrit_ns"],
                "raw_period_ns": float(period_policy["root_specific_margin"]) * float(probe["dcrit_ns"]),
                "quantized_period_ns": period,
                "quantum_ns": float(period_policy["root_specific_quantum_ns"]),
                "constraint_sha256": file_sha256(xdc),
                "part": part,
                "speed_grade": speed_match.group(1) if speed_match else "UNKNOWN",
                "reconstruction_source": config["assets"],
                "timed_fraction": final_probe["timed_fraction"],
                "target_timed_fraction": final_probe["target_timed_fraction"],
                "source_binding_rule": "MAX_TARGET_CLOCK_REACH_WITHIN_EXACT_SINK_DOMAIN_THEN_LEXICAL",
                "authoritative_no_clock_pins": final_probe["no_clock_pins"],
            }
        write_json(case_dir / "final-result.json", record)
        final_records.append(record)

    v1_candidates = [row for row in final_records if row["terminal"] == "SYNTHETIC_V0_SCORE_READY"][:limits["final_v1_cases"]]
    v1 = None
    if v1_candidates:
        validation_plan = json.loads((REPO / "configs" / "wp014f" / "validation-recon-v1.json").read_text())
        validation_plan["boundaries"]["per_case_timeout_seconds"] = limits["v1_timeout_seconds"]
        cases = []
        for row in v1_candidates:
            cases.append({
                "case_id": "synthetic_self__" + row["candidate_id"], "root_id": row["candidate_id"], "mechanism": "golden_self_synthetic_timing",
                "golden_dcp": row["final_dcp"], "revised_dcp": row["final_dcp"],
                "golden_sha256": row["final_dcp_sha256"], "revised_sha256": row["final_dcp_sha256"],
                "resolved_input_sha256": {"golden": row["final_dcp_sha256"], "revised": row["final_dcp_sha256"]},
                "sealed_holdout_accesses": 0,
            })
        v1 = run_cases(cases, validation_plan, args.output / "v1", "multiseed", min(limits["maximum_case_concurrency"], len(cases)))
    v1_pass_ids = {row["case"]["root_id"] for row in (v1 or {}).get("results", []) if row["terminal"] == "MULTISEED_FUNCTIONAL_PASS"}
    priority_passes = len(v1_pass_ids.intersection(promoted_ids))
    recommendation = "PROMISING_FOR_PROTOCOL_REVIEW" if priority_passes >= 3 else "PARTIAL_FEASIBILITY" if priority_passes else "NOT_FEASIBLE_UNDER_FROZEN_VALIDATOR"
    core = {
        "schema_version": "dof.wp014h-synthetic-timing-recon-summary.v1",
        "identity": config["identity"], "classification": config["classification"],
        "config_sha256": file_sha256(args.config), "recommendation": recommendation,
        "selected_pilot_period_policy": selected_policy, "routing_attempts": len(route_results),
        "route_terminals": dict(collections.Counter(row["terminal"] for row in route_results)),
        "topology": {cid: result["evaluation"] for cid, result in topology_by_id.items()},
        "promoted_priority_ids": promoted_ids, "cohort": cohort,
        "final_terminals": dict(collections.Counter(row["terminal"] for row in final_records)),
        "v1_terminal_counts": (v1 or {}).get("terminal_counts", {}), "priority_v1_passes": priority_passes,
        "queue_wall_seconds": time.time() - started,
        "provider_or_llm_calls": 0, "provider_cost_usd": 0, "optimization_calls": 0,
        "scientific_jobs": 0, "holdout_optimization_calls": 0,
        "existing_gate5_changed": False, "protocol_frozen": False, "roots_formally_admitted": 0,
        "all_v1_inputs_unchanged": None if v1 is None else v1["all_inputs_unchanged"],
        "owned_process_survivors": [] if v1 is None else v1["owned_process_survivors"],
        "total_wall_cap_exceeded": time.time() - started > limits["total_wall_seconds"],
    }
    summary = core | {"summary_sha256": object_sha256(core)}
    write_json(args.output / "synthetic-timing-recon-summary.json", summary)
    print(json.dumps({"recommendation": recommendation, "routing_attempts": len(route_results), "priority_v1_passes": priority_passes}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
