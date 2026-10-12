#!/usr/bin/env python3
"""Reproduce the WP-014 calibration-time public/runtime interface audit."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from optimizer_pipeline.dof.single_submission.language import (
    _PHYS_OPTIONS,
    _P_DIRECTIVES,
)
from optimizer_pipeline.dof.vivado_mutation_worker import _DIRECTIVES


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _committed_program(row: dict[str, Any]) -> dict[str, Any] | None:
    for turn in reversed(row.get("turns", [])):
        result = turn.get("planner_result") or {}
        if result.get("status") == "COMMITTED":
            return result.get("compiled")
    return None


def audit(calibration: Path, vivado_log: Path) -> dict[str, Any]:
    aggregate = json.loads(calibration.read_text())
    help_text = vivado_log.read_text(errors="replace")
    incompatible: list[dict[str, Any]] = []
    unsupported_worker: list[dict[str, Any]] = []
    typed_unscored: list[str] = []

    for row in aggregate["jobs"]:
        compiled = _committed_program(row)
        if compiled:
            for step in compiled.get("host_steps", []):
                operation = step.get("operation")
                args = step.get("args") or {}
                if operation == "vivado.phys_opt" and args.get("options"):
                    directive = args.get("directive")
                    if directive != "Default":
                        incompatible.append(
                            {
                                "job_id": row["job_id"],
                                "directive": directive,
                                "options": args["options"],
                            }
                        )
                worker_operation = {
                    "vivado.place": "vivado.place_design",
                    "vivado.route": "vivado.route_design",
                    "vivado.phys_opt": "vivado.phys_opt_design",
                }.get(operation)
                if worker_operation and args.get("directive") not in _DIRECTIVES[worker_operation]:
                    unsupported_worker.append(
                        {
                            "job_id": row["job_id"],
                            "operation": operation,
                            "directive": args.get("directive"),
                        }
                    )
        execution = row.get("execution") or {}
        if (
            row.get("canonical_terminal") == "VALID_COMPLETION"
            and row.get("selected_language") in {"P", "G"}
            and execution.get("pre_timing") is not None
            and execution.get("post_timing") is not None
            and execution.get("fmax_improvement_mhz") is None
        ):
            typed_unscored.append(row["job_id"])

    authoritative_help_checks = {
        "vivado_2025_1_banner_present": "Vivado v2025.1" in help_text,
        "place_has_net_delay_weight_high": (
            "-net_delay_weight <arg>" in help_text
            and "valid options are\n                       low, medium, high" in help_text
        ),
        "place_lists_extra_net_delay_high_as_directive": (
            "*  ExtraNetDelay_high" in help_text
        ),
        "phys_directive_options_incompatibility_documented": (
            "not compatible with any specific optimization options" in help_text
        ),
        "route_aggressive_explore_documented": "*  AggressiveExplore" in help_text,
    }
    blocking = bool(
        incompatible
        and authoritative_help_checks["vivado_2025_1_banner_present"]
        and authoritative_help_checks["phys_directive_options_incompatibility_documented"]
    )
    return {
        "schema_version": "dof.wp014-interface-blocker-audit.v1",
        "calibration_path": str(calibration),
        "calibration_sha256": _sha256(calibration),
        "vivado_help_log_path": str(vivado_log),
        "vivado_help_log_sha256": _sha256(vivado_log),
        "public_directives": {key: sorted(value) for key, value in _P_DIRECTIVES.items()},
        "public_phys_options": sorted(_PHYS_OPTIONS),
        "worker_directives": {key: sorted(value) for key, value in _DIRECTIVES.items()},
        "authoritative_help_checks": authoritative_help_checks,
        "committed_vivado_incompatible_phys_opt_jobs": incompatible,
        "committed_worker_unsupported_directives": unsupported_worker,
        "valid_typed_jobs_missing_common_fmax_score": typed_unscored,
        "blocker_class": "FROZEN_PUBLIC_RUNTIME_SEMANTICS_UNDEFINED",
        "requires_protocol_owner_decision": blocking,
        "primary_queue_must_not_start": blocking,
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--vivado-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = audit(args.calibration, args.vivado_log)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print(json.dumps({
        "requires_protocol_owner_decision": value["requires_protocol_owner_decision"],
        "incompatible_jobs": len(value["committed_vivado_incompatible_phys_opt_jobs"]),
        "typed_unscored_jobs": len(value["valid_typed_jobs_missing_common_fmax_score"]),
    }, sort_keys=True))
    return 2 if value["requires_protocol_owner_decision"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
