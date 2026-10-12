import asyncio
from pathlib import Path
import pytest
from experiments.dof.runtime.post_commit import PostCommit, validate_config
from experiments.dof.runtime.prepared import mock_material, run_prepared
from optimizer_pipeline.scoring import file_sha256
from optimizer_pipeline.core.store import SQLiteRunStore


def config():
    return dict(seeds=[1, 2, 3], vectors=200, precheck_vectors=100,
        prepare_timeout_seconds=60, seed_timeout_seconds=60, score_timeout_seconds=60,
        target_clock="clk", epsilon_fmax=0.01)


def ample_memory():
    return (1024**4, 1024**4)


def callbacks(tmp_path, *, wrong_seed=False, infrastructure=False, score_failure=False):
    def score(path, **kwargs):
        if score_failure:
            raise TimeoutError()
        return {"score": {"fmax_mhz": 100 if Path(path).name == "root.dcp" else 105}}
    def prepare(**kwargs):
        return {"workspace": tmp_path, "report": {"simulation_report": {"full_vectors": 200}}}
    def seed(**kwargs):
        if infrastructure:
            raise TimeoutError()
        return dict(stimulus_seed=1 if wrong_seed else kwargs["stimulus_seed"],
            cycles_simulated=200, passed=True, mismatch_count=0, protocol_mismatch_count=0,
            result_fail_seen=False, simulator_failed=False, infrastructure_failure=False,
            infrastructure_reason=None)
    return score, prepare, seed


@pytest.mark.parametrize("condition", ["recipe", "direct", "constructed"])
def test_fixed_full_chain_records_scoring_seeds_and_research_separately(tmp_path, condition):
    root = tmp_path / "root.dcp"
    root.write_text("fixture only; no FPGA tool invoked")
    context, messages = mock_material(condition)
    context.update(root_sha256=file_sha256(root), observation={"timing": {}},
                   trial_id="offline-adapter-test", handle_secret_hex="01" * 32)
    calls = []
    async def execute(**kwargs):
        calls.append("execute")
        child = tmp_path / "child.dcp"
        child.write_text("synthetic adapter child")
        return {"canonical_endpoint": "VALID_COMPLETION", "child_path": str(child)}
    score, prepare, seed = callbacks(tmp_path)
    post = PostCommit(config=config(), golden=root, python=Path("python"), vivado=Path("vivado"),
        scorer_script=Path("scorer.tcl"), scorer=score, prepare=prepare, seed_runner=seed, memory_probe=ample_memory)
    result = asyncio.run(run_prepared(condition=condition, mode="prepared-real", root=root,
        output=tmp_path / "run", context=context, messages=messages * 2, execute=execute, post_commit=post))
    assert calls == ["execute"]
    assert result["post_commit_scoring"]["delta_fmax_mhz"] == 5
    assert result["functional_validation"]["passed"] is True
    assert result["research_judgement"]["functional_scientific_delta_mhz"] == 5
    store = SQLiteRunStore(tmp_path / "run/run.db")
    try:
        assert {"validation-functional", "validation-seed-0", "validation-seed-1", "validation-seed-2"} <= {
            row.validation_id for row in store.validations()}
    finally:
        store.close()


@pytest.mark.parametrize("case, expected", [("wrong_seed", "FUNCTIONAL_INCOMPLETE"),
                                           ("infrastructure", "VALIDATOR_INFRASTRUCTURE_FAILURE")])
def test_no_missing_or_duplicate_seed_becomes_a_pass(tmp_path, case, expected):
    root, child = tmp_path / "root.dcp", tmp_path / "child.dcp"
    root.write_text("root"); child.write_text("child")
    score, prepare, seed = callbacks(tmp_path, **{case: True})
    post = PostCommit(config=config(), golden=root, python=Path("python"), vivado=Path("vivado"),
        scorer_script=Path("scorer"), scorer=score, prepare=prepare, seed_runner=seed, memory_probe=ample_memory)
    result = post(root=root, child=child, output=tmp_path / "post")
    assert result["functional_validation"]["status"] == expected
    assert result["functional_validation"]["passed"] is None
    assert result["research_judgement"]["functional_scientific_delta_mhz"] is None


def test_scoring_failure_does_not_erase_functional_result(tmp_path):
    root, child = tmp_path / "root.dcp", tmp_path / "child.dcp"
    root.write_text("root"); child.write_text("child")
    score, prepare, seed = callbacks(tmp_path, score_failure=True)
    result = PostCommit(config=config(), golden=root, python=Path("python"), vivado=Path("vivado"),
        scorer_script=Path("scorer"), scorer=score, prepare=prepare, seed_runner=seed, memory_probe=ample_memory)(
            root=root, child=child, output=tmp_path / "post")
    assert result["scoring"]["status"] == "SCORING_FAILURE"
    assert result["functional_validation"]["passed"] is True
    assert result["research_judgement"]["functional_scientific_delta_mhz"] is None


def test_duplicate_config_seed_stops_before_tools():
    with pytest.raises(ValueError, match="Duplicate"):
        validate_config(config() | {"seeds": [1, 1]})
