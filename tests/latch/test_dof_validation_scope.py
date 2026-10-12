"""Offline validation-label fixtures; no EDA/model or historical output writes."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from experiments.acceptance.runner import public_dof
from experiments.dof.runtime.post_commit import PostCommit, validate_config
from experiments.dof.runtime.prepared import mock_material, run_prepared
from experiments.dof.runtime.validation_scope import WP015_SEEDS, describe_validation, public_validation_plan
from optimizer_pipeline.core.store import SQLiteRunStore
from optimizer_pipeline.scoring import file_sha256


def config(seeds=WP015_SEEDS, vectors=200, precheck_vectors=100):
    return dict(seeds=list(seeds), vectors=vectors, precheck_vectors=precheck_vectors,
        prepare_timeout_seconds=60, seed_timeout_seconds=60, score_timeout_seconds=60,
        target_clock="clk", epsilon_fmax=0,
        private_extra={"secret": "not-for-public-output"})


def adapter(tmp_path, cfg, *, fault=None, delta=5):
    root, child = tmp_path / "root.dcp", tmp_path / "child.dcp"
    root.write_text("offline root fixture")
    child.write_text("offline child fixture")
    calls = []
    def score(path, **kwargs):
        calls.append(("score", Path(path).name))
        if fault == "score_failure":
            raise TimeoutError()
        return {"score": {"fmax_mhz": 100 if Path(path) == root else 100 + delta}}
    def prepare(**kwargs):
        calls.append(("prepare", list(kwargs["seeds"]), kwargs["vectors"], kwargs["precheck_vectors"]))
        expected = kwargs["precheck_vectors"] < kwargs["vectors"]
        simulation = {"full_vectors": kwargs["vectors"],
            "precheck_vectors": kwargs["precheck_vectors"] if expected else None,
            "precheck_passed": True if expected else None,
            "precheck_report": dict(passed=True, cycles_simulated=kwargs["precheck_vectors"],
                mismatch_count=0, protocol_mismatch_count=0, infrastructure_failure=False,
                result_fail_seen=False, simulator_failed=False) if expected else None}
        if fault == "unknown_precheck":
            simulation.pop("precheck_report")
        if fault == "short_precheck":
            simulation["precheck_report"]["cycles_simulated"] -= 1
        return {"workspace": tmp_path, "report": {"phase1_passed": True, "simulation_report": simulation}}
    def seed(**kwargs):
        calls.append(("seed", kwargs["stimulus_seed"], kwargs["vectors"]))
        if fault == "infrastructure":
            raise TimeoutError()
        row = dict(stimulus_seed=kwargs["stimulus_seed"], vectors_requested=kwargs["vectors"],
            cycles_simulated=kwargs["vectors"], passed=True, mismatch_count=0, protocol_mismatch_count=0,
            result_fail_seen=False, simulator_failed=False, infrastructure_failure=False,
            infrastructure_reason=None)
        if fault == "duplicate":
            row["stimulus_seed"] = cfg["seeds"][0]
        if fault == "missing":
            row.pop("stimulus_seed")
        if fault == "short_seed":
            row["cycles_simulated"] -= 1
        if fault == "mismatch":
            row.update(passed=False, mismatch_count=1, result_fail_seen=True)
        return row
    post = PostCommit(config=cfg, golden=root, python=Path("fixture-python"), vivado=Path("fixture-vivado"),
        scorer_script=Path("fixture-scorer"), scorer=score, prepare=prepare, seed_runner=seed,
        memory_probe=lambda: (1024**4, 1024**4))
    return root, child, post, calls


@pytest.mark.parametrize("seeds,vectors,precheck,matched,mismatches", [
    ([7], 200, 100, False, ["seeds"]),
    ([1, 2, 3], 200, 100, False, ["seeds"]),
    (WP015_SEEDS, 200, 100, True, []),
    (WP015_SEEDS, 201, 100, False, ["vectors"]),
    (WP015_SEEDS, 200, 99, False, ["precheck_vectors"]),
    ([1, 2, 3, 4, 5], 200, 100, False, ["seeds"]),
    (tuple(reversed(WP015_SEEDS)), 200, 100, True, []),
    ([7], 1, 1, False, ["seeds", "vectors", "precheck_vectors"]),
])
def test_custom_and_wp015_strength_are_described_without_restricting_execution(tmp_path, seeds, vectors, precheck, matched, mismatches):
    cfg = config(seeds, vectors, precheck)
    root, child, post, calls = adapter(tmp_path, cfg)
    before = deepcopy(cfg)
    result = post(root=root, child=child, output=tmp_path / "post")
    plan, scope = result["validation_plan"], result["validation_assessment"]
    assert plan == dict(seeds=list(seeds), vectors=vectors, precheck_vectors=precheck)
    assert scope["configured_validation_passed"] is True
    assert scope["wp015_profile_match"] is matched and scope["wp015_profile_mismatch_fields"] == mismatches
    assert scope["execution_complete"] is True and scope["wp015_profile_sampled_pass"] is matched
    assert scope["display_status"] == ("WP015_PROFILE_SAMPLED_PASS" if matched else "CUSTOM_SAMPLED_PASS")
    assert result["functional_validation"]["status"] == "MULTISEED_FUNCTIONAL_PASS"
    assert result["research_judgement"] == {"functional_scientific_delta_mhz": 5,
                                           "status": "FUNCTIONALLY_CERTIFIED_MEASURED"}
    assert calls == [("score", "root.dcp"), ("score", "child.dcp"),
        ("prepare", list(seeds), vectors, precheck), *[("seed", seed, vectors) for seed in seeds]]
    assert cfg == before
    assert "not-for-public-output" not in json.dumps(result)
    assert json.loads((tmp_path / "post/post-commit.json").read_text()) == result
    public = public_dof(dict(result, post_commit_scoring=result["scoring"]))
    assert public["validation_plan"] == plan and public["validation_assessment"] == scope


@pytest.mark.parametrize("fault,legacy_status,legacy_pass,complete", [
    ("duplicate", "FUNCTIONAL_INCOMPLETE", None, False),
    ("missing", "FUNCTIONAL_INCOMPLETE", None, False),
    ("mismatch", "FUNCTIONAL_MISMATCH", False, True),
    ("infrastructure", "VALIDATOR_INFRASTRUCTURE_FAILURE", None, False),
    ("short_seed", "MULTISEED_FUNCTIONAL_PASS", True, False),
    ("unknown_precheck", "MULTISEED_FUNCTIONAL_PASS", True, None),
    ("short_precheck", "MULTISEED_FUNCTIONAL_PASS", True, False),
])
def test_profile_match_is_not_actual_pass_or_execution_completeness(tmp_path, fault, legacy_status, legacy_pass, complete):
    root, child, post, _ = adapter(tmp_path, config(), fault=fault)
    result = post(root=root, child=child, output=tmp_path / "post")
    functional, scope = result["functional_validation"], result["validation_assessment"]
    assert functional["status"] == legacy_status and functional["passed"] is legacy_pass
    assert scope["wp015_profile_match"] is True
    assert scope["configured_validation_passed"] is legacy_pass
    assert scope["execution_complete"] is complete and scope["wp015_profile_sampled_pass"] is False
    assert not scope["display_status"].endswith("SAMPLED_PASS")
    assert result["research_judgement"]["functional_scientific_delta_mhz"] == (5 if legacy_pass is True else None)
    assert public_dof(result)["validation_assessment"] == scope


@pytest.mark.parametrize("delta", [0, -5])
def test_label_changes_do_not_filter_zero_or_negative_scientific_delta(tmp_path, delta):
    root, child, post, _ = adapter(tmp_path, config([7]), delta=delta)
    result = post(root=root, child=child, output=tmp_path / "post")
    assert result["research_judgement"]["functional_scientific_delta_mhz"] == delta
    assert result["validation_assessment"]["display_status"] == "CUSTOM_SAMPLED_PASS"


def test_scoring_failure_is_separate_from_functional_profile(tmp_path):
    root, child, post, _ = adapter(tmp_path, config(), fault="score_failure")
    result = post(root=root, child=child, output=tmp_path / "post")
    assert result["scoring"]["status"] == "SCORING_FAILURE"
    assert result["functional_validation"]["passed"] is True
    assert result["research_judgement"]["functional_scientific_delta_mhz"] is None
    assert result["validation_assessment"]["wp015_profile_sampled_pass"] is True


@pytest.mark.parametrize("stage", ["invalid", "stop", "execution_failed", "execution_exception"])
def test_provided_plan_is_recorded_not_run_before_post_commit(tmp_path, stage):
    root, child, post, calls = adapter(tmp_path, config())
    context, messages = mock_material("recipe")
    context.update(root_sha256=file_sha256(root), observation={"timing": {}},
                   trial_id="validation-scope-fixture", handle_secret_hex="01" * 32)
    if stage == "invalid":
        messages = [{"kind": "COMMIT", "candidate": {"language": "M", "action_key": "macro.unknown"}}] * 4
    elif stage == "stop":
        messages = [{"kind": "STOP"}]
    async def execute(**kwargs):
        if stage == "execution_exception":
            raise RuntimeError("fixture dispatch failure")
        return {"canonical_endpoint": "FIXTURE_FAILURE", "child_path": str(child)}
    arguments = dict(condition="recipe", mode="prepared-real", root=root,
        output=tmp_path / "run", context=context, messages=messages, execute=execute, post_commit=post)
    if stage == "execution_exception":
        with pytest.raises(RuntimeError, match="fixture dispatch failure"):
            asyncio.run(run_prepared(**arguments))
        result = json.loads((tmp_path / "run/summary.json").read_text())
    else:
        result = asyncio.run(run_prepared(**arguments))
    assert calls == []
    assert result["validation_plan"] == public_validation_plan(config())
    assert result["validation_assessment"]["wp015_profile_match"] is True
    assert result["validation_assessment"]["display_status"] == "NOT_RUN"
    assert result["validation_assessment"]["configured_validation_passed"] is None
    assert result["validation_assessment"]["wp015_profile_sampled_pass"] is False
    assert "not-for-public-output" not in json.dumps(public_dof(result))


def test_summary_and_sqlite_validation_details_keep_actual_plan(tmp_path):
    root, child, post, _ = adapter(tmp_path, config([7]))
    context, messages = mock_material("recipe")
    context.update(root_sha256=file_sha256(root), observation={"timing": {}},
                   trial_id="validation-scope-fixture", handle_secret_hex="01" * 32)
    async def execute(**kwargs):
        return {"canonical_endpoint": "VALID_COMPLETION", "child_path": str(child)}
    result = asyncio.run(run_prepared(condition="recipe", mode="prepared-real", root=root,
        output=tmp_path / "run", context=context, messages=messages, execute=execute, post_commit=post))
    assert result["validation_plan"] == public_validation_plan(config([7]))
    assert result["validation_assessment"]["display_status"] == "CUSTOM_SAMPLED_PASS"
    store = SQLiteRunStore(tmp_path / "run/run.db")
    try:
        row = next(row for row in store.validations() if row.validation_id == "validation-functional")
        assert row.details.to_dict()["validation_plan"] == result["validation_plan"]
        assert row.details.to_dict()["validation_assessment"] == result["validation_assessment"]
    finally:
        store.close()
    assert json.loads((tmp_path / "run/summary.json").read_text()) == result


@pytest.mark.parametrize("seeds", [[], [7, 7], None])
def test_existing_invalid_configuration_refusal_is_unchanged(seeds):
    with pytest.raises(ValueError):
        validate_config(config() | {"seeds": seeds})


def test_older_public_result_retains_fields_without_guessing_profile():
    facts = dict(functional_validation=dict(status="MULTISEED_FUNCTIONAL_PASS", passed=True),
        research_judgement=dict(status="FUNCTIONALLY_CERTIFIED_MEASURED", functional_scientific_delta_mhz=5))
    before = deepcopy(facts)
    public = public_dof(facts)
    assert facts == before and public["functional_status"] == "MULTISEED_FUNCTIONAL_PASS"
    assert public["research_judgement"] == facts["research_judgement"]
    assert public["validation_plan"] is None
    assert public["validation_assessment"]["wp015_profile_match"] is None
    assert public["validation_assessment"]["display_status"] == "CONFIGURED_PASS_EXECUTION_UNVERIFIED"


def test_public_scope_does_not_trust_unknown_fields_or_short_seed_completion(tmp_path):
    root, child, post, _ = adapter(tmp_path, config(), fault="short_seed")
    facts = post(root=root, child=child, output=tmp_path / "post")
    facts["validation_assessment"].update(execution_complete=True, display_status="not-for-public-output",
        profile_comparison_scope="not-for-public-output", unknown="not-for-public-output")
    public = public_dof(facts)
    assert "not-for-public-output" not in json.dumps(public)
    assert public["validation_assessment"]["execution_complete"] is False
    assert public["validation_assessment"]["wp015_profile_sampled_pass"] is False


def test_plain_callback_without_plan_stays_compatible(tmp_path):
    root, child, _, _ = adapter(tmp_path, config())
    context, messages = mock_material("recipe")
    context.update(root_sha256=file_sha256(root), observation={"timing": {}},
                   trial_id="validation-scope-fixture", handle_secret_hex="01" * 32)
    async def execute(**kwargs):
        return {"canonical_endpoint": "VALID_COMPLETION", "child_path": str(child)}
    def old_callback(**kwargs):
        return dict(scoring={"status": "MEASURED", "delta_fmax_mhz": 5},
            functional_validation={"status": "MULTISEED_FUNCTIONAL_PASS", "passed": True},
            research_judgement={"status": "FUNCTIONALLY_CERTIFIED_MEASURED", "functional_scientific_delta_mhz": 5},
            inputs_unchanged=True)
    result = asyncio.run(run_prepared(condition="recipe", mode="prepared-real", root=root,
        output=tmp_path / "run", context=context, messages=messages, execute=execute, post_commit=old_callback))
    assert result["functional_validation"]["passed"] is True
    assert result["research_judgement"]["functional_scientific_delta_mhz"] == 5
    assert result["validation_plan"] is None and result["validation_assessment"]["wp015_profile_match"] is None
    assert result["validation_assessment"]["display_status"] == "CONFIGURED_PASS_EXECUTION_UNVERIFIED"
