#!/usr/bin/env python3
"""Freeze and hash-audit the canonical WP-014C Action Surface v3 registry."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.single_submission.action_surface_v3 import (
    SOURCE_EVIDENCE,
    canonical_json_bytes,
    public_registry,
    registry_sha256,
)


REVIEWED_SOURCE_NAMES = {
    "observed_registry": "vivado-2025.1-command-registry-observed.json",
    "directive_compatibility": "directive-option-compatibility.csv",
    "stage_device_applicability": "stage-device-applicability.csv",
    "mechanism_alignment": "mechanism-alignment-matrix.csv",
    "property_allowlist": "property-allowlist-candidates.json",
    "exclusion_register": "exclusion-register.md",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--reviewed-source", type=Path, required=True)
    parser.add_argument("--freeze-decision", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    observed: dict[str, str] = {
        key: digest(args.reviewed_source / name)
        for key, name in REVIEWED_SOURCE_NAMES.items()
    }
    observed["freeze_decision"] = digest(args.freeze_decision)
    expected = dict(SOURCE_EVIDENCE)
    mismatches = {
        key: {"expected": expected[key], "observed": value}
        for key, value in observed.items()
        if expected.get(key) != value
    }
    if mismatches:
        raise SystemExit("source_evidence_mismatch:" + json.dumps(mismatches, sort_keys=True))

    args.out.mkdir(parents=True, exist_ok=True)
    registry = public_registry()
    raw = canonical_json_bytes(registry)
    (args.out / "action-surface-v3.json").write_bytes(raw + b"\n")
    manifest = {
        "schema_version": "dof.wp014c-action-surface-freeze.v1",
        "accepted": True,
        "registry_sha256": registry_sha256(),
        "registry_bytes": len(raw),
        "source_evidence": expected,
        "operation_count": len(registry["P"]["operations"]),
        "g_generated_node_count": len(registry["G"]["generated_nodes"]),
        "excluded": registry["excluded"],
    }
    manifest_raw = canonical_json_bytes(manifest)
    (args.out / "manifest.json").write_bytes(manifest_raw + b"\n")
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
