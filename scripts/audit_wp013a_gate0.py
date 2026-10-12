#!/usr/bin/env python3
"""Reproduce and bind the four WP-013 interface defects before WP-013A changes."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry  # noqa: E402
from optimizer_pipeline.dof.single_submission.language import CompileContext  # noqa: E402
from optimizer_pipeline.dof.single_submission.observation import (  # noqa: E402
    CommonQuerySurface,
    build_common_observation,
)
from optimizer_pipeline.dof.single_submission.planner import (  # noqa: E402
    SingleSubmissionSession,
    SystemBinding,
)
from optimizer_pipeline.dof.single_submission.specs import canonical_json_bytes  # noqa: E402


EXPECTED = {
    "m8_manifest": "aebbb53d6ba5e2175c09310f6029e4ca8c288b547d097fc80759685c0b3a477b",
    "l1_manifest": "e34901bc739709c3f668bda3607a7b02870f876c352655b6a06d50f0da60fedf",
    "panel_ledger": "97c155ece915d9263d938f74606ac83f63f99e57162589e84a64162d04362fd7",
    "p_spatial": "e37d4e7e56d34f79b2ae23ad7b398d8cfd56a0754bcb5f46d5375b789af72e95",
    "p_hardblock": "d45657e192730fa1590b4b034e4fb633398ea860859567342f5d0ee96b966afd",
    "g_spatial": "71e04da8ad6d57c51d0466c9a88129bd880de5d24f7250b8292d193be72bed72",
    "g_hardblock": "99a0c677bb8078a23c061c52d15102dfc84a81a4156417804e84d3dc5754eb1f",
    "negative_e0": "ac2894021ab1a0b09d6b9e9d21fc35ff21a120bbb2e8d1ce00870daaa21fcffd",
}


def digest_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"object_required:{path}")
    return value


def old_e0_module():
    spec = importlib.util.spec_from_file_location(
        "wp013a_gate0_old_e0", REPO / "scripts/run_wp013_e0.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def old_session() -> SingleSubmissionSession:
    root = hashlib.sha256(b"wp013a-gate0-root").hexdigest()
    observation = build_common_observation(
        root_pseudonym="root-" + root[:16],
        target_clock={},
        timing={},
        utilization={},
        implementation={},
        constraints={},
        geometry={},
        congestion={},
        critical_paths=[],
        budget={"planning_turns": 4, "queries": 8, "feedback_rounds": 3},
    )
    registry = TrialHandleRegistry(
        trial_id="wp013a-gate0", root_sha256=root, secret=hashlib.sha256(b"gate0").digest()
    )
    binding = SystemBinding.create(
        observation_hash=observation.sha256,
        root_binding=root,
        catalogue_version="wp013.v1",
        catalogue_hash=hashlib.sha256(b"wp013.v1").hexdigest(),
        provider="offline",
        model="offline",
        route="offline",
        session_nonce="gate0",
    )

    def provider(name: str, top_k: int):
        return (
            [{"canonical_identity": f"private/{name}", "handle_type": "net", "status": "captured"}],
            ["offline-read-only"],
            0.0,
            None,
        )

    return SingleSubmissionSession(
        "L2",
        observation,
        CommonQuerySurface(registry),
        CompileContext(registry, "ROUTED"),
        binding,
        provider,
    )


def audit(*, runs: Path) -> dict[str, Any]:
    runs = runs.resolve(strict=True)
    paths = {
        "m8_manifest": REPO / "configs/wp013/final-m-v1.json",
        "l1_manifest": REPO / "configs/wp013/l1-heuristic-v1.json",
        "panel_ledger": runs / "macro-panel-v1/controller-ledger.json",
        "p_spatial": runs / "admission-p-spatial-v6/admission-summary.json",
        "p_hardblock": runs / "admission-p-hardblock-v2/admission-summary.json",
        "g_spatial": runs / "admission-g-spatial-v1/admission-summary.json",
        "g_hardblock": runs / "admission-g-hardblock-v2/admission-summary.json",
        "negative_e0": runs / "e0-v3-run-v1/e0-aggregate.json",
    }
    hashes = {
        key: {
            "path": str(path),
            "sha256": digest_file(path),
            "expected_sha256": EXPECTED[key],
        }
        for key, path in paths.items()
    }
    for value in hashes.values():
        value["matches"] = value["sha256"] == value["expected_sha256"]

    action_key_session = old_session()
    action_key_result = action_key_session.accept(
        {"kind": "COMMIT", "candidate": {"language": "M", "action_key": "macro.phys_opt"}}
    )

    old = old_e0_module()
    empty_paths: dict[str, int] = {}
    for root in ("vexriscv_re-place", "external_compatibility_a"):
        _, rows, _, _ = old._registry_rows(
            runs / "e0-v3-capture-v2" / root,
            read_json(REPO / "configs/wp013/e0-v3-plan.json")["roots"][0 if root == "vexriscv_re-place" else 1]["sha256"],
        )
        empty_paths[root] = len(rows["critical_path_details"])

    query_session = old_session()
    final_query_results = []
    for top_k in (1, 2, 3, 4):
        result = query_session.accept(
            {"kind": "QUERY", "query": {"name": "route_summary", "top_k": top_k}}
        )
        final_query_results.append(result.status)

    manifest = read_json(REPO / "configs/wp013/final-m-v1.json")
    declared = {
        row["flow_name"]: row["heavy_call_range"][0]
        for row in manifest["frozen_actions"]
    }
    observed = {
        key: row["observed_calls_per_job"]
        for key, row in read_json(
            REPO / "configs/wp013/macro-heavy-accounting-erratum-v1.json"
        )["actions"].items()
    }
    mismatch = {
        key: {"declared": declared[key], "observed": observed[key]}
        for key in ("memory_path_opt", "hardblock_relocate")
    }

    defects = {
        "old_action_key_rejection": {
            "status": action_key_result.status,
            "feedback": list(action_key_result.feedback),
            "reproduced": action_key_result.feedback == ("macro_candidate_schema_invalid",),
        },
        "empty_critical_path_details": {
            "rows_by_root": empty_paths,
            "reproduced": set(empty_paths.values()) == {0},
        },
        "final_turn_query_without_submission": {
            "turn_statuses": final_query_results,
            "turns_used": query_session.turns_used,
            "terminal": query_session.terminal,
            "valid_commits": query_session.valid_commits,
            "reproduced": query_session.turns_used == 4 and query_session.terminal is None,
        },
        "three_vs_four_heavy_call_mismatch": {
            "actions": mismatch,
            "reproduced": all(row == {"declared": 3, "observed": 4} for row in mismatch.values()),
        },
    }
    payload = {
        "schema_version": "dof.wp013a-gate0-audit.v1",
        "technical_non_scientific": True,
        "preserved_hashes": hashes,
        "negative_e0_classification": "NEGATIVE_INTERFACE_EVIDENCE",
        "defects": defects,
        "accepted": all(row["matches"] for row in hashes.values())
        and all(row["reproduced"] for row in defects.values()),
    }
    payload["audit_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    args = parser.parse_args()
    result = audit(runs=args.runs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"accepted": result["accepted"], "audit_sha256": result["audit_sha256"]}))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
