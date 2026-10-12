#!/usr/bin/env python3
"""Fresh, bounded production-path admission for WP-014C Action Surface v3."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import importlib
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.single_submission.action_surface_v3 import (  # noqa: E402
    PHYS_OPT_DIRECTIVES,
    PHYS_OPT_PLACED_OPTIONS,
    PHYS_OPT_POST_ROUTE_OPTIONS,
    PLACE_DIRECTIVES,
    PLACE_NET_DELAY_WEIGHTS,
    ROUTE_DIRECTIVES,
    registry_sha256,
)
from optimizer_pipeline.dof.single_submission.contract_v5 import compile_candidate_v5  # noqa: E402
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry  # noqa: E402
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from scripts.run_wp013_admission import Admission  # noqa: E402
from scripts.run_wp013a_e0 import _execute_typed, file_digest  # noqa: E402


SCHEMA = "dof.wp014c-production-admission.v1"
PLAN_SCHEMA = "dof.wp014c-production-admission-plan.v1"
ROOT_ID = "vexriscv_re-place"
MAX_HEAVY = 96
MAX_CONCURRENCY = 8


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def write_json(path: Path, value: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")
    if private:
        path.chmod(0o600)


def git(command: str) -> str:
    return subprocess.run(
        ("git", *command.split()), cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.strip()


def assert_clean() -> dict[str, str]:
    status = git("status --porcelain")
    if status:
        raise RuntimeError("repository_not_clean")
    return {"commit": git("rev-parse HEAD"), "tree": git("rev-parse HEAD^{tree}"), "status": "clean"}


def root_record(config: Path) -> dict[str, Any]:
    value = json.loads(config.read_text())
    selected = next(row for row in value["roots"] if row["root_id"] == ROOT_ID)
    root = Path(value["root_path_prefix"]) / selected["filename"]
    if not root.is_file() or file_digest(root) != selected["sha256"]:
        raise RuntimeError("development_root_binding_failed")
    return dict(selected) | {"path": str(root.resolve())}


def resource_type(text: str) -> str:
    value = text.upper()
    return "DSP" if "DSP" in value else "URAM" if "URAM" in value else "BRAM" if "RAMB" in value else text


def select_compatible_cell_pair(
    rows: list[tuple[str, str, str]], registry: TrialHandleRegistry,
) -> list[tuple[str, str, str]]:
    """Prefer the largest same-resource group, then exact canonical identity."""

    by_resource: dict[str, list[tuple[str, str, str]]] = {}
    for row in rows:
        by_resource.setdefault(row[2], []).append(row)
    for _resource, group in sorted(by_resource.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(group) >= 2:
            return sorted(
                group,
                key=lambda row: registry.resolve(row[0], expected_type="cell").canonical_identity,
            )[:2]
    raise RuntimeError("fresh_target_probe_no_compatible_cell_pair")


def registry_from_probe(probe: Path, root: Mapping[str, Any]) -> tuple[TrialHandleRegistry, dict[str, Any]]:
    captured = json.loads((probe / "private" / "resolved-objects.json").read_text())
    secret = hashlib.sha256(("wp014c-gate3:" + str(root["sha256"])).encode()).digest()
    registry = TrialHandleRegistry(trial_id="wp014c-gate3-v1", root_sha256=str(root["sha256"]), secret=secret)
    handles: dict[str, Any] = {"regions": [], "cells": [], "cell_sites": [], "nets": [], "hardblocks": [], "sites": []}

    for row in captured.get("device_regions", []):
        match = re.fullmatch(r"(?:CLOCKREGION_)?X(\d+)Y(\d+)", row["identity"])
        if match:
            handles["regions"].append(registry.register(
                object_type="region", canonical_identity=row["identity"],
                attributes={"bounds": [int(match.group(1)), int(match.group(1)), int(match.group(2)), int(match.group(2))]},
            ))
    location_rows = []
    for row in captured.get("critical_cell_locations", []):
        ref, site, region = row["attribute"].split("\x1f")
        location_rows.append((row, ref, site, region))

    hardblocks = captured.get("critical_hardblocks", [])
    hb_resource = resource_type(hardblocks[0]["attribute"]) if hardblocks else "ANY"
    compatible_site_names = {row["identity"] for row in captured.get("compatible_sites", [])}
    site_resources: dict[str, set[str]] = {}
    for _row, ref, site, _region in location_rows:
        if site:
            site_resources.setdefault(site, set()).add(ref)
    for name in compatible_site_names:
        site_resources.setdefault(name, set()).add(hb_resource)
    sites_by_name = {
        name: registry.register(
            object_type="site", canonical_identity=name,
            attributes={"compatible_resource_types": sorted(resources), "site_class": "observed_or_admitted_compatible"},
        )
        for name, resources in sorted(site_resources.items())
    }

    for row, ref, site, _region in location_rows:
        attrs = {"path_member": True, "path_rank": int(row["index"]) + 1, "resource_type": ref, "location_status": "PLACED" if site else "UNPLACED"}
        cell = registry.register(object_type="cell", canonical_identity=row["identity"], attributes=attrs)
        handles["cells"].append(cell)
        if site:
            handles["cell_sites"].append((cell, sites_by_name[site], ref))
    rapidwright_census = probe.parent / "private" / "rapidwright-net-census.json"
    if rapidwright_census.exists():
        census = json.loads(rapidwright_census.read_text())
        net_rows = [
            {"identity": row["name"], "attribute": row["fanout"], "index": index, "source_tool": "rapidwright"}
            for index, row in enumerate(census["nets"])
        ]
    else:
        net_rows = sorted(captured.get("high_fanout_nets", []), key=lambda row: (-int(row["attribute"]), row["identity"]))
    for row in net_rows:
        handles["nets"].append(registry.register(
            object_type="net", canonical_identity=row["identity"],
            attributes={"fanout": int(row["attribute"]), "path_member": True, "path_rank": int(row["index"]) + 1},
            source_tool=str(row.get("source_tool", "vivado")),
        ))
    handles["sites"] = [sites_by_name[row["identity"]] for row in captured.get("compatible_sites", [])]
    for row in hardblocks:
        kind = resource_type(row["attribute"])
        handles["hardblocks"].append(registry.register(
            object_type="hardblock", canonical_identity=row["identity"],
            attributes={"path_member": True, "path_rank": int(row["index"]) + 1, "resource_type": kind, "compatible_site_handles": list(handles["sites"])},
        ))

    if len(handles["regions"]) < 2 or len(handles["cells"]) < 2 or not handles["nets"] or not handles["hardblocks"] or not handles["sites"]:
        raise RuntimeError("fresh_target_probe_incomplete:" + json.dumps({key: len(value) for key, value in handles.items()}, sort_keys=True))
    handles["cell_pair"] = select_compatible_cell_pair(handles["cell_sites"], registry)
    bounds = [registry.resolve(handle, expected_type="region").attributes["bounds"] for handle in handles["regions"]]
    handles["geometry_bounds"] = [0, max(row[1] for row in bounds), 0, max(row[3] for row in bounds)]
    return registry, handles


def rapidwright_net_census(root: Path, *, limit: int = 64) -> dict[str, Any]:
    """Read one DCP and return deterministic RapidWright-native WIRE names."""

    module_dir = str(REPO / "RapidWrightMCP")
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    rw = importlib.import_module("rapidwright_tools")
    initialized = rw.initialize_rapidwright("8G")
    if initialized.get("error") or initialized.get("status") not in {"success", "already_initialized"}:
        raise RuntimeError("rapidwright_census_initialization_failed")
    loaded = rw.read_checkpoint(str(root.resolve()))
    if loaded.get("error") or loaded.get("status") != "success":
        raise RuntimeError("rapidwright_census_read_failed")
    rows = []
    for net in rw._current_design.getNets():
        fanout = int(net.getFanOut())
        kind = str(net.getType())
        name = str(net.getName())
        if fanout >= 2 and kind == "WIRE" and name not in {"<const0>", "<const1>", "GLOBAL_LOGIC0", "GLOBAL_LOGIC1"}:
            rows.append({"name": name, "fanout": fanout, "net_type": kind})
    rows.sort(key=lambda row: (-row["fanout"], row["name"]))
    core = {
        "schema_version": "dof.wp014c-rapidwright-net-census.v1", "root_sha256": file_digest(root),
        "selection_rule": "RapidWright-native WIRE nets with fanout>=2, ordered by fanout descending then exact name",
        "nets": rows[:limit], "returned": min(limit, len(rows)), "available": len(rows),
    }
    return core | {"census_sha256": digest(core)}


def step(index: int, op: str, args: Mapping[str, Any] | None = None, *, bind: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"step_id": f"s{index}", "op": op, "args": dict(args or {})}
    if bind is not None:
        value["bind"] = bind
    return value


def p(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"language": "P", "steps": list(steps)}


def g(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"language": "G", "steps": list(steps)}


def admission_jobs(handles: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(job_id: str, family: str, candidate: dict[str, Any], *, nonempty: bool = False, g_witness: str | None = None) -> None:
        rows.append({"job_id": job_id, "mechanism_family": family, "candidate": candidate, "nonempty_required": nonempty, "g_witness": g_witness})

    for directive in PLACE_DIRECTIVES:
        add(f"place--{directive.lower()}", "whole_place", p(
            step(1, "UNROUTE"), step(2, "UNPLACE"), step(3, "PLACE", {"directive": directive}), step(4, "ROUTE", {"directive": "Default"}),
        ))
    for weight in PLACE_NET_DELAY_WEIGHTS:
        add(f"place-weight--{weight}", "place_parameter", p(
            step(1, "UNROUTE"), step(2, "UNPLACE"), step(3, "PLACE", {"directive": "Default", "net_delay_weight": weight}), step(4, "ROUTE", {"directive": "Default"}),
        ))
    add("place-combination--explore-high", "place_parameter_combination", p(
        step(1, "UNROUTE"), step(2, "UNPLACE"), step(3, "PLACE", {"directive": "Explore", "net_delay_weight": "high"}), step(4, "ROUTE", {"directive": "Default"}),
    ))
    for directive in ROUTE_DIRECTIVES:
        add(f"route--{directive.lower()}", "whole_route", p(step(1, "UNROUTE"), step(2, "ROUTE", {"directive": directive})))
    for directive in PHYS_OPT_DIRECTIVES:
        add(f"phys-directive--{directive.lower()}", "phys_directive", p(step(1, "PHYS_OPT", {"mode": "DIRECTIVE", "directive": directive})))
    for option in PHYS_OPT_POST_ROUTE_OPTIONS:
        add(f"phys-post--{option}", "phys_post_route_option", p(step(1, "PHYS_OPT", {"mode": "OPTIONS", "options": [option]})))
    for option in PHYS_OPT_PLACED_OPTIONS:
        add(f"phys-placed--{option}", "phys_placed_option", p(
            step(1, "UNROUTE"), step(2, "PHYS_OPT", {"mode": "OPTIONS", "options": [option]}), step(3, "ROUTE", {"directive": "Default"}),
        ))
    add("phys-combination--slr-tns", "phys_conditional_combination", p(
        step(1, "PHYS_OPT", {"mode": "OPTIONS", "options": ["slr_crossing_opt", "tns_cleanup"]}),
    ))

    net = handles["nets"][0]
    cell1, site1, _resource1 = handles["cell_pair"][0]
    cell2, _site2, _resource2 = handles["cell_pair"][1]
    region1, region2 = handles["regions"][:2]
    hardblock, target_site = handles["hardblocks"][0], handles["sites"][0]
    add("targeted-route--auto-delay", "targeted_route", p(step(1, "ROUTE_NET", {"typed_net": net, "delay_mode": "AUTO_DELAY"})), nonempty=True)
    add("targeted-vivado-replication", "targeted_vivado_replication", p(
        step(1, "UNROUTE"), step(2, "REPLICATE_NET", {"typed_net": net}), step(3, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)
    add("targeted-rapidwright-replication", "rapidwright_replication", p(
        step(1, "REPLICATE_DRIVER", {"typed_net": net, "split_factor": 2}), step(2, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)
    add("cell-place-unplace-roundtrip", "cell_place_unplace", p(
        step(1, "UNROUTE"), step(2, "UNPLACE_CELL", {"typed_cell": cell1}), step(3, "PLACE_CELL", {"typed_cell": cell1, "destination_site": site1}),
        step(4, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)
    add("cell-swap", "cell_swap", p(
        step(1, "UNROUTE"), step(2, "SWAP_CELLS", {"first_cell": cell1, "second_cell": cell2}), step(3, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)
    add("pblock-create-resize-assign", "pblock_create_resize_assign", p(
        step(1, "SELECT_CRITICAL_CELLS", {"top_k": 1}, bind="targets"), step(2, "CREATE_PBLOCK", {"predefined_region_id": region1}, bind="box"),
        step(3, "RESIZE_PBLOCK", {"pblock_handle": {"ref": "box"}, "region_handle": region2, "mode": "ADD"}),
        step(4, "ASSIGN", {"target_handle": {"ref": "targets"}, "pblock_handle": {"ref": "box"}}),
        step(5, "UNROUTE"), step(6, "UNPLACE"), step(7, "PLACE", {"directive": "Default"}), step(8, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)
    add("hardblock-relocation", "hardblock_relocation", p(
        step(1, "UNROUTE"), step(2, "RELOCATE_HARDBLOCK", {"typed_target": hardblock, "destination": target_site}), step(3, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)
    add("property-cell-loc-fixed", "property_cell", p(
        step(1, "SET_CELL_LOC_FIXED", {"target_handle": cell1, "value": True}), step(2, "UNROUTE"), step(3, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)
    add("property-net-dont-touch", "property_net", p(
        step(1, "SET_NET_DONT_TOUCH", {"target_handle": net, "value": True}), step(2, "UNROUTE"), step(3, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)
    add("property-pblock-exclude-placement", "property_pblock", p(
        step(1, "CREATE_PBLOCK", {"predefined_region_id": region1}, bind="box"), step(2, "UNROUTE"), step(3, "UNPLACE"),
        step(4, "SET_PBLOCK_EXCLUDE_PLACEMENT", {"target_handle": {"ref": "box"}, "value": True}),
        step(5, "PLACE", {"directive": "Default"}), step(6, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)
    add("property-site-prohibit", "property_site", p(
        step(1, "UNROUTE"), step(2, "UNPLACE"), step(3, "SET_SITE_PROHIBIT", {"target_handle": target_site, "value": True}),
        step(4, "PLACE", {"directive": "Default"}), step(5, "ROUTE", {"directive": "Default"}),
    ), nonempty=True)

    x0, _x1, y0, _y1 = handles["geometry_bounds"]
    add("g-witness--spatial", "g_generated_spatial", g(
        step(1, "SELECT_WHERE", {"source_type": "cell", "predicate": {"op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True}, "top_k": 1}, bind="targets"),
        step(2, "CREATE_REGION", {"x_min": x0, "x_max": x0, "y_min": y0, "y_max": y0}, bind="region"),
        step(3, "ASSIGN_REGION", {"target_handle": {"ref": "targets"}, "region_handle": {"ref": "region"}}),
        step(4, "UNROUTE"), step(5, "UNPLACE"), step(6, "PLACE", {"directive": "Default"}), step(7, "ROUTE", {"directive": "Default"}),
    ), nonempty=True, g_witness="generated_predicate_spatial_geometry_assignment")
    add("g-witness--target-route", "g_generated_target_route", g(
        step(1, "SELECT_WHERE", {"source_type": "net", "predicate": {"op": "COMPARE", "field": "fanout", "operator": "GE", "value": 2}, "top_k": 1}, bind="target"),
        step(2, "ROUTE_NET", {"typed_net": {"ref": "target"}, "delay_mode": "DELAY"}),
    ), nonempty=True, g_witness="generated_predicate_target_specific_route")
    return rows


def compile_jobs(probe: Path, root: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    registry, handles = registry_from_probe(probe, root)
    context = CompileContext(
        registry, "ROUTED", tuple(handles["regions"]), tuple(handles["geometry_bounds"]),
        fanout_admitted=True, action_surface_version="v3",
    )
    compiled_rows = []
    for row in admission_jobs(handles):
        compiled = compile_candidate_v5(row["candidate"], context)
        compiled_rows.append(row | {
            "candidate_sha256": digest(row["candidate"]), "compiled_public_sha256": compiled.public_sha256,
            "compiled_private_plan_sha256": compiled.private_plan_sha256,
            "planned_heavy_calls": compiled.heavy_calls, "planned_light_mutations": compiled.light_mutations,
        })
    return compiled_rows, {"manifest": registry.public_manifest(), "counts": {key: len(value) for key, value in handles.items() if isinstance(value, list)}}


async def probe(args: argparse.Namespace) -> int:
    repository = assert_clean()
    root = root_record(args.roots)
    out = args.out / "target-probe"
    summary = await Admission(SimpleNamespace(mode="contract-probe", root=Path(root["path"]), out=out, python=args.python, vivado=args.vivado)).run()
    census = await asyncio.to_thread(rapidwright_net_census, Path(root["path"]))
    write_json(args.out / "private" / "rapidwright-net-census.json", census, private=True)
    result = {
        "schema_version": SCHEMA + ".probe", "repository": repository, "root": root, "summary": summary,
        "rapidwright_census_sha256": census["census_sha256"], "rapidwright_net_count": census["returned"],
        "accepted": bool(summary["accepted"]) and census["returned"] > 0,
    }
    write_json(args.out / "probe-evidence.json", result)
    print(json.dumps({"accepted": result["accepted"], "query_counts": summary["query_counts"]}, sort_keys=True))
    return 0 if result["accepted"] else 2


def freeze(args: argparse.Namespace) -> int:
    repository = assert_clean()
    root = root_record(args.roots)
    probe_evidence = json.loads((args.out / "probe-evidence.json").read_text())
    if not probe_evidence.get("accepted") or probe_evidence["repository"] != repository:
        raise RuntimeError("probe_repository_binding_failed")
    jobs, handle_evidence = compile_jobs(args.out / "target-probe", root)
    if args.only_job:
        requested = set(args.only_job)
        known = {row["job_id"] for row in jobs}
        if requested - known:
            raise RuntimeError("unknown_only_job:" + ",".join(sorted(requested - known)))
        jobs = [row for row in jobs if row["job_id"] in requested]
    planned_heavy = sum(row["planned_heavy_calls"] for row in jobs)
    if planned_heavy > MAX_HEAVY:
        raise RuntimeError("production_admission_heavy_cap_exceeded")
    core = {
        "schema_version": PLAN_SCHEMA, "continuation_identity": "wp014c-gate3-production-admission-v1",
        "repository": repository, "action_surface_registry_sha256": registry_sha256(), "root": root,
        "probe_evidence_sha256": digest(probe_evidence), "handle_evidence": handle_evidence,
        "diagnostic_predecessor_sha256": file_digest(args.predecessor_aggregate) if args.predecessor_aggregate else None,
        "caps": {"actual_heavy_calls": MAX_HEAVY, "parallel_jobs": min(args.parallel, MAX_CONCURRENCY), "sealed_holdout_accesses": 0},
        "jobs": jobs, "counts": {"jobs": len(jobs), "planned_heavy_calls": planned_heavy},
    }
    value = core | {"plan_sha256": digest(core)}
    write_json(args.out / "admission-plan.json", value)
    print(json.dumps(value["counts"] | {"plan_sha256": value["plan_sha256"]}, sort_keys=True))
    return 0


async def run(args: argparse.Namespace) -> int:
    repository = assert_clean()
    plan = json.loads((args.out / "admission-plan.json").read_text())
    if plan["repository"] != repository or plan["action_surface_registry_sha256"] != registry_sha256():
        raise RuntimeError("admission_plan_binding_failed")
    root = plan["root"]
    semaphore = asyncio.Semaphore(min(args.parallel, MAX_CONCURRENCY))

    async def one(row: Mapping[str, Any]) -> dict[str, Any]:
        result_path = args.out / "jobs" / str(row["job_id"]) / "result.json"
        if result_path.exists():
            return json.loads(result_path.read_text())
        async with semaphore:
            registry, handles = registry_from_probe(args.out / "target-probe", root)
            context = CompileContext(registry, "ROUTED", tuple(handles["regions"]), tuple(handles["geometry_bounds"]), fanout_admitted=True, action_surface_version="v3")
            compiled = compile_candidate_v5(row["candidate"], context)
            if compiled.public_sha256 != row["compiled_public_sha256"] or compiled.private_plan_sha256 != row["compiled_private_plan_sha256"]:
                raise RuntimeError("compiled_program_drift:" + str(row["job_id"]))
            result = await _execute_typed(
                compiled=compiled, registry=registry, root=Path(root["path"]),
                out=result_path.parent / "runtime", python=args.python, vivado=args.vivado,
            )
            accepted = (
                result.get("canonical_endpoint") == "VALID_COMPLETION" and result.get("root_unchanged") is True
                and result.get("child") is not None
                and (not row["nonempty_required"] or (int(result.get("selected_count") or 0) > 0 and int(result.get("transformed_count") or 0) > 0))
            )
            evidence = {
                "schema_version": SCHEMA + ".job", "job_id": row["job_id"], "mechanism_family": row["mechanism_family"],
                "candidate_sha256": row["candidate_sha256"], "compiled_public_sha256": compiled.public_sha256,
                "compiled_private_plan_sha256": compiled.private_plan_sha256, "planned_heavy_calls": compiled.heavy_calls,
                "planned_light_mutations": compiled.light_mutations, "g_witness": row["g_witness"], "result": result, "accepted": accepted,
            }
            write_json(result_path, evidence)
            print(json.dumps({"job_id": row["job_id"], "endpoint": result.get("canonical_endpoint"), "accepted": accepted}, sort_keys=True), flush=True)
            return evidence

    results = await asyncio.gather(*(one(row) for row in plan["jobs"]))
    actual_heavy = sum(int(row["result"].get("actual_heavy_calls") or 0) for row in results)
    if actual_heavy > MAX_HEAVY:
        raise RuntimeError("actual_heavy_cap_exceeded")
    endpoints = Counter(str(row["result"].get("canonical_endpoint")) for row in results)
    families: dict[str, dict[str, int]] = {}
    for row in results:
        family = families.setdefault(row["mechanism_family"], {"jobs": 0, "accepted": 0})
        family["jobs"] += 1; family["accepted"] += int(row["accepted"])
    accepted_g = sorted(row["g_witness"] for row in results if row["g_witness"] and row["accepted"])
    aggregate_core = {
        "schema_version": SCHEMA + ".aggregate", "plan_sha256": plan["plan_sha256"], "repository": repository,
        "counts": {"jobs": len(results), "accepted": sum(int(row["accepted"]) for row in results), "actual_heavy_calls": actual_heavy},
        "endpoint_counts": dict(sorted(endpoints.items())), "mechanism_families": families, "accepted_g_witnesses": accepted_g,
        "failed_jobs": [{"job_id": row["job_id"], "family": row["mechanism_family"], "endpoint": row["result"].get("canonical_endpoint"), "error": row["result"].get("error")} for row in results if not row["accepted"]],
        "root_unchanged": all(row["result"].get("root_unchanged") is True for row in results),
        "sealed_holdout_accesses": 0,
    }
    required_g = min(2, len({row["g_witness"] for row in plan["jobs"] if row["g_witness"]}))
    aggregate = aggregate_core | {"accepted": len(results) == sum(int(row["accepted"]) for row in results) and len(accepted_g) >= required_g, "aggregate_sha256": digest(aggregate_core)}
    write_json(args.out / "aggregate.json", aggregate)
    print(json.dumps({"accepted": aggregate["accepted"], **aggregate["counts"], "endpoint_counts": aggregate["endpoint_counts"]}, sort_keys=True))
    return 0 if aggregate["accepted"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    sub = value.add_subparsers(dest="command", required=True)
    for name in ("probe", "freeze", "run"):
        item = sub.add_parser(name, allow_abbrev=False)
        item.add_argument("--roots", type=Path, default=REPO / "configs/wp013/development-roots-v1.json")
        item.add_argument("--out", type=Path, required=True)
        item.add_argument("--python", type=Path, required=True)
        item.add_argument("--vivado", type=Path, required=True)
        item.add_argument("--parallel", type=int, default=MAX_CONCURRENCY)
        item.add_argument("--only-job", action="append", default=[])
        item.add_argument("--predecessor-aggregate", type=Path)
    return value


def main() -> int:
    args = parser().parse_args()
    if not 1 <= args.parallel <= MAX_CONCURRENCY:
        raise SystemExit("parallel_out_of_range")
    if args.command == "probe":
        return asyncio.run(probe(args))
    if args.command == "freeze":
        return freeze(args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
