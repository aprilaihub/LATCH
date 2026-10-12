#!/usr/bin/env python3
"""Capture object-specific Vivado property metadata on development roots."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any


REPO = Path(__file__).resolve().parents[1]
SCHEMA = "dof.wp014b-property-capture.v1"


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def decode(value: str) -> str:
    return base64.b64decode(value).decode("utf-8", "replace")


def parse_tsv(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"classes": {}, "object_counts": {}, "metadata": {}}
    for raw in path.read_text(errors="replace").splitlines():
        fields = raw.split("\t")
        if fields[0] == "WP014B_OPEN":
            result["open"] = {"status": int(fields[1]), "detail": decode(fields[2])}
        elif fields[0] == "WP014B_CLASS":
            result["classes"][fields[1]] = {"status": int(fields[2]), "property_count": int(fields[3]), "properties": []}
        elif fields[0] == "WP014B_CLASS_ERROR":
            result["classes"][fields[1]]["error"] = decode(fields[2])
        elif fields[0] == "WP014B_PROPERTY":
            result["classes"][fields[1]]["properties"].append({
                "property": fields[2], "report_status": int(fields[3]),
                "report": decode(fields[4]), "legal_values_status": int(fields[5]),
                "legal_values": decode(fields[6]).split() if decode(fields[6]).strip() else [],
            })
        elif fields[0] == "WP014B_OBJECT_COUNT":
            result["object_counts"][fields[1]] = {"status": int(fields[2]), "count": int(fields[3])}
        elif fields[0] in {"WP014B_PART", "WP014B_DESIGN_MODE", "WP014B_ROUTE_STATUS"}:
            result["metadata"][fields[0].removeprefix("WP014B_").lower()] = fields[1]
    return result


def run_one(vivado: Path, root: dict[str, Any], prefix: Path, output: Path, timeout: float) -> dict[str, Any]:
    job = output / "raw" / root["root_id"]
    job.mkdir(parents=True, exist_ok=False)
    root_path = prefix / root["filename"]
    tsv = job / "properties.tsv"
    started = time.monotonic()
    command = [
        str(vivado), "-mode", "batch", "-nojournal", "-notrace",
        "-log", str(job / "vivado.log"), "-source", str(REPO / "scripts/wp014b_property_probe.tcl"),
        "-tclargs", str(root_path), str(tsv),
    ]
    try:
        completed = subprocess.run(command, cwd=job, capture_output=True, timeout=timeout)
        process = {"returncode": completed.returncode, "timeout": False,
                   "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
                   "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest()}
        (job / "stdout.txt").write_bytes(completed.stdout)
        (job / "stderr.txt").write_bytes(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        process = {"returncode": None, "timeout": True,
                   "stdout_sha256": hashlib.sha256(exc.stdout or b"").hexdigest(),
                   "stderr_sha256": hashlib.sha256(exc.stderr or b"").hexdigest()}
        (job / "stdout.txt").write_bytes(exc.stdout or b"")
        (job / "stderr.txt").write_bytes(exc.stderr or b"")
    result = {
        "root_id": root["root_id"], "family": root["family"], "root_sha256": root["sha256"],
        "root_observed_sha256": hashlib.sha256(root_path.read_bytes()).hexdigest(),
        "elapsed_seconds": time.monotonic() - started, "process": process,
        "capture": parse_tsv(tsv) if tsv.exists() else None,
    }
    (job / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--root-id", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args()
    vivado_raw = os.environ.get("VIVADO_EXEC")
    if not vivado_raw:
        raise SystemExit("VIVADO_EXEC_missing_source_env")
    vivado = Path(vivado_raw).resolve(strict=True)
    roots_manifest = json.loads(args.roots.read_text())
    selected = [row for row in roots_manifest["roots"] if row["root_id"] in set(args.root_id)]
    if {row["root_id"] for row in selected} != set(args.root_id):
        raise SystemExit("unknown_root_id")
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    with ThreadPoolExecutor(max_workers=min(args.max_workers, len(selected))) as pool:
        futures = {pool.submit(run_one, vivado, row, Path(roots_manifest["root_path_prefix"]), args.output, args.timeout_seconds): row for row in selected}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda row: row["root_id"])
    core = {
        "schema_version": SCHEMA, "continuation_identity": "wp014b-action-surface-continuation-v1",
        "vivado_executable": str(vivado), "vivado_version": "2025.1",
        "implementation_heavy_calls": 0, "roots": results,
        "accepted": all(row["process"]["returncode"] == 0 and row["capture"] and row["root_sha256"] == row["root_observed_sha256"] for row in results),
    }
    aggregate = core | {"aggregate_sha256": digest(core)}
    (args.output / "aggregate.json").write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": aggregate["accepted"], "roots": len(results), "heavy_calls": 0}, sort_keys=True))
    return 0 if aggregate["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
