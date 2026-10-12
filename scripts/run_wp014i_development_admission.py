#!/usr/bin/env python3
"""Run V0 and golden=self five-seed V1 over the fourteen known roots."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.run_wp014f_validation_recon import file_sha256, object_sha256, run_cases
from scripts.run_wp014h_benchmark_admission import admission_v1_terminal, probe


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    config = json.loads(args.config.read_text())
    limits = config["limits"]
    if limits["provider_or_llm_calls"] or limits["optimization_calls"] or limits["holdout_or_transfer_accesses"]:
        raise ValueError("development_admission_firewall_not_zero")
    if len(config["roots"]) != 14:
        raise ValueError("exactly_fourteen_development_roots_required")
    vivado = os.environ.get("VIVADO_EXEC")
    if not vivado or not Path(vivado).is_file():
        raise RuntimeError("VIVADO_EXEC_not_available")
    candidates = []
    for row in config["roots"]:
        candidate = {
            **row,
            "portfolio": "development_known",
            "source": "FPL26_DEVELOPMENT",
            "target_clock": config["target_clock"],
            "sealed_read_only": False,
        }
        if file_sha256(Path(candidate["dcp"])) != candidate["sha256"]:
            raise ValueError("root_hash_mismatch:" + candidate["candidate_id"])
        candidates.append(candidate)
    args.output.mkdir(parents=True)
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(limits["v0_parallel"])) as pool:
        v0 = list(pool.map(lambda row: probe(row, args.output, vivado), candidates))
    passed = [row for row in v0 if row["terminal"] == "V0_PASS"]
    validation_plan = json.loads((REPO / "configs/wp014f/validation-recon-v1.json").read_text())
    validation_plan["boundaries"]["per_case_timeout_seconds"] = int(limits["v1_timeout_seconds"])
    cases = [{
        "case_id": "development_self__" + row["candidate"]["candidate_id"],
        "root_id": row["candidate"]["candidate_id"],
        "mechanism": "golden_self_development",
        "golden_dcp": row["candidate"]["dcp"],
        "revised_dcp": row["candidate"]["dcp"],
        "golden_sha256": row["candidate"]["sha256"],
        "revised_sha256": row["candidate"]["sha256"],
        "resolved_input_sha256": {
            "golden": row["candidate"]["sha256"],
            "revised": row["candidate"]["sha256"],
        },
        "sealed_holdout_accesses": 0,
    } for row in passed]
    v1 = run_cases(
        cases, validation_plan, args.output / "v1", "multiseed",
        min(int(limits["v1_parallel"]), len(cases)),
    ) if cases else None
    v1_rows = {}
    for row in (v1 or {}).get("results", []):
        log = args.output / "v1" / row["case"]["case_id"] / "validator.log"
        v1_rows[row["case"]["root_id"]] = admission_v1_terminal(
            row, log.read_text(errors="replace")
        )
    rows = [{
        "root_id": row["candidate"]["candidate_id"],
        "family": row["candidate"]["family"],
        "resource_class": row["candidate"]["resource_class"],
        "v0": row["terminal"],
        "v1": v1_rows.get(row["candidate"]["candidate_id"]),
        "admitted": row["terminal"] == "V0_PASS" and v1_rows.get(row["candidate"]["candidate_id"]) == "MULTISEED_FUNCTIONAL_PASS",
    } for row in v0]
    core = {
        "schema_version": "dof.wp014i-development-admission-result.v1",
        "identity": config["identity"],
        "config_sha256": file_sha256(args.config),
        "roots": rows,
        "admitted": sum(row["admitted"] for row in rows),
        "v0_terminal_counts": dict(collections.Counter(row["v0"] for row in rows)),
        "v1_terminal_counts": (v1 or {}).get("terminal_counts", {}),
        "queue_wall_seconds": time.time() - started,
        "all_inputs_unchanged": all(row["input_sha256_before"] == row["input_sha256_after"] for row in v0) and (v1 is None or v1["all_inputs_unchanged"]),
        "owned_process_survivors": [] if v1 is None else v1["owned_process_survivors"],
        "provider_or_llm_calls": 0,
        "optimization_calls": 0,
        "holdout_or_transfer_accesses": 0,
    }
    result = core | {"aggregate_sha256": object_sha256(core)}
    write(args.output / "aggregate.json", result)
    print(json.dumps({key: result[key] for key in ("admitted", "v0_terminal_counts", "v1_terminal_counts", "queue_wall_seconds")}, sort_keys=True))
    return 0 if result["admitted"] == 14 and not result["owned_process_survivors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
