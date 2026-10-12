"""Independent no-model smoke of the installed generic harness.

Only organizes existing execution/version/scoring/validation capabilities. It
does not replay DoF/Ablation conditions, open their ledgers, or change protocols.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

from .config import VALIDATION, validate_tools_config
from .runner import AcceptanceBatch, dump


def candidate_facts(workspace):
    """Use the executed artifact, never a selected root fallback."""
    db = sqlite3.connect(f"file:{Path(workspace) / 'run.db'}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT o.status,o.error_type,o.metrics_json,a.uri,o.attempt_id "
            "FROM outcomes o LEFT JOIN artifacts a ON a.artifact_id=o.output_artifact_id").fetchall()
        if len(rows) != 1:
            raise ValueError("Exactly one physical attempt is required")
        status, error, encoded, uri, attempt = rows[0]
        validations = db.execute("SELECT status FROM validations WHERE level='ATTEMPT' AND attempt_id=?",
                                 (attempt,)).fetchall()
        metrics = json.loads(encoded)
        valid = status == "SUCCEEDED" and len(validations) == 1 and validations[0][0] in {"CLEAN", "WARNING"}
        child = Path(uri).resolve(strict=True) if uri else None
        if child is not None and (not child.is_file() or not child.is_relative_to(Path(workspace).resolve())):
            raise ValueError("Candidate is outside the independent workspace")
        return child, dict(executed_candidates=1, execution_status=status, error_type=error,
            physical_valid=valid, physical_validation=[r[0] for r in validations],
            execution_metrics={k: metrics.get(k) for k in ("final_fmax_mhz", "final_wns_ns", "validation_status")})
    finally:
        db.close()


def validation_facts(result):
    functional = result.get("functional_validation") or {}
    seeds = [{k: row.get(k) for k in ("stimulus_seed", "passed", "cycles_simulated", "mismatch_count",
        "protocol_mismatch_count", "infrastructure_failure", "infrastructure_reason")}
        for row in functional.get("seed_reports", [])]
    reached = (result.get("inputs_unchanged") is True and result.get("scoring", {}).get("status") == "MEASURED"
        and functional.get("all_seeds_present_exactly_once") is True
        and [r["stimulus_seed"] for r in seeds] == VALIDATION["seeds"]
        and all(r["cycles_simulated"] == 200 and r["infrastructure_failure"] is False for r in seeds)
        and functional.get("passed") in (True, False))
    return dict(scoring=result.get("scoring"), functional_status=functional.get("status"),
        functional_passed=functional.get("passed"), seeds=seeds, validation_reached=reached,
        validated_inputs_unchanged=result.get("inputs_unchanged"))


def run(cfg, config_path, output):
    from optimizer_pipeline.backends.xilinx.owned_process import run_owned
    from optimizer_pipeline.scoring import file_sha256
    validate_tools_config(cfg)
    source, root = Path(cfg["output"]).resolve(), Path(cfg["input"]).resolve(strict=True)
    output = Path(output).absolute()
    if (output.exists() or output.is_symlink() or not output.is_absolute()
            or root.is_relative_to(output.resolve()) or output.resolve().is_relative_to(source)
            or source.is_relative_to(output.resolve())):
        raise ValueError("New independent output required; no replay or source nesting")
    fingerprint = file_sha256(root)  # Existing input-protection mechanism.
    os.umask(0o077)
    output.mkdir(mode=0o700)
    result = dict(scope="generic installed harness; one fixed phys_opt; independent no-model tool acceptance",
        model_requests=0, optimization_budget=1, execution_timeout_seconds=7200,
        promotes_old_acceptance=False, status="UNREACHED")
    try:
        batch = AcceptanceBatch(cfg | {"output": str(output), "ledger_path": str(output / "batch.db")}, config_path)
        batch.memory_check()
        batch.versions()  # No prepare(): never opens a cost/study ledger.
        dump(output / "tools.json", cfg["tools"])
        dump(output / "run-config.json", dict(max_total_runs=1, max_parallel_jobs=1,
            online_deep_analysis=False, model=None, validation_mode="log_aware"))
        env = dict(os.environ)
        for key in list(env):
            if "API_KEY" in key or key in {"OPENAI_ACCESS_TOKEN", "ANTHROPIC_AUTH_TOKEN", "PYTHONPATH"}:
                env.pop(key)
        completed = run_owned([sys.executable, "-m", "optimizer_pipeline.apps.experiment", str(root),
            "--run-dir", str(output / "run"), "--tool-config", str(output / "tools.json"),
            "--config", str(output / "run-config.json"), "--custom-pipeline",
            "optimizer_pipeline.apps.experiment.pipelines:one_physical_transition"],
            cwd="/tmp", env=env, timeout=7200, text=True)
        with (output / "tools.log").open("x") as handle:
            handle.write(completed.stdout + "\n" + completed.stderr)
        result["cli_returncode"] = completed.returncode
        child, facts = candidate_facts(output / "run")
        result.update(facts)
        if completed.returncode or not facts["physical_valid"] or child is None or child == root:
            result["status"] = "EXECUTION_OR_PHYSICAL_UNREACHED"
        else:
            batch.memory_check()
            from experiments.dof.runtime.post_commit import PostCommit
            post = PostCommit(config=VALIDATION, golden=root, python=Path(cfg["tools"]["python"]),
                vivado=Path(cfg["tools"]["vivado"]),
                scorer_script=Path(__file__).resolve().parents[2] / "scripts/e1_score_dcp.tcl")
            result.update(validation_facts(post(root=root, child=child, output=output / "validation")))
            result["status"] = ("HARNESS_COVERAGE_REACHED" if result["validation_reached"]
                                else "VALIDATION_UNREACHED")
    except (Exception, KeyboardInterrupt) as exc:
        result.update(status="INFRASTRUCTURE_FAILURE", error_type=type(exc).__name__)
    finally:
        result["original_input_unchanged"] = root.is_file() and file_sha256(root) == fingerprint
        if not result["original_input_unchanged"]:
            result["status"] = "INPUT_PROTECTION_FAILURE"
        result["captured_at_utc"] = datetime.now(timezone.utc).isoformat()
        dump(output / "report.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Existing approved source tools config (read-only)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=["checklist", "run", "report"], default="checklist")
    parser.add_argument("--authorized-tools", action="store_true")
    args = parser.parse_args()
    if args.phase == "report":
        print((args.output / "report.json").read_text())
        return 0
    cfg = json.loads(args.config.read_text())
    validate_tools_config(cfg)
    if args.phase == "checklist":
        print(json.dumps(dict(scope="independent generic harness smoke", tools=cfg["tools"],
            design=Path(cfg["input"]).name, validation=VALIDATION, output=str(args.output),
            model_requests=0, executable_pipeline="one_physical_transition", execution_budget=1), indent=2))
        return 0
    if not args.authorized_tools:
        parser.error("separate real-tool authorization required")
    result = run(cfg, args.config, args.output)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "HARNESS_COVERAGE_REACHED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
