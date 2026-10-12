from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import pytest

from optimizer_pipeline.dof.mutation_worker_binding import (
    MutationWorkerBindingError,
    argv_sha256,
    build_mutation_worker_argv,
    build_mutation_worker_environment,
    content_hash,
    mutation_worker_entry_path,
    mutation_worker_environment_policy,
    mutation_worker_source_manifest,
    python_executable_identity,
    verified_fake_attestation_evidence_sha256,
)
from optimizer_pipeline.dof.processes import (
    OwnedLineProtocolSession,
    OwnedProtocolSessionError,
    ProcessEndpoint,
    ProcessLimits,
)
from optimizer_pipeline.dof.real_mutation import (
    RealMutationExecutionError,
    _verify_worker_attestation,
)
from optimizer_pipeline.dof.vivado_mutation_worker import (
    EFFECTIVE_ENV_PREFIX,
    INIT_POLICY_LINE,
    LAUNCHED_LINE,
    PYTHON_VERSION_PREFIX,
    READY_LINE,
    STARTED_LINE,
    STATUS_PREFIX,
    VERSION_PREFIX,
    WORKER_PROTOCOL_PREFIX,
    WORKER_REQUEST_SCHEMA,
    WORKER_TRANSPORT_PROTOCOL_VERSION,
    WORKER_VERSION,
    WORKER_VERSION_PREFIX,
    WorkerProtocolError,
    _activate_bound_dependencies,
    _bound_dependency_manifest,
    build_fixed_tcl,
    effective_vivado_environment_hash,
    parse_request_line,
    parse_tcl_result,
    request_bytes,
    terminal_marker,
)
from optimizer_pipeline.dof.vivado_readonly_worker import (
    parse_request_line as parse_readonly_request_line,
)


def _request(operation: str, arguments: dict) -> bytes:
    return json.dumps(
        {
            "schema_version": WORKER_REQUEST_SCHEMA,
            "operation_id": operation,
            "call_token": "call-001",
            "timeout_seconds": 10.0,
            "arguments": arguments,
        },
        separators=(",", ":"),
    ).encode()


def _worker_limits() -> ProcessLimits:
    return ProcessLimits(
        initialization_timeout_seconds=2.0,
        whole_run_timeout_seconds=4.0,
        graceful_close_seconds=0.1,
        terminate_grace_seconds=0.3,
        kill_grace_seconds=0.7,
        cleanup_timeout_seconds=1.2,
        output_limit_bytes=64 * 1024,
    )


def _write_fake_vivado(
    tmp_path: Path,
    *,
    version: str = "2025.1",
    reach_prompt: bool = True,
) -> tuple[Path, Path, Path, Path]:
    executable = tmp_path / "Vivado" / "2025.1" / "bin" / "vivado"
    executable.parent.mkdir(parents=True)
    record = tmp_path / "fake-vivado-started"
    executable.write_text(
        f"""#!{sys.executable}
import re
import sys
import time

with open({os.fspath(record)!r}, "w", encoding="utf-8") as handle:
    handle.write("started")
print("Vivado v{version}", flush=True)
if not {reach_prompt!r}:
    time.sleep(60)
    raise SystemExit(70)
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
    workspace = tmp_path / "owned-run"
    workspace.mkdir()
    home = workspace / "isolated-home"
    home.mkdir(mode=0o700)
    return executable, record, workspace, home


def _exact_argv(
    fake_vivado: Path,
    *,
    workspace: Path,
    home: Path,
    worker_entry: Path | None = None,
    initialization_timeout_seconds: float = 1.0,
) -> tuple[str, ...]:
    return build_mutation_worker_argv(
        python_exec=sys.executable,
        worker_entry=worker_entry or mutation_worker_entry_path(),
        vivado_exec=fake_vivado,
        initialization_timeout_seconds=initialization_timeout_seconds,
        run_workspace=workspace,
        isolated_home=home,
    )


@pytest.mark.parametrize(
    "operation,arguments,fragment",
    [
        ("vivado.unroute_all", {}, "route_design -unroute"),
        ("vivado.unplace_all", {}, "place_design -unplace"),
        ("vivado.place_design", {"directive": "Explore"}, "place_design -directive Explore"),
        ("vivado.route_design", {"directive": "Default"}, "route_design -directive Default"),
        ("vivado.phys_opt_design", {"directive": "AddRetime"}, "phys_opt_design -directive AddRetime"),
    ],
)
def test_mutation_worker_has_only_closed_typed_tcl(
    operation: str,
    arguments: dict,
    fragment: str,
) -> None:
    request = parse_request_line(_request(operation, arguments))
    tcl = build_fixed_tcl(request)
    assert fragment in tcl
    assert "run_tcl" not in tcl


def test_open_and_write_paths_are_base64_bound() -> None:
    encoded = base64.b64encode(b"/bounded workspace/input.dcp").decode()
    for operation in ("vivado.open_checkpoint", "vivado.write_checkpoint"):
        parsed = parse_request_line(_request(operation, {"dcp_path_b64": encoded}))
        tcl = build_fixed_tcl(parsed)
        assert encoded in tcl
        assert "/bounded workspace/input.dcp" not in tcl


def test_wp013_object_query_is_closed_and_target_is_base64_bound() -> None:
    target = base64.b64encode(b"private/hier/dsp0").decode()
    parsed = parse_request_line(
        _request(
            "vivado.wp013_query_objects",
            {
                "query_kind": "compatible_sites",
                "top_k": 4,
                "resource_type": "",
                "target_cell_b64": target,
            },
        )
    )
    tcl = build_fixed_tcl(parsed)
    assert target in tcl
    assert "private/hier/dsp0" not in tcl
    assert "DOF_WP013_QUERY_V1:compatible_sites" in tcl
    assert "${__dof_prefix}_X*Y*" in tcl
    assert "lsearch -exact $__dof_occupied" in tcl
    assert "IS_USED" not in tcl

    hardblocks = parse_request_line(
        _request(
            "vivado.wp013_query_objects",
            {
                "query_kind": "critical_hardblocks",
                "top_k": 8,
                "resource_type": "ANY",
                "target_cell_b64": "",
            },
        )
    )
    hardblock_tcl = build_fixed_tcl(hardblocks)
    assert "REF_NAME =~ DSP*" in hardblock_tcl
    assert "REF_NAME =~ RAMB*" in hardblock_tcl
    assert "REF_NAME =~ URAM*" in hardblock_tcl

    fanout = parse_request_line(
        _request(
            "vivado.wp013_query_objects",
            {
                "query_kind": "high_fanout_nets",
                "top_k": 8,
                "resource_type": "",
                "target_cell_b64": "",
            },
        )
    )
    fanout_tcl = build_fixed_tcl(fanout)
    assert "FLAT_PIN_COUNT >= 2" in fanout_tcl
    assert "<const0> <const1>" in fanout_tcl
    assert "lsort -integer -decreasing -index 0" in fanout_tcl
    assert "IS_CLOCK" not in fanout_tcl


def test_wp013_typed_spatial_and_hardblock_mutations_are_exact_and_encoded() -> None:
    cell = base64.b64encode(b"private/hier/dsp0").decode()
    site = base64.b64encode(b"DSP48E2_X0Y1").decode()
    region = base64.b64encode(b"CLOCKREGION_X0Y0").decode()
    spatial = parse_request_line(
        _request(
            "vivado.wp013_apply_pblock",
            {"pblock_id": "wp013_spatial", "cell_names_b64": [cell], "region_name_b64": region},
        )
    )
    spatial_tcl = build_fixed_tcl(spatial)
    assert cell in spatial_tcl and region in spatial_tcl
    assert "private/hier/dsp0" not in spatial_tcl
    assert "DOF_WP013_APPLY_PBLOCK_V1" in spatial_tcl

    create = parse_request_line(
        _request(
            "vivado.wp013_create_pblock",
            {"pblock_id": "wp013_spatial", "region_name_b64": region},
        )
    )
    create_tcl = build_fixed_tcl(create)
    assert "CLOCKREGION_${__dof_region}:CLOCKREGION_${__dof_region}" in create_tcl

    relocate = parse_request_line(
        _request(
            "vivado.wp013_relocate_hardblocks",
            {"cell_names_b64": [cell], "site_names_b64": [site]},
        )
    )
    relocate_tcl = build_fixed_tcl(relocate)
    assert cell in relocate_tcl and site in relocate_tcl
    assert "private/hier/dsp0" not in relocate_tcl
    assert "place_cell" in relocate_tcl
    assert "wp013_relocation_no_effect" in relocate_tcl
    assert "DOF_WP013_RELOCATE_V1:$__dof_n:$__dof_moved" in relocate_tcl


@pytest.mark.parametrize(
    "operation,arguments,error",
    [
        ("vivado.wp013_query_objects", {"query_kind": "critical_cells", "top_k": 0, "resource_type": "", "target_cell_b64": ""}, "query_top_k_invalid"),
        ("vivado.wp013_apply_pblock", {"pblock_id": "bad;exit", "cell_names_b64": ["YQ=="], "region_name_b64": "Yg=="}, "pblock_id_invalid"),
        ("vivado.wp013_relocate_hardblocks", {"cell_names_b64": ["YQ=="], "site_names_b64": ["Yg==", "Yw=="]}, "relocation_cardinality_mismatch"),
    ],
)
def test_wp013_worker_extensions_fail_closed(operation, arguments, error) -> None:
    with pytest.raises(WorkerProtocolError, match=error):
        parse_request_line(_request(operation, arguments))


@pytest.mark.parametrize(
    "operation,arguments,error",
    [
        ("vivado.phys_opt_design", {"directive": "AggressiveExplore"}, "directive_not_allowed"),
        ("vivado.phys_opt_design", {"directive": "Explore", "tcl": "exit"}, "operation_arguments_not_exact"),
        ("hidden.raw_tcl", {}, "operation_not_allowed"),
        ("vivado.write_checkpoint", {"path": "/tmp/out.dcp"}, "operation_arguments_not_exact"),
    ],
)
def test_worker_rejects_unknown_directives_fields_operations_and_raw_paths(
    operation: str,
    arguments: dict,
    error: str,
) -> None:
    with pytest.raises(WorkerProtocolError, match=error):
        parse_request_line(_request(operation, arguments))


def test_readonly_worker_still_rejects_mutation_schema_and_operation() -> None:
    raw = request_bytes(
        "vivado.phys_opt_design",
        "call-001",
        timeout_seconds=10.0,
        arguments={"directive": "Explore"},
    )
    with pytest.raises(WorkerProtocolError):
        parse_readonly_request_line(raw.rstrip())


def test_request_and_terminal_output_round_trip() -> None:
    raw = request_bytes(
        "vivado.phys_opt_design",
        "call-001",
        timeout_seconds=30.0,
        arguments={"directive": "Explore"},
    )
    parsed = parse_request_line(raw.rstrip())
    assert parsed.operation_id == "vivado.phys_opt_design"
    status, retained = parse_tcl_result(
        "call-001",
        b"report line\nDOF_VIVADO_MUTATION_TCL_STATUS_V1:call-001:0\n",
    )
    assert status == 0
    assert retained == b"report line\n"
    assert terminal_marker("call-001") == b"DOF_VIVADO_MUTATION_CALL_DONE_V1:call-001"


def test_mutation_worker_environment_is_closed_and_public_hash_is_value_free(
    tmp_path: Path,
) -> None:
    source = {
        "PATH": "/untrusted/bin",
        "LM_LICENSE_FILE": "first-license",
        "XILINXD_LICENSE_FILE": "second-license",
        "OPENAI_API_KEY": "provider-secret",
        "OPENROUTER_API_KEY": "provider-secret",
        "FPL26CONTEST_TOKEN": "contest-secret",
        "DATABASE_URL": "postgres://private",
        "AWS_ACCESS_KEY_ID": "cloud-secret",
        "SSH_AUTH_SOCK": "/private/agent",
        "PYTHONPATH": "/private/modules",
        "PYTHONHOME": "/private/python",
        "LD_PRELOAD": "/private/inject.so",
        "TCLLIBPATH": "/private/tcl",
        "RAPIDWRIGHT_PATH": "/private/rapidwright",
        "JAVA_HOME": "/private/java",
    }
    filtered = build_mutation_worker_environment(source)
    assert filtered == {
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "LM_LICENSE_FILE": "first-license",
        "XILINXD_LICENSE_FILE": "second-license",
    }
    policy = mutation_worker_environment_policy()
    assert policy["unlisted_keys"] == "DROP"
    assert policy["public_evidence_value_policy"] == (
        "REDACTED_PRESENT_FOR_ALL_KEYS"
    )

    changed = dict(filtered)
    changed["LM_LICENSE_FILE"] = "different-license"
    assert effective_vivado_environment_hash(filtered, tmp_path) == (
        effective_vivado_environment_hash(changed, tmp_path)
    )
    changed["EXTRA_KEY"] = "anything"
    assert effective_vivado_environment_hash(filtered, tmp_path) != (
        effective_vivado_environment_hash(changed, tmp_path)
    )


def test_bound_dependency_loader_ignores_unmanifested_root_level_shadow(
    tmp_path: Path,
) -> None:
    for distribution_name in ("pexpect", "ptyprocess"):
        distribution = importlib.metadata.distribution(distribution_name)
        root = Path(distribution.locate_file("")).resolve()
        shutil.copytree(root / distribution_name, tmp_path / distribution_name)
        metadata = next(root.glob(f"{distribution_name}-*.dist-info"))
        copied_metadata = tmp_path / metadata.name
        copied_metadata.mkdir()
        for name in ("METADATA", "WHEEL"):
            shutil.copyfile(metadata / name, copied_metadata / name)
    shadow_record = tmp_path / "shadow-executed"
    (tmp_path / "pexpect.py").write_text(
        "from pathlib import Path\n"
        f"Path({os.fspath(shadow_record)!r}).write_text('executed')\n"
        "raise RuntimeError('unbound shadow executed')\n",
        encoding="utf-8",
    )
    manifest = _bound_dependency_manifest(tmp_path)
    previous = {
        key: value
        for key, value in sys.modules.items()
        if key == "pexpect"
        or key.startswith("pexpect.")
        or key == "ptyprocess"
        or key.startswith("ptyprocess.")
    }
    try:
        loaded = _activate_bound_dependencies(
            os.fspath(tmp_path), manifest["manifest_sha256"]
        )
        assert Path(loaded.__file__).resolve() == (
            tmp_path / "pexpect" / "__init__.py"
        ).resolve()
        assert not shadow_record.exists()
    finally:
        for key in tuple(sys.modules):
            if (
                key == "pexpect"
                or key.startswith("pexpect.")
                or key == "ptyprocess"
                or key.startswith("ptyprocess.")
            ):
                sys.modules.pop(key, None)
        sys.modules.update(previous)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_exact_production_argv_reaches_fake_ready_call_and_clean_exit(
    tmp_path: Path,
) -> None:
    fake, record, workspace, home = _write_fake_vivado(tmp_path)
    argv = _exact_argv(fake, workspace=workspace, home=home)
    assert argv[1:3] == ("-I", "-S")
    env = build_mutation_worker_environment(os.environ)
    policy = mutation_worker_environment_policy()
    assert set(env) == set(policy["fixed_values"]) | (
        set(os.environ) & set(policy["inherited_keys"])
    )

    async def exercise():
        session = OwnedLineProtocolSession(
            argv,
            ready_line=READY_LINE,
            close_command=request_bytes(
                "worker.exit", "close-fake", timeout_seconds=0.5
            ),
            limits=_worker_limits(),
            cwd=workspace,
            env=env,
        )
        try:
            await session.start()
        except OwnedProtocolSessionError as exc:
            pytest.fail(
                "exact isolated worker failed before READY: "
                + json.dumps(
                    exc.result.to_dict(include_output_text=True),
                    sort_keys=True,
                )
            )
        startup = session.startup_output
        attestation = _verify_worker_attestation(
            startup.data,
            expected_vivado="2025.1",
            expected_python=platform.python_version(),
            expected_environment_hash=effective_vivado_environment_hash(
                env, home
            ),
            expected_worker=WORKER_VERSION,
            expected_protocol=WORKER_TRANSPORT_PROTOCOL_VERSION,
        )
        call = await session.call(
            request_bytes(
                "vivado.report_route_status",
                "fake-readonly",
                timeout_seconds=0.5,
            ),
            terminal_marker=terminal_marker("fake-readonly"),
            timeout_seconds=0.75,
        )
        result = await session.close()
        return startup, attestation, call, result

    startup, attestation, call, result = asyncio.run(exercise())
    status, report = parse_tcl_result("fake-readonly", call.stdout.data)
    assert record.read_text(encoding="utf-8") == "started"
    assert status == 0
    assert b"fully routed" in report
    assert attestation == {
        "status": "PASS",
        "vivado_version": "2025.1",
        "python_version": platform.python_version(),
        "effective_environment_sha256": effective_vivado_environment_hash(
            env, home
        ),
        "worker_version": WORKER_VERSION,
        "transport_protocol_version": WORKER_TRANSPORT_PROTOCOL_VERSION,
    }
    assert result.endpoint is ProcessEndpoint.SUCCEEDED
    assert result.returncode == 0
    assert result.cleanup_ok
    assert result.orphan_process_tokens == ()
    assert not any(
        row["event_type"] in {
            "PROCESS_GROUP_SIGNALLED",
            "PROCESS_ROOT_SIGNALLED",
        }
        for row in result.lifecycle
    )
    startup_text = startup.data.decode("utf-8", "replace")
    for marker in (
        PYTHON_VERSION_PREFIX + platform.python_version(),
        WORKER_VERSION_PREFIX + WORKER_VERSION,
        WORKER_PROTOCOL_PREFIX + WORKER_TRANSPORT_PROTOCOL_VERSION,
        INIT_POLICY_LINE,
        EFFECTIVE_ENV_PREFIX
        + effective_vivado_environment_hash(env, home),
        LAUNCHED_LINE,
        STARTED_LINE,
        VERSION_PREFIX + "2025.1",
        READY_LINE,
    ):
        assert startup_text.count(marker) == 1

    source = mutation_worker_source_manifest()
    public_attestation = {
        "source_manifest_sha256": source["manifest_sha256"],
        "production_argv_sha256": argv_sha256(argv),
        "python_executable": python_executable_identity(sys.executable),
        "startup_output_sha256": startup.sha256,
        "call_output_sha256": call.stdout.sha256,
        "endpoint": result.endpoint.value,
        "returncode": result.returncode,
        "cleanup_ok": result.cleanup_ok,
        "orphan_count": len(result.orphan_process_tokens),
    }
    assert len(content_hash(public_attestation)) == 64


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_isolated_entry_import_failure_is_pre_ready_and_never_starts_fake_vivado(
    tmp_path: Path,
) -> None:
    fake, record, workspace, home = _write_fake_vivado(tmp_path)
    isolated_entry = tmp_path / "isolated-entry" / "vivado_mutation_worker.py"
    isolated_entry.parent.mkdir()
    shutil.copyfile(mutation_worker_entry_path(), isolated_entry)
    isolated_sibling = isolated_entry.with_name("vivado_readonly_worker.py")
    shutil.copyfile(
        mutation_worker_entry_path().with_name("vivado_readonly_worker.py"),
        isolated_sibling,
    )
    argv = _exact_argv(
        fake,
        workspace=workspace,
        home=home,
        worker_entry=isolated_entry,
    )
    isolated_sibling.unlink()
    env = build_mutation_worker_environment(os.environ)

    async def exercise():
        session = OwnedLineProtocolSession(
            argv,
            ready_line=READY_LINE,
            close_command=request_bytes(
                "worker.exit", "close-import", timeout_seconds=0.25
            ),
            limits=_worker_limits(),
            cwd=workspace,
            env=env,
        )
        with pytest.raises(OwnedProtocolSessionError) as caught:
            await session.start()
        return caught.value.result

    result = asyncio.run(exercise())
    assert result.endpoint is ProcessEndpoint.FAILED
    assert result.returncode not in {None, 0}
    assert result.cleanup_ok
    assert READY_LINE.encode() not in result.stdout.data
    assert not record.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_source_change_after_argv_freeze_fails_before_fake_vivado_start(
    tmp_path: Path,
) -> None:
    fake, record, workspace, home = _write_fake_vivado(tmp_path)
    isolated_entry = tmp_path / "isolated-source" / "vivado_mutation_worker.py"
    isolated_entry.parent.mkdir()
    shutil.copyfile(mutation_worker_entry_path(), isolated_entry)
    isolated_sibling = isolated_entry.with_name("vivado_readonly_worker.py")
    shutil.copyfile(
        mutation_worker_entry_path().with_name("vivado_readonly_worker.py"),
        isolated_sibling,
    )
    argv = _exact_argv(
        fake,
        workspace=workspace,
        home=home,
        worker_entry=isolated_entry,
    )
    isolated_sibling.write_bytes(isolated_sibling.read_bytes() + b"\n# changed\n")
    env = build_mutation_worker_environment(os.environ)

    async def exercise():
        session = OwnedLineProtocolSession(
            argv,
            ready_line=READY_LINE,
            close_command=request_bytes(
                "worker.exit", "close-source", timeout_seconds=0.25
            ),
            limits=_worker_limits(),
            cwd=workspace,
            env=env,
        )
        with pytest.raises(OwnedProtocolSessionError) as caught:
            await session.start()
        return caught.value.result

    result = asyncio.run(exercise())
    assert result.endpoint is ProcessEndpoint.FAILED
    assert result.returncode == 71
    assert result.cleanup_ok
    assert READY_LINE.encode() not in result.stdout.data
    assert not record.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_tampered_dependency_binding_fails_before_fake_vivado_start(
    tmp_path: Path,
) -> None:
    fake, record, workspace, home = _write_fake_vivado(tmp_path)
    argv = list(_exact_argv(fake, workspace=workspace, home=home))
    marker = argv.index("--dependency-manifest-sha256")
    argv[marker + 1] = "0" * 64
    env = build_mutation_worker_environment(os.environ)

    async def exercise():
        session = OwnedLineProtocolSession(
            argv,
            ready_line=READY_LINE,
            close_command=request_bytes(
                "worker.exit", "close-dependency", timeout_seconds=0.25
            ),
            limits=_worker_limits(),
            cwd=workspace,
            env=env,
        )
        with pytest.raises(OwnedProtocolSessionError) as caught:
            await session.start()
        return caught.value.result

    result = asyncio.run(exercise())
    assert result.returncode == 72
    assert result.cleanup_ok
    assert READY_LINE.encode() not in result.stdout.data
    assert not record.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_exact_argv_version_mismatch_fails_attestation_before_any_request(
    tmp_path: Path,
) -> None:
    fake, record, workspace, home = _write_fake_vivado(
        tmp_path, version="2024.2"
    )
    argv = _exact_argv(fake, workspace=workspace, home=home)
    env = build_mutation_worker_environment(os.environ)

    async def exercise():
        session = OwnedLineProtocolSession(
            argv,
            ready_line=READY_LINE,
            close_command=request_bytes(
                "worker.exit", "close-version", timeout_seconds=0.5
            ),
            limits=_worker_limits(),
            cwd=workspace,
            env=env,
        )
        await session.start()
        try:
            with pytest.raises(
                RealMutationExecutionError,
                match="startup_attestation_mismatch",
            ):
                _verify_worker_attestation(
                    session.startup_output.data,
                    expected_vivado="2025.1",
                    expected_python=platform.python_version(),
                    expected_environment_hash=(
                        effective_vivado_environment_hash(env, home)
                    ),
                    expected_worker=WORKER_VERSION,
                    expected_protocol=WORKER_TRANSPORT_PROTOCOL_VERSION,
                )
        finally:
            result = await session.close()
        return result

    result = asyncio.run(exercise())
    assert record.exists()
    assert result.endpoint is ProcessEndpoint.SUCCEEDED
    assert result.returncode == 0
    assert not any(
        row["event_type"] == "PROTOCOL_CALL_SEND_STARTED"
        for row in result.lifecycle
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_exact_argv_startup_timeout_is_pre_ready_and_bounded(
    tmp_path: Path,
) -> None:
    fake, record, workspace, home = _write_fake_vivado(
        tmp_path, reach_prompt=False
    )
    argv = _exact_argv(
        fake,
        workspace=workspace,
        home=home,
        initialization_timeout_seconds=0.15,
    )
    env = build_mutation_worker_environment(os.environ)
    limits = ProcessLimits(
        initialization_timeout_seconds=1.0,
        whole_run_timeout_seconds=2.0,
        graceful_close_seconds=0.1,
        terminate_grace_seconds=0.3,
        kill_grace_seconds=0.7,
        cleanup_timeout_seconds=1.2,
        output_limit_bytes=64 * 1024,
    )

    async def exercise():
        session = OwnedLineProtocolSession(
            argv,
            ready_line=READY_LINE,
            close_command=request_bytes(
                "worker.exit", "close-timeout", timeout_seconds=0.25
            ),
            limits=limits,
            cwd=workspace,
            env=env,
        )
        with pytest.raises(OwnedProtocolSessionError) as caught:
            await session.start()
        return caught.value.result

    result = asyncio.run(exercise())
    assert record.exists()
    assert result.endpoint is ProcessEndpoint.FAILED
    assert result.returncode == 73
    assert result.cleanup_ok
    assert READY_LINE.encode() not in result.stdout.data


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_public_attestation_command_writes_hash_bindable_path_free_evidence(
    tmp_path: Path,
) -> None:
    output = tmp_path / "worker-attestation.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_wp004a_worker_attestation.py",
            "--output",
            os.fspath(output),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    payload = json.loads(output.read_bytes())
    evidence_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
    assert summary["status"] == payload["status"] == "PASS"
    assert summary["evidence_sha256"] == evidence_sha256
    assert verified_fake_attestation_evidence_sha256(output) == evidence_sha256
    serialized = json.dumps(payload, sort_keys=True)
    assert os.fspath(tmp_path) not in serialized
    assert "/home/" not in serialized
    assert payload["paths_included"] is False
    assert payload["raw_output_included"] is False

    payload["readonly_call"]["command_sha256"] = "0" * 64
    output.write_text(json.dumps(payload, sort_keys=True) + "\n")
    with pytest.raises(
        MutationWorkerBindingError,
        match="fake_attestation_payload_hash_mismatch",
    ):
        verified_fake_attestation_evidence_sha256(output)
