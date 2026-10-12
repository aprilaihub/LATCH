#!/usr/bin/env python3
"""Build the minimal local/private WP-016A scientific replay capsule."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any


ROOT_ID = "transfer_boom_soc_v2"
REFERENCE_JOBS = {
    "wp015__e21ce5ebede6cb5b7fe7f7c518fc7c88",
    "wp015__e1819e2c6f2e16b2f443c5ea31e9d346",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _path_identity(path: Path) -> str:
    return sha256_bytes(str(path.resolve(strict=True)).encode())


def build(args: argparse.Namespace) -> dict[str, Any]:
    if args.output.exists():
        raise FileExistsError("replay_capsule_output_already_exists")
    args.output.mkdir(parents=True)

    source_bundle = json.loads(args.scientific_bundle.read_text(encoding="utf-8"))
    jobs = [row for row in source_bundle["jobs"] if row.get("job_id") in REFERENCE_JOBS]
    if len(jobs) != 2:
        raise ValueError("two_reference_jobs_required")
    subset = {
        "schema_version": "wp016a.replay-scientific-bundle.v1",
        "source_bundle_sha256": file_sha256(args.scientific_bundle),
        "source_schema_version": source_bundle.get("schema_version"),
        "protocol_id": source_bundle.get("protocol_id"),
        "protocol_epoch": source_bundle.get("protocol_epoch"),
        "roots": {ROOT_ID: source_bundle["roots"][ROOT_ID]},
        "observations": {ROOT_ID: source_bundle["observations"][ROOT_ID]},
        "jobs": jobs,
    }
    bundle_target = args.output / "scientific-bundle/scientific-bundle-subset.json"
    write_json(bundle_target, subset)

    copies = [
        ("real_observation", args.capture_root / ROOT_ID / "vivado-observation/real-observation.json",
         Path("capture") / ROOT_ID / "vivado-observation/real-observation.json"),
        ("resolved_private_objects", args.capture_root / ROOT_ID / "contract-probe/private/resolved-objects.json",
         Path("capture") / ROOT_ID / "contract-probe/private/resolved-objects.json"),
        ("rapidwright_net_census", args.capture_root / ROOT_ID / "private/rapidwright-net-census.json",
         Path("capture") / ROOT_ID / "private/rapidwright-net-census.json"),
        ("hardblock_scan_aggregate", args.hardblock_scan / "aggregate.json",
         Path("hardblock-scan/aggregate.json")),
        ("hardblock_scan_gate_audit", args.hardblock_scan / "gate1-audit.json",
         Path("hardblock-scan/gate1-audit.json")),
        ("closed_retrieval_registry", args.retrieval_registry,
         Path("registry/closed-retrieval-registry.json")),
    ]
    entries = [{
        "logical_role": "scientific_bundle_subset",
        "source_path_identity_sha256": _path_identity(args.scientific_bundle),
        "relative_replay_capsule_path": str(bundle_target.relative_to(args.output)),
        "sha256": file_sha256(bundle_target),
        "size_bytes": bundle_target.stat().st_size,
        "immutability_classification": "DERIVED_MINIMAL_FROM_FROZEN_SOURCE",
        "privacy_classification": "PROJECT_SCIENTIFIC_METADATA",
    }]
    for role, source, relative in copies:
        source = source.resolve(strict=True)
        target = args.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        entries.append({
            "logical_role": role,
            "source_path_identity_sha256": _path_identity(source),
            "relative_replay_capsule_path": str(relative),
            "sha256": file_sha256(target),
            "size_bytes": target.stat().st_size,
            "immutability_classification": "BYTE_IDENTICAL_FROZEN_INPUT",
            "privacy_classification": (
                "LOCAL_HANDOFF_PRIVATE_TYPED_IDENTITIES"
                if role == "resolved_private_objects" else "PROJECT_SCIENTIFIC_METADATA"
            ),
        })
    entries.sort(key=lambda row: row["logical_role"])
    merkle = sha256_bytes(canonical_bytes({row["relative_replay_capsule_path"]: row["sha256"]
                                           for row in entries}))
    core = {
        "schema_version": "wp016a.scientific-input-manifest.v1",
        "root_id": ROOT_ID,
        "entries": entries,
        "file_count": len(entries),
        "capsule_merkle_sha256": merkle,
        "privacy_review": {
            "redistribution_scope": "LOCAL_USER_GPT_PRO_HANDOFF_ONLY",
            "contains_secrets_or_api_keys": False,
            "contains_dcp": False,
            "contains_private_typed_object_identities": True,
            "raw_private_reasoning": False,
        },
    }
    manifest = core | {"manifest_sha256": sha256_bytes(canonical_bytes(core))}
    write_json(args.output / "scientific-input-manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scientific-bundle", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--hardblock-scan", type=Path, required=True)
    parser.add_argument("--retrieval-registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args)
    print(json.dumps({"status": "PASS", "files": result["file_count"],
                      "merkle": result["capsule_merkle_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
