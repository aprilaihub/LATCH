#!/usr/bin/env python3
"""Freeze the WP-014H benchmark gate from source and read-only admission evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


OFFICIAL_PAGE = "https://xilinx.github.io/fpl26_optimization_contest/benchmarks.html"


def sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def parse(path: Path) -> dict[str, object]:
    rows: dict[str, object] = {"source_sha256": sha(path)}
    for line in path.read_text(errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "clock_count":
            rows[key] = int(value)
        elif key == "route_status":
            rows["routing_errors"] = None
            marker = "# of nets with routing errors.......... :"
            if marker in value:
                tail = value.split(marker, 1)[1].split(":", 1)[0]
                try:
                    rows["routing_errors"] = int(tail.strip())
                except ValueError:
                    pass
        else:
            rows[key] = value
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--public-admission", type=Path, required=True)
    parser.add_argument("--fpga24-archive", type=Path, required=True)
    parser.add_argument("--fpl26-archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text())
    development = sorted(bundle["roots"])
    if len(development) != 10:
        raise RuntimeError("exact_ten_development_roots_required")
    archive_rows = subprocess.check_output(["tar", "-tzf", str(args.fpga24_archive)], text=True).splitlines()
    priority = ("koios_clstm_like_large", "titan23_orig_gsm_x6", "mlcad_d181", "ispd16_fpga03")
    candidates = {}
    for name in priority:
        files = sorted(row for row in archive_rows if row.startswith(name))
        candidates[name] = {
            "source_files": files,
            "source_xdc_present": any(row.endswith(".xdc") for row in files),
            "classification": "NO_SOURCE_SUPPORTED_TIMING_CONTRACT",
            "optimization_calls": 0,
        }
    public = {
        "rapidwright_picoblazearray": parse(args.public_admission / "fpga24_picoblaze.txt"),
        "corundum_100g": parse(args.public_admission / "fpga24_corundum.txt"),
    }
    public["rapidwright_picoblazearray"]["classification"] = "NO_TIMING_CONTRACT"
    public["corundum_100g"]["classification"] = "V0_ROUTE_INVALID_AND_NO_TIMING_CONTRACT"
    core = {
        "schema_version": "dof.wp014h-benchmark-freeze-gate.v1",
        "development_known": {"confirmed": len(development), "required": 10, "root_ids": development},
        "same_family_temporal": {
            "admitted": 0,
            "target": [7, 10],
            "official_current_release_observed": "v1.2.0",
            "incoming_claimed_release": "v1.3.0",
            "classification": "SOURCE_DRIFT_UNRESOLVED",
            "official_page": OFFICIAL_PAGE,
            "local_archive_sha256": sha(args.fpl26_archive),
            "local_archive_version": "v1.2.0",
        },
        "true_family_heldout": {
            "admitted": 0,
            "minimum": 4,
            "target": 6,
            "public_dcp_admissions": public,
            "fpga24_priority_sources": candidates,
            "archive_sha256": sha(args.fpga24_archive),
            "old_sealed_wp008_roots_accessed": False,
            "old_sealed_wp008_admission": "NOT_RUN_REQUIRES_EXPLICIT_AUTHORIZATION",
        },
        "gate": "NO_GO",
        "stop_rule_triggered": "fewer than four distinct-family heldouts survive evidence-based candidates",
        "protocol_preserved": {
            "invented_timing_constraints": False,
            "holdout_optimization_calls": 0,
            "sealed_candidate_qor_access": 0,
            "scientific_runs_started": False,
        },
        "reframe_options_requiring_leader_decision": [
            "provide a verified v1.3.0 archive and provenance",
            "provide source-supported clocks/XDC for FPGA24 reconstructions",
            "explicitly authorize read-only admission of the two old sealed WP008 roots",
            "reduce or redesign the family-heldout requirement",
        ],
    }
    core["freeze_sha256"] = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "benchmark-freeze-gate.json").write_text(json.dumps(core, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"gate": core["gate"], "freeze_sha256": core["freeze_sha256"], "stop": core["stop_rule_triggered"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
