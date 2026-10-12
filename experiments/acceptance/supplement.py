"""One-shot diagnostic validation of A3's existing pruned S0 candidate.

No search, ledger mutation, model client, replay or promotion of A3 acceptance.
The worker delegates validation to the original Ablation gate; the parent uses
the existing owned-process runner so timeouts clean up tool descendants.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

from .config import validate_config
from .runner import AcceptanceBatch, dump

TIMEOUT = 14400
SEED = 0xDEADBEEF  # Ablation default, not the DoF WP015 seed set.


def select_candidate(cfg, output, *, running=False):
    validate_config(cfg)
    source = Path(cfg["output"]).resolve(strict=True)
    extension = source / "S0-extension"
    summary = json.loads((extension / "pipeline_summary.json").read_text())
    baseline = json.loads((source / "S0-baseline/pipeline_summary.json").read_text())
    if summary["state"]["root_dcp"] != baseline["final_output_dcp"]:
        raise ValueError("Extension root does not match the recorded baseline output")
    nodes = [n for n in summary["nodes"] if n["depth"] > 0]
    if len(nodes) != 1:
        raise ValueError("Exactly one existing non-root candidate is required")
    node = nodes[0]
    if (node["metrics"].get("prune_reason") != "severe_regression"
            or node["metrics"].get("pruned_by_qor_gate") is not True
            or summary["final_output_dcp"] is not None):
        raise ValueError("This entry is only for the recorded pruned A3 candidate")
    candidate = Path(node["output_dcp"]).resolve(strict=True)
    golden = Path(cfg["golden"]).resolve(strict=True)
    output = Path(output)
    if not output.is_absolute():
        raise ValueError("An absolute independent output directory is required")
    output = output.resolve()
    if (not candidate.is_file() or not candidate.is_relative_to(extension.resolve())
            or candidate == golden or output.is_relative_to(source)
            or source.is_relative_to(output) or golden.is_relative_to(output)
            or (output.exists() and not running)):
        raise ValueError("Input/output isolation violated or output already exists; no replay")
    return golden, candidate, output


def sanitized_gate(gate):
    result = {k: gate.get(k) for k in ("passed", "phase1_passed", "phase2_passed",
                                      "phase2_skipped", "infrastructure_failure")}
    report_path = gate.get("report_path")
    raw = json.loads(Path(report_path).read_text()) if report_path else {}
    result["validation_parameters"] = {k: raw.get(k) for k in
        ("num_vectors", "precheck_vectors", "stimulus_seeds", "stimulus_profile", "simulation_mode")}
    sim = raw.get("simulation_report") or {}
    result["precheck"] = {k: (sim.get("precheck_report") or {}).get(k) for k in
        ("passed", "cycles_simulated", "mismatch_count", "protocol_mismatch_count", "infrastructure_failure")}
    result["simulation"] = {k: sim.get(k) for k in
        ("cycles_simulated", "mismatch_count", "protocol_mismatch_count", "seed_reports_complete")}
    result["seeds"] = [{k: r.get(k) for k in
        ("stimulus_seed", "passed", "cycles_simulated", "mismatch_count",
         "protocol_mismatch_count", "infrastructure_failure")} for r in sim.get("seed_reports", [])]
    if result["infrastructure_failure"] is True:
        status = "INFRASTRUCTURE_FAILURE"
    elif result["phase1_passed"] is False:
        status = "STRUCTURAL_FAILURE"
    elif result["phase2_skipped"] is not False or result["passed"] not in (True, False):
        status = "UNREACHED"
    elif (result["validation_parameters"].get("stimulus_seeds") != [SEED]
          or result["validation_parameters"].get("num_vectors") != 200
          or result["validation_parameters"].get("precheck_vectors") != 100):
        status = "UNREACHED"
    elif result["passed"] is False:
        status = "FUNCTIONAL_MISMATCH"
    elif (sim.get("seed_reports_complete") is not True or len(result["seeds"]) != 1
          or result["seeds"][0].get("stimulus_seed") != SEED
          or result["seeds"][0].get("passed") is not True
          or result["infrastructure_failure"] is not False
          or result["seeds"][0].get("infrastructure_failure") is not False
          or result["seeds"][0].get("mismatch_count") != 0
          or result["seeds"][0].get("protocol_mismatch_count") != 0
          or result["seeds"][0].get("cycles_simulated") != 200
          or result["phase1_passed"] is not True or result["phase2_passed"] is not True):
        status = "UNREACHED"
    else:
        status = "DIAGNOSTIC_PASS"
    return result | {"status": status}


def worker(cfg, candidate, output):
    from optimizer_pipeline.backends.xilinx.configured_environment import configured_environment
    from experiments.ablation.runner_cli import run_equivalence_gate
    os.environ.update(configured_environment(**cfg["tools"]))
    gate = asyncio.run(run_equivalence_gate(Path(cfg["golden"]), Path(candidate),
        vectors=200, precheck_vectors=100, workspace_dir=Path(output) / "validation"))
    dump(Path(output) / "functional-result.json", sanitized_gate(gate))


def run(cfg, config_path, output):
    from optimizer_pipeline.backends.xilinx.owned_process import run_owned
    from optimizer_pipeline.scoring import file_sha256
    golden, candidate, output = select_candidate(cfg, output)
    source = Path(cfg["output"])
    # Existing input-integrity helper, not a new release/code-tree baseline.
    protected = [golden, candidate, source / "batch.db",
                 source / "S0-extension/pipeline_summary.json",
                 source / "S0-baseline/pipeline_summary.json"]
    before = [file_sha256(p) for p in protected]
    os.umask(0o077)
    output.mkdir(mode=0o700)
    batch = AcceptanceBatch(cfg | {"output": str(output), "ledger_path": str(output / "batch.db")}, config_path)
    result = {"scope": "independent diagnostic validation of an existing pruned candidate",
              "source_batch_acceptance": "INCOMPLETE", "promotes_source_batch": False,
              "optimization_runs": 0, "model_requests": 0, "outer_timeout_seconds": TIMEOUT,
              "candidate": str(candidate), "golden": str(golden)}
    try:
        batch.memory_check()
        batch.versions()  # No prepare(): never create/open a budget ledger.
        env = dict(os.environ)
        for key in list(env):
            if "API_KEY" in key or key in {"OPENAI_ACCESS_TOKEN", "ANTHROPIC_AUTH_TOKEN"}:
                env.pop(key)
        completed = run_owned([sys.executable, "-m", "experiments.acceptance.supplement",
            "--config", str(Path(config_path).resolve()), "--output", str(output),
            "--phase", "worker", "--candidate", str(candidate), "--authorized-tools"],
            env=env, timeout=TIMEOUT, text=True)
        with (output / "tools.log").open("x") as handle:
            handle.write(completed.stdout + "\n" + completed.stderr)
        os.chmod(output / "tools.log", 0o600)
        if completed.returncode != 0:
            result.update(status="INFRASTRUCTURE_FAILURE", error_type="WorkerFailed")
        else:
            result.update(json.loads((output / "functional-result.json").read_text()))
    except (Exception, KeyboardInterrupt) as exc:
        result.update(status="INFRASTRUCTURE_FAILURE", error_type=type(exc).__name__)
        # Timeout text/raw tools may contain private material; never print it.
    finally:
        result["protected_inputs_unchanged"] = all(p.is_file() and file_sha256(p) == digest
                                                  for p, digest in zip(protected, before))
        if not result["protected_inputs_unchanged"]:
            result["status"] = "INPUT_PROTECTION_FAILURE"
        result["captured_at_utc"] = datetime.now(timezone.utc).isoformat()
        dump(output / "report.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=["checklist", "run", "report", "worker"], default="checklist")
    parser.add_argument("--candidate", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--authorized-tools", action="store_true")
    args = parser.parse_args()
    try:
        if args.phase in {"run", "worker"} and not args.authorized_tools:
            raise ValueError("Separate no-model supplement authorization required")
        if args.phase == "report":
            result = json.loads((args.output / "report.json").read_text())
        else:
            cfg = json.loads(args.config.read_text())
            if args.phase == "run":
                result = run(cfg, args.config, args.output)
            elif args.phase == "worker":
                # Recheck the original selection, without choosing a fallback.
                _, selected, _ = select_candidate(cfg, args.output, running=True)
                if args.candidate.resolve() != selected or not args.output.is_dir():
                    raise ValueError("Worker candidate differs from the authorized selection")
                if (args.output / "functional-result.json").exists() or (args.output / "report.json").exists():
                    raise ValueError("Completed supplement cannot be replayed")
                worker(cfg, args.candidate, args.output)
                return
            else:
                golden, candidate, output = select_candidate(cfg, args.output)
                result = dict(golden=str(golden), candidate=str(candidate), output=str(output),
                    vectors=200, precheck_vectors=100, seeds=[SEED], executed=False,
                    promotes_source_batch=False, outer_timeout_seconds=TIMEOUT)
        print(json.dumps(result, indent=2))
        if args.phase == "run" and result["status"] in {"UNREACHED", "INFRASTRUCTURE_FAILURE", "INPUT_PROTECTION_FAILURE"}:
            raise RuntimeError("Supplement requires operator review")
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(2, "Supplement stopped: " + type(exc).__name__ + "; no replay.\n")


if __name__ == "__main__":
    main()
