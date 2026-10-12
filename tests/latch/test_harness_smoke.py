import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest

from experiments.acceptance import harness as h
from experiments.acceptance.config import VALIDATION
from experiments.acceptance.runner import AcceptanceBatch
from tests.latch.test_acceptance_orchestration import config


def fixture_candidate(workspace, status="SUCCEEDED", validation="CLEAN", root_fallback=False):
    workspace.mkdir()
    child = workspace / "candidate.dcp"
    child.write_text("fixed candidate")
    db = sqlite3.connect(workspace / "run.db")
    db.executescript("CREATE TABLE artifacts(artifact_id,uri); "
        "CREATE TABLE outcomes(status,error_type,metrics_json,output_artifact_id,attempt_id); "
        "CREATE TABLE validations(status,level,attempt_id);")
    db.execute("INSERT INTO artifacts VALUES(?,?)", ("child", str(child)))
    db.execute("INSERT INTO outcomes VALUES(?,?,?,?,?)", (status,None,"{}","child","one"))
    db.execute("INSERT INTO validations VALUES(?,?,?)", (validation,"ATTEMPT","one"))
    db.commit()
    db.close()
    # A root-selected manifest must not substitute for the executed child.
    (workspace / "result_manifest.json").write_text(json.dumps({"final_dcp":"not-the-candidate"}))
    return child


@pytest.mark.parametrize("status,validation,valid", [("SUCCEEDED","CLEAN",True),
    ("SUCCEEDED","WARNING",True),("SUCCEEDED","INVALID",False),("FAILED","CLEAN",False)])
def test_candidate_reads_attempt_not_selection(tmp_path,status,validation,valid):
    child = fixture_candidate(tmp_path / "run",status,validation)
    actual,facts = h.candidate_facts(tmp_path / "run")
    assert actual == child
    assert facts["physical_valid"] is valid


def post_result():
    return dict(inputs_unchanged=True,scoring=dict(status="MEASURED",delta_fmax_mhz=-1),
        functional_validation=dict(status="MULTISEED_FUNCTIONAL_PASS",passed=True,
            all_seeds_present_exactly_once=True,secret="never export",
            seed_reports=[dict(stimulus_seed=s,passed=True,cycles_simulated=200,mismatch_count=0,
                protocol_mismatch_count=0,infrastructure_failure=False,secret="never export") for s in VALIDATION["seeds"]]))


@pytest.mark.parametrize("fault", ["missing","duplicate","unknown_infra","score","input","cycles"])
def test_incomplete_evidence_not_promoted(tmp_path,fault):
    result = post_result()
    rows = result["functional_validation"]["seed_reports"]
    if fault == "missing": rows.pop()
    elif fault == "duplicate": rows[1] = rows[0]
    elif fault == "unknown_infra": rows[0]["infrastructure_failure"] = None
    elif fault == "score": result["scoring"]["status"] = "SCORING_FAILURE"
    elif fault == "input": result["inputs_unchanged"] = False
    elif fault == "cycles": rows[0]["cycles_simulated"] = 0
    facts = h.validation_facts(result)
    assert not facts["validation_reached"]
    assert "never export" not in json.dumps(facts)


def test_no_model_owned_run_is_independent_and_no_replay(tmp_path,monkeypatch):
    cfg = config(tmp_path)
    cfg.pop("cost")  # Physical-only acceptance must not invent fresh pricing.
    source = Path(cfg["output"])
    source.mkdir()
    (source / "batch.db").write_bytes(b"old immutable ledger")
    output = tmp_path / "harness"
    monkeypatch.setenv("OPENAI_API_KEY","must not copy")
    monkeypatch.setenv("XILINXD_LICENSE_FILE","retain license")
    monkeypatch.setattr(AcceptanceBatch,"versions",lambda self: None)
    monkeypatch.setattr(AcceptanceBatch,"memory_check",lambda self: None)
    def owned(argv,**kwargs):
        assert kwargs["cwd"] == "/tmp" and kwargs["timeout"] == 7200
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["XILINXD_LICENSE_FILE"] == "retain license"
        fixture_candidate(output / "run")
        return SimpleNamespace(returncode=0,stdout="",stderr="")
    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned",owned)
    class Post:
        def __init__(self,**kwargs): assert kwargs["config"] == VALIDATION
        def __call__(self,**kwargs): return post_result()
    monkeypatch.setattr("experiments.dof.runtime.post_commit.PostCommit",Post)
    result = h.run(cfg,tmp_path / "config.json",output)
    assert result["status"] == "HARNESS_COVERAGE_REACHED"
    assert result["model_requests"] == 0
    assert result["original_input_unchanged"]
    assert not (output / "batch.db").exists()
    assert (source / "batch.db").read_bytes() == b"old immutable ledger"
    with pytest.raises(ValueError): h.run(cfg,tmp_path / "config.json",output)


def test_paid_batch_still_rejects_unknown_price_before_preparing_ledger(tmp_path):
    cfg = config(tmp_path)
    cfg["cost"]["verified_current"] = False
    batch = AcceptanceBatch(cfg, tmp_path / "config.json")
    with pytest.raises(ValueError, match="pricing evidence"):
        batch.prepare()
    assert not Path(cfg["ledger_path"]).exists()
