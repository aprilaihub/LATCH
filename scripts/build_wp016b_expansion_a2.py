#!/usr/bin/env python3
"""Materialise WP-016B Expansion Attempt 2 with fresh runtime identities."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from optimizer_pipeline.dof.wp016_canary import file_sha256  # noqa: E402
from optimizer_pipeline.dof.wp016b_expansion_a2 import (  # noqa: E402
    MANIFEST, SCHEDULE, build_attempt2_manifest_and_schedule, exact_authority,
    joined_schedule_cells, materialize_attempt2,
)


def main() -> int:
    exact_authority()
    materialize_attempt2()
    manifest, schedule = build_attempt2_manifest_and_schedule()
    joined = joined_schedule_cells(manifest, schedule)
    print(json.dumps({
        "status": "PASS_WP016B_EXPANSION_A2_BUILD",
        "attempt": 2, "scientific_cells": len(joined),
        "blocks": len(schedule["blocks"]),
        "manifest_sha256": file_sha256(MANIFEST),
        "schedule_sha256": file_sha256(SCHEDULE),
        "fresh_identity_counts": {
            key: len({row[2][key] for row in joined})
            for key in ("configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id")
        },
        "provider_calls": 0, "backend_calls": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
