import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
import subprocess
import sys

import pytest
from experiments.acceptance import paid
from experiments.acceptance.runner import AcceptanceBatch, ablation_research_stop
from experiments.acceptance.config import TOOL_ORDER, WP015_SEEDS
from optimizer_pipeline.scoring import file_sha256
from integrations.llm.runtime.batch_budget import BatchBudget
from tests.latch.test_acceptance_supplement import prepared


def accepted(tmp_path):
    source_cfg, source_path, candidate, supplement = prepared(tmp_path)
    source = Path(source_cfg["output"])
    (source / "batch.db").unlink()  # Fixture placeholder only.
    BatchBudget(source / "batch.db", source_cfg["cost"])
    batch = AcceptanceBatch(source_cfg, source_path)
    batch.prepare()
    for name in TOOL_ORDER:
        batch.once(name, lambda n=name: "INCOMPLETE" if n == "S0-extension" else "REACHED")
    batch.ledger.stop("CONDITION_INFRASTRUCTURE_OR_UNREACHED")
    (source / "materials/context.json").write_text(json.dumps(dict(root_sha256=file_sha256(Path(source_cfg["input"])), secret="stay-local")))
    facts = dict(planning_terminal="COMMITTED", execution_started=True, physical_valid=True,
        root_unchanged=True, post_commit_scoring=dict(status="MEASURED"), infrastructure_failure=False,
        functional_validation=dict(passed=True,all_seeds_present_exactly_once=True,
            seed_reports=[dict(stimulus_seed=s, infrastructure_failure=False) for s in WP015_SEEDS]))
    for name in ("fixed-recipe", "fixed-direct", "fixed-constructed"):
        (source / name).mkdir()
        (source / name / "summary.json").write_text(json.dumps(facts))
    baseline = dict(configured_attempt_budget=3, scheduled_attempts=3, executed_candidates=3,
        unexplained_execution_failures=0, functional_check=dict(passed=True, phase1_passed=True,
            phase2_passed=True, phase2_skipped=False, infrastructure_failure=False))
    (source / "S0-baseline/acceptance-result.json").write_text(json.dumps(baseline))
    summary_path = source / "S0-extension/pipeline_summary.json"
    summary = json.loads(summary_path.read_text()) | dict(max_total_runs=8,scheduled_runs=1)
    summary_path.write_text(json.dumps(summary))
    db = sqlite3.connect(source / "S0-extension/run.db")
    db.execute("CREATE TABLE outcomes(status TEXT,error_type TEXT,metrics_json TEXT)")
    db.execute("INSERT INTO outcomes VALUES('SUCCEEDED',NULL,'{}')")
    db.commit(); db.close()
    supplement.mkdir()
    report = dict(status="DIAGNOSTIC_PASS",golden=source_cfg["golden"],candidate=str(candidate),
        source_batch_acceptance="INCOMPLETE",promotes_source_batch=False,protected_inputs_unchanged=True,
        model_requests=0,optimization_runs=0,passed=True,phase1_passed=True,phase2_passed=True,
        phase2_skipped=False,infrastructure_failure=False,
        validation_parameters=dict(stimulus_seeds=[paid.SEED],num_vectors=200,precheck_vectors=100),
        seeds=[dict(stimulus_seed=paid.SEED,passed=True,cycles_simulated=200,infrastructure_failure=False,
            mismatch_count=0,protocol_mismatch_count=0)])
    (supplement / "report.json").write_text(json.dumps(report))
    output = tmp_path / "independent-b"
    cfg = source_cfg | dict(output=str(output),ledger_path=str(output / "batch.db"))
    cfg["cost"] = cfg["cost"] | {"verified_at":"2026-10-03T00:00:00Z"}
    path = tmp_path / "b.json"; path.write_text(json.dumps(cfg))
    return cfg, path, source_path, supplement


def clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime(2026,10,3,0,1,tzinfo=timezone.utc)
    monkeypatch.setattr("experiments.acceptance.runner.datetime",Clock)


def test_checklist_is_read_only_preserves_incomplete_source_and_omits_context(tmp_path):
    cfg,path,source_path,supplement = accepted(tmp_path)
    source_cfg = json.loads(source_path.read_text()); source = Path(source_cfg["output"])
    before = (source / "batch.db").read_bytes()
    _, evidence = paid.accepted_evidence(cfg,source_path,supplement)
    assert evidence["source_S0_extension_status"] == "INCOMPLETE"
    assert evidence["source_promoted"] is evidence["S0_repeated"] is False
    assert "stay-local" not in json.dumps(evidence)
    assert not Path(cfg["output"]).exists()
    assert (source / "batch.db").read_bytes() == before


@pytest.mark.parametrize("fault",["seed", "supplement-status", "candidate", "model", "source-config", "root", "source-overlap"])
def test_missing_contradictory_or_changed_source_stops_before_paid_output(tmp_path,fault):
    cfg,path,source_path,supplement = accepted(tmp_path)
    source_cfg = json.loads(source_path.read_text()); source = Path(source_cfg["output"])
    if fault in {"seed","supplement-status","candidate"}:
        report_path = supplement / "report.json"; report=json.loads(report_path.read_text())
        if fault == "seed": report["seeds"][0]["mismatch_count"] = 1
        elif fault == "supplement-status": report["status"] = "UNREACHED"
        else: report["candidate"] = cfg["golden"]
        report_path.write_text(json.dumps(report))
    elif fault == "model": cfg["cost"]["model"] = "other"
    elif fault == "source-config":
        source_cfg["cost"]["max_requests"] = 199; source_path.write_text(json.dumps(source_cfg))
    elif fault == "root": Path(cfg["input"]).write_text("mutated")
    else: cfg.update(output=str(source / "b"),ledger_path=str(source / "b/batch.db"))
    with pytest.raises((ValueError,RuntimeError)):
        paid.accepted_evidence(cfg,source_path,supplement)
    assert not Path(cfg["output"]).exists()


def test_paid_only_dispatches_six_conditions_and_resume_never_replays(tmp_path,monkeypatch):
    cfg,path,source_path,supplement = accepted(tmp_path); clock(monkeypatch)
    source = Path(json.loads(source_path.read_text())["output"])
    before=(source / "batch.db").read_bytes(); calls=[]
    monkeypatch.setattr(AcceptanceBatch,"versions",lambda self:calls.append("versions"))
    monkeypatch.setattr(AcceptanceBatch,"dof",lambda self,c,paid:calls.append((c,paid)))
    monkeypatch.setattr(AcceptanceBatch,"ablation",lambda self,n:calls.append(n))
    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.configured_environment.configured_environment",lambda **kw:{})
    report=paid.run(cfg,path,source_path,supplement)
    assert calls == ["versions",("recipe",True),("direct",True),("constructed",True),"S1","S2","S3"]
    assert report["accounting"]["requests"] == []
    assert not any(n in report["accounting"]["conditions"] for n in TOOL_ORDER[1:])
    paid.run(cfg,path,source_path,supplement)
    assert len(calls) == 7
    assert (source / "batch.db").read_bytes() == before
    assert (Path(cfg["output"]) / "materials/context.json").stat().st_mode & 0o777 == 0o600
    assert "stay-local" not in json.dumps(report)


def test_normal_entry_still_rejects_stopped_incomplete_a(tmp_path):
    _,_,source_path,_=accepted(tmp_path)
    batch=AcceptanceBatch(json.loads(source_path.read_text()),source_path)
    with pytest.raises(RuntimeError,match="capability"):
        batch.run("paid")
    assert batch.ledger.summary()["requests"] == 0


def test_new_entry_requires_three_explicit_decisions(tmp_path):
    result=subprocess.run([sys.executable,"-m","experiments.acceptance.paid",
        "--config",str(tmp_path / "missing"),"--source-tools-config",str(tmp_path / "a"),
        "--supplement",str(tmp_path / "s"),"--phase","run","--authorized-paid"],capture_output=True,text=True)
    assert result.returncode == 2 and "ValueError" in result.stderr
    assert not list(tmp_path.iterdir())


def test_stale_prices_stop_independent_b_before_conditions(tmp_path,monkeypatch):
    cfg,path,source_path,supplement=accepted(tmp_path); clock(monkeypatch)
    cfg["cost"]["verified_at"]="2026-10-01T00:00:00Z"; path.write_text(json.dumps(cfg))
    monkeypatch.setattr(AcceptanceBatch,"versions",lambda self:None)
    with pytest.raises(RuntimeError,match="price"):
        paid.run(cfg,path,source_path,supplement)
    report=AcceptanceBatch(cfg,path).report()
    assert report["accounting"]["requests"] == []
    assert report["accounting"]["conditions"] == {"versions":"REACHED"}


@pytest.mark.parametrize("condition,reason,status,final,expected", [
    ("S1","NO_VALID_CANDIDATE","COMPLETED",None,True),
    ("S2","LLM_REQUESTED_STOP","COMPLETED",None,True),
    ("S3","NO_VALID_CANDIDATE","COMPLETED",None,True),
    ("S0-extension","NO_VALID_CANDIDATE","COMPLETED",None,False),
    ("S1","EXECUTION_FAILED","FAILED",None,False),
    ("S1","NO_VALID_CANDIDATE","FAILED",None,False),
    ("S1","MAX_ITERATIONS_REACHED","COMPLETED",None,False),
    ("S1","NO_VALID_CANDIDATE","COMPLETED","candidate.dcp",False)])
def test_research_stop_does_not_promote_a_or_infrastructure(condition,reason,status,final,expected):
    assert ablation_research_stop(condition,dict(final_output_dcp=final,state=dict(status=status,stop_reason=reason))) is expected
