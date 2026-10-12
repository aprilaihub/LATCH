#!/usr/bin/env python3
"""Bounded priority admission of one additional RapidWright public root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import zipfile


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

from scripts.run_wp014f_validation_recon import file_sha256, object_sha256, run_cases  # noqa: E402
from scripts.run_wp014h_benchmark_admission import admission_v1_terminal, probe  # noqa: E402


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def member_hash(path: Path, member: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return hashlib.sha256(archive.read(member)).hexdigest()


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
    config = read(args.config)
    limits = config["limits"]
    if any(limits[key] for key in (
        "provider_or_llm_calls", "optimization_calls",
        "holdout_or_transfer_optimization_calls",
    )):
        raise ValueError("rapidwright_collateral_firewall_not_zero")
    vivado = os.environ.get("VIVADO_EXEC")
    if not vivado or not Path(vivado).is_file():
        raise RuntimeError("VIVADO_EXEC_not_available")
    args.output.mkdir(parents=True)
    plan = read(REPO / "configs/wp014f/validation-recon-v1.json")
    plan["boundaries"]["per_case_timeout_seconds"] = int(limits["v1_timeout_seconds"])
    inspected = []
    admitted = None
    for selection in config["candidates"]:
        dcp = Path(selection["dcp"])
        if file_sha256(dcp) != selection["sha256"]:
            raise ValueError("collateral_dcp_hash_mismatch:" + selection["candidate_id"])
        if member_hash(dcp, selection["source_xdc_member"]) != selection["source_xdc_sha256"]:
            raise ValueError("collateral_source_xdc_hash_mismatch:" + selection["candidate_id"])
        candidate = {
            **selection,
            "portfolio": "true_family_heldout",
            "source": "RAPIDWRIGHT_PUBLIC_TEST_COLLATERAL",
            "sealed_read_only": True,
        }
        v0 = probe(candidate, args.output, vivado)
        row = {"candidate": selection, "v0": v0, "v1": None, "admitted": False}
        if v0["terminal"] == "V0_PASS":
            case = {
                "case_id": "rapidwright_self__" + selection["candidate_id"],
                "root_id": selection["candidate_id"],
                "mechanism": "golden_self_public_collateral",
                "golden_dcp": str(dcp), "revised_dcp": str(dcp),
                "golden_sha256": selection["sha256"], "revised_sha256": selection["sha256"],
                "resolved_input_sha256": {"golden":selection["sha256"],"revised":selection["sha256"]},
                "sealed_holdout_accesses": 1,
            }
            v1 = run_cases([case], plan, args.output / ("v1-" + selection["candidate_id"]), "multiseed", 1)
            result = v1["results"][0]
            log = args.output / ("v1-" + selection["candidate_id"]) / case["case_id"] / "validator.log"
            terminal = admission_v1_terminal(result, log.read_text(errors="replace"))
            row["v1"] = {
                "terminal": terminal, "result_sha256": result["result_sha256"],
                "wall_seconds": result["wall_seconds"],
            }
            row["admitted"] = terminal == "MULTISEED_FUNCTIONAL_PASS"
        inspected.append(row)
        if row["admitted"]:
            admitted = selection["candidate_id"]
            break
    core = {
        "schema_version": "dof.wp014i-rapidwright-collateral-result.v1",
        "identity": config["identity"],
        "selection_rule": config["selection_rule"],
        "config_sha256": file_sha256(args.config),
        "inspected": inspected, "admitted_candidate_id": admitted,
        "inspected_count": len(inspected), "admitted_count": int(admitted is not None),
        "qor_used_for_selection": False,
        "provider_or_llm_calls": 0, "optimization_calls": 0,
        "holdout_or_transfer_optimization_calls": 0,
        "all_inputs_unchanged": all(
            row["v0"]["input_sha256_before"] == row["v0"]["input_sha256_after"]
            for row in inspected
        ),
    }
    result = core | {"aggregate_sha256": object_sha256(core)}
    write(args.output / "aggregate.json", result)
    print(json.dumps({
        "inspected_count": result["inspected_count"],
        "admitted_candidate_id": result["admitted_candidate_id"],
    }, sort_keys=True))
    return 0 if admitted is not None and result["all_inputs_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
