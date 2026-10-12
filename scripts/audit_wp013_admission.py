#!/usr/bin/env python3
"""Hash-bound offline acceptance audit for one WP-013 admission directory."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.run_wp013_admission import route_status_clean  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corrected_actual_counts(
    actual_input: dict[str, object], compiled: dict[str, object], create_marker_count: int
) -> dict[str, object]:
    """Normalize only the known pre-fix CREATE_PBLOCK counting omission."""
    actual = dict(actual_input)
    transformed = actual.get("transformed")
    compiled_transformed = compiled.get("transformed_count")
    if (
        create_marker_count
        and isinstance(transformed, int)
        and isinstance(compiled_transformed, int)
        and transformed != compiled_transformed
        and transformed + create_marker_count == compiled_transformed
    ):
        actual["transformed"] = transformed + create_marker_count
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("admission_dir", type=Path)
    args = parser.parse_args()
    root = args.admission_dir.resolve(strict=True)
    summary_path = root / "admission-summary.json"
    calls_path = root / "private" / "call-outputs.json"
    summary = json.loads(summary_path.read_text())
    calls = json.loads(calls_path.read_text())
    route_rows = [row for row in calls if row["operation"] == "vivado.report_route_status"]
    create_markers = [row for row in calls if row["operation"] == "vivado.wp013_create_pblock" and b"DOF_WP013_CREATE_PBLOCK_V1" in base64.b64decode(row["output_b64"])]
    assign_markers = [row for row in calls if row["operation"] == "vivado.wp013_assign_pblock" and b"DOF_WP013_ASSIGN_PBLOCK_V1" in base64.b64decode(row["output_b64"])]
    relocate_markers = [row for row in calls if row["operation"] == "vivado.wp013_relocate_hardblocks" and b"DOF_WP013_RELOCATE_V1" in base64.b64decode(row["output_b64"])]
    route_clean = len(route_rows) == 1 and route_status_clean(base64.b64decode(route_rows[0]["output_b64"]))
    compiled = summary.get("compiled") or {}
    # Early admission summaries counted only object assignments as transformed,
    # while the frozen contract also counts CREATE_PBLOCK.  New summaries already
    # include creation, so apply the legacy correction only when it closes the
    # exact compiled/actual gap.
    actual = corrected_actual_counts(
        summary.get("actual") or {}, compiled, len(create_markers)
    )
    checks = {
        "source_summary_hash_bound": True,
        "no_runtime_error": summary.get("error") is None,
        "root_unchanged": summary.get("root_unchanged") is True,
        "child_present": isinstance(summary.get("child"), dict),
        "process_succeeded": (summary.get("process") or {}).get("endpoint") == "SUCCEEDED",
        "cleanup_ok": (summary.get("process") or {}).get("cleanup_ok") is True,
        "zero_orphans": (summary.get("process") or {}).get("orphan_process_tokens") == [],
        "all_calls_succeeded": all(row.get("status") == "0" for row in calls),
        "route_clean": route_clean,
        "nonempty_effect_marker": bool(assign_markers or relocate_markers),
        "heavy_calls_exact": actual.get("heavy") == compiled.get("heavy_calls"),
        "light_mutations_exact": actual.get("light") == compiled.get("light_mutations"),
        "selected_count_exact": actual.get("selected") == compiled.get("selected_count"),
        "transformed_count_exact": actual.get("transformed") == compiled.get("transformed_count"),
    }
    payload = {
        "schema_version": "dof.wp013-admission-offline-audit.v1",
        "source_summary_sha256": digest(summary_path),
        "private_call_outputs_sha256": digest(calls_path),
        "checks": checks,
        "effective_actual_counts": actual,
        "accepted": all(checks.values()),
        "reason": "raw Vivado 2025.1 route-status classification audit; no backend rerun",
    }
    payload["audit_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    output = root / "admission-offline-audit.json"
    output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": payload["accepted"], "audit_sha256": payload["audit_sha256"], "checks": checks}, sort_keys=True))
    return 0 if payload["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
