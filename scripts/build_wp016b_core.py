#!/usr/bin/env python3
"""Build the frozen WP-016B source ledger, payloads and blocked schedule."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016b import (  # noqa: E402
    ANALYSIS_CONTRACT, MANIFEST, SCHEDULE, SOURCE_BUNDLE, SOURCE_LEDGER,
    build_sources, exact_authority, materialize_manifests,
)
from optimizer_pipeline.dof.wp016_canary import file_sha256  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-bundle", type=Path, required=True)
    parser.add_argument("--accepted-capsule-bundle", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--hardblock-scan", type=Path, required=True)
    args = parser.parse_args()
    exact_authority()
    rows = build_sources(
        full_bundle_path=args.full_bundle.resolve(strict=True),
        accepted_capsule_bundle_path=args.accepted_capsule_bundle.resolve(strict=True),
        capture_root=args.capture_root.resolve(strict=True),
        hardblock_scan=args.hardblock_scan.resolve(strict=True),
    )
    materialize_manifests(rows)
    print(json.dumps({
        "status": "PASS_WP016B_CORE_BUILD", "source_configurations": len(rows),
        "provider_calls": 0, "backend_calls": 0,
        "source_ledger_sha256": file_sha256(SOURCE_LEDGER),
        "source_bundle_sha256": file_sha256(SOURCE_BUNDLE),
        "manifest_sha256": file_sha256(MANIFEST), "schedule_sha256": file_sha256(SCHEDULE),
        "analysis_contract_sha256": file_sha256(ANALYSIS_CONTRACT),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
