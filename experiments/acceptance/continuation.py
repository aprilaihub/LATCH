"""Authorized conservative continuation; prior candidates and ledgers stay immutable.

Only unstarted conditions are dispatched. Prior received requests are carried into
the new ledger, so the original $10/200-request limits do not reset. This is a
smoke-only policy, not a change to either research protocol or historical runner.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sqlite3

from integrations.llm.runtime.batch_budget import BatchStopped
from .config import PAID_ORDER, validate_config
from .paid import accepted_evidence
from .runner import AcceptanceBatch, dump, public_dof


def source_snapshot(cfg, previous_config):
    previous = validate_config(json.loads(Path(previous_config).read_text()))
    cfg = validate_config(cfg)
    if {k:v for k,v in cfg.items() if k not in {"output", "ledger_path"}} != {
            k:v for k,v in previous.items() if k not in {"output", "ledger_path"}}:
        raise ValueError("Continuation cannot change model, inputs, prices or limits")
    source, output = Path(previous["output"]).resolve(), Path(cfg["output"]).resolve()
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("Continuation output must be independent")
    if json.loads((source / "materials/locked-config.json").read_text()) != previous:
        raise ValueError("Previous locked configuration changed")
    AcceptanceBatch(previous, previous_config).check_root()
    with sqlite3.connect(f"file:{source / 'batch.db'}?mode=ro", uri=True) as db:
        stop = db.execute("SELECT stopped FROM batch WHERE id=1").fetchone()[0]
        if json.loads(db.execute("SELECT config FROM batch WHERE id=1").fetchone()[0]) != previous["cost"]:
            raise ValueError("Prior accounting configuration differs")
        requests = [list(row) for row in db.execute(
            "SELECT id,scope,status,reserved,accounted,input_tokens,output_tokens FROM requests ORDER BY id")]
        conditions = dict(db.execute("SELECT name,status FROM conditions"))
    # The original stopped B1 is the only source supported by this entry.
    if stop != "CONDITION_INFRASTRUCTURE_OR_UNREACHED" or conditions != {
            "versions":"REACHED", "dof-recipe":"INCOMPLETE"}:
        raise ValueError("Only the diagnosed stopped B1 is supported; no automatic chained retry")
    if not requests or len(requests) > 4 or any(r[1] != "dof-recipe" or r[2] != "RECEIVED"
            or not isinstance(r[4], (int,float)) or not math.isfinite(r[4]) or r[4] < 0
            or type(r[5]) is not int or type(r[6]) is not int or min(r[5],r[6]) < 0
            or not math.isclose(r[4], (r[5]*cfg["cost"]["input_usd_per_million"] +
                r[6]*cfg["cost"]["output_usd_per_million"])/1e6, rel_tol=1e-12, abs_tol=1e-12)
            for r in requests):
        raise BatchStopped("Unknown/failed/pending prior usage cannot be continued")
    facts = json.loads((source / "dof-recipe/summary.json").read_text())
    if not candidate_rejection(source / "dof-recipe", facts):
        raise ValueError("Prior recipe is not an explained physical rejection")
    return source, {"source_config":str(Path(previous_config).resolve()),
        "source_output":str(source), "source_stop":stop, "source_conditions":conditions,
        "requests":requests, "excluded_conditions":["dof-recipe"],
        "remaining_conditions":PAID_ORDER[1:], "source_promoted":False,
        "scope":"authorized continuation; prior physical rejection retained, no replay"}


def candidate_rejection(output, facts):
    validation = facts.get("functional_validation") or {}
    scoring = facts.get("post_commit_scoring")
    score_status = scoring.get("status") if isinstance(scoring, dict) else scoring
    if (facts.get("planning_terminal") != "COMMITTED" or facts.get("execution_started") is not True
            or facts.get("physical_terminal") != "INVALID_ARTIFACT"
            or facts.get("physical_valid") is not False or facts.get("root_unchanged") is not True
            or facts.get("infrastructure_failure") or validation.get("infrastructure_failure")
            or score_status != "NOT_RUN"
            or validation.get("status") != "NOT_RUN"):
        return False
    # A physical rejection is not a timeout/license/process failure. Child outcome
    # records must exist and explicitly report normal execution before skipping.
    outcomes = []
    for path in Path(output).joinpath("execution").rglob("run.db"):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT status,error_type,metrics_json FROM outcomes").fetchall()
        outcomes.extend(rows)
    return bool(outcomes) and all(status == "SUCCEEDED" and error is None
            and json.loads(metrics).get("validation_status") == "INVALID"
            for status,error,metrics in outcomes)


class ConservativeBatch(AcceptanceBatch):
    def dof(self, condition, *, paid):
        try:
            return super().dof(condition, paid=paid)
        except RuntimeError as exc:
            if not paid or str(exc) != "DoF execution/scoring/all-seed validation not reached":
                raise
            output = self.output / ("dof-" + condition)
            facts = json.loads((output / "summary.json").read_text())
            if not candidate_rejection(output, facts):
                raise
            dump(output / "continuation-result.json", public_dof(facts) | {
                "research_result":"physical candidate rejected; scoring and functional checks not reached",
                "original_result_unchanged":True, "candidate_repaired":False})
            return "RESEARCH_RESULT"


def run(cfg, config_path, previous_config, source_tools_config, supplement):
    # Reuse the existing physical-evidence/parameter checks, never repeat A or S0.
    _, evidence = accepted_evidence(cfg, source_tools_config, supplement)
    source, snapshot = source_snapshot(cfg, previous_config)
    batch = ConservativeBatch(cfg, config_path)
    materials = batch.prepare()
    record = batch.output / "continuation-source.json"
    if record.exists():
        if json.loads(record.read_text()) != snapshot:
            raise ValueError("Prior evidence changed; cannot resume")
    else:
        # Carry requests atomically into an empty ledger. Original ledger stays
        # read-only, permanently stopped; no condition/request is duplicated.
        with batch.ledger.transaction() as db:
            if db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] or db.execute(
                    "SELECT COUNT(*) FROM conditions").fetchone()[0]:
                raise ValueError("Continuation initialization is uncertain; do not replay")
            db.executemany("INSERT INTO requests VALUES(?,?,?,?,?,?,?)", snapshot["requests"])
            db.execute("INSERT INTO conditions VALUES('dof-recipe','INCOMPLETE')")
        dump(record, snapshot)
    evidence_path = batch.output / "accepted-tool-evidence.json"
    if evidence_path.exists():
        if json.loads(evidence_path.read_text()) != evidence:
            raise ValueError("Accepted A evidence changed")
    else:
        dump(evidence_path, evidence)
    context = json.loads((source / "materials/context.json").read_text())
    target = materials / "context.json"
    if target.exists():
        if json.loads(target.read_text()) != context:
            raise ValueError("Captured context changed")
    else:
        dump(target, context)
    batch.check_root()
    batch.once("versions", batch.versions)
    batch.run_paid_conditions(names=PAID_ORDER[1:])
    return report(cfg, config_path)


def report(cfg, config_path):
    result = AcceptanceBatch(cfg, config_path).report()
    path = Path(cfg["output"]) / "continuation-source.json"
    if path.is_file():
        source = json.loads(path.read_text())
        result["continuation"] = {k:v for k,v in source.items() if k != "requests"}
        result["continuation"]["carried_requests"] = len(source["requests"])
        result["continuation"]["carried_accounted_usd"] = sum(r[4] for r in source["requests"])
        previous = json.loads(Path(source["source_config"]).read_text())
        result["paths"]["dof-recipe"] = AcceptanceBatch(previous,source["source_config"]).report()["paths"]["dof-recipe"]
        result["paths"]["dof-recipe"]["carried_from_stopped_source"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--previous-config",type=Path,required=True)
    parser.add_argument("--source-tools-config",type=Path,required=True)
    parser.add_argument("--supplement",type=Path,required=True)
    parser.add_argument("--phase",choices=["checklist","run","report"],default="checklist")
    parser.add_argument("--authorized-conservative-continuation",action="store_true")
    args = parser.parse_args()
    try:
        cfg = json.loads(args.config.read_text())
        if args.phase == "report":
            result = report(cfg,args.config)
        elif args.phase == "run":
            if not args.authorized_conservative_continuation:
                raise ValueError("Explicit human continuation authorization required")
            result = run(cfg,args.config,args.previous_config,args.source_tools_config,args.supplement)
        else:
            _, snapshot = source_snapshot(cfg,args.previous_config)
            _, evidence = accepted_evidence(cfg,args.source_tools_config,args.supplement)
            result = {k:v for k,v in snapshot.items() if k != "requests"} | {
                "carried_requests":len(snapshot["requests"]),
                "carried_accounted_usd":sum(r[4] for r in snapshot["requests"]),
                "accepted_tool_evidence":evidence, "executed":False}
        print(json.dumps(result,indent=2))
    except (Exception,KeyboardInterrupt) as exc:
        parser.exit(2,"Continuation stopped: " + type(exc).__name__ + "; preserve evidence, no replay.\n")


if __name__ == "__main__":
    main()
