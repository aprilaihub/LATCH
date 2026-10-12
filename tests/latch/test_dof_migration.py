import asyncio
import ast
import json
from pathlib import Path

import pytest

from experiments.dof.runtime.prepared import make_session, mock_material, run_prepared
from optimizer_pipeline.core.store import SQLiteRunStore
from optimizer_pipeline.core.domain import ValidationStatus
from optimizer_pipeline.scoring import file_sha256


@pytest.mark.parametrize("condition", ["recipe", "direct", "constructed"])
def test_fixed_response_single_commit_and_no_fake_functional_pass(tmp_path, condition):
    root = tmp_path / "root.dcp"
    root.write_text("mock root")
    context, messages = mock_material(condition)
    calls = []
    async def executor(**kwargs):
        calls.append(kwargs)
        child = tmp_path / "child.dcp"
        child.write_text("mock child")
        return {"canonical_endpoint": "MOCK_COMPLETION", "child_path": str(child)}
    facts = asyncio.run(run_prepared(condition=condition, mode="mock", root=root,
        output=tmp_path / "run", context=context, messages=messages + messages, execute=executor))
    assert facts["planning_terminal"] == "COMMITTED"
    assert facts["valid_commits"] == facts["turns_used"] == len(calls) == 1
    assert facts["functional_validation"]["passed"] is None
    assert facts["root_unchanged"]
    assert not facts["historical_replay"]
    store = SQLiteRunStore(tmp_path / "run/run.db")
    try:
        assert len(store.attempts()) == 1
        assert all(row.status != ValidationStatus.PASSED for row in store.validations())
    finally:
        store.close()


def test_real_context_binding_cannot_be_silently_disabled(tmp_path):
    root = tmp_path / "root.dcp"
    root.write_text("root")
    with pytest.raises(ValueError, match="does not bind"):
        make_session(condition="recipe", root=root, context={}, mode="prepared-real")
    with pytest.raises(ValueError, match="captured observation"):
        make_session(condition="recipe", root=root,
                     context={"root_sha256": file_sha256(root)}, mode="prepared-real")


def test_dispatch_completion_without_child_is_not_physically_valid(tmp_path):
    root = tmp_path / "root.dcp"
    root.write_text("mock material, no tools invoked")
    context, messages = mock_material("recipe")
    context.update(root_sha256=file_sha256(root), observation={"timing": {}},
                   trial_id="test", handle_secret_hex="01" * 32)
    async def broken_executor(**kwargs):
        return {"canonical_endpoint": "VALID_COMPLETION"}
    facts = asyncio.run(run_prepared(condition="recipe", mode="prepared-real", root=root,
        output=tmp_path / "run", context=context, messages=messages, execute=broken_executor))
    assert facts["physical_valid"] is False
    assert facts["physical_terminal"] == "MISSING_CHILD_ARTIFACT"
    assert facts["functional_validation"]["passed"] is None


def test_bad_proposal_is_research_result_and_does_not_execute(tmp_path):
    root = tmp_path / "root.dcp"
    root.write_text("mock root")
    context, _ = mock_material("recipe")
    messages = [{"kind": "COMMIT", "candidate": {"language": "M", "action_key": "macro.invented"}}] * 4
    facts = asyncio.run(run_prepared(condition="recipe", mode="mock", root=root,
        output=tmp_path / "run", context=context, messages=messages))
    assert facts["planning_terminal"] == "NO_VALID_SUBMISSION"
    assert facts["valid_commits"] == 0
    assert not facts["execution_started"]


def test_relocated_protocol_function_bodies_match_historical_library():
    root = Path(__file__).resolve().parents[2]
    for path in (root / "experiments/dof/protocol").glob("*.py"):
        old = root / "optimizer_pipeline/dof/single_submission" / path.name
        def bodies(source):
            return [ast.dump(node, include_attributes=False) for node in ast.parse(source).body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        assert bodies(path.read_text()) == bodies(old.read_text()), path.name


def test_strict_constructed_firewall_rejects_p_disguised_as_g(tmp_path):
    root = tmp_path / "root.dcp"
    root.write_text("mock root")
    context, _ = mock_material("constructed")
    session = make_session(condition="constructed", root=root, context=context, mode="mock")
    candidate = {"language": "G", "steps": [
        {"step_id": "s1", "op": "UNROUTE", "args": {}},
        {"step_id": "s2", "op": "ROUTE", "args": {"directive": "Default"}}]}
    result = session.accept({"kind": "COMMIT", "candidate": candidate})
    assert result.status == "CONSTRUCTION_INVALID"
    assert session.valid_commits == 0
