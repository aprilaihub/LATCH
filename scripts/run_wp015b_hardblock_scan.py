#!/usr/bin/env python3
"""Run the QoR-blind 14-root WP-015B hardblock compatibility scan."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.hardblock_compatibility import build_relation, digest, validate_relation  # noqa: E402
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry  # noqa: E402


RESOURCE_ORDER = {"CORE": 0, "LARGE": 1, "VERY_LARGE": 2}
PROTOCOL_ID = "WP015B_HARDBLOCK_COMPATIBILITY_SCAN_V2"


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def materialize_relation(
    root: Mapping[str, Any], probe_dir: Path,
) -> dict[str, Any]:
    summary = read(probe_dir / "admission-summary.json")
    if summary.get("accepted") is not True:
        raise ValueError("probe_not_accepted")
    forbidden_qor = {"pre_timing", "post_timing", "route_valid"}.intersection(summary)
    if forbidden_qor:
        raise ValueError("qor_fields_present:" + ",".join(sorted(forbidden_qor)))
    raw = read(probe_dir / "private/resolved-objects.json")
    meta = read(probe_dir / "private/hardblock-compatibility-queries.json")
    hardblocks = raw.get("critical_hardblocks", [])
    if not isinstance(hardblocks, list):
        raise ValueError("hardblock_rows_invalid")
    secret = hashlib.sha256(
        (PROTOCOL_ID + ":" + str(root["sha256"])).encode("utf-8")
    ).digest()
    registry = TrialHandleRegistry(
        trial_id=PROTOCOL_ID + ":" + str(root["root_id"]),
        root_sha256=str(root["sha256"]), secret=secret,
    )
    meta_by_index = {
        int(row["target_index"]): row for row in meta.get("targets", [])
        if isinstance(row, Mapping)
    }
    sites_by_index: dict[int, list[str]] = {}
    query_hashes: dict[int, str] = {}
    for index in range(len(hardblocks)):
        item = meta_by_index.get(index)
        if item is None:
            raise ValueError("target_site_query_metadata_missing")
        evidence_key = str(item["evidence_key"])
        site_rows = raw.get(evidence_key, [])
        if not isinstance(site_rows, list):
            raise ValueError("target_site_rows_invalid")
        observed_hash = hashlib.sha256(
            json.dumps(site_rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if observed_hash != item.get("query_result_sha256"):
            raise ValueError("target_site_query_hash_mismatch")
        query_hashes[index] = observed_hash
        handles = []
        for row in site_rows:
            handles.append(registry.register(
                object_type="site", canonical_identity=str(row["identity"]),
                attributes={"site_class": str(row.get("attribute") or "UNKNOWN")},
            ))
        sites_by_index[index] = handles

    target_rows = []
    target_query_sha = digest(hardblocks)
    target_site_query_sha: dict[str, str] = {}
    for index, raw_target in enumerate(hardblocks):
        observed = str(raw_target.get("attribute") or "").upper()
        resource = (
            "DSP" if "DSP" in observed else "URAM" if "URAM" in observed
            else "BRAM" if "RAMB" in observed else "ANY"
        )
        identity = str(raw_target["identity"])
        handle = registry.register(
            object_type="hardblock", canonical_identity=identity,
            attributes={
                "path_member": True,
                "path_rank": int(raw_target["index"]) + 1,
                "resource_type": resource,
                "compatible_site_handles": sites_by_index[index],
            },
        )
        target_rows.append({
            "handle": handle,
            "target_identity_sha256": hashlib.sha256(identity.encode()).hexdigest(),
            "resource_type": resource,
            "compatible_site_handles": sites_by_index[index],
        })
        target_site_query_sha[handle] = query_hashes[index]

    relation = build_relation(
        root_sha256=str(root["sha256"]), stage="ROUTED",
        target_rows=target_rows, target_query_sha256=target_query_sha,
        target_site_query_sha256=target_site_query_sha,
    )
    validate_relation(relation)
    query_payload = {
        "schema_version": "dof.single-submission.query-q.v2",
        "query_name": "critical_hardblock_candidates",
        "rows": [
            {
                "handle": row["target_handle"],
                "path_rank": index + 1,
                "resource_type": row["resource_type"],
                "compatible_site_handles": row["compatible_site_handles"],
            }
            for index, row in enumerate(relation["targets"])
        ],
    }
    return {
        "root_id": root["root_id"],
        "root_sha256": root["sha256"],
        "resource_class": root["resource_class"],
        "probe_accepted": summary.get("accepted") is True,
        "probe_error": summary.get("error"),
        "root_unchanged": summary.get("root_unchanged") is True,
        "query_item_size_bytes": len(json.dumps(query_payload, sort_keys=True, separators=(",", ":")).encode()),
        "relation": relation,
    }


async def run(args: argparse.Namespace) -> int:
    bundle = read(args.bundle)
    roots = sorted(
        [dict(row) | {"root_id": root_id} for root_id, row in bundle["roots"].items()
         if row.get("split") == "DEVELOPMENT_KNOWN"],
        key=lambda row: str(row["root_id"]),
    )
    if len(roots) != 14:
        raise ValueError("wp015b_requires_exact_14_development_roots")
    args.output.mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(args.max_parallel)

    async def one(root: Mapping[str, Any]) -> dict[str, Any]:
        root_path = Path(str(root["root_path"])).resolve(strict=True)
        if file_sha256(root_path) != root["sha256"]:
            raise RuntimeError("root_hash_mismatch:" + str(root["root_id"]))
        out = args.output / "probes" / str(root["root_id"])
        command = [
            str(args.python), str(REPO / "scripts/run_wp013_admission.py"),
            "--mode", "hardblock-compatibility-probe", "--root", str(root_path),
            "--out", str(out), "--python", str(args.python), "--vivado", str(args.vivado),
        ]
        async with semaphore:
            completed = await asyncio.to_thread(
                subprocess.run, command, cwd=REPO, text=True,
                capture_output=True, timeout=args.timeout_seconds,
            )
        if not (out / "admission-summary.json").is_file():
            return {
                "root_id": root["root_id"], "root_sha256": root["sha256"],
                "resource_class": root["resource_class"], "probe_accepted": False,
                "returncode": completed.returncode,
                "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
                "error": "probe_summary_missing",
            }
        try:
            row = materialize_relation(root, out)
        except Exception as exc:
            summary = read(out / "admission-summary.json")
            return {
                "root_id": root["root_id"], "root_sha256": root["sha256"],
                "resource_class": root["resource_class"], "probe_accepted": False,
                "returncode": completed.returncode,
                "error": f"materialization:{type(exc).__name__}:{exc}",
                "probe_error": summary.get("error"),
            }
        write(args.output / "relations" / f"{root['root_id']}.json", row)
        return row | {"returncode": completed.returncode}

    rows = await asyncio.gather(*(one(root) for root in roots))
    feasible = [row for row in rows if row.get("probe_accepted") and row.get("relation", {}).get("feasible")]
    feasible.sort(key=lambda row: (RESOURCE_ORDER.get(str(row["resource_class"]), 99), str(row["root_id"])))
    selected = feasible[0] if feasible else None
    core = {
        "schema_version": "dof.wp015b-hardblock-scan.v1",
        "protocol_id": PROTOCOL_ID,
        "status": "PASS" if selected is not None else "STOP_NO_FEASIBLE_DEVELOPMENT_ROOT",
        "development_root_count": len(roots),
        "probe_accepted_count": sum(bool(row.get("probe_accepted")) for row in rows),
        "feasible_root_count": len(feasible),
        "selected_admission_root": None if selected is None else selected["root_id"],
        "selection_rule": "lowest frozen resource class then root_id lexical order",
        "qor_fields_used": [],
        "rows": rows,
    }
    result = core | {"aggregate_sha256": digest(core)}
    write(args.output / "aggregate.json", result)
    print(json.dumps({
        "status": result["status"], "roots": len(rows),
        "feasible_roots": len(feasible), "selected": result["selected_admission_root"],
    }, sort_keys=True))
    return 0 if selected is not None else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--bundle", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--vivado", type=Path, required=True)
    value.add_argument("--max-parallel", type=int, default=14)
    value.add_argument("--timeout-seconds", type=float, default=3600.0)
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
