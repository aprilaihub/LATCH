import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from experiments.acceptance import continuation as c
from experiments.acceptance.runner import AcceptanceBatch
from tests.latch.test_acceptance_paid import accepted, clock


def rejected(output, **changes):
    output.mkdir(parents=True,exist_ok=True)
    facts = dict(planning_terminal="COMMITTED",execution_started=True,physical_terminal="INVALID_ARTIFACT",
        physical_valid=False,root_unchanged=True,post_commit_scoring=dict(status="NOT_RUN"),
        functional_validation=dict(status="NOT_RUN"),infrastructure_failure=False) | changes
    (output / "summary.json").write_text(json.dumps(facts))
    child = output / "execution/macro/run.db"; child.parent.mkdir(parents=True)
    with sqlite3.connect(child) as db:
        db.execute("CREATE TABLE outcomes(status TEXT,error_type TEXT,metrics_json TEXT)")
        db.execute("INSERT INTO outcomes VALUES('SUCCEEDED',NULL,?)",(json.dumps(dict(validation_status="INVALID")),))
    return facts


def stopped(tmp_path):
    cfg,path,a,supplement=accepted(tmp_path)
    batch=AcceptanceBatch(cfg,path); batch.prepare()
    source=Path(json.loads(a.read_text())["output"])
    (Path(cfg["output"]) / "materials/context.json").write_text((source / "materials/context.json").read_text())
    batch.once("versions",lambda:None)
    batch.ledger.begin_condition("dof-recipe")
    request_id=batch.ledger.reserve("dof-recipe",20)
    response=SimpleNamespace(provider="openai",model="gpt-5.6-luna",prompt_tokens=100,
        completion_tokens=20,raw_response=dict(model="gpt-5.6-luna",usage=dict(prompt_tokens=100,completion_tokens=20)))
    batch.ledger.reconcile(request_id,response)
    batch.ledger.finish_condition("dof-recipe","INCOMPLETE")
    batch.ledger.stop("CONDITION_INFRASTRUCTURE_OR_UNREACHED")
    rejected(Path(cfg["output"]) / "dof-recipe")
    target=tmp_path / "continued"
    cfg2=cfg | dict(output=str(target),ledger_path=str(target / "batch.db"))
    p2=tmp_path / "continued.json"; p2.write_text(json.dumps(cfg2))
    return cfg2,p2,path,a,supplement


def test_readonly_snapshot_preserves_source(tmp_path):
    cfg,p,previous,a,s=stopped(tmp_path)
    db=Path(json.loads(previous.read_text())["ledger_path"]); before=db.read_bytes()
    _, snapshot=c.source_snapshot(cfg,previous)
    assert snapshot["remaining_conditions"] == ["dof-direct","dof-constructed","S1","S2","S3"]
    assert snapshot["excluded_conditions"] == ["dof-recipe"]
    assert db.read_bytes()==before and not Path(cfg["output"]).exists()


@pytest.mark.parametrize("fault",["pending","usage","stop","started","root","model","limits","output"])
def test_unsafe_source_or_changed_scope_refused(tmp_path,fault):
    cfg,p,previous,a,s=stopped(tmp_path)
    old=json.loads(previous.read_text())
    if fault in {"pending","usage","stop","started"}:
        with sqlite3.connect(old["ledger_path"]) as db:
            if fault=="pending":db.execute("UPDATE requests SET status='STARTED'")
            if fault=="usage":db.execute("UPDATE requests SET accounted=NULL")
            if fault=="stop":db.execute("UPDATE batch SET stopped='PROVIDER_ATTEMPT_FAILED'")
            if fault=="started":db.execute("INSERT INTO conditions VALUES('dof-direct','STARTED')")
    elif fault=="root": Path(cfg["input"]).write_text("changed")
    elif fault=="model":cfg["cost"]["model"]="other"
    elif fault=="limits":cfg["cost"]["max_requests"]=199
    else:cfg.update(output=old["output"],ledger_path=old["ledger_path"])
    with pytest.raises((ValueError,RuntimeError)):
        c.source_snapshot(cfg,previous)


def test_new_ledger_carries_prior_cost_and_never_replays(tmp_path,monkeypatch):
    cfg,p,previous,a,s=stopped(tmp_path); clock(monkeypatch)
    before=Path(json.loads(previous.read_text())["ledger_path"]).read_bytes(); calls=[]
    monkeypatch.setattr(AcceptanceBatch,"versions",lambda self:calls.append("versions"))
    monkeypatch.setattr(c.ConservativeBatch,"dof",lambda self,n,paid:calls.append(n))
    monkeypatch.setattr(AcceptanceBatch,"ablation",lambda self,n:calls.append(n))
    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.configured_environment.configured_environment",lambda **kw:{})
    report=c.run(cfg,p,previous,a,s)
    assert calls==["versions","direct","constructed","S1","S2","S3"]
    assert report["accounting"]["conditions"]["dof-recipe"]=="INCOMPLETE"
    assert report["continuation"]["carried_requests"]==1
    assert report["continuation"]["carried_accounted_usd"]>0
    assert len(report["accounting"]["requests"])==1
    assert report["paths"]["dof-recipe"]["physical_valid"] is False
    assert "stay-local" not in json.dumps(report)
    c.run(cfg,p,previous,a,s)
    assert len(calls)==6
    assert Path(json.loads(previous.read_text())["ledger_path"]).read_bytes()==before


@pytest.mark.parametrize("changes",[{"physical_terminal":"TOOL_TIMEOUT"},{"root_unchanged":False},
    {"infrastructure_failure":True},{"planning_terminal":"STOP"},{"physical_valid":None}])
def test_physical_rejection_is_not_infrastructure_or_unknown(tmp_path,changes):
    facts=rejected(tmp_path / "candidate",**changes)
    assert not c.candidate_rejection(tmp_path / "candidate",facts)


def test_only_continuation_skips_explained_physical_rejection(tmp_path,monkeypatch):
    cfg,p,previous,a,s=stopped(tmp_path)
    batch=c.ConservativeBatch(cfg,p);batch.prepare()
    out=Path(cfg["output"]) / "dof-direct"; rejected(out)
    def failure(self,condition,paid):
        raise RuntimeError("DoF execution/scoring/all-seed validation not reached")
    monkeypatch.setattr(AcceptanceBatch,"dof",failure)
    assert batch.dof("direct",paid=True)=="RESEARCH_RESULT"
    assert json.loads((out / "summary.json").read_text())["physical_valid"] is False
    assert json.loads((out / "continuation-result.json").read_text())["candidate_repaired"] is False
    with pytest.raises(RuntimeError):batch.dof("direct",paid=False)


@pytest.mark.parametrize("value",["NOT_RUN",{"status":"NOT_RUN"}])
def test_unrun_score_accepts_actual_string_and_dictionary_shape(tmp_path,value):
    facts=rejected(tmp_path / "candidate",post_commit_scoring=value)
    assert c.candidate_rejection(tmp_path / "candidate",facts)


def test_imported_cost_controls_remaining_reservation(tmp_path,monkeypatch):
    cfg,p,previous,a,s=stopped(tmp_path);clock(monkeypatch)
    monkeypatch.setattr(AcceptanceBatch,"versions",lambda self:None)
    monkeypatch.setattr(c.ConservativeBatch,"dof",lambda self,n,paid:None)
    monkeypatch.setattr(AcceptanceBatch,"ablation",lambda self,n:None)
    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.configured_environment.configured_environment",lambda **kw:{})
    c.run(cfg,p,previous,a,s)
    from integrations.llm.runtime.batch_budget import BatchBudget,BatchStopped
    ledger=BatchBudget(Path(cfg["ledger_path"]),cfg["cost"])
    with ledger.transaction() as db:db.execute("UPDATE requests SET accounted=9.99")
    with pytest.raises(BatchStopped,match="INSUFFICIENT_RESERVATION"):ledger.reserve("S1",20)
