#!/usr/bin/env python3
"""Reconcile the frozen WP-015 portfolio against local WP-014I evidence.

This command is deliberately read-only with respect to DCP inputs.  It creates
the external runtime layout and writes a compact, hash-bound Gate-0 aggregate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
WORK = REPO.parents[1]
DEFAULT_OUTPUT = WORK / "wp015-runs" / "bootstrap" / "gate0-entry-v1.json"


def load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO, text=True, stderr=subprocess.DEVNULL
    ).strip()


def source_rows() -> dict[str, dict[str, Any]]:
    development = load(REPO / "configs/wp014i/development-known14-v1.json")
    transfer = load(REPO / "configs/wp014h/benchmark-candidates-continuation-v1.json")
    rows: dict[str, dict[str, Any]] = {}
    for item in development["roots"]:
        rows[item["candidate_id"]] = {
            "source_root_id": item["candidate_id"],
            "dcp": item["dcp"],
            "sha256": item["sha256"],
            "target_clock": development["target_clock"],
            "evidence_source": "configs/wp014i/development-known14-v1.json",
        }
    wanted = {
        "transfer_amd_mini_isp_v2",
        "transfer_boom_soc_v2",
        "transfer_rosetta_3d_v2",
        "transfer_vtr_mcml_v2",
        "transfer_fir_symmetric",
    }
    for item in transfer["dcp_candidates"]:
        if item["candidate_id"] in wanted:
            rows[item["candidate_id"]] = {
                "source_root_id": item["candidate_id"],
                "dcp": item["dcp"],
                "sha256": item["sha256"],
                "target_clock": item["target_clock"],
                "evidence_source": "configs/wp014h/benchmark-candidates-continuation-v1.json",
            }

    synthetic_manifest = load(
        REPO / ".research-flow/artifacts/WP-014I/synthetic-timing-contract-manifest.json"
    )
    synthetic_paths = {
        "finn_mobilenetv1": WORK
        / "wp014i-runs/synthetic-transfer-v1-continuation-05/final/finn_mobilenetv1/finn_mobilenetv1_synthetic.dcp",
        "ispd16_example2_v2": WORK
        / "wp014i-runs/synthetic-transfer-v1-continuation-05/final/ispd16_example2_v2/ispd16_example2_v2_synthetic.dcp",
        "rapidwright_picoblazearray": WORK
        / "wp014i-runs/synthetic-external-v1/final/rapidwright_picoblazearray/rapidwright_picoblazearray_synthetic.dcp",
        "koios_clstm_like_large": WORK
        / "wp014i-runs/synthetic-external-v1/final/koios_clstm_like_large/koios_clstm_like_large_synthetic.dcp",
        "titan23_orig_gsm_x6": WORK
        / "wp014i-runs/synthetic-external-v1/final/titan23_orig_gsm_x6/titan23_orig_gsm_x6_synthetic.dcp",
        "mlcad_d181": WORK
        / "wp014i-runs/synthetic-mlcad-retry-v1-continuation-03/final/mlcad_d181/mlcad_d181_synthetic.dcp",
    }
    section_by_source = {
        "finn_mobilenetv1": synthetic_manifest["transfer"]["records"],
        "ispd16_example2_v2": synthetic_manifest["transfer"]["records"],
        "rapidwright_picoblazearray": synthetic_manifest["external"]["records"],
        "koios_clstm_like_large": synthetic_manifest["external"]["records"],
        "titan23_orig_gsm_x6": synthetic_manifest["external"]["records"],
        "mlcad_d181": synthetic_manifest["mlcad_bounded_retry"]["records"],
    }
    for source_id, section in section_by_source.items():
        item = section[source_id]
        if item.get("admitted"):
            rows[source_id] = {
                "source_root_id": source_id,
                "dcp": str(synthetic_paths[source_id]),
                "sha256": item["final_dcp_sha256"],
                "target_clock": item["certificate"]["clock_target"],
                "period_ns": item["period_ns"],
                "constraint_sha256": item["certificate"]["constraint_sha256"],
                "evidence_source": ".research-flow/artifacts/WP-014I/synthetic-timing-contract-manifest.json",
            }
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    portfolio_path = REPO / ".research-flow/source/WP015-Benchmark-Portfolio-Freeze-v1.json"
    portfolio = load(portfolio_path)
    evidence = source_rows()
    aliases = {"rapidwright_picoblaze": "rapidwright_picoblazearray"}
    rows = []
    problems = []
    for frozen in portfolio["roots"]:
        root_id = frozen["root_id"]
        source_id = aliases.get(root_id, root_id)
        record = evidence.get(source_id)
        if record is None:
            problems.append({"root_id": root_id, "problem": "NO_EVIDENCE_MAPPING"})
            continue
        path = Path(record["dcp"])
        exists = path.is_file()
        observed = sha256(path) if exists else None
        hash_match = observed == record["sha256"]
        if not exists or not hash_match:
            problems.append(
                {
                    "root_id": root_id,
                    "problem": "MISSING_DCP" if not exists else "DCP_HASH_MISMATCH",
                    "expected_sha256": record["sha256"],
                    "observed_sha256": observed,
                }
            )
        rows.append(
            {
                **frozen,
                **record,
                "root_id": root_id,
                "alias_applied": source_id != root_id,
                "exists": exists,
                "observed_sha256": observed,
                "hash_match": hash_match,
            }
        )

    runtime_roots = {}
    for name in ("bootstrap", "scientific", "validation", "references", "aggregate"):
        path = WORK / "wp015-runs" / name
        path.mkdir(parents=True, exist_ok=True)
        runtime_roots[name] = str(path)

    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    report = {
        "schema_version": "dof.wp015-entry-reconciliation.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if len(rows) == 25 and not problems else "FAIL",
        "portfolio_sha256": sha256(portfolio_path),
        "frozen_root_count": len(portfolio["roots"]),
        "reconciled_root_count": len(rows),
        "hash_match_count": sum(row["hash_match"] for row in rows),
        "aliases": aliases,
        "roots": rows,
        "problems": problems,
        "prior_scientific_cell_reuse": 0,
        "runtime_roots": runtime_roots,
        "scientific_source_commit": None,
        "scientific_source_tree": None,
        "handoff_commit": head,
        "handoff_tree": tree,
        "note": "scientific_source_commit/tree remain null until Gate 4 freezes the source",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "status", "frozen_root_count", "reconciled_root_count", "hash_match_count", "problems"
    )}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
