"""Explicit independent B batch using human-accepted A3 plus diagnostic evidence.

Does not promote or mutate the stopped A3 ledger and never dispatches S0 again.
The normal acceptance entry keeps its original all-REACHED prerequisite.
"""
import argparse
import json
from pathlib import Path

from .config import TOOL_ORDER, PAID_ORDER, validate_config
from .runner import AcceptanceBatch, dump, dof_reached
from .supplement import select_candidate, SEED


def accepted_evidence(cfg, source_config, supplement):
    source_cfg = validate_config(json.loads(Path(source_config).read_text()))
    cfg = validate_config(cfg)
    for key in ("input", "golden", "period_ns", "rapidwright_revision", "tools", "validation"):
        if cfg[key] != source_cfg[key]:
            raise ValueError("B differs from accepted physical inputs: " + key)
    evidence_fields = {"verified_at", "price_source", "verified_current"}
    if {k:v for k,v in cfg["cost"].items() if k not in evidence_fields} != {
            k:v for k,v in source_cfg["cost"].items() if k not in evidence_fields}:
        raise ValueError("B cannot change the approved model, rates or limits")
    source, output = Path(source_cfg["output"]).resolve(), Path(cfg["output"]).resolve()
    supplement = Path(supplement).resolve(strict=True)
    if (output.is_relative_to(source) or source.is_relative_to(output)
            or output.is_relative_to(supplement) or supplement.is_relative_to(output)):
        raise ValueError("B output must be independent of accepted evidence")
    if json.loads((source / "materials/locked-config.json").read_text()) != source_cfg:
        raise ValueError("Source configuration no longer matches its locked record")
    batch = AcceptanceBatch(source_cfg, source_config)
    report = batch.report()  # Opens the source ledger read-only; no prepare().
    accounting = report["accounting"] or {}
    states = accounting.get("conditions", {})
    if (accounting.get("requests") != []
            or accounting.get("stopped") != "CONDITION_INFRASTRUCTURE_OR_UNREACHED"
            or states.get("S0-extension") != "INCOMPLETE"
            or any(states.get(n) != "REACHED" for n in TOOL_ORDER[:-1])
            or any(n in states for n in PAID_ORDER)):
        raise ValueError("This explicit scope only accepts the known stopped no-model A3")
    batch.check_root()
    for condition in ("recipe", "direct", "constructed"):
        facts = json.loads((source / ("fixed-" + condition) / "summary.json").read_text())
        if not dof_reached(facts) or facts.get("infrastructure_failure"):
            raise ValueError("Accepted DoF physical chain not reached")
    baseline, extension = (report["paths"][n] for n in ("S0-baseline", "S0-extension"))
    gate = baseline["functional_check"]
    if (baseline["configured_attempt_budget"] != 3 or baseline["scheduled_attempts"] != 3
            or baseline["executed_candidates"] != 3 or baseline["unexplained_execution_failures"] != 0
            or gate.get("passed") is not True or gate.get("phase1_passed") is not True
            or gate.get("phase2_passed") is not True or gate.get("phase2_skipped") is not False
            or gate.get("infrastructure_failure") is not False
            or extension["configured_attempt_budget"] != 8 or extension["scheduled_attempts"] != 1
            or extension["executed_candidates"] != 1 or extension["unexplained_execution_failures"] != 0):
        raise ValueError("Accepted S0 execution/validation evidence differs")
    golden, candidate, _ = select_candidate(source_cfg, supplement, running=True)
    diagnostic = json.loads((supplement / "report.json").read_text())
    if (diagnostic.get("status") != "DIAGNOSTIC_PASS"
            or diagnostic.get("golden") != str(golden) or diagnostic.get("candidate") != str(candidate)
            or diagnostic.get("source_batch_acceptance") != "INCOMPLETE"
            or diagnostic.get("promotes_source_batch") is not False
            or diagnostic.get("protected_inputs_unchanged") is not True
            or diagnostic.get("model_requests") != 0 or diagnostic.get("optimization_runs") != 0
            or diagnostic.get("passed") is not True or diagnostic.get("phase1_passed") is not True
            or diagnostic.get("phase2_passed") is not True or diagnostic.get("phase2_skipped") is not False
            or diagnostic.get("infrastructure_failure") is not False
            or diagnostic.get("validation_parameters", {}).get("stimulus_seeds") != [SEED]
            or diagnostic.get("validation_parameters", {}).get("num_vectors") != 200
            or diagnostic.get("validation_parameters", {}).get("precheck_vectors") != 100):
        raise ValueError("Independent diagnostic evidence missing or contradictory")
    seeds = diagnostic.get("seeds", [])
    if (len(seeds) != 1 or seeds[0].get("stimulus_seed") != SEED
            or seeds[0].get("passed") is not True or seeds[0].get("cycles_simulated") != 200
            or seeds[0].get("infrastructure_failure") is not False
            or seeds[0].get("mismatch_count") != 0 or seeds[0].get("protocol_mismatch_count") != 0):
        raise ValueError("Diagnostic seed coverage is incomplete")
    return source, {"source_config": str(Path(source_config).resolve()),
        "source_batch": str(source), "supplement": str(supplement),
        "scope": "human-accepted A3 plus independent pruned-candidate diagnostic capability",
        "source_S0_extension_status": "INCOMPLETE", "source_promoted": False,
        "extension_attempts": 1, "extension_ceiling": 8, "extension_has_final_output": False,
        "diagnostic_status": "DIAGNOSTIC_PASS", "S0_repeated": False}


def run(cfg, config_path, source_config, supplement):
    source, evidence = accepted_evidence(cfg, source_config, supplement)
    batch = AcceptanceBatch(cfg, config_path)
    materials = batch.prepare()
    record = batch.output / "accepted-tool-evidence.json"
    if record.exists():
        if json.loads(record.read_text()) != evidence:
            raise ValueError("Accepted source evidence cannot change on resume")
    else:
        dump(record, evidence)
    context = json.loads((source / "materials/context.json").read_text())
    target = materials / "context.json"
    if target.exists():
        if json.loads(target.read_text()) != context:
            raise ValueError("Captured context cannot change on resume")
    else:
        dump(target, context)  # Restricted on the same server, never exported.
    batch.check_root()
    batch.once("versions", batch.versions)
    batch.run_paid_conditions()
    return batch.report()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-tools-config", type=Path, required=True)
    parser.add_argument("--supplement", type=Path, required=True)
    parser.add_argument("--phase", choices=["checklist", "run", "report"], default="checklist")
    parser.add_argument("--authorized-tools", action="store_true")
    parser.add_argument("--authorized-paid", action="store_true")
    parser.add_argument("--accepted-limited-a", action="store_true",
                        help="Only after human acceptance of the explicitly limited A3 plus supplement scope")
    args = parser.parse_args()
    try:
        if args.phase == "run" and not (args.authorized_tools and args.authorized_paid and args.accepted_limited_a):
            raise ValueError("Tool, paid and limited-A human decisions are required")
        cfg = json.loads(args.config.read_text())
        if args.phase == "report":
            result = AcceptanceBatch(cfg, args.config).report()
        elif args.phase == "run":
            result = run(cfg, args.config, args.source_tools_config, args.supplement)
        else:
            _, evidence = accepted_evidence(cfg, args.source_tools_config, args.supplement)
            result = AcceptanceBatch(cfg, args.config).checklist() | {"accepted_tool_evidence": evidence}
        print(json.dumps(result, indent=2))
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(2, "Independent B stopped: " + type(exc).__name__ + "; inspect restricted evidence, no replay.\n")


if __name__ == "__main__":
    main()
