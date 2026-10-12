#!/usr/bin/env python3
"""Deterministic Gate-0/Gate-1 evidence audit for WP-013B."""

from __future__ import annotations

from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.single_submission import language  # noqa: E402
from optimizer_pipeline.dof.single_submission.contract_v3 import (  # noqa: E402
    CARD_FIELDS, COMMON_OBJECTIVE, G_CARDS, M_ACTION_KEYS, M_CARDS, P_CARDS,
    SYNTAX_EXAMPLES, compile_candidate_v3, contract_for_level,
)
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry  # noqa: E402
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


IMMUTABLE = {
    "m8_manifest": "configs/wp013/final-m-v1.json",
    "m_cost_overlay": "configs/wp013/macro-heavy-accounting-erratum-v1.json",
    "panel_ledger": "historical:wp013-runs/macro-panel-v1/controller-ledger.json",
    "l1_global_ranking": "configs/wp013/l1-heuristic-v1.json",
    "p_spatial_admission": "historical:wp013-runs/admission-p-spatial-v6/admission-summary.json",
    "p_hardblock_admission": "historical:wp013-runs/admission-p-hardblock-v2/admission-summary.json",
    "g_spatial_admission": "historical:wp013-runs/admission-g-spatial-v1/admission-summary.json",
    "g_hardblock_admission": "historical:wp013-runs/admission-g-hardblock-v2/admission-summary.json",
    "wp013a_protocol": "configs/wp013a/protocol-freeze-v2.json",
    "wp013a_e0": "historical:wp013a-runs/e0-v4-run-v1/e0-aggregate.json",
    "wp013a_independent_audit": "historical:wp013a-runs/e0-v4-audit-v1.json",
    "wp013a_process_audit": "historical:wp013a-runs/e0-v4-process-audit-v1.json",
    "wp013a_analysis": "historical:wp013a-runs/e0-v4-analysis-v1/e0-v4-analysis.json",
}


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def resolve(value: str, *, workspace: Path | None = None) -> Path:
    if value.startswith("historical:"):
        if workspace is None:
            raise ValueError("historical_workspace_required")
        return (workspace.resolve(strict=True) / value.removeprefix("historical:")).resolve(strict=True)
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, text=True, capture_output=True).stdout.strip()


def _example_context() -> tuple[CompileContext, dict[str, Any]]:
    root = hashlib.sha256(b"wp013b-semantic-example").hexdigest()
    registry = TrialHandleRegistry(trial_id="wp013b-semantic-example", root_sha256=root, secret=b"w" * 32)
    cell = registry.register(object_type="cell", canonical_identity="cell-1", attributes={"path_member": True, "path_rank": 1})
    region = registry.register(object_type="region", canonical_identity="region-1", attributes={"bounds": [0, 0, 0, 0]})
    site = registry.register(object_type="site", canonical_identity="site-1", attributes={"compatible_resource_types": ["DSP"]})
    hardblock = registry.register(object_type="hardblock", canonical_identity="dsp-1", attributes={"path_member": True, "path_rank": 1, "resource_type": "DSP", "compatible_site_handles": [site]})
    return CompileContext(registry, "ROUTED", (region,), (0, 1, 0, 1)), {"cell": cell, "region": region, "site": site, "hardblock": hardblock}


def _substitute(name: str, candidate: dict[str, Any], handles: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(candidate)
    replacements: dict[str, Any] = {
        "<ADMITTED_ACTION_KEY>": "macro.phys_opt",
        "<PREDEFINED_REGION_HANDLE>": handles["region"],
        "<COMPATIBLE_SITE_HANDLE>": handles["site"],
        "<X_MIN>": 0, "<X_MAX>": 0, "<Y_MIN>": 0, "<Y_MAX>": 0,
    }
    def walk(item: Any) -> Any:
        if isinstance(item, str): return replacements.get(item, item)
        if isinstance(item, list): return [walk(x) for x in item]
        if isinstance(item, dict): return {k: walk(v) for k, v in item.items()}
        return item
    return walk(value)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--historical-workspace", type=Path, required=True)
    args = parser.parse_args()
    files = {key: {"path": value, "sha256": digest_bytes(resolve(value, workspace=args.historical_workspace).read_bytes()), "size_bytes": resolve(value, workspace=args.historical_workspace).stat().st_size} for key, value in IMMUTABLE.items()}
    old_protocol = read(resolve(IMMUTABLE["wp013a_protocol"], workspace=args.historical_workspace))
    old_e0 = read(resolve(IMMUTABLE["wp013a_e0"], workspace=args.historical_workspace))
    old_jobs = old_e0.get("jobs", [])
    old_llm = [row for row in old_jobs if row.get("level") in {"L2", "L3", "L4"}]
    old_sizes = {level: len(canonical_json_bytes(old_protocol["contracts"][level])) for level in ("L2", "L3", "L4")}
    observed_inputs = [turn.get("total_input_size_bytes", 0) for row in old_llm for turn in row.get("turns", [])]

    contracts = {level: contract_for_level(level) for level in ("L2", "L3", "L4")}
    context, handles = _example_context()
    compiled = {}
    for name, row in SYNTAX_EXAMPLES.items():
        candidate = _substitute(name, row["candidate"], handles)
        result = compile_candidate_v3(candidate, context)
        compiled[name] = {"language": result.language, "public_sha256": result.public_sha256, "heavy_calls": result.heavy_calls, "light_mutations": result.light_mutations, "final_stage": result.final_stage}

    card_sets = {"M": M_CARDS, "P": P_CARDS, "G": G_CARDS}
    tokens = ("benchmark", "historical winner", "expected qor", "expected gain", "recommended action")
    forbidden_hits = {lang: [token for token in tokens if token in canonical_json_bytes(rows).decode().lower()] for lang, rows in card_sets.items()}
    p_runtime = set(language._P_OPS)
    p_public = {row["key"] for row in P_CARDS}
    generated_runtime = {"P_PROGRAM", "SELECT_WHERE", "CREATE_REGION", "ASSIGN_REGION"}
    g_public = {row["key"] for row in G_CARDS}
    checks = {
        "clean_tracked_tree_at_audit": git("status", "--short", "--untracked-files=no") == "",
        "m8_exact": [row["key"] for row in M_CARDS] == list(M_ACTION_KEYS),
        "p_runtime_public_bijection": p_runtime == p_public,
        "g_runtime_public_bijection": p_runtime | generated_runtime == g_public,
        "all_card_shapes_exact": all(tuple(row) == CARD_FIELDS for rows in card_sets.values() for row in rows),
        "all_examples_compile": len(compiled) == 4 and all(row["final_stage"] == "ROUTED" for row in compiled.values()),
        "p_example_exclusive": len(SYNTAX_EXAMPLES["P_EXCLUSIVE"]["candidate"]["steps"]) > 1,
        "g_spatial_example_exclusive": any(row["node"] in {"SELECT_WHERE", "CREATE_REGION", "ASSIGN_REGION"} for row in SYNTAX_EXAMPLES["G_SPATIAL_EXCLUSIVE"]["candidate"]["statements"]),
        "g_hardblock_example_exclusive": any("candidate_sites" in row.get("args", {}) for row in SYNTAX_EXAMPLES["G_HARDBLOCK_EXCLUSIVE"]["candidate"]["statements"]),
        "l2_m_bytes_inherited": contracts["L2"]["M"] == contracts["L3"]["M"] == contracts["L4"]["M"],
        "l3_p_bytes_inherited": contracts["L3"]["P"] == contracts["L4"]["P"],
        "common_objective_byte_identical": all(row["common_objective"] == COMMON_OBJECTIVE for row in contracts.values()),
        "no_hint_tokens": not any(forbidden_hits.values()),
        "g_mechanisms_have_restricted_p_counterparts": {"SELECT_WHERE", "CREATE_REGION", "ASSIGN_REGION"} <= generated_runtime and {"SELECT_CRITICAL_CELLS", "CREATE_PBLOCK", "ASSIGN", "RELOCATE_HARDBLOCK"} <= p_runtime,
    }
    gate0 = {
        "classification": "DEVELOPMENT_SEMANTIC_ASYMMETRY_EVIDENCE",
        "legacy_contract_sizes_bytes": old_sizes,
        "observed_max_input_bytes": max(observed_inputs, default=None),
        "selected_language_counts": {language_name: sum(row.get("selected_language") == language_name for row in old_llm) for language_name in ("M", "P", "G")},
        "llm_sessions": len(old_llm),
        "public_examples_only_call_macro": all("CALL_MACRO" in canonical_json_bytes(old_protocol["contracts"][level].get("syntax_templates", {})).decode() for level in ("L3", "L4")),
        "operation_level_cards_absent": all("cards" not in canonical_json_bytes(old_protocol["contracts"][level]).decode() for level in ("L3", "L4")),
    }
    payload = {
        "schema_version": "dof.wp013b-semantic-parity-audit.v1",
        "repository": {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")},
        "immutable_evidence": files,
        "gate0_negative_evidence": gate0,
        "contract_sizes_bytes": {level: len(canonical_json_bytes(value)) for level, value in contracts.items()},
        "contract_hashes": {level: digest_bytes(canonical_json_bytes(value)) for level, value in contracts.items()},
        "card_counts": {lang: len(rows) for lang, rows in card_sets.items()},
        "forbidden_token_hits": forbidden_hits,
        "compiled_examples": compiled,
        "checks": checks,
        "accepted": all(checks.values()) and gate0["llm_sessions"] == 12 and gate0["selected_language_counts"] == {"M": 12, "P": 0, "G": 0},
    }
    payload["audit_sha256"] = digest_bytes(canonical_json_bytes(payload))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": payload["accepted"], "gate0": gate0, "contract_sizes_bytes": payload["contract_sizes_bytes"]}, sort_keys=True))
    return 0 if payload["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
