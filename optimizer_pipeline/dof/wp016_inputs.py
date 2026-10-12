"""Hash-verified external input and environment bindings for WP-016A."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import ssl
import sys
from typing import Any, Mapping


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def value_sha256(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_scientific_input_manifest(path: Path) -> dict[str, Path]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    claimed = manifest.pop("manifest_sha256", None)
    if (
        manifest.get("schema_version") != "wp016a.scientific-input-manifest.v1"
        or claimed != value_sha256(manifest)
    ):
        raise RuntimeError("scientific_input_manifest_invalid")
    root = path.resolve(strict=True).parent
    resolved: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for row in manifest.get("entries", ()):
        target = (root / str(row["relative_replay_capsule_path"])).resolve(strict=True)
        if root != target and root not in target.parents:
            raise RuntimeError("scientific_input_path_escape")
        observed = file_sha256(target)
        if observed != row.get("sha256") or target.stat().st_size != row.get("size_bytes"):
            raise RuntimeError("scientific_input_hash_drift:" + str(row.get("logical_role")))
        role = str(row["logical_role"])
        if role in resolved:
            raise RuntimeError("duplicate_scientific_input_role:" + role)
        resolved[role] = target
        hashes[str(row["relative_replay_capsule_path"])] = observed
    if len(resolved) != manifest.get("file_count") or value_sha256(hashes) != manifest.get("capsule_merkle_sha256"):
        raise RuntimeError("scientific_input_capsule_merkle_mismatch")
    required = {
        "scientific_bundle_subset", "real_observation", "resolved_private_objects",
        "rapidwright_net_census", "hardblock_scan_aggregate",
        "hardblock_scan_gate_audit", "closed_retrieval_registry",
    }
    if set(resolved) != required:
        raise RuntimeError("scientific_input_role_set_mismatch")
    return resolved


def environment_snapshot(*, repository_root: Path) -> dict[str, Any]:
    distributions = sorted(
        (dist.metadata.get("Name", "UNKNOWN"), dist.version)
        for dist in importlib.metadata.distributions()
    )
    dependency_lock = [{"name": name, "version": version} for name, version in distributions]
    relevant = {}
    for name in ("httpx", "jsonschema", "pytest"):
        relevant[name] = importlib.metadata.version(name)
    executable = Path(sys.executable).resolve(strict=True)
    provider_files = {
        "provider_profiles": repository_root / "configs/wp014e/provider-profiles-v1.json",
        "provider_pricing": repository_root / "configs/wp014e/pricing-snapshot-2026-08-12.json",
        "wp016a_current_pricing": repository_root / "configs/wp016a/openai-current-pricing-cache-audit.json",
    }
    core = {
        "schema_version": "wp016a.environment-manifest.v1",
        "python": {
            "executable_identity_sha256": value_sha256(str(executable).encode()),
            "executable_sha256": file_sha256(executable),
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "platform": {
            "system": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "libc": list(platform.libc_ver()),
            "openssl": ssl.OPENSSL_VERSION,
        },
        "relevant_distributions": relevant,
        "installed_distribution_lock_sha256": value_sha256(dependency_lock),
        "installed_distribution_count": len(dependency_lock),
        "provider_files": {name: {"sha256": file_sha256(path), "size_bytes": path.stat().st_size}
                           for name, path in provider_files.items()},
        "environment_overrides_rejected": [
            "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_SERVICE_TIER",
            "OPENAI_PROMPT_CACHE_MODE",
        ],
    }
    return core | {"manifest_sha256": value_sha256(core)}


def write_environment_manifest(path: Path, *, repository_root: Path) -> dict[str, Any]:
    value = environment_snapshot(repository_root=repository_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return value


def verify_environment_manifest(path: Path, *, repository_root: Path) -> dict[str, Any]:
    expected = json.loads(path.read_text(encoding="utf-8"))
    observed = environment_snapshot(repository_root=repository_root)
    if expected != observed:
        raise RuntimeError("environment_manifest_drift")
    return observed


def runtime_manifest(*, runtime_base: Path, cell_count: int = 6) -> dict[str, Any]:
    if cell_count not in (5, 6):
        raise ValueError("runtime_manifest_cell_count_must_be_five_or_six")
    resolved = runtime_base.resolve()
    core = {
        "schema_version": "wp016a.runtime-manifest.v2",
        "runtime_base_path_identity_sha256": value_sha256(str(resolved).encode()),
        "runtime_root_derivation": "<runtime_base>/wp016a-run__sha256(run_nonce)[:24]",
        "required_subdirectories": [
            "authority", "journal", "cells", "requests", "responses", "logs", "final",
        ],
        "cell_subdirectory_count": cell_count,
        "canonical_configuration_identity_kind": "configuration",
        "identity_path_binding_manifest": "authority/identity-path-bindings.json",
        "per_turn_request_receipt": "requests/<cell_id>__turn-<n>.json",
        "per_turn_response_receipt": "responses/<cell_id>__turn-<n>.json",
        "per_cell_result": "cells/<cell_id>/output/cell-result.json",
        "fail_on_existing_runtime_root": True,
        "one_shot_consumption_before_request_construction": True,
    }
    return core | {"manifest_sha256": value_sha256(core)}


def write_runtime_manifest(path: Path, *, runtime_base: Path,
                           cell_count: int = 6) -> dict[str, Any]:
    value = runtime_manifest(runtime_base=runtime_base, cell_count=cell_count)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return value


def verify_runtime_manifest(path: Path, *, runtime_base: Path) -> dict[str, Any]:
    expected = json.loads(path.read_text(encoding="utf-8"))
    observed = runtime_manifest(
        runtime_base=runtime_base,
        cell_count=int(expected.get("cell_subdirectory_count", 0)),
    )
    if expected != observed:
        raise RuntimeError("runtime_manifest_drift")
    return observed


__all__ = [
    "environment_snapshot", "file_sha256", "runtime_manifest",
    "verify_environment_manifest", "verify_runtime_manifest",
    "verify_scientific_input_manifest", "write_environment_manifest",
    "write_runtime_manifest",
]
