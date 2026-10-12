#!/usr/bin/env python3
"""Run bounded development-only WP-013 P/G production admissions."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.backends.xilinx.legacy import parse_timing_summary_static  # noqa: E402
from optimizer_pipeline.dof.mutation_worker_binding import (  # noqa: E402
    build_mutation_worker_argv,
    build_mutation_worker_environment,
    mutation_worker_entry_path,
)
from optimizer_pipeline.dof.processes import OwnedLineProtocolSession, ProcessLimits  # noqa: E402
from optimizer_pipeline.dof.real_mutation import _verify_worker_attestation  # noqa: E402
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry  # noqa: E402
from optimizer_pipeline.dof.single_submission.language import CompileContext, CompiledProgram, compile_g, compile_p  # noqa: E402
from optimizer_pipeline.dof.vivado_mutation_worker import (  # noqa: E402
    READY_LINE,
    WORKER_TRANSPORT_PROTOCOL_VERSION,
    WORKER_VERSION,
    effective_vivado_environment_hash,
    parse_tcl_result,
    request_bytes,
    terminal_marker,
)


MODES = (
    "probe", "contract-probe", "hardblock-compatibility-probe", "hardblock-probe", "p-spatial", "g-spatial",
    "p-hardblock", "g-hardblock",
)
QUERY_RE = re.compile(rb"^DOF_WP013_QUERY_V1:([^:]+):(\d+):([^:]*):([^:]*)$")
COUNT_RE = re.compile(rb"^DOF_WP013_QUERY_COUNT_V1:([^:]+):(\d+)$")
RELOCATE_RE = re.compile(
    rb"^(?:DOF_WP01(?:3|4C)_RELOCATE_V1|DOF_WP014H_APPLY_ALLOCATION_V1):(\d+):(\d+)$"
)
ALLOCATION_EVIDENCE_RE = re.compile(
    rb"^DOF_WP015B_ALLOCATION_EVIDENCE_V1:([0-9a-f]{64}):([0-9a-f]{64}):([0-9a-f]{64}):([0-9a-f]{64})$"
)


def fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": size}


def b64(value: str | Path) -> str:
    return base64.b64encode(os.fsencode(value)).decode("ascii")


def decode(value: bytes) -> str:
    return base64.b64decode(value, validate=True).decode("utf-8")


def relocation_effect_count(output: bytes, expected: int) -> int:
    parsed = [
        match
        for line in output.splitlines()
        if (match := RELOCATE_RE.fullmatch(line.strip())) is not None
    ]
    if len(parsed) != 1 or int(parsed[0].group(1)) != expected:
        raise RuntimeError("relocation_effect_marker_invalid")
    moved = int(parsed[0].group(2))
    if moved != expected:
        raise RuntimeError("relocation_effect_count_mismatch")
    return moved


def allocation_effect_evidence(
    output: bytes, *, dispatch_pairs_sha256: str, target_identity_sha256: str,
    compatibility_relation_sha256: str, allocation_sha256: str,
) -> None:
    rows = [
        match for line in output.splitlines()
        if (match := ALLOCATION_EVIDENCE_RE.fullmatch(line.strip())) is not None
    ]
    expected = (
        dispatch_pairs_sha256, target_identity_sha256,
        compatibility_relation_sha256, allocation_sha256,
    )
    if len(rows) != 1 or tuple(value.decode() for value in rows[0].groups()) != expected:
        raise RuntimeError("wp015b_allocation_dispatch_evidence_mismatch")


def route_status_clean(output: bytes) -> bool:
    text = output.decode("utf-8", errors="replace")
    errors = re.search(r"#\s*of\s*nets\s*with\s*routing\s*errors[^:]*:\s*(\d+)", text, re.I)
    unrouted = re.search(r"#\s*of\s*unrouted\s*nets[^:]*:\s*(\d+)", text, re.I)
    if errors is not None and int(errors.group(1)) != 0:
        return False
    if unrouted is not None and int(unrouted.group(1)) != 0:
        return False
    return (errors is not None or unrouted is not None) and "fully routed" in text.lower()


def write_json(path: Path, value: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    if private:
        path.chmod(0o600)


class Admission:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = args.root.resolve(strict=True)
        self.out = args.out.resolve()
        self.child = self.out / "child.dcp"
        self.private = self.out / "private"
        self.home = self.private / "vivado-home"
        self.root_before = fingerprint(self.root)
        secret = hashlib.sha256(f"wp013:{args.mode}:{self.root_before['sha256']}".encode()).digest()
        self.registry = TrialHandleRegistry(trial_id=f"wp013-{args.mode}", root_sha256=self.root_before["sha256"], secret=secret)
        self.trace: list[dict[str, Any]] = []
        self.raw_queries: dict[str, list[dict[str, str]]] = {}
        self.compatibility_queries: list[dict[str, Any]] = []
        self.raw_call_outputs: list[dict[str, str]] = []
        self.counter = 0
        self.session: OwnedLineProtocolSession | None = None
        self.process: dict[str, Any] | None = None

    async def call(self, operation: str, arguments: Mapping[str, Any] | None = None, *, timeout: float = 300.0) -> bytes:
        if self.session is None:
            raise RuntimeError("worker_not_started")
        token = f"wp013-{self.counter:03d}"
        self.counter += 1
        started = time.monotonic()
        result = await self.session.call(
            request_bytes(operation, token, timeout_seconds=timeout, arguments=dict(arguments or {})),
            terminal_marker=terminal_marker(token),
            timeout_seconds=timeout,
        )
        try:
            status, output = parse_tcl_result(token, result.stdout.data)
        except Exception as exc:
            # Preserve the bounded protocol payload that triggered a parser
            # failure.  Without this row, a failed fixed-Tcl query leaves only
            # the exception class and makes real-tool contract repair guesswork.
            self.raw_call_outputs.append(
                {
                    "operation": operation,
                    "status": "PROTOCOL_PARSE_FAILURE",
                    "error": f"{type(exc).__name__}:{exc}",
                    "output_b64": base64.b64encode(result.stdout.data).decode("ascii"),
                }
            )
            raise
        self.raw_call_outputs.append(
            {
                "operation": operation,
                "status": str(status),
                "output_b64": base64.b64encode(output).decode("ascii"),
            }
        )
        self.trace.append({
            "index": len(self.trace),
            "worker_dispatch_id": token,
            "operation": operation,
            "arguments_sha256": hashlib.sha256(json.dumps(dict(arguments or {}), sort_keys=True).encode()).hexdigest(),
            "status": status,
            "duration_seconds": time.monotonic() - started,
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "output_size_bytes": len(output),
        })
        if status != 0:
            raise RuntimeError(f"vivado_operation_failed:{operation}")
        return output

    async def query(
        self, kind: str, *, top_k: int, resource: str = "", target: str = "",
        evidence_key: str | None = None,
    ) -> list[dict[str, str]]:
        output = await self.call(
            "vivado.wp013_query_objects",
            {"query_kind": kind, "top_k": top_k, "resource_type": resource, "target_cell_b64": b64(target) if target else ""},
            timeout=300.0,
        )
        rows: list[dict[str, str]] = []
        count = None
        for line in output.splitlines():
            match = QUERY_RE.fullmatch(line.strip())
            if match:
                row_kind = match.group(1).decode()
                if row_kind != kind:
                    raise RuntimeError("query_kind_marker_mismatch")
                rows.append({"index": match.group(2).decode(), "identity": decode(match.group(3)), "attribute": decode(match.group(4))})
            count_match = COUNT_RE.fullmatch(line.strip())
            if count_match and count_match.group(1).decode() == kind:
                count = int(count_match.group(2))
        if count is None or count != len(rows):
            raise RuntimeError("query_count_marker_mismatch")
        self.raw_queries[evidence_key or kind] = rows
        return rows

    def register_rows(self, kind: str, rows: list[dict[str, str]], *, resource: str | None = None) -> list[str]:
        handles = []
        for row in rows:
            attrs: dict[str, Any] = {}
            object_type = {
                "critical_cells": "cell", "critical_cell_locations": "cell",
                "timing_paths": "path", "critical_hardblocks": "hardblock",
                "device_regions": "region", "compatible_sites": "site",
                "high_fanout_nets": "net",
            }[kind]
            if kind in {"critical_cells", "critical_hardblocks"}:
                attrs.update({"path_member": True, "path_rank": int(row["index"]) + 1})
            if kind == "timing_paths":
                parts = row["attribute"].split("\x1f")
                if len(parts) != 4:
                    raise RuntimeError("timing_path_attribute_shape_invalid")
                slack, delay, startpoint, endpoint = parts
                start_handle = self.registry.register(
                    object_type="pin", canonical_identity=startpoint,
                    attributes={"path_member": True, "row_semantics": "timing_path_pin"},
                )
                end_handle = self.registry.register(
                    object_type="pin", canonical_identity=endpoint,
                    attributes={"path_member": True, "row_semantics": "timing_path_pin"},
                )
                attrs.update(
                    {
                        "path_rank": int(row["index"]) + 1,
                        "slack_ns": float(slack),
                        "delay_ns": float(delay),
                        "startpoint_handle": start_handle,
                        "endpoint_handle": end_handle,
                    }
                )
            elif kind == "critical_cell_locations":
                parts = row["attribute"].split("\x1f")
                if len(parts) != 3:
                    raise RuntimeError("critical_cell_location_attribute_shape_invalid")
                ref_name, site_name, region_name = parts
                site_handle = self.registry.register(
                    object_type="location_site", canonical_identity=site_name or "UNPLACED:" + row["identity"],
                    attributes={"row_semantics": "observed_cell_location"},
                )
                region_handle = self.registry.register(
                    object_type="location_region", canonical_identity=region_name or "UNKNOWN_REGION:" + row["identity"],
                    attributes={"row_semantics": "observed_cell_location"},
                )
                attrs.update(
                    {
                        "path_member": True,
                        "path_rank": int(row["index"]) + 1,
                        "resource_type": ref_name,
                        "location_status": "PLACED" if site_name else "UNPLACED",
                        "location_site_handle": site_handle,
                        "location_region_handle": region_handle,
                    }
                )
            if kind == "critical_hardblocks":
                observed = row["attribute"].upper()
                inferred = "DSP" if "DSP" in observed else "URAM" if "URAM" in observed else "BRAM" if "RAMB" in observed else resource or "ANY"
                attrs["resource_type"] = inferred
            elif kind == "high_fanout_nets":
                attrs["fanout"] = int(row["attribute"])
            elif kind == "device_regions":
                match = re.fullmatch(r"(?:CLOCKREGION_)?X(\d+)Y(\d+)", row["identity"])
                if match is None:
                    continue
                attrs["bounds"] = [int(match.group(1)), int(match.group(1)), int(match.group(2)), int(match.group(2))]
            elif kind == "compatible_sites":
                attrs["compatible_resource_types"] = [resource or "ANY"]
                attrs["site_class"] = row["attribute"]
            handle = self.registry.register(object_type=object_type, canonical_identity=row["identity"], attributes=attrs)
            handles.append(handle)
        return handles

    async def build_program(self) -> CompiledProgram | None:
        if self.args.mode == "probe":
            self.register_rows("critical_cells", await self.query("critical_cells", top_k=8))
            self.register_rows("device_regions", await self.query("device_regions", top_k=8))
            self.register_rows("high_fanout_nets", await self.query("high_fanout_nets", top_k=8))
            return None
        if self.args.mode == "contract-probe":
            self.register_rows("timing_paths", await self.query("timing_paths", top_k=8))
            self.register_rows(
                "critical_cell_locations",
                await self.query("critical_cell_locations", top_k=8),
            )
            self.register_rows("device_regions", await self.query("device_regions", top_k=8))
            if self.args.omit_vivado_fanout:
                # WP-014C binds high-fanout evidence to the direct
                # RapidWright WIRE census.  Preserve an explicit empty legacy
                # row rather than running the superseded full-design Vivado
                # traversal or silently reconstructing it after selection.
                self.raw_queries["high_fanout_nets"] = []
            else:
                self.register_rows(
                    "high_fanout_nets",
                    await self.query("high_fanout_nets", top_k=8),
                )
            hardblocks = self.register_rows(
                "critical_hardblocks",
                await self.query("critical_hardblocks", top_k=8, resource="ANY"),
            )
            if hardblocks:
                target = self.registry.resolve(hardblocks[0], expected_type="hardblock")
                resource = str(target.attributes["resource_type"])
                self.register_rows(
                    "compatible_sites",
                    await self.query(
                        "compatible_sites", top_k=8, target=target.canonical_identity
                    ),
                    resource=resource,
                )
            return None
        if self.args.mode == "hardblock-compatibility-probe":
            hardblock_rows = await self.query(
                "critical_hardblocks", top_k=8, resource="ANY",
            )
            hardblocks = self.register_rows("critical_hardblocks", hardblock_rows)
            for index, handle in enumerate(hardblocks):
                target = self.registry.resolve(handle, expected_type="hardblock")
                resource = str(target.attributes["resource_type"])
                evidence_key = f"compatible_sites_target_{index:02d}"
                site_rows = await self.query(
                    "compatible_sites", top_k=16,
                    target=target.canonical_identity, evidence_key=evidence_key,
                )
                self.compatibility_queries.append({
                    "target_index": index,
                    "target_canonical_identity": target.canonical_identity,
                    "target_identity_sha256": hashlib.sha256(
                        target.canonical_identity.encode("utf-8")
                    ).hexdigest(),
                    "target_handle": handle,
                    "resource_type": resource,
                    "evidence_key": evidence_key,
                    "query_result_sha256": hashlib.sha256(
                        json.dumps(site_rows, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "site_count": len(site_rows),
                })
            return None
        if self.args.mode.endswith("spatial"):
            cells = self.register_rows("critical_cells", await self.query("critical_cells", top_k=8))
            regions = self.register_rows("device_regions", await self.query("device_regions", top_k=8))
            if not cells or not regions:
                raise RuntimeError("spatial_no_target")
            bounds = [self.registry.resolve(handle, expected_type="region").attributes["bounds"] for handle in regions]
            max_x = max(row[1] for row in bounds); max_y = max(row[3] for row in bounds)
            ctx = CompileContext(self.registry, "ROUTED", tuple(regions), (0, max_x, 0, max_y))
            if self.args.mode == "p-spatial":
                program = {"language": "P", "steps": [
                    {"step_id": "s1", "op": "SELECT_CRITICAL_CELLS", "args": {"top_k": 8}, "bind": "targets"},
                    {"step_id": "s2", "op": "CREATE_PBLOCK", "args": {"predefined_region_id": regions[0]}, "bind": "box"},
                    {"step_id": "s3", "op": "ASSIGN", "args": {"target_handle": {"ref": "targets"}, "pblock_handle": {"ref": "box"}}},
                    {"step_id": "s4", "op": "UNROUTE", "args": {}},
                    {"step_id": "s5", "op": "UNPLACE", "args": {}},
                    {"step_id": "s6", "op": "PLACE", "args": {"directive": "Default"}},
                    {"step_id": "s7", "op": "ROUTE", "args": {"directive": "Default"}},
                ]}
                return compile_p(program, ctx)
            x0, _, y0, _ = bounds[0]
            predicate = {"op": "AND", "children": [
                {"op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True},
                {"op": "COMPARE", "field": "path_rank", "operator": "LE", "value": 8},
            ]}
            program = {"language": "G", "statements": [
                {"statement_id": "g1", "node": "SELECT_WHERE", "args": {"source_type": "cell", "predicate": predicate, "top_k": 8}, "bind": "targets"},
                {"statement_id": "g2", "node": "CREATE_REGION", "args": {"x_min": x0, "x_max": x0, "y_min": y0, "y_max": y0}, "bind": "box"},
                {"statement_id": "g3", "node": "ASSIGN_REGION", "args": {"target_handle": {"ref": "targets"}, "region_handle": {"ref": "box"}}},
                {"statement_id": "g4", "node": "UNROUTE", "args": {}},
                {"statement_id": "g5", "node": "UNPLACE", "args": {}},
                {"statement_id": "g6", "node": "PLACE", "args": {"directive": "Default"}},
                {"statement_id": "g7", "node": "ROUTE", "args": {"directive": "Default"}},
            ]}
            return compile_g(program, ctx)
        hardblocks = self.register_rows("critical_hardblocks", await self.query("critical_hardblocks", top_k=8, resource="ANY"))
        if not hardblocks:
            raise RuntimeError("hardblock_no_critical_target")
        target = self.registry.resolve(hardblocks[0], expected_type="hardblock")
        resource = str(target.attributes["resource_type"])
        sites = self.register_rows("compatible_sites", await self.query("compatible_sites", top_k=8, target=target.canonical_identity), resource=resource)
        if not sites:
            raise RuntimeError("hardblock_no_compatible_site")
        if self.args.mode == "hardblock-probe":
            return None
        ctx = CompileContext(self.registry, "ROUTED", (), (0, 0, 0, 0))
        if self.args.mode == "p-hardblock":
            return compile_p({"language": "P", "steps": [
                {"step_id": "s1", "op": "SELECT_CRITICAL_HARDBLOCKS", "args": {"type": resource, "top_k": 1}, "bind": "target"},
                {"step_id": "s2", "op": "RELOCATE_HARDBLOCK", "args": {"typed_target": {"ref": "target"}, "destination": sites[0]}},
                {"step_id": "s3", "op": "ROUTE", "args": {"directive": "Default"}},
            ]}, ctx)
        return compile_g({"language": "G", "statements": [
            {"statement_id": "g1", "node": "SELECT_WHERE", "args": {"source_type": "hardblock", "predicate": {"op": "COMPARE", "field": "resource_type", "operator": "EQ", "value": resource}, "top_k": 1}, "bind": "target"},
            {"statement_id": "g2", "node": "RELOCATE_HARDBLOCK", "args": {"typed_target": {"ref": "target"}, "candidate_sites": sites, "site_index": min(1, len(sites) - 1)}},
            {"statement_id": "g3", "node": "ROUTE", "args": {"directive": "Default"}},
        ]}, ctx)

    async def execute(self, compiled: CompiledProgram) -> dict[str, int]:
        pblock_ids: dict[str, str] = {}
        actual = {"selected": 0, "transformed": 0, "heavy": 0, "light": 0}
        for index, step in enumerate(compiled.host_steps):
            op = step.operation
            output = b""
            if op == "vivado.create_pblock":
                region_handle = str(step.public_args["region"])
                region = self.registry.resolve(region_handle, expected_type="region")
                bounds = region.attributes.get("bounds")
                region_name = region.canonical_identity if re.fullmatch(r"(?:CLOCKREGION_)?X\d+Y\d+", region.canonical_identity) else f"X{bounds[0]}Y{bounds[2]}"
                pblock_handle = next(obj.handle for obj in self.registry.objects(object_type="pblock") if obj.attributes.get("region_handle") == region_handle)
                pblock_id = f"wp013_pb{index}"
                pblock_ids[pblock_handle] = pblock_id
                output = await self.call("vivado.wp013_create_pblock", {"pblock_id": pblock_id, "region_name_b64": b64(region_name)})
                actual["transformed"] += 1
            elif op == "vivado.assign_pblock":
                targets = [self.registry.resolve(handle).canonical_identity for handle in step.public_args["targets"]]
                output = await self.call("vivado.wp013_assign_pblock", {"pblock_id": pblock_ids[str(step.public_args["pblock"])], "cell_names_b64": [b64(name) for name in targets]})
                actual["selected"] += len(targets); actual["transformed"] += len(targets)
            elif op == "vivado.relocate_hardblock":
                targets = [self.registry.resolve(handle, expected_type="hardblock").canonical_identity for handle in step.public_args["targets"]]
                site = self.registry.resolve(str(step.public_args["destination"]), expected_type="site").canonical_identity
                output = await self.call("vivado.wp013_relocate_hardblocks", {"cell_names_b64": [b64(name) for name in targets], "site_names_b64": [b64(site) for _ in targets]}, timeout=900.0)
                moved = relocation_effect_count(output, len(targets))
                actual["selected"] += len(targets); actual["transformed"] += moved
            elif op == "vivado.unroute": output = await self.call("vivado.unroute_all", timeout=900.0)
            elif op == "vivado.unplace": output = await self.call("vivado.unplace_all", timeout=900.0)
            elif op == "vivado.place": output = await self.call("vivado.place_design", {"directive": str(step.public_args.get("directive", "Default"))}, timeout=900.0)
            elif op == "vivado.route": output = await self.call("vivado.route_design", {"directive": str(step.public_args.get("directive", "Default"))}, timeout=900.0)
            else: raise RuntimeError(f"admission_dispatch_unsupported:{op}")
            if step.event_class == "IMPLEMENTATION_HEAVY": actual["heavy"] += 1
            else: actual["light"] += 1
            if op == "vivado.assign_pblock" and b"DOF_WP013_ASSIGN_PBLOCK_V1" not in output: raise RuntimeError("spatial_effect_marker_missing")
            if op == "vivado.relocate_hardblock" and b"DOF_WP013_RELOCATE_V1" not in output: raise RuntimeError("relocation_effect_marker_missing")
        if actual["heavy"] != compiled.heavy_calls or actual["light"] != compiled.light_mutations:
            raise RuntimeError("compiled_runtime_call_accounting_mismatch")
        return actual

    async def run(self) -> dict[str, Any]:
        if self.out.exists(): raise FileExistsError(self.out)
        self.home.mkdir(parents=True, mode=0o700)
        limits = ProcessLimits(initialization_timeout_seconds=120.0, whole_run_timeout_seconds=7200.0, graceful_close_seconds=20.0, terminate_grace_seconds=5.0, kill_grace_seconds=10.0, cleanup_timeout_seconds=40.0, output_limit_bytes=8 * 1024 * 1024)
        environment = build_mutation_worker_environment(os.environ)
        argv = build_mutation_worker_argv(python_exec=self.args.python, worker_entry=mutation_worker_entry_path(), vivado_exec=self.args.vivado, initialization_timeout_seconds=120.0, run_workspace=self.out, isolated_home=self.home)
        close = request_bytes("worker.exit", "cleanup", timeout_seconds=10.0)
        self.session = OwnedLineProtocolSession(argv, ready_line=READY_LINE, close_command=close, limits=limits, cwd=self.out, env=environment)
        started = time.monotonic()
        error = None
        compiled = None
        actual = None
        pre_timing = post_timing = route = None
        try:
            await self.session.start()
            _verify_worker_attestation(self.session.startup_output.data, expected_vivado="2025.1", expected_python=platform.python_version(), expected_environment_hash=effective_vivado_environment_hash(environment, self.home), expected_worker=WORKER_VERSION, expected_protocol=WORKER_TRANSPORT_PROTOCOL_VERSION)
            await self.call("vivado.open_checkpoint", {"dcp_path_b64": b64(self.root)}, timeout=900.0)
            if self.args.mode != "hardblock-compatibility-probe":
                pre_timing = await self.call("vivado.report_timing_summary", timeout=300.0)
            compiled = await self.build_program()
            if compiled is not None:
                actual = await self.execute(compiled)
                await self.call("vivado.write_checkpoint", {"dcp_path_b64": b64(self.child)}, timeout=900.0)
                post_timing = await self.call("vivado.report_timing_summary", timeout=300.0)
                route = await self.call("vivado.report_route_status", timeout=300.0)
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
        finally:
            if self.session is not None:
                try:
                    result = await self.session.close()
                    self.process = result.to_dict(include_output_text=False)
                except Exception as exc:
                    if error is None:
                        error = f"worker_close:{type(exc).__name__}:{exc}"
                    result = self.session.result
                    if result is not None:
                        self.process = result.to_dict(include_output_text=False)
        root_after = fingerprint(self.root)
        child = fingerprint(self.child) if self.child.exists() else None
        valid_route = bool(route and route_status_clean(route))
        summary = {
            "schema_version": "dof.wp013-production-admission.v1",
            "mode": self.args.mode,
            "technical_non_scientific": True,
            "root": self.root_before,
            "root_after": root_after,
            "root_unchanged": root_after == self.root_before,
            "child": child,
            "compiled": None if compiled is None else {"language": compiled.language, "public_sha256": compiled.public_sha256, "private_plan_sha256": compiled.private_plan_sha256, "heavy_calls": compiled.heavy_calls, "light_mutations": compiled.light_mutations, "selected_count": compiled.selected_count, "transformed_count": compiled.transformed_count},
            "actual": actual,
            "query_counts": {key: len(value) for key, value in self.raw_queries.items()},
            "compatibility_target_query_count": len(self.compatibility_queries),
            "handle_manifest": self.registry.public_manifest(),
            "trace": self.trace,
            "process": self.process,
            "error": error,
            "accepted": error is None and root_after == self.root_before and self.process is not None and self.process["cleanup_ok"] is True and self.process["endpoint"] == "SUCCEEDED" and (compiled is None or (child is not None and valid_route and actual is not None and actual["transformed"] > 0)),
            "elapsed_seconds": time.monotonic() - started,
        }
        if self.args.mode != "hardblock-compatibility-probe":
            summary.update({
                "pre_timing": None if pre_timing is None else parse_timing_summary_static(pre_timing.decode(errors="replace")),
                "post_timing": None if post_timing is None else parse_timing_summary_static(post_timing.decode(errors="replace")),
                "route_valid": valid_route if compiled is not None else None,
            })
        write_json(self.private / "resolved-objects.json", self.raw_queries, private=True)
        write_json(
            self.private / "hardblock-compatibility-queries.json",
            {"targets": self.compatibility_queries}, private=True,
        )
        write_json(self.private / "call-outputs.json", self.raw_call_outputs, private=True)
        write_json(self.out / "admission-summary.json", summary)
        return summary


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--mode", choices=MODES, required=True)
    value.add_argument("--root", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--vivado", type=Path, required=True)
    value.add_argument("--omit-vivado-fanout", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    summary = asyncio.run(Admission(args).run())
    print(json.dumps({key: summary[key] for key in ("mode", "accepted", "error", "query_counts", "actual", "elapsed_seconds")}, sort_keys=True))
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
