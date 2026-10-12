"""Execution helpers extracted from the historical WP-015 dispatcher.

The compiled dispatcher owns typed operation dispatch. Worker process ownership,
environment policy, argv binding, and the isolated Vivado worker remain in the
existing transport modules; those are retained transport components, not study
planning code. Macro execution still has a script-backed flow-matrix fallback.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import time
from types import SimpleNamespace
from typing import Any, Mapping

from optimizer_pipeline.backends.xilinx.legacy import (
    FPGAOptimizerTest,
    parse_timing_summary_static,
)
from optimizer_pipeline.dof.hardblock_compatibility import validate_allocation
from optimizer_pipeline.dof.processes import (
    OwnedLineProtocolSession,
    OwnedProcessCancelled,
    OwnedProcessRunner,
    ProcessLimits,
)
from optimizer_pipeline.dof.mutation_worker_binding import (
    build_mutation_worker_argv,
    build_mutation_worker_environment,
    mutation_worker_entry_path,
)
from optimizer_pipeline.dof.vivado_mutation_worker import (
    EFFECTIVE_ENV_PREFIX,
    INIT_POLICY_LINE,
    LAUNCHED_LINE,
    PYTHON_VERSION_PREFIX,
    READY_LINE,
    STARTED_LINE,
    VERSION_PREFIX,
    WORKER_PROTOCOL_PREFIX,
    WORKER_VERSION_PREFIX,
    WORKER_TRANSPORT_PROTOCOL_VERSION,
    WORKER_VERSION,
    effective_vivado_environment_hash,
    parse_tcl_result,
    request_bytes,
    terminal_marker,
)

REPO = Path(__file__).resolve().parents[3]

RELOCATE_RE = re.compile(
    b"^(?:DOF_WP01(?:3|4C)_RELOCATE_V1|DOF_WP014H_APPLY_ALLOCATION_V1):(\\d+):(\\d+)$"
)

ALLOCATION_EVIDENCE_RE = re.compile(
    b"^DOF_WP015B_ALLOCATION_EVIDENCE_V1:([0-9a-f]{64}):([0-9a-f]{64}):([0-9a-f]{64}):([0-9a-f]{64})$"
)


def instrumentation_markers(outputs):
    """Project existing worker instrumentation, without exporting raw output."""
    pattern = re.compile(r'DOF_WP014[CH]_[A-Z0-9_]+_V[0-9]+:[^\r\n\"]+')
    markers = set()
    for row in outputs:
        if isinstance(row, Mapping) and isinstance(row.get("output_b64"), str):
            decoded = base64.b64decode(row["output_b64"]).decode(errors="replace")
            markers.update(pattern.findall(decoded))
    return sorted(marker for marker in markers if "$__dof_" not in marker)


class RealMutationExecutionError(RuntimeError):
    def __init__(self, endpoint: str, reason: str) -> None:
        self.endpoint = endpoint
        self.reason = reason
        super().__init__(reason)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_atomic(path: Path, value: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if private:
        path.chmod(384)


def b64(value: str | Path) -> str:
    return base64.b64encode(os.fsencode(value)).decode("ascii")


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
    output: bytes,
    *,
    dispatch_pairs_sha256: str,
    target_identity_sha256: str,
    compatibility_relation_sha256: str,
    allocation_sha256: str,
) -> None:
    rows = [
        match
        for line in output.splitlines()
        if (match := ALLOCATION_EVIDENCE_RE.fullmatch(line.strip())) is not None
    ]
    expected = (
        dispatch_pairs_sha256,
        target_identity_sha256,
        compatibility_relation_sha256,
        allocation_sha256,
    )
    if (
        len(rows) != 1
        or tuple((value.decode() for value in rows[0].groups())) != expected
    ):
        raise RuntimeError("wp015b_allocation_dispatch_evidence_mismatch")


def route_status_clean(output: bytes) -> bool:
    text = output.decode("utf-8", errors="replace")
    errors = re.search(
        "#\\s*of\\s*nets\\s*with\\s*routing\\s*errors[^:]*:\\s*(\\d+)", text, re.I
    )
    unrouted = re.search("#\\s*of\\s*unrouted\\s*nets[^:]*:\\s*(\\d+)", text, re.I)
    if errors is not None and int(errors.group(1)) != 0:
        return False
    if unrouted is not None and int(unrouted.group(1)) != 0:
        return False
    return (
        errors is not None or unrouted is not None
    ) and "fully routed" in text.lower()


class _WorkerClient:
    def __init__(self, args: Any) -> None:
        self.args = args
        self.root = args.root.resolve(strict=True)
        self.out = args.out.resolve()
        self.child = self.out / "child.dcp"
        self.private = self.out / "private"
        self.home = self.private / "vivado-home"
        self.trace: list[dict[str, Any]] = []
        self.raw_call_outputs: list[dict[str, str]] = []
        self.counter = 0
        self.session: OwnedLineProtocolSession | None = None

    async def call(
        self,
        operation: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float = 300.0,
    ) -> bytes:
        if self.session is None:
            raise RuntimeError("worker_not_started")
        token = f"wp013-{self.counter:03d}"
        self.counter += 1
        started = time.monotonic()
        result = await self.session.call(
            request_bytes(
                operation,
                token,
                timeout_seconds=timeout,
                arguments=dict(arguments or {}),
            ),
            terminal_marker=terminal_marker(token),
            timeout_seconds=timeout,
        )
        try:
            status, output = parse_tcl_result(token, result.stdout.data)
        except Exception as exc:
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
        self.trace.append(
            {
                "index": len(self.trace),
                "worker_dispatch_id": token,
                "operation": operation,
                "arguments_sha256": hashlib.sha256(
                    json.dumps(dict(arguments or {}), sort_keys=True).encode()
                ).hexdigest(),
                "status": status,
                "duration_seconds": time.monotonic() - started,
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "output_size_bytes": len(output),
            }
        )
        if status != 0:
            raise RuntimeError(f"vivado_operation_failed:{operation}")
        return output


def _verify_worker_attestation(
    raw: bytes,
    *,
    expected_vivado: str,
    expected_python: str,
    expected_environment_hash: str,
    expected_worker: str,
    expected_protocol: str,
) -> dict[str, Any]:
    lines = [line.rstrip(b"\r") for line in raw.splitlines()]
    for marker in (INIT_POLICY_LINE, LAUNCHED_LINE, STARTED_LINE, READY_LINE):
        if lines.count(marker.encode("ascii")) != 1:
            raise RealMutationExecutionError(
                "TOOL_INITIALIZATION_FAILURE", "startup_marker_cardinality"
            )

    def value(prefix: str) -> str | None:
        matches = [
            line[len(prefix) :].decode("ascii", "replace")
            for line in lines
            if line.startswith(prefix.encode("ascii"))
        ]
        return matches[0] if len(matches) == 1 else None

    vivado = value(VERSION_PREFIX)
    python = value(PYTHON_VERSION_PREFIX)
    environment = value(EFFECTIVE_ENV_PREFIX)
    worker = value(WORKER_VERSION_PREFIX)
    protocol = value(WORKER_PROTOCOL_PREFIX)
    if (vivado, python, environment, worker, protocol) != (
        expected_vivado,
        expected_python,
        expected_environment_hash,
        expected_worker,
        expected_protocol,
    ):
        raise RealMutationExecutionError(
            "TOOL_INITIALIZATION_FAILURE", "startup_attestation_mismatch"
        )
    return {
        "status": "PASS",
        "vivado_version": vivado,
        "python_version": python,
        "effective_environment_sha256": environment,
        "worker_version": worker,
        "transport_protocol_version": protocol,
    }


def _macro_attempt(job_dir: Path, flow: str) -> dict[str, Any]:
    log_path = job_dir / "logs" / f"01_{flow}.log"
    call_counts = {"place_design": 0, "route_design": 0, "phys_opt_design": 0}
    if log_path.exists():
        pattern = re.compile(
            "\\[VIVADO\\] Calling (place_design|route_design|phys_opt_design) with args:"
        )
        for operation in pattern.findall(log_path.read_text(errors="replace")):
            call_counts[operation] += 1
    accounting = {
        "actual_heavy_calls": sum(call_counts.values()),
        "actual_heavy_call_counts": call_counts,
        "heavy_accounting_source": "exact host-call start markers in immutable flow log",
    }
    summary_path = job_dir / "flow_matrix_summary.json"
    if not summary_path.exists():
        return accounting | {
            "canonical_endpoint": "TOOL_FAILURE",
            "missing_reason": "flow_matrix_summary_missing",
        }
    outer = read_object(summary_path)["flows"][0]
    run_db = job_dir / "runs" / f"01_{flow}" / "run.db"
    output_dcp = Path(str(outer.get("output_dcp") or ""))
    result: dict[str, Any] = {
        "outer_status": outer.get("status"),
        "outer_exit_code": outer.get("exit_code"),
        "outer_duration_seconds": outer.get("duration_seconds"),
        "child_path": str(output_dcp) if output_dcp.is_file() else None,
        "child": {
            "sha256": file_digest(output_dcp),
            "size_bytes": output_dcp.stat().st_size,
        }
        if output_dcp.is_file()
        else None,
    }
    if not run_db.exists():
        return (
            result
            | accounting
            | {"canonical_endpoint": "TOOL_FAILURE", "missing_reason": "run_db_missing"}
        )
    connection = sqlite3.connect(run_db)
    connection.row_factory = sqlite3.Row
    try:
        attempt = connection.execute(
            "SELECT * FROM attempts ORDER BY scheduled_at DESC LIMIT 1"
        ).fetchone()
        if attempt is None:
            return (
                result
                | accounting
                | {
                    "canonical_endpoint": "TOOL_FAILURE",
                    "missing_reason": "attempt_missing",
                }
            )
        validation = connection.execute(
            "SELECT * FROM validations WHERE attempt_id=? ORDER BY checked_at DESC LIMIT 1",
            (attempt["attempt_id"],),
        ).fetchone()
        outcome = connection.execute(
            "SELECT * FROM outcomes WHERE attempt_id=? ORDER BY finished_at DESC LIMIT 1",
            (attempt["attempt_id"],),
        ).fetchone()
        metrics = json.loads(outcome["metrics_json"] or "{}") if outcome else {}
        validation_status = validation["status"] if validation else None
        endpoint = {
            "CLEAN": "VALID_COMPLETION",
            "VALID": "VALID_COMPLETION",
            "INVALID": "INVALID_ARTIFACT",
            "TOOL_FAILED": "TOOL_FAILURE",
            "TIMEOUT": "TOOL_TIMEOUT",
        }.get(validation_status, "TOOL_FAILURE")
        return (
            result
            | accounting
            | {
                "canonical_endpoint": endpoint,
                "attempt_id": attempt["attempt_id"],
                "validation_status": validation_status,
                "outcome_status": outcome["status"] if outcome else None,
                "error_type": outcome["error_type"] if outcome else None,
                "output_artifact_id": outcome["output_artifact_id"]
                if outcome
                else None,
                "initial_wns_ns": metrics.get("initial_wns_ns"),
                "final_wns_ns": metrics.get("final_wns_ns"),
                "initial_fmax_mhz": metrics.get("initial_fmax_mhz"),
                "final_fmax_mhz": metrics.get("final_fmax_mhz"),
                "fmax_improvement_mhz": metrics.get("fmax_improvement_mhz"),
                "duration_seconds": metrics.get("duration_seconds"),
            }
        )
    finally:
        connection.close()


async def _execute_macro(
    *, root: Path, flow: str, out: Path, python: Path
) -> dict[str, Any]:
    command = (
        str(python),
        str(REPO / "scripts" / "run_flow_matrix.py"),
        str(root),
        "--flows",
        flow,
        "--out",
        str(out),
        "--run-mode",
        "research-dev",
        "--skip-functional-validation",
    )
    started = time.monotonic()
    limits = ProcessLimits(
        initialization_timeout_seconds=60.0,
        whole_run_timeout_seconds=7200.0,
        graceful_close_seconds=20.0,
        terminate_grace_seconds=10.0,
        kill_grace_seconds=15.0,
        cleanup_timeout_seconds=60.0,
        output_limit_bytes=1024 * 1024,
    )
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    try:
        process = await OwnedProcessRunner(limits).run(
            command, cwd=REPO, env=environment
        )
    except OwnedProcessCancelled as exc:
        process = exc.result
    attempted = _macro_attempt(out, flow)
    if process.endpoint.value == "TIMED_OUT":
        attempted["canonical_endpoint"] = "TOOL_TIMEOUT"
    elif (
        not process.cleanup_ok
        and attempted.get("canonical_endpoint") == "VALID_COMPLETION"
    ):
        attempted["canonical_endpoint"] = "TOOL_FAILURE"
    return attempted | {
        "process": process.to_dict(include_output_text=False),
        "wall_seconds": time.monotonic() - started,
    }


def _typed_supported(compiled: Any) -> tuple[bool, list[str]]:
    supported = {
        "vivado.create_pblock",
        "vivado.assign_pblock",
        "vivado.remove_pblock",
        "vivado.relocate_hardblock",
        "vivado.unroute",
        "vivado.unplace",
        "vivado.place",
        "vivado.route",
        "vivado.phys_opt",
        "vivado.wp014c_create_pblock",
        "vivado.wp014c_resize_pblock",
        "vivado.wp014c_assign_pblock",
        "vivado.wp014c_unassign_pblock",
        "vivado.wp014c_delete_pblock",
        "vivado.wp014c_relocate_hardblocks",
        "vivado.wp014c_replicate_net",
        "vivado.wp014c_route_net",
        "vivado.wp014c_place_cell",
        "vivado.wp014c_unplace_cell",
        "vivado.wp014c_swap_cells",
        "vivado.wp014c_set_cell_loc_fixed",
        "vivado.wp014c_set_net_dont_touch",
        "vivado.wp014c_set_pblock_exclude_placement",
        "vivado.wp014c_set_site_prohibit",
        "vivado.wp014h_batch_route_nets",
        "vivado.wp014h_apply_allocation",
        "rapidwright.optimize_fanout",
    }
    unknown = sorted({step.operation for step in compiled.host_steps} - supported)
    return (not unknown, unknown)


async def _execute_rapidwright_typed(
    *, compiled: Any, registry: Any, root: Path, out: Path, python: Path, vivado: Path
) -> dict[str, Any]:
    """Execute the one admitted RapidWright target mutation, then fixed Vivado steps."""
    indices = [
        index
        for index, step in enumerate(compiled.host_steps)
        if step.operation == "rapidwright.optimize_fanout"
    ]
    if len(indices) != 1 or indices[0] != 0:
        return {
            "canonical_endpoint": "CAPABILITY_ACTION_REJECTION",
            "error": "rapidwright_step_shape_invalid",
            "actual_heavy_calls": 0,
            "actual_light_mutations": 0,
        }
    step = compiled.host_steps[0]
    intermediate = out / "rapidwright" / "child.dcp"
    intermediate.parent.mkdir(parents=True, exist_ok=True)
    tester = FPGAOptimizerTest(debug=False, run_dir=out / "rapidwright" / "mcp")
    trace: list[dict[str, Any]] = []
    started = time.monotonic()
    root_before = file_digest(root)
    try:
        await tester.start_servers()
        calls = (
            ("initialize_rapidwright", {"jvm_max_memory": "8G"}, 120.0),
            ("read_checkpoint", {"dcp_path": str(root.resolve())}, 600.0),
            (
                "optimize_fanout",
                {
                    "net_name": str(step.private_args["net"]),
                    "split_factor": int(step.private_args["split_factor"]),
                },
                300.0,
            ),
            (
                "write_checkpoint",
                {"dcp_path": str(intermediate), "overwrite": True},
                600.0,
            ),
        )
        optimization: dict[str, Any] | None = None
        for name, arguments, timeout in calls:
            call_started = time.monotonic()
            output = await tester.call_rapidwright_tool(
                name, arguments, timeout=timeout
            )
            trace.append(
                {
                    "operation": "rapidwright." + name,
                    "arguments_sha256": digest(arguments),
                    "duration_seconds": time.monotonic() - call_started,
                    "output_sha256": digest(output.encode()),
                    "output_size_bytes": len(output.encode()),
                }
            )
            parsed = json.loads(output)
            if (
                not isinstance(parsed, dict)
                or parsed.get("error")
                or parsed.get("status") not in {"success", "already_initialized"}
            ):
                raise RuntimeError(f"rapidwright_{name}_not_successful")
            if name == "optimize_fanout":
                optimization = parsed
        if not intermediate.is_file() or intermediate.stat().st_size == 0:
            raise RuntimeError("rapidwright_intermediate_checkpoint_missing")
        if (
            not optimization
            or optimization.get("status") != "success"
            or optimization.get("error")
        ):
            raise RuntimeError("rapidwright_optimize_fanout_not_successful")
    except Exception as exc:
        return {
            "canonical_endpoint": "TOOL_FAILURE",
            "error": f"rapidwright:{type(exc).__name__}:{exc}",
            "actual_heavy_calls": 0,
            "actual_light_mutations": len(
                [
                    row
                    for row in trace
                    if row["operation"] == "rapidwright.optimize_fanout"
                ]
            ),
            "rapidwright_trace": trace,
            "root_unchanged": file_digest(root) == root_before,
            "wall_seconds": time.monotonic() - started,
        }
    finally:
        try:
            await tester.cleanup()
        except Exception:
            pass
    remaining = tuple(compiled.host_steps[1:])
    remainder = replace(
        compiled,
        initial_stage="PLACED",
        host_steps=remaining,
        heavy_calls=sum(
            (row.event_class == "IMPLEMENTATION_HEAVY" for row in remaining)
        ),
        light_mutations=sum((row.event_class == "MUTATION_LIGHT" for row in remaining)),
    )
    result = await _execute_typed(
        compiled=remainder,
        registry=registry,
        root=intermediate,
        out=out / "vivado",
        python=python,
        vivado=vivado,
    )
    observed_new_nets = len(optimization.get("new_nets", [])) if optimization else 0
    result["rapidwright_trace"] = trace
    result["rapidwright_result"] = optimization
    result["rapidwright_intermediate"] = {
        "sha256": file_digest(intermediate),
        "size_bytes": intermediate.stat().st_size,
    }
    result["actual_light_mutations"] = (
        int(result.get("actual_light_mutations") or 0) + 1
    )
    result["selected_count"] = int(result.get("selected_count") or 0) + 1
    result["transformed_count"] = (
        int(result.get("transformed_count") or 0) + observed_new_nets
    )
    result["root_unchanged"] = file_digest(root) == root_before
    result["wall_seconds"] = time.monotonic() - started
    return result


async def _execute_typed(
    *, compiled: Any, registry: Any, root: Path, out: Path, python: Path, vivado: Path
) -> dict[str, Any]:
    macro_indices = [
        index
        for index, step in enumerate(compiled.host_steps)
        if step.operation == "macro.execute"
    ]
    if macro_indices:
        if len(macro_indices) != 1 or macro_indices[0] != 0:
            return {
                "canonical_endpoint": "CAPABILITY_ACTION_REJECTION",
                "error": "embedded_macro_must_precede_mutating_host_steps",
                "actual_heavy_calls": 0,
                "actual_light_mutations": 0,
                "boundary_runtime_exercised": False,
            }
        macro_step = compiled.host_steps[0]
        flow = str(macro_step.private_args["flow_name"])
        macro_result = await _execute_macro(
            root=root, flow=flow, out=out / "macro", python=python
        )
        macro_trace = {
            "source_construct": "CALL_MACRO",
            "action_key": macro_step.public_args["action_key"],
            "flow_name": flow,
            "runtime_exercised": bool(macro_result.get("actual_heavy_calls")),
            "canonical_endpoint": macro_result.get("canonical_endpoint"),
        }
        if macro_result.get("canonical_endpoint") != "VALID_COMPLETION":
            return macro_result | {
                "embedded_macro_trace": [macro_trace],
                "boundary_runtime_exercised": macro_trace["runtime_exercised"],
            }
        remaining = tuple(compiled.host_steps[1:])
        if not remaining:
            return macro_result | {
                "embedded_macro_trace": [macro_trace],
                "boundary_runtime_exercised": macro_trace["runtime_exercised"],
            }
        macro_child = Path(str(macro_result.get("child_path") or ""))
        if not macro_child.is_file():
            return macro_result | {
                "canonical_endpoint": "INVALID_ARTIFACT",
                "error": "embedded_macro_successor_missing",
                "embedded_macro_trace": [macro_trace],
                "boundary_runtime_exercised": macro_trace["runtime_exercised"],
            }
        remainder = replace(
            compiled,
            initial_stage="ROUTED",
            host_steps=remaining,
            heavy_calls=sum((step.heavy_cost for step in remaining)),
            light_mutations=sum((step.light_cost for step in remaining)),
        )
        suffix = await _execute_typed(
            compiled=remainder,
            registry=registry,
            root=macro_child,
            out=out / "suffix",
            python=python,
            vivado=vivado,
        )
        return suffix | {
            "actual_heavy_calls": int(macro_result.get("actual_heavy_calls") or 0)
            + int(suffix.get("actual_heavy_calls") or 0),
            "actual_light_mutations": int(
                macro_result.get("actual_light_mutations") or 0
            )
            + int(suffix.get("actual_light_mutations") or 0),
            "embedded_macro_trace": [macro_trace],
            "boundary_runtime_exercised": macro_trace["runtime_exercised"],
            "macro_execution": macro_result,
        }
    if any(
        (
            step.operation == "rapidwright.optimize_fanout"
            for step in compiled.host_steps
        )
    ):
        return await _execute_rapidwright_typed(
            compiled=compiled,
            registry=registry,
            root=root,
            out=out,
            python=python,
            vivado=vivado,
        )
    supported, unknown = _typed_supported(compiled)
    if not supported:
        return {
            "canonical_endpoint": "CAPABILITY_ACTION_REJECTION",
            "unsupported_host_operations": unknown,
            "actual_heavy_calls": 0,
            "actual_light_mutations": 0,
        }
    root_before = file_digest(root)
    backend = _WorkerClient(
        SimpleNamespace(mode="probe", root=root, out=out, python=python, vivado=vivado)
    )
    backend.registry = registry
    backend.home.mkdir(parents=True, mode=448)
    limits = ProcessLimits(
        initialization_timeout_seconds=120.0,
        whole_run_timeout_seconds=7200.0,
        graceful_close_seconds=20.0,
        terminate_grace_seconds=5.0,
        kill_grace_seconds=10.0,
        cleanup_timeout_seconds=40.0,
        output_limit_bytes=8 * 1024 * 1024,
    )
    environment = build_mutation_worker_environment(os.environ)
    argv = build_mutation_worker_argv(
        python_exec=python,
        worker_entry=mutation_worker_entry_path(),
        vivado_exec=vivado,
        initialization_timeout_seconds=120.0,
        run_workspace=out,
        isolated_home=backend.home,
    )
    backend.session = OwnedLineProtocolSession(
        argv,
        ready_line=READY_LINE,
        close_command=request_bytes("worker.exit", "cleanup", timeout_seconds=10.0),
        limits=limits,
        cwd=out,
        env=environment,
    )
    started = time.monotonic()
    error = None
    actual = None
    pre_timing = post_timing = route = None
    pblock_ids: dict[str, str] = {}
    try:
        await backend.session.start()
        _verify_worker_attestation(
            backend.session.startup_output.data,
            expected_vivado="2025.1",
            expected_python=platform.python_version(),
            expected_environment_hash=effective_vivado_environment_hash(
                environment, backend.home
            ),
            expected_worker=WORKER_VERSION,
            expected_protocol=WORKER_TRANSPORT_PROTOCOL_VERSION,
        )
        await backend.call(
            "vivado.open_checkpoint", {"dcp_path_b64": b64(root)}, timeout=900.0
        )
        pre_timing = await backend.call("vivado.report_timing_summary", timeout=300.0)
        actual = {"selected": 0, "transformed": 0, "heavy": 0, "light": 0}
        for index, step in enumerate(compiled.host_steps):
            op = step.operation
            trace_start = len(backend.trace)
            if op in {"vivado.create_pblock", "vivado.wp014c_create_pblock"}:
                region = registry.resolve(
                    str(step.public_args["region"]), expected_type="region"
                )
                bounds = region.attributes["bounds"]
                if re.fullmatch(
                    "(?:CLOCKREGION_)?X\\d+Y\\d+", region.canonical_identity
                ):
                    coordinate = region.canonical_identity.removeprefix("CLOCKREGION_")
                    region_name = f"CLOCKREGION_{coordinate}:CLOCKREGION_{coordinate}"
                else:
                    region_name = f"CLOCKREGION_X{bounds[0]}Y{bounds[2]}:CLOCKREGION_X{bounds[1]}Y{bounds[3]}"
                pblock_handle = str(
                    step.public_args.get("pblock")
                    or next(
                        (
                            obj.handle
                            for obj in registry.objects(object_type="pblock")
                            if obj.attributes.get("region_handle") == region.handle
                        )
                    )
                )
                pblock_id = (
                    f"wp014c_pb{index}"
                    if op.startswith("vivado.wp014c")
                    else f"wp013_e0_pb{index}"
                )
                pblock_ids[pblock_handle] = pblock_id
                worker_op = (
                    "vivado.wp014c_create_pblock"
                    if op.startswith("vivado.wp014c")
                    else "vivado.wp013_create_pblock"
                )
                await backend.call(
                    worker_op,
                    {"pblock_id": pblock_id, "region_name_b64": b64(region_name)},
                )
                actual["transformed"] += 1
            elif op in {
                "vivado.assign_pblock",
                "vivado.wp014c_assign_pblock",
                "vivado.wp014c_unassign_pblock",
            }:
                targets = [
                    registry.resolve(handle).canonical_identity
                    for handle in step.public_args["targets"]
                ]
                worker_op = {
                    "vivado.assign_pblock": "vivado.wp013_assign_pblock",
                    "vivado.wp014c_assign_pblock": "vivado.wp014c_assign_pblock",
                    "vivado.wp014c_unassign_pblock": "vivado.wp014c_unassign_pblock",
                }[op]
                await backend.call(
                    worker_op,
                    {
                        "pblock_id": pblock_ids[str(step.public_args["pblock"])],
                        "cell_names_b64": [b64(name) for name in targets],
                    },
                )
                actual["selected"] += len(targets)
                actual["transformed"] += len(targets)
            elif op == "vivado.wp014c_resize_pblock":
                region = registry.resolve(
                    str(step.public_args["region"]), expected_type="region"
                )
                bounds = region.attributes["bounds"]
                region_name = f"CLOCKREGION_X{bounds[0]}Y{bounds[2]}:CLOCKREGION_X{bounds[1]}Y{bounds[3]}"
                await backend.call(
                    "vivado.wp014c_resize_pblock",
                    {
                        "pblock_id": pblock_ids[str(step.public_args["pblock"])],
                        "region_name_b64": b64(region_name),
                        "mode": str(step.public_args["mode"]),
                    },
                )
                actual["transformed"] += 1
            elif op in {"vivado.remove_pblock", "vivado.wp014c_delete_pblock"}:
                worker_op = (
                    "vivado.wp014c_delete_pblock"
                    if op.startswith("vivado.wp014c")
                    else "vivado.wp013_remove_pblock"
                )
                await backend.call(
                    worker_op,
                    {"pblock_id": pblock_ids[str(step.public_args["pblock"])]},
                )
                actual["transformed"] += 1
            elif op in {
                "vivado.relocate_hardblock",
                "vivado.wp014c_relocate_hardblocks",
            }:
                targets = [
                    registry.resolve(
                        handle, expected_type="hardblock"
                    ).canonical_identity
                    for handle in step.public_args["targets"]
                ]
                site = registry.resolve(
                    str(step.public_args["destination"]), expected_type="site"
                ).canonical_identity
                worker_op = (
                    "vivado.wp014c_relocate_hardblocks"
                    if op.startswith("vivado.wp014c")
                    else "vivado.wp013_relocate_hardblocks"
                )
                output = await backend.call(
                    worker_op,
                    {
                        "cell_names_b64": [b64(name) for name in targets],
                        "site_names_b64": [b64(site) for _ in targets],
                    },
                    timeout=900.0,
                )
                actual["selected"] += len(targets)
                actual["transformed"] += relocation_effect_count(output, len(targets))
            elif op == "vivado.wp014c_replicate_net":
                net = registry.resolve(
                    str(step.public_args["net"]), expected_type="net"
                ).canonical_identity
                await backend.call(op, {"net_name_b64": b64(net)}, timeout=900.0)
                actual["selected"] += 1
                actual["transformed"] += 1
            elif op == "vivado.wp014c_route_net":
                net = registry.resolve(
                    str(step.public_args["net"]), expected_type="net"
                ).canonical_identity
                await backend.call(
                    op,
                    {
                        "net_name_b64": b64(net),
                        "delay_mode": str(step.public_args["delay_mode"]),
                    },
                    timeout=900.0,
                )
                actual["selected"] += 1
                actual["transformed"] += 1
            elif op == "vivado.wp014h_batch_route_nets":
                nets = [
                    registry.resolve(
                        str(handle), expected_type="net"
                    ).canonical_identity
                    for handle in step.public_args["nets"]
                ]
                await backend.call(
                    op,
                    {
                        "net_names_b64": [b64(net) for net in nets],
                        "delay_mode": str(step.public_args["delay_mode"]),
                    },
                    timeout=900.0,
                )
                actual["selected"] += len(nets)
                actual["transformed"] += len(nets)
            elif op == "vivado.wp014h_apply_allocation":
                relation_objects = registry.objects(object_type="compatibility")
                if len(relation_objects) != 1:
                    raise RuntimeError("wp015b_dispatch_relation_object_missing")
                relation = relation_objects[0].attributes.get("relation")
                allocation = registry.resolve(
                    str(step.public_args["allocation"]), expected_type="allocation"
                )
                if not isinstance(relation, Mapping):
                    raise RuntimeError("wp015b_dispatch_relation_payload_missing")
                validate_allocation(
                    allocation.attributes,
                    relation,
                    root_sha256=registry.root_sha256,
                    stage=str(relation.get("stage")),
                )
                targets = [
                    registry.resolve(
                        str(handle), expected_type="hardblock"
                    ).canonical_identity
                    for handle in step.public_args["targets"]
                ]
                sites = [
                    registry.resolve(
                        str(handle), expected_type="site"
                    ).canonical_identity
                    for handle in step.public_args["sites"]
                ]
                target_identity_sha256 = digest(targets)
                dispatch_pairs_sha256 = digest(
                    [
                        {"target": target, "site": site}
                        for target, site in zip(targets, sites)
                    ]
                )
                worker_arguments = {
                    "cell_names_b64": [b64(name) for name in targets],
                    "site_names_b64": [b64(name) for name in sites],
                    "target_site_pair_hashes": list(
                        step.public_args["target_site_pair_hashes"]
                    ),
                    "compatibility_relation_sha256": str(
                        step.public_args["compatibility_relation_sha256"]
                    ),
                    "feasibility_query_sha256": str(
                        step.public_args["feasibility_query_sha256"]
                    ),
                    "allocation_sha256": str(step.public_args["allocation_sha256"]),
                    "target_identity_sha256": target_identity_sha256,
                    "dispatch_pairs_sha256": dispatch_pairs_sha256,
                }
                output = await backend.call(op, worker_arguments, timeout=900.0)
                moved = relocation_effect_count(output, len(targets))
                allocation_effect_evidence(
                    output,
                    dispatch_pairs_sha256=dispatch_pairs_sha256,
                    target_identity_sha256=target_identity_sha256,
                    compatibility_relation_sha256=worker_arguments[
                        "compatibility_relation_sha256"
                    ],
                    allocation_sha256=worker_arguments["allocation_sha256"],
                )
                actual["selected"] += len(targets)
                actual["transformed"] += moved
            elif op == "vivado.wp014c_place_cell":
                cell = registry.resolve(
                    str(step.public_args["cell"]), expected_type="cell"
                ).canonical_identity
                site = registry.resolve(
                    str(step.public_args["site"]), expected_type="site"
                ).canonical_identity
                await backend.call(
                    op,
                    {"cell_name_b64": b64(cell), "site_name_b64": b64(site)},
                    timeout=900.0,
                )
                actual["selected"] += 1
                actual["transformed"] += 1
            elif op == "vivado.wp014c_unplace_cell":
                cell = registry.resolve(
                    str(step.public_args["cell"]), expected_type="cell"
                ).canonical_identity
                await backend.call(op, {"cell_name_b64": b64(cell)}, timeout=900.0)
                actual["selected"] += 1
                actual["transformed"] += 1
            elif op == "vivado.wp014c_swap_cells":
                first = registry.resolve(
                    str(step.public_args["first"]), expected_type="cell"
                ).canonical_identity
                second = registry.resolve(
                    str(step.public_args["second"]), expected_type="cell"
                ).canonical_identity
                await backend.call(
                    op,
                    {"first_cell_b64": b64(first), "second_cell_b64": b64(second)},
                    timeout=900.0,
                )
                actual["selected"] += 2
                actual["transformed"] += 2
            elif op in {
                "vivado.wp014c_set_cell_loc_fixed",
                "vivado.wp014c_set_net_dont_touch",
                "vivado.wp014c_set_pblock_exclude_placement",
                "vivado.wp014c_set_site_prohibit",
            }:
                kind = {
                    "vivado.wp014c_set_cell_loc_fixed": "cell",
                    "vivado.wp014c_set_net_dont_touch": "net",
                    "vivado.wp014c_set_pblock_exclude_placement": "pblock",
                    "vivado.wp014c_set_site_prohibit": "site",
                }[op]
                target = registry.resolve(
                    str(step.public_args["target"]), expected_type=kind
                ).canonical_identity
                if kind == "pblock":
                    target = pblock_ids[str(step.public_args["target"])]
                await backend.call(
                    op,
                    {
                        "target_name_b64": b64(target),
                        "value": bool(step.public_args["value"]),
                    },
                    timeout=900.0,
                )
                actual["selected"] += 1
                actual["transformed"] += 1
            elif op == "vivado.unroute":
                await backend.call("vivado.unroute_all", timeout=900.0)
            elif op == "vivado.unplace":
                await backend.call("vivado.unplace_all", timeout=900.0)
            elif op == "vivado.place":
                place_args = {
                    "directive": str(step.public_args.get("directive", "Default"))
                }
                if step.public_args.get("net_delay_weight") is not None:
                    place_args["net_delay_weight"] = str(
                        step.public_args["net_delay_weight"]
                    )
                await backend.call("vivado.place_design", place_args, timeout=900.0)
            elif op == "vivado.route":
                directive = str(step.public_args.get("directive", "Default"))
                await backend.call(
                    "vivado.route_design", {"directive": directive}, timeout=900.0
                )
            elif op == "vivado.phys_opt":
                if step.public_args.get("mode") == "DIRECTIVE":
                    phys_args = {
                        "mode": "DIRECTIVE",
                        "directive": str(step.public_args["directive"]),
                    }
                elif step.public_args.get("mode") == "OPTIONS":
                    phys_args = {
                        "mode": "OPTIONS",
                        "options": list(step.public_args["options"]),
                    }
                else:
                    phys_args = {"directive": str(step.public_args.get("directive"))}
                await backend.call("vivado.phys_opt_design", phys_args, timeout=900.0)
            target_identity_hash = (
                target_identity_sha256
                if op == "vivado.wp014h_apply_allocation"
                else digest(dict(step.private_args))
            )
            for trace_row in backend.trace[trace_start:]:
                trace_row["source_step_id"] = step.source_step_id
                trace_row["source_construct"] = step.source_construct
                trace_row["consumer_input_slots"] = [
                    {"input_slot": slot, "binding_id": binding}
                    for slot, binding in step.input_bindings
                ]
                trace_row["actual_target_identity_hash"] = target_identity_hash
                if op == "vivado.wp014h_apply_allocation":
                    trace_row.update(
                        {
                            "compatibility_relation_sha256": worker_arguments[
                                "compatibility_relation_sha256"
                            ],
                            "feasibility_query_sha256": worker_arguments[
                                "feasibility_query_sha256"
                            ],
                            "target_site_pair_hashes": worker_arguments[
                                "target_site_pair_hashes"
                            ],
                            "allocation_sha256": worker_arguments["allocation_sha256"],
                            "dispatch_pairs_sha256": worker_arguments[
                                "dispatch_pairs_sha256"
                            ],
                            "pair_membership_revalidated": True,
                        }
                    )
            actual[
                "heavy" if step.event_class == "IMPLEMENTATION_HEAVY" else "light"
            ] += 1
        await backend.call(
            "vivado.write_checkpoint",
            {"dcp_path_b64": b64(backend.child)},
            timeout=900.0,
        )
        post_timing = await backend.call("vivado.report_timing_summary", timeout=300.0)
        route = await backend.call("vivado.report_route_status", timeout=300.0)
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
    finally:
        process = None
        try:
            process_result = await backend.session.close()
            process = process_result.to_dict(include_output_text=False)
        except Exception as exc:
            if error is None:
                error = f"worker_close:{type(exc).__name__}:{exc}"
            if backend.session.result is not None:
                process = backend.session.result.to_dict(include_output_text=False)
    write_atomic(
        out / "private" / "worker-call-outputs.json",
        backend.raw_call_outputs,
        private=True,
    )
    root_after = file_digest(root)
    clean_route = bool(route and route_status_clean(route))
    child = (
        {
            "sha256": file_digest(backend.child),
            "size_bytes": backend.child.stat().st_size,
        }
        if backend.child.exists()
        else None
    )
    endpoint = (
        "VALID_COMPLETION"
        if error is None
        and clean_route
        and (child is not None)
        and process
        and process["cleanup_ok"]
        else "TOOL_FAILURE"
    )
    observed_heavy = sum(
        (
            row["operation"]
            in {
                "vivado.place_design",
                "vivado.route_design",
                "vivado.phys_opt_design",
                "vivado.wp014c_replicate_net",
                "vivado.wp014c_route_net",
                "vivado.wp014h_batch_route_nets",
            }
            for row in backend.trace
        )
    )
    observed_light = sum(
        (
            row["operation"]
            in {
                "vivado.unroute_all",
                "vivado.unplace_all",
                "vivado.wp013_create_pblock",
                "vivado.wp013_assign_pblock",
                "vivado.wp013_remove_pblock",
                "vivado.wp013_relocate_hardblocks",
                "vivado.wp014c_create_pblock",
                "vivado.wp014c_resize_pblock",
                "vivado.wp014c_assign_pblock",
                "vivado.wp014c_unassign_pblock",
                "vivado.wp014c_delete_pblock",
                "vivado.wp014c_relocate_hardblocks",
                "vivado.wp014c_place_cell",
                "vivado.wp014c_unplace_cell",
                "vivado.wp014c_swap_cells",
                "vivado.wp014c_set_cell_loc_fixed",
                "vivado.wp014c_set_net_dont_touch",
                "vivado.wp014c_set_pblock_exclude_placement",
                "vivado.wp014c_set_site_prohibit",
                "vivado.wp014h_apply_allocation",
            }
            for row in backend.trace
        )
    )
    return {
        "canonical_endpoint": endpoint,
        "error": error,
        "root_unchanged": root_after == root_before,
        "child": child,
        "route_valid": clean_route,
        "pre_timing": parse_timing_summary_static(pre_timing.decode(errors="replace"))
        if pre_timing
        else None,
        "post_timing": parse_timing_summary_static(post_timing.decode(errors="replace"))
        if post_timing
        else None,
        "actual_heavy_calls": observed_heavy,
        "actual_light_mutations": observed_light,
        "call_trace": backend.trace,
        "selected_count": actual["selected"] if actual else 0,
        "transformed_count": actual["transformed"] if actual else 0,
        "process": process,
        "worker_call_outputs_sha256": digest(backend.raw_call_outputs),
        "instrumentation_markers": instrumentation_markers(backend.raw_call_outputs),
        "wall_seconds": time.monotonic() - started,
    }


async def execute_compiled(
    compiled: Any,
    registry: Any,
    root: Path,
    out: Path,
    python: Path,
    vivado: Path,
) -> dict[str, Any]:
    """Dispatch one already-compiled program through the Xilinx backend."""
    before = file_digest(root) if root.is_file() else None
    result = await _execute_typed(
        compiled=compiled,
        registry=registry,
        root=root,
        out=out,
        python=python,
        vivado=vivado,
    )
    # The historical controller resolved child paths outside the dispatch
    # helper. Make that adapter explicit without changing its operation body.
    if "child_path" not in result:
        steps, workspace = tuple(compiled.host_steps), out
        while steps and steps[0].operation in {"macro.execute", "rapidwright.optimize_fanout"}:
            if steps[0].operation == "macro.execute" and len(steps) == 1:
                workspace = workspace / "macro"
                break
            workspace = workspace / ("suffix" if steps[0].operation == "macro.execute" else "vivado")
            steps = steps[1:]
        child = workspace / "child.dcp"
        result["child_path"] = str(child) if child.is_file() else None
    if before is not None:
        result["root_unchanged"] = root.is_file() and file_digest(root) == before
    return result
