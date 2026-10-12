#!/usr/bin/env python3
"""Write public-safe exact-argv fake startup evidence for WP-004A Gate 2."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:] = [
    entry
    for entry in sys.path
    if Path(entry or os.curdir).resolve() != REPO_ROOT
]
sys.path.insert(0, str(REPO_ROOT))

from optimizer_pipeline.dof import mutation_worker_binding as _binding_module
from optimizer_pipeline.dof import processes as _processes_module
from optimizer_pipeline.dof import real_mutation as _real_mutation_module
from optimizer_pipeline.dof import vivado_mutation_worker as _worker_module
from optimizer_pipeline.dof.mutation_worker_binding import (
    FAKE_ATTESTATION_SCHEMA,
    argv_sha256,
    build_mutation_worker_argv,
    build_mutation_worker_environment,
    content_hash,
    mutation_worker_argv_form,
    mutation_worker_dependency_manifest,
    mutation_worker_environment_policy,
    mutation_worker_entry_path,
    mutation_worker_source_manifest,
    python_executable_identity,
)
from optimizer_pipeline.dof.processes import (
    OwnedLineProtocolSession,
    ProcessEndpoint,
    ProcessLimits,
)
from optimizer_pipeline.dof.real_mutation import _verify_worker_attestation
from optimizer_pipeline.dof.vivado_mutation_worker import (
    READY_LINE,
    WORKER_TRANSPORT_PROTOCOL_VERSION,
    WORKER_VERSION,
    effective_vivado_environment_hash,
    parse_tcl_result,
    request_bytes,
    terminal_marker,
)

for module, relative_name in (
    (_binding_module, "optimizer_pipeline/dof/mutation_worker_binding.py"),
    (_processes_module, "optimizer_pipeline/dof/processes.py"),
    (_real_mutation_module, "optimizer_pipeline/dof/real_mutation.py"),
    (_worker_module, "optimizer_pipeline/dof/vivado_mutation_worker.py"),
):
    if Path(module.__file__).resolve() != (REPO_ROOT / relative_name).resolve():
        raise RuntimeError("wp004a_attestation_import_origin_mismatch")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _write_fake_vivado(root: Path) -> tuple[Path, Path, Path, Path]:
    executable = root / "Vivado" / "2025.1" / "bin" / "vivado"
    executable.parent.mkdir(parents=True)
    started_record = root / "fake-vivado-started"
    executable.write_text(
        f"""#!{sys.executable}
import re
import sys

with open({os.fspath(started_record)!r}, "w", encoding="utf-8") as handle:
    handle.write("started")
print("Vivado v2025.1", flush=True)
print("Vivado% ", end="", flush=True)
for raw in sys.stdin.buffer:
    if raw.strip() == b"exit":
        break
    match = re.search(
        rb"DOF_VIVADO_MUTATION_TCL_STATUS_V1:([A-Za-z0-9_-]+):", raw
    )
    if match is None:
        continue
    print("Design is fully routed", flush=True)
    print("# of unrouted nets : 0", flush=True)
    print(
        "DOF_VIVADO_MUTATION_TCL_STATUS_V1:"
        + match.group(1).decode("ascii")
        + ":0",
        flush=True,
    )
    print("Vivado% ", end="", flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    workspace = root / "owned-run"
    workspace.mkdir()
    home = workspace / "isolated-home"
    home.mkdir(mode=0o700)
    return executable, started_record, workspace, home


async def _attest(root: Path) -> dict[str, object]:
    fake, started_record, workspace, home = _write_fake_vivado(root)
    environment = build_mutation_worker_environment(os.environ)
    environment_policy = mutation_worker_environment_policy()
    expected_environment_keys = set(environment_policy["fixed_values"]) | (
        set(os.environ) & set(environment_policy["inherited_keys"])
    )
    if set(environment) != expected_environment_keys:
        raise RuntimeError("wp004a_attestation_environment_policy_mismatch")
    argv = build_mutation_worker_argv(
        python_exec=sys.executable,
        worker_entry=mutation_worker_entry_path(),
        vivado_exec=fake,
        initialization_timeout_seconds=1.0,
        run_workspace=workspace,
        isolated_home=home,
    )
    limits = ProcessLimits(
        initialization_timeout_seconds=2.0,
        whole_run_timeout_seconds=4.0,
        graceful_close_seconds=0.1,
        terminate_grace_seconds=0.3,
        kill_grace_seconds=0.7,
        cleanup_timeout_seconds=1.2,
        output_limit_bytes=64 * 1024,
    )
    session = OwnedLineProtocolSession(
        argv,
        ready_line=READY_LINE,
        close_command=request_bytes(
            "worker.exit", "close-attestation", timeout_seconds=0.5
        ),
        limits=limits,
        cwd=workspace,
        env=environment,
    )
    primary_error: BaseException | None = None
    try:
        await session.start()
        startup = session.startup_output
        attestation = _verify_worker_attestation(
            startup.data,
            expected_vivado="2025.1",
            expected_python=platform.python_version(),
            expected_environment_hash=effective_vivado_environment_hash(
                environment, home
            ),
            expected_worker=WORKER_VERSION,
            expected_protocol=WORKER_TRANSPORT_PROTOCOL_VERSION,
        )
        call = await session.call(
            request_bytes(
                "vivado.report_route_status",
                "attestation-readonly",
                timeout_seconds=0.5,
            ),
            terminal_marker=terminal_marker("attestation-readonly"),
            timeout_seconds=0.75,
        )
        status, report = parse_tcl_result(
            "attestation-readonly", call.stdout.data
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if session.result is None:
            try:
                await session.close()
            except BaseException:
                if primary_error is None:
                    raise
    result = session.result
    assert result is not None
    lifecycle = [dict(item) for item in result.lifecycle]
    signal_events = [
        item
        for item in lifecycle
        if item["event_type"]
        in {"PROCESS_GROUP_SIGNALLED", "PROCESS_ROOT_SIGNALLED"}
    ]
    passed = (
        started_record.is_file()
        and attestation["status"] == "PASS"
        and status == 0
        and b"fully routed" in report
        and result.endpoint is ProcessEndpoint.SUCCEEDED
        and result.returncode == 0
        and result.cleanup_ok
        and not result.orphan_process_tokens
        and not signal_events
    )
    source = mutation_worker_source_manifest()
    payload: dict[str, object] = {
        "schema_version": FAKE_ATTESTATION_SCHEMA,
        "technical_non_scientific": True,
        "fake_vivado": True,
        "status": "PASS" if passed else "FAIL",
        "source_manifest_sha256": source["manifest_sha256"],
        "worker_entry_sha256": source["files"][0]["sha256"],
        "argv_form_sha256": mutation_worker_argv_form()[
            "argv_form_sha256"
        ],
        "dependency_manifest_sha256": mutation_worker_dependency_manifest()[
            "manifest_sha256"
        ],
        "environment_policy_sha256": mutation_worker_environment_policy()[
            "policy_sha256"
        ],
        "fake_exact_argv_sha256": argv_sha256(argv),
        "python_executable": python_executable_identity(sys.executable),
        "worker_attestation": attestation,
        "startup_output": {
            "sha256": startup.sha256,
            "size_bytes": startup.total_bytes,
            "truncated": startup.truncated,
        },
        "readonly_call": {
            "operation_id": "vivado.report_route_status",
            "command_sha256": call.command_hash,
            "output_sha256": call.stdout.sha256,
            "output_size_bytes": call.stdout.total_bytes,
            "status_code": status,
        },
        "process": {
            "endpoint": result.endpoint.value,
            "returncode": result.returncode,
            "cleanup_ok": result.cleanup_ok,
            "observed_process_count": len(result.observed_process_tokens),
            "orphan_count": len(result.orphan_process_tokens),
            "signal_event_count": len(signal_events),
            "lifecycle_sha256": content_hash(lifecycle),
        },
        "fake_vivado_start_observed": started_record.is_file(),
        "raw_output_included": False,
        "paths_included": False,
    }
    payload["attestation_payload_sha256"] = content_hash(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wp004a-worker-attestation-") as raw:
        payload = asyncio.run(_attest(Path(raw)))
    encoded = _canonical_bytes(payload) + b"\n"
    output.write_bytes(encoded)
    evidence_sha256 = hashlib.sha256(encoded).hexdigest()
    print(
        json.dumps(
            {
                "status": payload["status"],
                "evidence_sha256": evidence_sha256,
                "attestation_payload_sha256": payload[
                    "attestation_payload_sha256"
                ],
            },
            sort_keys=True,
        )
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
