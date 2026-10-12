import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest
from experiments.acceptance import supplement as s
from experiments.acceptance.runner import AcceptanceBatch
from tests.latch.test_acceptance_orchestration import config


def prepared(tmp_path):
    cfg = config(tmp_path)
    source = Path(cfg["output"])
    (source / "S0-extension/nodes").mkdir(parents=True)
    (source / "S0-baseline").mkdir()
    candidate = source / "S0-extension/nodes/output.dcp"
    candidate.write_text("existing pruned checkpoint")
    baseline = source / "S0-baseline/final_output.dcp"
    baseline.write_text("baseline")
    node = dict(depth=1, output_dcp=str(candidate), metrics=dict(
        prune_reason="severe_regression", pruned_by_qor_gate=True))
    summary = dict(nodes=[node], state=dict(root_dcp=str(baseline)), final_output_dcp=None)
    (source / "S0-extension/pipeline_summary.json").write_text(json.dumps(summary))
    (source / "S0-baseline/pipeline_summary.json").write_text(json.dumps(dict(final_output_dcp=str(baseline))))
    (source / "batch.db").write_bytes(b"immutable fixture ledger")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return cfg, path, candidate, tmp_path / "supplement"


def test_unique_existing_candidate_and_no_output_created(tmp_path):
    cfg, _, candidate, output = prepared(tmp_path)
    assert s.select_candidate(cfg, output) == (Path(cfg["golden"]), candidate, output)
    assert not output.exists()


@pytest.mark.parametrize("fault", ["missing", "ambiguous", "wrong-root", "outside", "not-pruned"])
def test_missing_ambiguous_or_different_candidate_stops(tmp_path, fault):
    cfg, _, candidate, output = prepared(tmp_path)
    path = Path(cfg["output"]) / "S0-extension/pipeline_summary.json"
    summary = json.loads(path.read_text())
    if fault == "missing":
        candidate.unlink()
    elif fault == "ambiguous":
        summary["nodes"] *= 2
    elif fault == "wrong-root":
        summary["state"]["root_dcp"] = cfg["golden"]
    elif fault == "outside":
        summary["nodes"][0]["output_dcp"] = cfg["golden"]
    else:
        summary["nodes"][0]["metrics"]["pruned_by_qor_gate"] = False
    path.write_text(json.dumps(summary))
    with pytest.raises((ValueError, FileNotFoundError)):
        s.select_candidate(cfg, output)
    assert not output.exists()


def test_source_enclosing_and_existing_outputs_rejected(tmp_path):
    cfg, _, _, output = prepared(tmp_path)
    output.mkdir()
    for target in (output, Path(cfg["output"]) / "new", tmp_path):
        with pytest.raises(ValueError):
            s.select_candidate(cfg, target)


def raw_gate(tmp_path, **changes):
    raw = dict(num_vectors=200, precheck_vectors=100, stimulus_seeds=[s.SEED], secret="omitted",
        simulation_report=dict(seed_reports_complete=True, cycles_simulated=200,
            mismatch_count=0, protocol_mismatch_count=0,
            seed_reports=[dict(stimulus_seed=s.SEED, passed=True, cycles_simulated=200,
                mismatch_count=0, protocol_mismatch_count=0, infrastructure_failure=False, secret="omitted")]))
    path = tmp_path / "validation.json"
    path.write_text(json.dumps(raw))
    gate = dict(passed=True, phase1_passed=True, phase2_passed=True, phase2_skipped=False,
                infrastructure_failure=False, report_path=str(path)) | changes
    return gate, path


@pytest.mark.parametrize("changes,status", [({}, "DIAGNOSTIC_PASS"),
    (dict(passed=False, phase2_passed=False), "FUNCTIONAL_MISMATCH"),
    (dict(phase1_passed=False, passed=False), "STRUCTURAL_FAILURE"),
    (dict(infrastructure_failure=True, passed=False), "INFRASTRUCTURE_FAILURE"),
    (dict(phase2_skipped=True), "UNREACHED"), (dict(report_path=None), "UNREACHED")])
def test_result_classification_and_secret_omission(tmp_path, changes, status):
    gate, _ = raw_gate(tmp_path, **changes)
    result = s.sanitized_gate(gate)
    assert result["status"] == status
    assert "secret" not in json.dumps(result) and "omitted" not in json.dumps(result)


def test_wp015_or_incomplete_seed_evidence_cannot_be_promoted(tmp_path):
    gate, path = raw_gate(tmp_path)
    raw = json.loads(path.read_text())
    raw["stimulus_seeds"] = [0xC0FFEE]
    path.write_text(json.dumps(raw))
    assert s.sanitized_gate(gate)["status"] == "UNREACHED"
    raw["stimulus_seeds"] = [s.SEED]
    raw["simulation_report"]["seed_reports_complete"] = False
    path.write_text(json.dumps(raw))
    assert s.sanitized_gate(gate)["status"] == "UNREACHED"



@pytest.mark.parametrize("field,value", [("mismatch_count", 1), ("protocol_mismatch_count", 1),
                                        ("infrastructure_failure", None)])
def test_contradictory_or_unknown_seed_evidence_is_not_pass(tmp_path, field, value):
    gate, path = raw_gate(tmp_path)
    raw = json.loads(path.read_text())
    raw["simulation_report"]["seed_reports"][0][field] = value
    path.write_text(json.dumps(raw))
    assert s.sanitized_gate(gate)["status"] == "UNREACHED"

@pytest.mark.parametrize("failure", [None, "timeout", "worker-failed", "input-mutated", "memory"])
def test_owned_run_is_independent_one_shot_and_no_model_credentials(tmp_path, monkeypatch, failure):
    cfg, path, candidate, output = prepared(tmp_path)
    source = Path(cfg["output"])
    ledger_before = (source / "batch.db").read_bytes()
    private_output = "private-worker-diagnostic-sentinel"
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-copy")
    monkeypatch.setenv("XILINXD_LICENSE_FILE", "retain-license")
    monkeypatch.setattr(AcceptanceBatch, "versions", lambda self: None)
    def memory(self):
        if failure == "memory":
            raise RuntimeError("memory")
    monkeypatch.setattr(AcceptanceBatch, "memory_check", memory)
    calls = []
    def owned(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 14400
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["XILINXD_LICENSE_FILE"] == "retain-license"
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 14400, output=private_output.encode())
        if failure == "input-mutated":
            candidate.write_text("changed")
        (output / "functional-result.json").write_text(json.dumps(dict(status="DIAGNOSTIC_PASS")))
        return SimpleNamespace(stdout=private_output, stderr="", returncode=1 if failure == "worker-failed" else 0)
    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.owned_process.run_owned", owned)
    result = s.run(cfg, path, output)
    expected = "DIAGNOSTIC_PASS" if failure is None else "INPUT_PROTECTION_FAILURE" if failure == "input-mutated" else "INFRASTRUCTURE_FAILURE"
    assert result["status"] == expected
    assert result["source_batch_acceptance"] == "INCOMPLETE" and result["promotes_source_batch"] is False
    assert result["model_requests"] == result["optimization_runs"] == 0
    assert (source / "batch.db").read_bytes() == ledger_before
    assert not (output / "batch.db").exists()
    serialized = json.dumps(result)
    assert private_output not in serialized and "do-not-copy" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert not {"stdout", "stderr", "env"}.intersection(result)
    assert len(calls) == (0 if failure == "memory" else 1)
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "report.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        s.run(cfg, path, output)


def test_worker_reuses_ablation_gate_and_default_seed_without_new_protocol(tmp_path, monkeypatch):
    cfg, _, candidate, output = prepared(tmp_path)
    output.mkdir()
    monkeypatch.setattr("optimizer_pipeline.backends.xilinx.configured_environment.configured_environment", lambda **kw: {})
    gate, _ = raw_gate(tmp_path)
    async def fake(golden, revised, **kwargs):
        assert golden == Path(cfg["golden"]) and revised == candidate
        assert kwargs == dict(vectors=200, precheck_vectors=100, workspace_dir=output / "validation")
        return gate
    monkeypatch.setattr("experiments.ablation.runner_cli.run_equivalence_gate", fake)
    s.worker(cfg, candidate, output)
    assert json.loads((output / "functional-result.json").read_text())["status"] == "DIAGNOSTIC_PASS"
