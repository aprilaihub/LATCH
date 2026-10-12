#!/usr/bin/env python3
"""Validate the claim-neutral Paper Evidence Capsule Pass 2."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


EXPECTED_SOURCE_HASHES = {
    "canonical-jobs.csv": "87b02886c96d7163182173d9a447bdcbc5223c9bc032821f743aaeafb938c6a4",
    "actions-and-boundaries.csv": "e916a5bdb7b60793d6f52f5c0eb9bb09f2decd1162830b5bd75e36f6cf76e1b1",
    "functional-validation.csv": "a11338d97cfa119a197385150b67ae4015d57c48ff10ead3b3d0685efc47bb1a",
    "costs.csv": "f8520400d5cb82dd3c7e0f14225f7fa919cba5ca7a09d3f0eac11a8419dd9d29",
    "provider-turns.csv": "5e9965ccbb3cbef9c392802295956ad5ce8cb59da7d296739baacb4b2325f9b6",
    "analysis-vnext-derived-jobs.csv": "62a0b15603cdf21bfa05926a6912a45072040f508ede2ab457cb064a1be2bbde",
    "analysis-vnext-integration-receipt.json": "53295fa06aaf79b573ad8bd8fb6e010c456a41197a63ea64c3362c5ed5bca7d4",
    "strict-g-v2-v3-v4-reconstruction.csv": "8e78b22eac6f8604e0d6ed97706ef8111dd05c5b8bea133ac9cfaf23a9a52970",
    "strict-g-v4-counts.json": "0a21da5bb6d7ab278ae59fee04b9d26f1b05943f0a4e51b73e1f1a84463ca010",
    "root-timing-scorer-contracts.csv": "b59c7c0cd1510ef8a79c6cf6463b1104a37cce37a0a4c381b286b218854aac34",
    "titan23-timing-contract-receipt.json": "6ae8bc7e8947714943c7e0f221b8d5b4c92f33d30925f5e8131850c26cdd81e8",
    "WP015-Benchmark-Portfolio-Freeze-v1.json": "a8c55f94b4648cab603fbfc0dad1e861f37a81e6542dc21fe9f0be66173f5af7",
    "m10-reference-cells.csv": "dbb02662966e41feb53100d0bff7d7b61e64a107387dbe9fed76968c2960e93c",
    "reference_oracle.csv": "0770ed715a3da4bd7bb733b2aca0ac273cd255a93ef18b9c94c26a849790bbd0",
    "combined144_raw_cells.csv": "ac4e91e0952b662640bde3aa77d596f950c32b85ea793b3e9ce22de78f50190c",
    "combined144_paired_arm_contrasts.csv": "dd8b92c06867aa746c2742909db6a8a596a7b900d747d3706d51c84c407d5e90",
    "public_failure_mode_taxonomy.json": "011c7fa7fa6ccf2d89b406f8697d49aa7f93c646f25431a62c50aed62913b91b",
}

FORBIDDEN_ARCHIVE_KEYS = re.compile(r"(^|_)(private_?reasoning|reasoning_content|chain_?of_?thought|hidden_?reasoning|request_body|prompt_text)($|_)", re.I)
SECRET_TEXT = re.compile(r"sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|or-v1-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|Bearer\s+[A-Za-z0-9._-]{20,}")


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def as_bool(value: str) -> bool:
    return value.lower() == "true"


def as_float(value: str) -> float | None:
    return None if value == "" else float(value)


def close(actual: float, expected: float, tolerance: float = 1e-8) -> bool:
    return math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance)


def mean(values: Iterable[float]) -> float:
    data = list(values)
    return sum(data) / len(data)


def check(condition: bool, label: str, actual: Any = None, expected: Any = None) -> dict[str, Any]:
    return {"check": label, "status": "PASS" if condition else "FAIL", "actual": actual, "expected": expected}


def iter_zstd_jsonl(path: Path):
    proc = subprocess.Popen(["zstd", "-q", "-d", "-c", str(path)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.strip():
            yield json.loads(line)
    rc = proc.wait()
    if rc:
        raise RuntimeError(f"zstd failed for {path}: {rc}")


def forbidden_keys(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if FORBIDDEN_ARCHIVE_KEYS.search(str(key)):
                found.append(path)
            found.extend(forbidden_keys(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(forbidden_keys(item, f"{prefix}[{index}]"))
    return found


def validate(capsule: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    jobs = rows(capsule / "canonical/wp015_jobs.csv")
    refs = rows(capsule / "canonical/wp015_reference_cells.csv")
    oracles = rows(capsule / "canonical/wp015_reference_oracles.csv")
    cells = rows(capsule / "canonical/wp016b_cells.csv")

    manifest_rows = rows(capsule / "01_SOURCE_ARTIFACT_MANIFEST.csv")
    source_actual = {row["filename"]: row["current_sha256"] for row in manifest_rows}
    source_ok = all(source_actual.get(name) == digest for name, digest in EXPECTED_SOURCE_HASHES.items())
    checks.append(check(source_ok, "authoritative source hashes match Pass 1", len(EXPECTED_SOURCE_HASHES), len(EXPECTED_SOURCE_HASHES)))

    checks.append(check(len(jobs) == 1134 and len({row["job_id"] for row in jobs}) == 1134, "WP-015 unique jobs", len(jobs), 1134))
    physical = [row for row in jobs if as_bool(row["physical_validity"])]
    deltas = [row for row in jobs if row["physical_signed_delta_preliminary"] != ""]
    checks.append(check(len(physical) == 634 and len(deltas) == 634, "634 corrected physical deltas", [len(physical), len(deltas)], [634, 634]))
    exact = sum(as_bool(row["exact_zero_scientific_delta_applied"]) for row in jobs)
    checks.append(check(exact == 16, "exact-zero repairs", exact, 16))
    functional = Counter(row["functional_status_class"] for row in jobs)
    expected_functional = {"PASS": 611, "MISMATCH": 3, "INFRASTRUCTURE_UNKNOWN": 20, "NOT_ATTEMPTED": 500}
    checks.append(check(all(functional[key] == value for key, value in expected_functional.items()), "functional taxonomy", dict(functional), expected_functional))

    expected_conditions = {
        "M_ONLY": (175, 166, 143, 20.2484823335),
        "P_BOUNDARY": (175, 121, 103, 19.4546510827),
        "G_STRICT_BOUNDARY": (175, 33, 3, 0.1619926635),
    }
    condition_actual = {}
    for condition, expected in expected_conditions.items():
        group = [row for row in jobs if row["condition"] == condition]
        actual = (
            len(group),
            sum(row["functional_status_class"] == "PASS" for row in group),
            sum(row["beneficial_functional_tristate"] == "true" for row in group),
            mean(float(row["assigned_utility_lower_bound_mhz"]) for row in group),
        )
        condition_actual[condition] = actual
        checks.append(check(actual[:3] == expected[:3] and close(actual[3], expected[3]), f"condition invariant {condition}", actual, expected))

    p_assigned = [row for row in jobs if row["condition"] == "P_BOUNDARY"]
    strict_p = [row for row in p_assigned if as_bool(row["P_exclusive_realisation"])]
    strict_p_pass = [row for row in strict_p if row["functional_status_class"] == "PASS"]
    p_metrics = {
        "exclusive": len(strict_p),
        "physical": sum(as_bool(row["physical_validity"]) for row in strict_p),
        "pass": len(strict_p_pass),
        "unknown": sum(row["functional_status_class"] == "INFRASTRUCTURE_UNKNOWN" for row in strict_p),
        "beneficial": sum(row["beneficial_functional_tristate"] == "true" for row in strict_p),
        "pass_mean": mean(float(row["functional_scientific_delta_mhz"]) for row in strict_p_pass),
        "assigned_contribution_lower_mean": mean(float(row["strict_p_attributed_assigned_lower_bound_mhz"]) for row in p_assigned),
    }
    checks.append(check(p_metrics["exclusive"] == 127 and p_metrics["physical"] == 113 and p_metrics["pass"] == 111 and p_metrics["unknown"] == 2 and p_metrics["beneficial"] == 94 and close(p_metrics["pass_mean"], 24.7635947842) and close(p_metrics["assigned_contribution_lower_mean"], 15.8755144759), "conditioned strict-P invariants", p_metrics, "127/113/111/2/94 and frozen means"))

    causal = [row for row in jobs if as_bool(row["G_CAUSAL_RUNTIME_EXECUTED"])]
    causal_pass = [row for row in causal if row["functional_status_class"] == "PASS"]
    g_assigned = [row for row in jobs if row["condition"] == "G_STRICT_BOUNDARY"]
    g_metrics = {
        "causal": len(causal),
        "physical": sum(as_bool(row["physical_validity"]) for row in causal),
        "pass": len(causal_pass),
        "unknown": sum(row["functional_status_class"] == "INFRASTRUCTURE_UNKNOWN" for row in causal),
        "beneficial": sum(row["beneficial_functional_tristate"] == "true" for row in causal),
        "pass_mean": mean(float(row["functional_scientific_delta_mhz"]) for row in causal_pass),
        "assigned_contribution_lower_mean": mean(float(row["causal_g_attributed_assigned_lower_bound_mhz"]) for row in g_assigned),
        "v4_proven": sum(as_bool(row["G_NAMED_FAMILY_PROVEN_V4"]) for row in jobs),
        "v4_unproven": sum(as_bool(row["G_CAUSAL_NAMED_FAMILY_UNPROVEN"]) for row in jobs),
    }
    checks.append(check(g_metrics["causal"] == 34 and g_metrics["physical"] == 34 and g_metrics["pass"] == 32 and g_metrics["unknown"] == 2 and g_metrics["beneficial"] == 2 and close(g_metrics["pass_mean"], -7.6779490661) and close(g_metrics["assigned_contribution_lower_mean"], 0.1483533862) and g_metrics["v4_proven"] == 31 and g_metrics["v4_unproven"] == 3, "causal-G and G-v4 invariants", g_metrics, "frozen counts and means"))

    unknown_rows = [row for row in jobs if row["functional_status_class"] == "INFRASTRUCTURE_UNKNOWN"]
    checks.append(check(all(row["assigned_utility_observed_mhz"] == "" for row in unknown_rows), "infrastructure unknown remains null in observed utility", sum(row["assigned_utility_observed_mhz"] != "" for row in unknown_rows), 0))
    negative_pass = [row for row in jobs if row["functional_status_class"] == "PASS" and float(row["functional_scientific_delta_mhz"]) < 0]
    checks.append(check(bool(negative_pass) and all(float(row["functional_scientific_delta_mhz"]) < 0 for row in negative_pass), "negative primary deltas are not clipped", len(negative_pass), ">0"))

    source_counts = Counter(row["source"] for row in refs)
    checks.append(check(len(refs) == 250 and source_counts["DEVELOPMENT_PRE_AGENT"] == 140 and source_counts["NONDEVELOPMENT_POST_AGENT"] == 110, "reference row/source split", {"rows": len(refs), **source_counts}, {"rows": 250, "DEVELOPMENT_PRE_AGENT": 140, "NONDEVELOPMENT_POST_AGENT": 110}))
    titan = next((row for row in oracles if row["root_id"] == "titan23_orig_gsm_x6"), None)
    checks.append(check(titan is not None and titan["oracle_status"] == "REFERENCE_ORACLE_UNKNOWN_FUNCTIONAL_INFRA", "Titan reference oracle unknown", titan, "REFERENCE_ORACLE_UNKNOWN_FUNCTIONAL_INFRA"))

    checks.append(check(len(cells) == 144 and len({row["cell_id"] for row in cells}) == 144 and sum(row["primary_success"] == "1" for row in cells) == 32, "WP-016B 144/32", [len(cells), sum(row["primary_success"] == "1" for row in cells)], [144, 32]))
    checks.append(check(all(row["physical_validity"] == "NOT_APPLICABLE" and row["functional_validity"] == "NOT_APPLICABLE" and row["fmax_qor"] == "NOT_APPLICABLE" for row in cells), "WP-016B physical/function/FMax not applicable", 144, 144))

    old_names = {"02-freedom-realisation-funnel.png", "03-conditioned-qor.png", "04-coverage-deficit-vs-extra-freedom.png"}
    present_old = [path.name for path in (capsule / "figures/candidate").glob("*.png") if path.name in old_names]
    checks.append(check(not present_old, "no old WP-015C figure labelled current", present_old, []))
    figures = sorted((capsule / "figures/candidate").glob("*.png"))
    missing_sources = [path.name for path in figures if not (capsule / "figures/source_csv" / f"{path.stem}.csv").exists()]
    checks.append(check(len(figures) >= 14 and not missing_sources, "candidate figures have source CSV", {"figures": len(figures), "missing": missing_sources}, {"figures": 14, "missing": []}))

    selection = json.loads((capsule / "representative_evidence/SELECTION_MANIFEST.json").read_text())
    category_counts = Counter(category for record in selection["records"] for category in record["categories"])
    expected_minimum = {"functional_mismatch_all": 3, "functional_infrastructure_unknown_all": 20, "causal_g_all": 34, "exact_zero_all": 16, "valid_large_positive": 10, "valid_negative": 10, "successful_m": 10, "successful_conditioned_strict_p": 10, "p_conditioned_nonexclusive": 10, "physical_invalid_or_no_child": 10}
    checks.append(check(all(category_counts[key] == value for key, value in expected_minimum.items()), "representative category rules", dict(category_counts), expected_minimum))

    archive_counts = {}
    archive_forbidden: list[str] = []
    for name, expected in (("wp015_public_trajectories.jsonl.zst", 1134), ("wp016b_public_trajectories.jsonl.zst", 144)):
        records = list(iter_zstd_jsonl(capsule / "public_trajectories" / name))
        archive_counts[name] = len(records)
        for index, record in enumerate(records):
            keys = forbidden_keys(record)
            if keys:
                archive_forbidden.append(f"{name}:{index}:{keys[:3]}")
            if SECRET_TEXT.search(json.dumps(record)):
                archive_forbidden.append(f"{name}:{index}:secret-pattern")
        checks.append(check(len(records) == expected, f"trajectory count {name}", len(records), expected))
    checks.append(check(not archive_forbidden, "public trajectories contain no private reasoning/secrets", archive_forbidden[:10], []))

    source_ids = rows(capsule / "figures/source_csv/14_evidence_stage_nested_denominators.csv")
    checks.append(check(all(row.get("source_ids") for row in source_ids), "figure source rows remain addressable", sum(bool(row.get("source_ids")) for row in source_ids), len(source_ids)))

    failed = [item for item in checks if item["status"] != "PASS"]
    return {
        "schema_version": "research-flow.paper-evidence-capsule-pass2-validation.v1",
        "status": "PASS" if not failed else "FAIL",
        "provider_calls": 0,
        "eda_backend_calls": 0,
        "validation_reruns": 0,
        "check_count": len(checks),
        "failed_count": len(failed),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capsule", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = validate(args.capsule.resolve())
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": report["check_count"], "failed": report["failed_count"]}, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
