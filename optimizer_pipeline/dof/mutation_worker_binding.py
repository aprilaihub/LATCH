"""Content and launch bindings for the isolated Vivado mutation worker.

The real runner and offline fake-host attestation use the same argv builder.
Only public-safe content identities are persisted; absolute executable and run
paths affect the exact argv digest but are not included in the manifest.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import platform
import sys
from typing import Any, Mapping, Sequence

from .processes import (
    OWNED_LINE_PROTOCOL_RUNNER_VERSION,
    _supervisor_entry_path,
)
from .vivado_mutation_worker import (
    WORKER_BINDING_VERSION,
    WORKER_REQUEST_SCHEMA,
    WORKER_SOURCE_MANIFEST_SCHEMA,
    WORKER_TRANSPORT_PROTOCOL_VERSION,
    WORKER_VERSION,
    mutation_worker_source_manifest_for_entry,
)


WORKER_LAUNCH_BINDING_SCHEMA = "dof.mutation-worker-launch-binding.v1"
FAKE_ATTESTATION_SCHEMA = "dof.wp004a-mutation-worker-fake-attestation.v1"
WORKER_DEPENDENCY_MANIFEST_SCHEMA = "dof.mutation-worker-dependency-manifest.v1"
WORKER_ENVIRONMENT_POLICY_SCHEMA = "dof.mutation-worker-environment-policy.v1"
WORKER_ENVIRONMENT_POLICY_VERSION = "dof.wp004a-vivado-environment.v1"
_RUNTIME_DISTRIBUTIONS = ("pexpect", "ptyprocess")
MUTATION_WORKER_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        # Only license inputs are inherited.  Tool/interpreter paths are exact
        # argv values; HOME/XDG/TMP and locale/PATH are fixed below.
        "XILINXD_LICENSE_FILE",
        "XILINXLM_LICENSE_FILE",
        "XILINX_LICENSE_FILE",
        "LM_LICENSE_FILE",
        "FLEXLM_TIMEOUT",
        "FLEXLM_BATCH",
    }
)
_MUTATION_WORKER_FIXED_ENVIRONMENT = {
    "PATH": os.defpath,
    "LANG": "C",
    "LC_ALL": "C",
}
_MUTATION_WORKER_PRIVATE_OVERRIDES = (
    "HOME",
    "XDG_CONFIG_HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
)
_ARGV_FORM = (
    "{python_executable}",
    "-I",
    "-S",
    "{worker_entry}",
    "--source-manifest-sha256",
    "{source_manifest_sha256}",
    "--dependency-root",
    "{dependency_root}",
    "--dependency-manifest-sha256",
    "{dependency_manifest_sha256}",
    "--vivado-exec",
    "{vivado_executable}",
    "--initialization-timeout-seconds",
    "{initialization_timeout_seconds}",
    "--run-workspace",
    "{run_workspace}",
    "--isolated-home",
    "{isolated_home}",
)


class MutationWorkerBindingError(ValueError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def file_identity(path: str | Path) -> dict[str, Any]:
    target = Path(path).resolve(strict=True)
    if not target.is_file():
        raise MutationWorkerBindingError("binding_target_not_regular_file")
    digest = hashlib.sha256()
    size = 0
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": size}


def mutation_worker_entry_path() -> Path:
    return Path(__file__).resolve().with_name("vivado_mutation_worker.py")


def build_mutation_worker_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Build the closed environment permitted to reach the worker/Vivado.

    This is intentionally an allowlist.  A denylist cannot safely classify
    custom variables such as database URLs or cloud-provider credentials.
    """

    result = {
        str(key): str(value)
        for key, value in environment.items()
        if str(key) in MUTATION_WORKER_ENVIRONMENT_ALLOWLIST
    }
    result.update(_MUTATION_WORKER_FIXED_ENVIRONMENT)
    return result


def mutation_worker_environment_policy() -> dict[str, Any]:
    """Return the value-free policy identity bound by every worker plan."""

    payload = {
        "schema_version": WORKER_ENVIRONMENT_POLICY_SCHEMA,
        "policy_version": WORKER_ENVIRONMENT_POLICY_VERSION,
        "inherited_keys": sorted(MUTATION_WORKER_ENVIRONMENT_ALLOWLIST),
        "fixed_values": dict(sorted(_MUTATION_WORKER_FIXED_ENVIRONMENT.items())),
        "private_run_workspace_overrides": list(
            _MUTATION_WORKER_PRIVATE_OVERRIDES
        ),
        "unlisted_keys": "DROP",
        "public_evidence_value_policy": "REDACTED_PRESENT_FOR_ALL_KEYS",
    }
    return payload | {"policy_sha256": content_hash(payload)}


def mutation_worker_source_manifest(
    entry_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fingerprint the complete local direct-entry implementation surface."""

    entry = (
        mutation_worker_entry_path()
        if entry_path is None
        else Path(entry_path).resolve(strict=True)
    )
    return mutation_worker_source_manifest_for_entry(entry)


def mutation_worker_dependency_manifest() -> dict[str, Any]:
    """Bind the non-stdlib packages imported by the isolated worker closure."""

    dependencies = []
    for distribution_name in _RUNTIME_DISTRIBUTIONS:
        try:
            distribution = importlib.metadata.distribution(distribution_name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise MutationWorkerBindingError(
                f"worker_dependency_unavailable:{distribution_name}"
            ) from exc
        root = Path(distribution.locate_file("")).resolve(strict=True)
        records = []
        for packaged in distribution.files or ():
            relative = PurePosixPath(str(packaged))
            if "__pycache__" in relative.parts or relative.suffix == ".pyc":
                continue
            package_file = relative.parts and relative.parts[0] == distribution_name
            metadata_file = (
                relative.parts
                and relative.parts[0].endswith(".dist-info")
                and relative.name in {"METADATA", "WHEEL"}
            )
            if not package_file and not metadata_file:
                continue
            target = Path(distribution.locate_file(packaged)).resolve(strict=True)
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise MutationWorkerBindingError(
                    f"worker_dependency_file_escapes_root:{distribution_name}"
                ) from exc
            identity = file_identity(target)
            records.append(
                {
                    "relative_name": relative.as_posix(),
                    **identity,
                }
            )
        records.sort(key=lambda item: item["relative_name"])
        if not records:
            raise MutationWorkerBindingError(
                f"worker_dependency_content_unavailable:{distribution_name}"
            )
        dependencies.append(
            {
                "distribution": distribution_name,
                "version": distribution.version,
                "file_count": len(records),
                "total_size_bytes": sum(item["size_bytes"] for item in records),
                "content_sha256": content_hash(records),
            }
        )
    payload = {
        "schema_version": WORKER_DEPENDENCY_MANIFEST_SCHEMA,
        "dependencies": dependencies,
    }
    return payload | {"manifest_sha256": content_hash(payload)}


def mutation_worker_dependency_root() -> Path:
    roots = set()
    for distribution_name in _RUNTIME_DISTRIBUTIONS:
        try:
            distribution = importlib.metadata.distribution(distribution_name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise MutationWorkerBindingError(
                f"worker_dependency_unavailable:{distribution_name}"
            ) from exc
        roots.add(Path(distribution.locate_file("")).resolve(strict=True))
    if len(roots) != 1:
        raise MutationWorkerBindingError("worker_dependency_roots_not_unique")
    return next(iter(roots))


def python_executable_identity(python_exec: str | Path) -> dict[str, Any]:
    """Bind the exact interpreter used to run the trusted freeze command."""

    executable = Path(python_exec).resolve(strict=True)
    try:
        same_interpreter = os.path.samefile(executable, Path(sys.executable))
    except OSError as exc:
        raise MutationWorkerBindingError("python_executable_identity_failed") from exc
    if not same_interpreter:
        raise MutationWorkerBindingError(
            "python_executable_must_match_freeze_interpreter"
        )
    identity = file_identity(executable)
    return {
        "executable_name": executable.name,
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        **identity,
    }


def build_mutation_worker_argv(
    *,
    python_exec: str | Path,
    worker_entry: str | Path,
    vivado_exec: str | Path,
    initialization_timeout_seconds: float,
    run_workspace: str | Path,
    isolated_home: str | Path,
) -> tuple[str, ...]:
    timeout = float(initialization_timeout_seconds)
    if not 0 < timeout <= 900.0:
        raise MutationWorkerBindingError("worker_initialization_timeout_invalid")
    python_invocation = Path(python_exec).absolute()
    if not python_invocation.is_file() or not os.access(python_invocation, os.X_OK):
        raise MutationWorkerBindingError("python_executable_not_executable")
    values = {
        "{python_executable}": os.fspath(python_invocation),
        "{worker_entry}": os.fspath(Path(worker_entry).resolve(strict=True)),
        "{source_manifest_sha256}": mutation_worker_source_manifest(
            worker_entry
        )["manifest_sha256"],
        "{dependency_root}": os.fspath(mutation_worker_dependency_root()),
        "{dependency_manifest_sha256}": mutation_worker_dependency_manifest()[
            "manifest_sha256"
        ],
        "{vivado_executable}": os.fspath(Path(vivado_exec).resolve(strict=True)),
        "{initialization_timeout_seconds}": str(timeout),
        "{run_workspace}": os.fspath(Path(run_workspace).resolve()),
        "{isolated_home}": os.fspath(Path(isolated_home).resolve()),
    }
    return tuple(values.get(item, item) for item in _ARGV_FORM)


def mutation_worker_argv_form() -> dict[str, Any]:
    """Return the public-safe argv shape shared by fake and real launches."""

    payload = {
        "binding_version": WORKER_BINDING_VERSION,
        "argv": list(_ARGV_FORM),
    }
    return payload | {"argv_form_sha256": content_hash(payload)}


def argv_sha256(argv: Sequence[str]) -> str:
    return content_hash([str(item) for item in argv])


def mutation_worker_launch_binding(
    *,
    python_exec: str | Path,
    worker_entry: str | Path,
    vivado_exec: str | Path,
    initialization_timeout_seconds: float,
    run_workspace: str | Path,
    isolated_home: str | Path,
    fake_startup_attestation_sha256: str,
) -> dict[str, Any]:
    if not _is_sha256(fake_startup_attestation_sha256):
        raise MutationWorkerBindingError("fake_attestation_hash_invalid")
    source = mutation_worker_source_manifest(worker_entry)
    dependencies = mutation_worker_dependency_manifest()
    argv = build_mutation_worker_argv(
        python_exec=python_exec,
        worker_entry=worker_entry,
        vivado_exec=vivado_exec,
        initialization_timeout_seconds=initialization_timeout_seconds,
        run_workspace=run_workspace,
        isolated_home=isolated_home,
    )
    entry = next(
        item for item in source["files"] if item["role"] == "isolated_worker_entry"
    )
    payload = {
        "schema_version": WORKER_LAUNCH_BINDING_SCHEMA,
        "binding_version": WORKER_BINDING_VERSION,
        "worker_entry": entry,
        "transitive_source_manifest": source,
        "runtime_dependency_manifest": dependencies,
        "process_supervisor": {
            "relative_name": _supervisor_entry_path().name,
            **file_identity(_supervisor_entry_path()),
        },
        "outer_process_runner_version": OWNED_LINE_PROTOCOL_RUNNER_VERSION,
        "environment_policy": mutation_worker_environment_policy(),
        "argv_form": mutation_worker_argv_form(),
        "production_argv_sha256": argv_sha256(argv),
        "python_executable": python_executable_identity(python_exec),
        "worker_version": WORKER_VERSION,
        "transport_protocol_version": WORKER_TRANSPORT_PROTOCOL_VERSION,
        "request_schema_version": WORKER_REQUEST_SCHEMA,
        "fake_startup_attestation_sha256": fake_startup_attestation_sha256,
    }
    return payload | {"binding_sha256": content_hash(payload)}


def verified_fake_attestation_evidence_sha256(path: str | Path) -> str:
    """Validate a public fake-attestation document and hash its exact bytes."""

    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MutationWorkerBindingError("fake_attestation_not_json") from exc
    if not isinstance(value, dict):
        raise MutationWorkerBindingError("fake_attestation_not_object")
    if set(value) != {
        "schema_version",
        "technical_non_scientific",
        "fake_vivado",
        "status",
        "source_manifest_sha256",
        "worker_entry_sha256",
        "argv_form_sha256",
        "dependency_manifest_sha256",
        "environment_policy_sha256",
        "fake_exact_argv_sha256",
        "python_executable",
        "worker_attestation",
        "startup_output",
        "readonly_call",
        "process",
        "fake_vivado_start_observed",
        "raw_output_included",
        "paths_included",
        "attestation_payload_sha256",
    }:
        raise MutationWorkerBindingError("fake_attestation_fields_not_exact")
    integrity_payload = dict(value)
    claimed_payload_hash = integrity_payload.pop(
        "attestation_payload_sha256", None
    )
    if claimed_payload_hash != content_hash(integrity_payload):
        raise MutationWorkerBindingError("fake_attestation_payload_hash_mismatch")
    source = mutation_worker_source_manifest()
    dependencies = mutation_worker_dependency_manifest()
    environment_policy = mutation_worker_environment_policy()
    form = mutation_worker_argv_form()
    if (
        value.get("schema_version") != FAKE_ATTESTATION_SCHEMA
        or value.get("status") != "PASS"
        or value.get("technical_non_scientific") is not True
        or value.get("fake_vivado") is not True
        or value.get("source_manifest_sha256")
        != source["manifest_sha256"]
        or value.get("worker_entry_sha256") != source["files"][0]["sha256"]
        or value.get("argv_form_sha256") != form["argv_form_sha256"]
        or value.get("dependency_manifest_sha256")
        != dependencies["manifest_sha256"]
        or value.get("environment_policy_sha256")
        != environment_policy["policy_sha256"]
        or not _is_sha256(value.get("fake_exact_argv_sha256"))
        or value.get("python_executable")
        != python_executable_identity(sys.executable)
        or value.get("fake_vivado_start_observed") is not True
        or value.get("raw_output_included") is not False
        or value.get("paths_included") is not False
    ):
        raise MutationWorkerBindingError("fake_attestation_contract_mismatch")
    process = value.get("process")
    worker = value.get("worker_attestation")
    startup = value.get("startup_output")
    readonly = value.get("readonly_call")
    if (
        not isinstance(process, dict)
        or set(process) != {
            "endpoint",
            "returncode",
            "cleanup_ok",
            "observed_process_count",
            "orphan_count",
            "signal_event_count",
            "lifecycle_sha256",
        }
        or process.get("endpoint") != "SUCCEEDED"
        or process.get("returncode") != 0
        or process.get("cleanup_ok") is not True
        or not isinstance(process.get("observed_process_count"), int)
        or process["observed_process_count"] < 2
        or process.get("orphan_count") != 0
        or process.get("signal_event_count") != 0
        or not _is_sha256(process.get("lifecycle_sha256"))
        or not isinstance(worker, dict)
        or set(worker) != {
            "status",
            "vivado_version",
            "python_version",
            "effective_environment_sha256",
            "worker_version",
            "transport_protocol_version",
        }
        or worker.get("status") != "PASS"
        or worker.get("vivado_version") != "2025.1"
        or worker.get("python_version") != platform.python_version()
        or not _is_sha256(worker.get("effective_environment_sha256"))
        or worker.get("worker_version") != WORKER_VERSION
        or worker.get("transport_protocol_version")
        != WORKER_TRANSPORT_PROTOCOL_VERSION
        or not isinstance(startup, dict)
        or set(startup) != {"sha256", "size_bytes", "truncated"}
        or not _is_sha256(startup.get("sha256"))
        or not isinstance(startup.get("size_bytes"), int)
        or startup["size_bytes"] <= 0
        or startup.get("truncated") is not False
        or not isinstance(readonly, dict)
        or set(readonly) != {
            "operation_id",
            "command_sha256",
            "output_sha256",
            "output_size_bytes",
            "status_code",
        }
        or readonly.get("operation_id") != "vivado.report_route_status"
        or not _is_sha256(readonly.get("command_sha256"))
        or not _is_sha256(readonly.get("output_sha256"))
        or not isinstance(readonly.get("output_size_bytes"), int)
        or readonly["output_size_bytes"] <= 0
        or readonly.get("status_code") != 0
    ):
        raise MutationWorkerBindingError("fake_attestation_result_invalid")
    return hashlib.sha256(raw).hexdigest()


def validate_launch_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MutationWorkerBindingError("worker_launch_binding_not_object")
    binding = json.loads(_canonical_bytes(dict(value)))
    claimed = binding.pop("binding_sha256", None)
    if set(binding) != {
        "schema_version",
        "binding_version",
        "worker_entry",
        "transitive_source_manifest",
        "runtime_dependency_manifest",
        "process_supervisor",
        "outer_process_runner_version",
        "environment_policy",
        "argv_form",
        "production_argv_sha256",
        "python_executable",
        "worker_version",
        "transport_protocol_version",
        "request_schema_version",
        "fake_startup_attestation_sha256",
    }:
        raise MutationWorkerBindingError("worker_launch_binding_fields_not_exact")
    if binding.get("schema_version") != WORKER_LAUNCH_BINDING_SCHEMA:
        raise MutationWorkerBindingError("worker_launch_binding_schema_mismatch")
    if claimed != content_hash(binding):
        raise MutationWorkerBindingError("worker_launch_binding_hash_mismatch")
    source = binding.get("transitive_source_manifest")
    if not isinstance(source, dict):
        raise MutationWorkerBindingError("worker_source_manifest_missing")
    source_copy = dict(source)
    source_claimed = source_copy.pop("manifest_sha256", None)
    if (
        source_copy.get("schema_version") != WORKER_SOURCE_MANIFEST_SCHEMA
        or source_claimed != content_hash(source_copy)
    ):
        raise MutationWorkerBindingError("worker_source_manifest_hash_mismatch")
    files = source_copy.get("files")
    if not isinstance(files, list) or [item.get("role") for item in files] != [
        "isolated_worker_entry",
        "shared_owned_vivado_implementation",
    ]:
        raise MutationWorkerBindingError("worker_source_manifest_files_invalid")
    if [item.get("relative_name") for item in files] != [
        "vivado_mutation_worker.py",
        "vivado_readonly_worker.py",
    ]:
        raise MutationWorkerBindingError("worker_source_manifest_names_invalid")
    if any(
        not isinstance(item, dict)
        or not _is_sha256(item.get("sha256"))
        or not isinstance(item.get("size_bytes"), int)
        or item["size_bytes"] <= 0
        for item in files
    ):
        raise MutationWorkerBindingError("worker_source_manifest_identity_invalid")
    if binding.get("worker_entry") != files[0]:
        raise MutationWorkerBindingError("worker_entry_binding_mismatch")
    dependencies = binding.get("runtime_dependency_manifest")
    if not isinstance(dependencies, dict):
        raise MutationWorkerBindingError("worker_dependency_manifest_missing")
    dependency_copy = dict(dependencies)
    dependency_claimed = dependency_copy.pop("manifest_sha256", None)
    dependency_rows = dependency_copy.get("dependencies")
    if (
        dependency_copy.get("schema_version")
        != WORKER_DEPENDENCY_MANIFEST_SCHEMA
        or dependency_claimed != content_hash(dependency_copy)
        or not isinstance(dependency_rows, list)
        or [item.get("distribution") for item in dependency_rows]
        != list(_RUNTIME_DISTRIBUTIONS)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("version"), str)
            or not item["version"]
            or not isinstance(item.get("file_count"), int)
            or item["file_count"] <= 0
            or not isinstance(item.get("total_size_bytes"), int)
            or item["total_size_bytes"] <= 0
            or not _is_sha256(item.get("content_sha256"))
            for item in dependency_rows
        )
    ):
        raise MutationWorkerBindingError("worker_dependency_manifest_invalid")
    supervisor = binding.get("process_supervisor")
    expected_supervisor = {
        "relative_name": _supervisor_entry_path().name,
        **file_identity(_supervisor_entry_path()),
    }
    if supervisor != expected_supervisor:
        raise MutationWorkerBindingError("process_supervisor_binding_mismatch")
    if (
        binding.get("outer_process_runner_version")
        != OWNED_LINE_PROTOCOL_RUNNER_VERSION
    ):
        raise MutationWorkerBindingError("outer_process_runner_version_mismatch")
    if binding.get("argv_form") != mutation_worker_argv_form():
        raise MutationWorkerBindingError("worker_argv_form_mismatch")
    if binding.get("environment_policy") != mutation_worker_environment_policy():
        raise MutationWorkerBindingError("worker_environment_policy_mismatch")
    if (
        source_copy.get("worker_version") != WORKER_VERSION
        or source_copy.get("transport_protocol_version")
        != WORKER_TRANSPORT_PROTOCOL_VERSION
        or source_copy.get("request_schema_version") != WORKER_REQUEST_SCHEMA
        or binding.get("worker_version") != WORKER_VERSION
        or binding.get("transport_protocol_version")
        != WORKER_TRANSPORT_PROTOCOL_VERSION
        or binding.get("request_schema_version") != WORKER_REQUEST_SCHEMA
    ):
        raise MutationWorkerBindingError("worker_protocol_binding_mismatch")
    python = binding.get("python_executable")
    if (
        not isinstance(python, dict)
        or not _is_sha256(python.get("sha256"))
        or not isinstance(python.get("size_bytes"), int)
        or python["size_bytes"] <= 0
        or not isinstance(python.get("version"), str)
        or not python["version"]
    ):
        raise MutationWorkerBindingError("python_executable_binding_invalid")
    for key in (
        "production_argv_sha256",
        "fake_startup_attestation_sha256",
    ):
        if not _is_sha256(binding.get(key)):
            raise MutationWorkerBindingError(f"{key}_invalid")
    return binding | {"binding_sha256": claimed}


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
