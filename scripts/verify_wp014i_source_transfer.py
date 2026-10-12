#!/usr/bin/env python3
"""Re-verify immutable source-timed temporal-transfer evidence by hash."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


REPO = Path(__file__).resolve().parents[1]


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if git("status", "--porcelain"):
        raise RuntimeError("repository_must_be_clean")
    plan = read(args.config)
    if any(plan["limits"].values()):
        raise ValueError("source_transfer_verification_firewall_not_zero")
    source_config_path = REPO / plan["source_config"]
    source = read(source_config_path)
    prior_path = Path(plan["prior_summary"])
    prior = read(prior_path)
    claimed = prior.get("summary_sha256")
    unsigned = dict(prior)
    unsigned.pop("summary_sha256", None)
    if claimed != digest(unsigned) or claimed != plan["expected_prior_summary_sha256"]:
        raise ValueError("prior_source_transfer_summary_hash_mismatch")
    if prior["config_sha256"] != file_sha256(source_config_path):
        raise ValueError("prior_source_transfer_config_hash_mismatch")
    official = source["official_assets"]["fpl26_v1_3"]
    archive = Path(official["archive"])
    checksum_asset = Path(str(archive).removesuffix(".tar.gz") + ".sha256")
    archive_ok = file_sha256(archive) == official["archive_sha256"]
    checksum_asset_ok = (
        checksum_asset.is_file()
        and file_sha256(checksum_asset) == official["checksum_asset_sha256"]
    )
    candidates = {
        row["candidate_id"]: row for row in source["dcp_candidates"]
        if row["portfolio"] == "same_family_temporal"
    }
    prior_admissions = {row["candidate_id"]: row for row in prior["admissions"]}
    rows = []
    for candidate_id in plan["required_candidate_ids"]:
        candidate = candidates[candidate_id]
        admission = prior_admissions[candidate_id]
        dcp = Path(candidate["dcp"])
        current_sha = file_sha256(dcp)
        rows.append({
            "candidate_id": candidate_id,
            "family": candidate["family"],
            "source": candidate["source"],
            "dcp_sha256": current_sha,
            "hash_unchanged": current_sha == candidate["sha256"],
            "prior_v0": admission["v0"],
            "prior_v1": admission["v1"],
            "prior_admitted": admission["admitted"],
            "reused_without_new_validation": True,
        })
    accepted = bool(
        archive_ok and checksum_asset_ok and len(rows) == 5
        and all(
            row["hash_unchanged"] and row["prior_admitted"]
            and row["prior_v0"] == "V0_PASS"
            and row["prior_v1"] == "MULTISEED_FUNCTIONAL_PASS"
            for row in rows
        )
    )
    core = {
        "schema_version": "dof.wp014i-source-transfer-verification.v1",
        "identity": plan["identity"],
        "repository": {
            "head": git("rev-parse", "HEAD"),
            "tree": git("rev-parse", "HEAD^{tree}"),
        },
        "source_config_sha256": file_sha256(source_config_path),
        "prior_summary_sha256": claimed,
        "archive_sha256": file_sha256(archive),
        "archive_hash_unchanged": archive_ok,
        "checksum_asset_sha256": file_sha256(checksum_asset) if checksum_asset.is_file() else None,
        "checksum_asset_hash_unchanged": checksum_asset_ok,
        "rows": rows,
        "admitted_ids": [row["candidate_id"] for row in rows if row["prior_admitted"]],
        "accepted": accepted,
        "provider_or_llm_calls": 0,
        "optimization_calls": 0,
        "validation_calls": 0,
        "sealed_holdout_accesses": 0,
    }
    result = core | {"aggregate_sha256": digest(core)}
    args.output.mkdir(parents=True)
    (args.output / "aggregate.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    print(json.dumps({"accepted": accepted, "admitted": len(result["admitted_ids"])}, sort_keys=True))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
