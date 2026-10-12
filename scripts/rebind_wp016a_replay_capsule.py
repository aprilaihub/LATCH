#!/usr/bin/env python3
"""Rebind a verified WP-016A capsule to a reviewed retrieval registry."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016_inputs import verify_scientific_input_manifest  # noqa: E402


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build(source_manifest: Path, registry: Path, output: Path) -> dict[str, Any]:
    verify_scientific_input_manifest(source_manifest)
    if output.exists():
        raise FileExistsError("rebound_capsule_output_already_exists")
    source_root = source_manifest.resolve(strict=True).parent
    shutil.copytree(source_root, output)
    manifest_path = output / "scientific-input-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("manifest_sha256", None)
    row = next(entry for entry in manifest["entries"]
               if entry["logical_role"] == "closed_retrieval_registry")
    target = output / row["relative_replay_capsule_path"]
    shutil.copyfile(registry.resolve(strict=True), target)
    row.update({
        "source_path_identity_sha256": digest(str(registry.resolve()).encode()),
        "sha256": digest(target.read_bytes()),
        "size_bytes": target.stat().st_size,
        "immutability_classification": "DERIVED_GENERIC_PROTOCOL_REPAIR_FROM_FROZEN_REGISTRY",
    })
    hashes = {
        entry["relative_replay_capsule_path"]: entry["sha256"]
        for entry in manifest["entries"]
    }
    manifest["capsule_merkle_sha256"] = digest(canonical_bytes(hashes))
    manifest["manifest_sha256"] = digest(canonical_bytes(manifest))
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    verify_scientific_input_manifest(manifest_path)
    return {
        "status": "PASS_ZERO_CALL_CAPSULE_REBIND",
        "manifest_sha256": digest(manifest_path.read_bytes()),
        "capsule_merkle_sha256": manifest["capsule_merkle_sha256"],
        "registry_sha256": row["sha256"],
        "file_count": manifest["file_count"],
        "provider_calls": 0,
        "backend_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--retrieval-registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(
        args.source_manifest, args.retrieval_registry, args.output,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
