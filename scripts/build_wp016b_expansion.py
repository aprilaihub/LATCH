#!/usr/bin/env python3
"""Build WP-016B's frozen 96-cell Expansion inventory without provider calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016_canary import file_sha256  # noqa: E402
from optimizer_pipeline.dof.wp016b_expansion import (  # noqa: E402
    ANALYSIS_CONTRACT, DERIVED_SEED, DERIVED_SEED_HEX, MANIFEST, SCHEDULE,
    SOURCE_BUNDLE, SOURCE_LEDGER, build_sources, exact_authority,
    materialize_manifests,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-bundle", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--hardblock-scan", type=Path, required=True)
    args = parser.parse_args()
    exact_authority()
    rows = build_sources(
        full_bundle_path=args.full_bundle.resolve(strict=True),
        capture_root=args.capture_root.resolve(strict=True),
        hardblock_scan=args.hardblock_scan.resolve(strict=True),
    )
    materialize_manifests(rows)
    print(json.dumps({
        "status": "PASS_WP016B_EXPANSION_BUILD",
        "source_configurations": len(rows), "scientific_cells": 96,
        "blocks": 32, "derived_seed": DERIVED_SEED,
        "derived_seed_hex": DERIVED_SEED_HEX,
        "provider_calls": 0, "backend_calls": 0,
        "source_ledger_sha256": file_sha256(SOURCE_LEDGER),
        "source_bundle_sha256": file_sha256(SOURCE_BUNDLE),
        "manifest_sha256": file_sha256(MANIFEST),
        "schedule_sha256": file_sha256(SCHEDULE),
        "analysis_contract_sha256": file_sha256(ANALYSIS_CONTRACT),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
