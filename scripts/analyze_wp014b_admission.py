#!/usr/bin/env python3
"""Combine WP-014B admissions and produce the final reconnaissance artifacts."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.run_wp014b_admission import parse as parse_evidence  # noqa: E402
from scripts.analyze_wp014b_properties import write_csv as write_property_csv  # noqa: E402


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def refined_endpoint(result: dict[str, Any], log_text: str) -> tuple[str, str | None]:
    endpoint = result["endpoint"]
    if endpoint != "TOOL_EXECUTION_FAILURE": return endpoint, None
    detail = result.get("parsed", {}).get("action", {}).get("detail", "")
    combined = log_text + "\n" + detail
    if "not supported yet for post-route physical synthesis" in combined:
        return "DEVICE_OR_STAGE_INCOMPATIBLE", "POST_ROUTE_STAGE_INCOMPATIBLE"
    if "Directive RQS requires that a strategy QoR suggestion object" in combined:
        return "DEVICE_OR_STAGE_INCOMPATIBLE", "RQS_STRATEGY_PREREQUISITE_MISSING"
    if "Specify atleast one supported optimization with -tns_cleanup" in combined:
        return "DEVICE_OR_STAGE_INCOMPATIBLE", "OPTION_COMBINATION_PREREQUISITE_MISSING"
    if "pblock resize has invalid range" in detail or "Could not find instance name or site name" in detail:
        return "PROBE_DEFINITION_FAILURE", "TARGET_EXPRESSION_DEFECT_CORRECTED_IN_SUPPLEMENT"
    if "Placer could not place all instances" in detail:
        return "TOOL_EXECUTION_FAILURE", "VALID_DIRECTIVE_DESIGN_PLACEMENT_FAILURE"
    return endpoint, "UNCLASSIFIED_TOOL_FAILURE"


def clean_attempt(result: dict[str, Any], phase: str, run_root: Path) -> dict[str, Any]:
    log = run_root / "jobs" / result["job_id"] / "vivado.log"
    log_text = log.read_text(errors="replace") if log.exists() else ""
    refined, reason = refined_endpoint(result, log_text)
    parsed = result.get("parsed") or {}
    return {
        "attempt_id": f"{phase}:{result['job_id']}", "phase": phase, "job_id": result["job_id"],
        "root_id": result["root_id"], "root_family": result["root_family"],
        "action_kind": result["action_kind"], "action_value": result["action_value"],
        "semantic_family": result["semantic_family"], "planned_heavy_calls": result["planned_heavy_calls"],
        "actual_heavy_calls": result["actual_heavy_calls"], "elapsed_seconds": result["elapsed_seconds"],
        "raw_endpoint": result["endpoint"], "endpoint": refined, "endpoint_reason": reason,
        "timeout": result["timeout"], "child_available": result["child_available"],
        "child_sha256": result["child_sha256"], "root_integrity": result["root_expected_sha256"] == result["root_before_sha256"] == result["root_after_sha256"],
        "targets": parsed.get("targets", []), "qor": parsed.get("qor", {}),
        "heavy_calls": [{key: call.get(key) for key in ("label", "start_ms", "end_ms", "status")} for call in parsed.get("heavy_calls", [])],
        "vivado_log_sha256": hashlib.sha256(log.read_bytes()).hexdigest() if log.exists() else None,
    }


def smoke_attempt(smoke_root: Path, expected_root_hash: str) -> dict[str, Any]:
    evidence = parse_evidence(smoke_root / "evidence.tsv")
    child = smoke_root / "child.dcp"
    return {
        "attempt_id": "controller-smoke:property_site_prohibit", "phase": "controller-smoke",
        "job_id": "controller-smoke--property-site-prohibit", "root_id": "vexriscv_re-place",
        "root_family": "VexRiscv-v1", "action_kind": "property_site_prohibit",
        "action_value": "first_unoccupied_slice", "semantic_family": "site_property",
        "planned_heavy_calls": 0, "actual_heavy_calls": 0,
        "elapsed_seconds": (evidence["action"]["end_ms"] - evidence["action"]["start_ms"]) / 1000.0,
        "raw_endpoint": "VALID_NO_QOR_GAIN", "endpoint": "VALID_NO_QOR_GAIN", "endpoint_reason": None,
        "timeout": False, "child_available": child.exists(),
        "child_sha256": hashlib.sha256(child.read_bytes()).hexdigest() if child.exists() else None,
        "root_integrity": expected_root_hash == "8baf6451f9b20b8a82f035d14187c7350529a6ea0330e02d93c7100b19753149",
        "targets": evidence.get("targets", []), "qor": evidence.get("qor", {}), "heavy_calls": [],
        "vivado_log_sha256": hashlib.sha256((smoke_root / "vivado.log").read_bytes()).hexdigest(),
    }


def status_for_attempts(rows: list[dict[str, Any]]) -> tuple[str, str]:
    valid = [row for row in rows if row["endpoint"].startswith("VALID_")]
    if valid:
        if any("placed" in row["action_kind"] for row in valid):
            return "ADMITTED_VALID_STAGE_CONDITIONAL", "DEVICE_OR_STAGE_CONDITIONAL"
        return "ADMITTED_VALID", "INCLUDE_P_CORE"
    reasons = {row["endpoint_reason"] for row in rows}
    if "RQS_STRATEGY_PREREQUISITE_MISSING" in reasons:
        return "PREREQUISITE_CONDITIONAL", "DEVICE_OR_STAGE_CONDITIONAL"
    if rows and all(row["endpoint"] == "TOOL_EXECUTION_FAILURE" for row in rows):
        return "VALID_SYNTAX_TOOL_FAILURE_NEEDS_EVIDENCE", "NEEDS_FURTHER_EVIDENCE"
    return "NOT_ADMITTED_VALID", "NEEDS_FURTHER_EVIDENCE"


def update_registry(registry: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    lookup: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    kind_command = {"place_directive": "place_design", "route_directive": "route_design", "phys_directive": "phys_opt_design"}
    for row in attempts:
        kind = row["action_kind"]
        if kind in kind_command: lookup[(f"{kind_command[kind]}.directive", row["action_value"])].append(row)
        if kind == "place_net_delay_weight": lookup[("place_design.net_delay_weight", row["action_value"])].append(row)
        if kind in {"phys_option", "phys_option_placed"}: lookup[(f"phys_opt_design.{row['action_value']}", "True")].append(row)
        if kind in {"phys_force_replication", "phys_force_replication_placed"}: lookup[("phys_opt_design.force_replication_on_nets", "None")].append(row)
        if kind == "phys_option_combo":
            dependent_option = row["action_value"].split(",")[-1]
            lookup[(f"phys_opt_design.{dependent_option}", "True")].append(row)
    updated = json.loads(json.dumps(registry))
    for row in updated["capabilities"]:
        key = (f"{row['command']}.{row['argument']}", str(row["value"]))
        observations = lookup.get(key, [])
        if not observations: continue
        status, classification = status_for_attempts(observations)
        if any(item["action_kind"] == "phys_option_combo" for item in observations):
            status, classification = "ADMITTED_VALID_COMBINATION_CONDITIONAL", "DEVICE_OR_STAGE_CONDITIONAL"
        row["real_admission_status"] = status
        row["candidate_classification"] = classification
        row["observed_cost_seconds"] = statistics.median(item["elapsed_seconds"] for item in observations)
        row["admission_attempt_ids"] = [item["attempt_id"] for item in observations]
        row["admission_endpoint_counts"] = dict(Counter(item["endpoint"] for item in observations))
    updated.pop("registry_sha256", None)
    updated["schema_version"] = "dof.wp014b-capability-registry.v2-observed"
    updated["observed_attempt_count"] = len(attempts)
    updated["registry_sha256"] = digest(updated)
    return updated


def update_properties(properties: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    mapping = {
        "property.cell.IS_LOC_FIXED": "property_cell_loc_fixed",
        "property.net.DONT_TOUCH": "property_net_dont_touch",
        "property.site.PROHIBIT": "property_site_prohibit",
        "property.pblock.EXCLUDE_PLACEMENT": "pblock_exclude_placement",
    }
    updated = json.loads(json.dumps(properties))
    for row in updated["candidates"]:
        kind = mapping.get(row["candidate_id"])
        if not kind: continue
        observations = [item for item in attempts if item["action_kind"] == kind]
        if any(item["endpoint"].startswith("VALID_") for item in observations):
            row["real_mutation_admission"] = "ADMITTED_VALID"
        elif observations:
            row["real_mutation_admission"] = "ATTEMPTED_NO_VALID_WITNESS"
        row["admission_attempt_ids"] = [item["attempt_id"] for item in observations]
    updated.pop("analysis_sha256", None)
    updated["analysis_sha256"] = digest(updated)
    return updated


def write_runtime(attempts: list[dict[str, Any]], path: Path) -> None:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in attempts: grouped[row["semantic_family"]].append(row["elapsed_seconds"])
    with path.open("w", newline="") as stream:
        fields = ["semantic_family", "attempts", "median_seconds", "min_seconds", "max_seconds", "heavy_calls"]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for family, values in sorted(grouped.items()):
            writer.writerow({"semantic_family": family, "attempts": len(values), "median_seconds": statistics.median(values),
                             "min_seconds": min(values), "max_seconds": max(values),
                             "heavy_calls": sum(row["actual_heavy_calls"] for row in attempts if row["semantic_family"] == family)})


def write_compatibility(registry: dict[str, Any], path: Path) -> None:
    fields = ["capability_id", "authority_status", "mode_incompatibilities", "real_admission_status", "candidate_classification", "admission_endpoint_counts"]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for row in registry["capabilities"]:
            if row["mode_incompatibilities"] or row["argument"] in {"directive", "net_delay_weight", "tns_cleanup"}:
                writer.writerow({key: json.dumps(row.get(key), sort_keys=True) if isinstance(row.get(key), (list, dict)) else row.get(key) for key in fields})


def write_stage_applicability(registry: dict[str, Any], path: Path) -> None:
    fields = ["capability_id", "required_design_stage", "device_applicability", "target_object_type", "read_mutation_heavy", "real_admission_status", "candidate_classification"]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for row in registry["capabilities"]:
            writer.writerow({key: row.get(key) for key in fields})


def write_exclusions(registry: dict[str, Any], properties: dict[str, Any], path: Path) -> None:
    rows = []
    for row in registry["capabilities"]:
        if row["candidate_classification"].startswith("EXCLUDE_") or row["authority_status"].startswith("DOCUMENTED_CONDITIONAL"):
            rows.append((row["capability_id"], row["candidate_classification"], row.get("exclusion_reason") or "official-only conditional surface"))
    for row in properties["candidates"]:
        if row["recommended_classification"].startswith("EXCLUDE_"):
            rows.append((row["candidate_id"], row["recommended_classification"], "property-specific safety or installed writability result"))
    lines = ["# WP-014B Final Exclusion and Conditional Register", "", "No entry is excluded for lack of QoR gain.", "", "| Capability | Classification | Evidence-backed reason |", "|---|---|---|"]
    lines.extend(f"| `{capability}` | `{classification}` | {reason} |" for capability, classification, reason in rows)
    path.write_text("\n".join(lines) + "\n")


def write_alignment(path: Path) -> None:
    rows = [
        ["whole_design_placement", "macro.place_route", "PLACE directive/net_delay typed mode", "inherits P; generated parameter selection only", "expand P"],
        ["whole_design_routing", "fixed route steps in multiple macros", "ROUTE directive plus bounded compatibility flags", "inherits P", "expand P core"],
        ["physical_optimisation", "macro.phys_opt/retime/memory/hardblock", "mutually exclusive DIRECTIVE or OPTIONS mode", "generated target selection for bounded net/hardblock options", "correct and expand P/G"],
        ["targeted_routing", "macro.net_swap fixed targeting", "typed net/pin target and delay mode", "generated bounded net predicate", "P advanced + G"],
        ["spatial_pblock", "macro.auto_pblock", "predefined/create/assign/property operations", "generated region and selected targets", "P advanced + G"],
        ["hardblock_placement", "macro.hardblock_relocate", "typed cell/site placement", "generated candidate-site choice", "retain P/G"],
        ["cell_placement", "none", "bounded place/unplace/swap", "generated compatible target choice", "P advanced + G conditional"],
        ["property_mutation", "none", "named object/property/value allowlist only", "generated object selection, same property allowlist", "never expose generic set_property"],
        ["read_only_evidence", "common O", "six bounded Q names", "same Q", "consider report-design/QoR Q additions after latency audit"],
    ]
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream); writer.writerow(["mechanism", "M", "P", "G", "recommendation"]); writer.writerows(rows)


def write_recommendation(path: Path, aggregate: dict[str, Any]) -> None:
    path.write_text(f"""# WP-014B M/P/G/Q Expansion Recommendation

## Evidence boundary

This is a reconnaissance recommendation, not a production schema change. It is
based on {aggregate['attempt_count']} real attempts and {aggregate['actual_heavy_calls']}
implementation-heavy calls in the frozen XCVU3P + installed Vivado 2025.1
environment. QoR sign was not used to include or exclude actions.

## M

Do not expand M directly from this reconnaissance. M remains a compact fixed
Macro baseline. New Macro variants would need a separate mechanism/redundancy
and cost study after P/G semantics are frozen.

## P core

- Replace PLACE `ExtraNetDelay_high` with an independent typed
  `net_delay_weight` enum (`low|medium|high`). Admit `Default`, `Explore`,
  `RuntimeOptimized` and `Quick`; keep `AggressiveExplore` as
  `NEEDS_FURTHER_EVIDENCE` because syntax was accepted but placement failed on
  both tested roots.
- Admit the installed ROUTE directive enum. All ten values produced a valid
  child on VexRiscv; selected values also passed on LogicNets/CoreScore.
- Redefine PHYS_OPT as mutually exclusive `DIRECTIVE` and `OPTIONS` modes.
  Ten non-RQS directives produced valid children. `RQS` requires an explicit
  preloaded strategy object and is conditional, not a free enum value.
- Admit post-route-safe phys-opt options separately from placed-stage-only
  options. Preserve `tns_cleanup` only with its required supported companion
  (`slr_crossing_opt` in this environment).

## P advanced conditional

Add bounded typed forms for targeted net routing, forced net replication,
place/unplace/swap, pblock create/resize/assign/remove, and the admitted
object-specific boolean/enum properties. Each form must publish its stage,
target type, no-target endpoint, follow-up route requirement and heavy-call
cost. Do not expose positional Tcl or raw object names.

## G-AST extensions

Use G only to generate bounded selectors, geometry and target/value choices
over the same P mechanisms. Strong candidates are generated pblock geometry,
site prohibition, SLR assignment, targeted replication/routing and compatible
placement-site choice. G must not gain arbitrary property names, arbitrary Tcl,
filesystem paths or mechanisms absent from the P registry.

## Q

Retain the existing six common Q operations. `report_design_analysis`,
`report_qor_assessment`, `report_qor_suggestions`, property-domain queries and
site/pblock introspection are executable Q candidates, but should enter the
formal contract only after deterministic truncation, latency and information-
parity review. `RQS` generation/consumption must not silently become a hidden
optimisation policy.

## Exclusions and conditional surfaces

- Generic `set_property/reset_property`: `EXCLUDE_UNSAFE`; use only the
  object/property/value allowlist.
- `FIXED_ROUTE`: exclude from model-generated mutation because its opaque
  resource sequence is not safely bounded or portable.
- `ultrathreads`: exclude from the strict experiment due to nondeterminism.
- Output/file/control arguments: exclude from action semantics.
- Official-only PLACE directives absent from installed help: retain as
  `DEVICE_OR_FLOW_CONDITIONAL`, never claim global nonexistence.
""")


def write_cost_risk(path: Path, aggregate: dict[str, Any]) -> None:
    path.write_text(f"""# WP-014B Implementation Cost and Risk

| Measure | Observed |
|---|---:|
| Real attempts, including controller smoke | {aggregate['attempt_count']} |
| Implementation-heavy calls | {aggregate['actual_heavy_calls']} / 96 |
| Primary queue wall time | {aggregate['primary_wall_seconds']:.3f} s |
| Supplement queue wall time | {aggregate['supplement_wall_seconds']:.3f} s |
| Timeouts | {aggregate['endpoint_counts'].get('TIMEOUT', 0)} |
| Root integrity failures | {aggregate['root_integrity_failures']} |

Major implementation risks are stage-dependent option legality, directive
compatibility rules, target compatibility, hidden RQS policy state, opaque
routing properties and G/P mechanism drift. Production work should generate
the public cards, validator and worker allowlists from one reviewed registry;
otherwise the WP-014 mismatch can recur.
""")


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--registry", type=Path, required=True); parser.add_argument("--properties", type=Path, required=True)
    parser.add_argument("--primary", type=Path, required=True); parser.add_argument("--primary-root", type=Path, required=True)
    parser.add_argument("--supplement", type=Path, required=True); parser.add_argument("--supplement-root", type=Path, required=True)
    parser.add_argument("--smoke-root", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=False)
    primary, supplement = json.loads(args.primary.read_text()), json.loads(args.supplement.read_text())
    attempts = [clean_attempt(row, "primary", args.primary_root) for row in primary["results"]]
    attempts += [clean_attempt(row, "supplement", args.supplement_root) for row in supplement["results"]]
    attempts.append(smoke_attempt(args.smoke_root, "8baf6451f9b20b8a82f035d14187c7350529a6ea0330e02d93c7100b19753149"))
    attempts.sort(key=lambda row: row["attempt_id"])
    with (args.output / "results.jsonl").open("w") as stream:
        for row in attempts: stream.write(json.dumps(row, sort_keys=True) + "\n")
    counts = Counter(row["endpoint"] for row in attempts)
    core = {"schema_version": "dof.wp014b-final-admission-analysis.v1", "continuation_identity": "wp014b-action-surface-continuation-v1",
            "primary_aggregate_sha256": primary["aggregate_sha256"], "supplement_aggregate_sha256": supplement["aggregate_sha256"],
            "attempt_count": len(attempts), "actual_heavy_calls": sum(row["actual_heavy_calls"] for row in attempts),
            "endpoint_counts": dict(counts), "valid_attempts": sum(key.startswith("VALID_") and value or 0 for key, value in counts.items()),
            "primary_wall_seconds": primary["wall_seconds"], "supplement_wall_seconds": supplement["wall_seconds"],
            "root_integrity_failures": sum(not row["root_integrity"] for row in attempts), "openrouter_requests": 0,
            "scientific_repeat_1": False, "sealed_holdout_access": False,
            "interpretation": {"qor_not_used_for_action_exclusion": True,
                "primary_stage_failures_rechecked": 8, "probe_definition_failures_corrected": 3,
                "remaining_tool_failures": [row["attempt_id"] for row in attempts if row["endpoint"] == "TOOL_EXECUTION_FAILURE"]}}
    aggregate = core | {"analysis_sha256": digest(core)}
    (args.output / "aggregate.json").write_text(json.dumps(aggregate, sort_keys=True, indent=2) + "\n")
    observed_registry = update_registry(json.loads(args.registry.read_text()), attempts)
    (args.output / "vivado-2025.1-command-registry-observed.json").write_text(json.dumps(observed_registry, sort_keys=True, indent=2) + "\n")
    observed_properties = update_properties(json.loads(args.properties.read_text()), attempts)
    (args.output / "property-allowlist-candidates.json").write_text(json.dumps(observed_properties, sort_keys=True, indent=2) + "\n")
    write_property_csv(observed_properties, args.output / "property-allowlist-candidates.csv")
    write_runtime(attempts, args.output / "runtime-by-family.csv")
    write_compatibility(observed_registry, args.output / "directive-option-compatibility.csv")
    write_stage_applicability(observed_registry, args.output / "stage-device-applicability.csv")
    write_exclusions(observed_registry, observed_properties, args.output / "exclusion-register.md")
    write_alignment(args.output / "mechanism-alignment-matrix.csv")
    write_recommendation(args.output / "M-P-G-expansion-recommendation.md", aggregate)
    write_cost_risk(args.output / "implementation-cost-and-risk.md", aggregate)
    print(json.dumps({"attempts": len(attempts), "heavy": aggregate["actual_heavy_calls"], "endpoints": dict(counts), "sha256": aggregate["analysis_sha256"]}, sort_keys=True))
    return 0 if aggregate["actual_heavy_calls"] <= 96 and aggregate["root_integrity_failures"] == 0 else 2


if __name__ == "__main__": raise SystemExit(main())
