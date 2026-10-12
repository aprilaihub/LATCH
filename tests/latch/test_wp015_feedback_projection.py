"""Offline feedback/next-request checks against actual WP015, not WP016.

Source-only copies explicitly skip Git comparisons when 32d852a is absent.
LATCH_WP015_SOURCE_DIR may point to externally archived read-only Git sources
named wp015-32d852a-{language,contract_v2,contract_v5,planning}.py.
"""
from __future__ import annotations

import ast
import contextvars
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types
from typing import Any, Mapping

import pytest

from experiments.dof.protocol import contract_v2, contract_v5
from experiments.dof.protocol.contract_v6 import PROTOCOL_EPOCH, contract_for_cell, prompt_for_cell
from experiments.dof.protocol.planner_v2 import PlannerTurnResultV2, result_public
from experiments.dof.protocol.specs import canonical_json_bytes
from experiments.dof.runtime.planning_request import conditioned_job, planning_messages
from experiments.dof.runtime.prepared import make_session, mock_material
from experiments.dof.runtime.wp015_feedback import model_feedback
from optimizer_pipeline.dof.hardblock_compatibility import build_relation


ROOT = Path(__file__).resolve().parents[2]
CASES = ("macro", "p_direct", "p_macro", "p_typed", "g_spatial", "g_nested_p",
         "g_nested_macro", "g_collections", "g_allocation")


def material(tmp_path, case):
    condition = "recipe" if case == "macro" else "constructed" if case.startswith("g_") else "direct"
    root = tmp_path / "root.dcp"
    root.write_text("offline WP015 feedback root")
    context, _ = mock_material(condition)
    context.update(trial_id="feedback-fixture", handle_secret_hex="01" * 32,
                   fanout_admitted=True)
    session = make_session(condition=condition, root=root, context=context, mode="mock")
    registry = session.compile_context.registry
    nets = [registry.register(object_type="net", canonical_identity=f"net/{index}",
        attributes={"path_member": True, "path_rank": index + 1, "fanout": 64 - index})
        for index in range(3)]
    sites = [registry.register(object_type="site", canonical_identity=f"DSP48E2_X0Y{index}",
        attributes={"compatible_resource_types": ["DSP"]}) for index in range(2)]
    hardblocks = [registry.register(object_type="hardblock", canonical_identity=f"hb/{index}",
        attributes={"path_member": True, "path_rank": index + 1, "resource_type": "DSP",
                    "compatible_site_handles": sites}) for index in range(2)]
    region = registry.register(object_type="region", canonical_identity="predefined",
        attributes={"bounds": [0, 0, 0, 0]})
    relation = build_relation(root_sha256=registry.root_sha256, stage="ROUTED",
        target_rows=[{"handle": handle, "target_identity_sha256": str(index + 1) * 64,
                      "resource_type": "DSP", "compatible_site_handles": sites}
                     for index, handle in enumerate(hardblocks)],
        target_query_sha256="3" * 64,
        target_site_query_sha256={handle: str(index + 4) * 64 for index, handle in enumerate(hardblocks)})
    session.compile_context = replace(session.compile_context, predefined_regions=(region,),
                                     hardblock_compatibility_relation=relation)
    direct = [{"op": "UNROUTE", "args": {}},
              {"op": "ROUTE", "args": {"directive": "Default"}}]
    spatial = [
        {"op": "SELECT_WHERE", "args": {"source_type": "cell", "predicate": {
            "op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True}, "top_k": 1}, "bind": "targets"},
        {"op": "CREATE_REGION", "args": {"x_min": 1, "x_max": 2, "y_min": 1, "y_max": 2}, "bind": "region"},
        {"op": "ASSIGN_REGION", "args": {"target_handle": {"ref": "targets"}, "region_handle": {"ref": "region"}}},
    ]
    def steps(rows):
        return [dict(row, step_id=f"s{index}") for index, row in enumerate(rows, 1)]
    if case == "macro":
        candidate = {"language": "M", "action_key": "macro.phys_opt"}
    elif case == "p_direct":
        candidate = {"language": "P", "steps": steps(direct)}
    elif case == "p_macro":
        candidate = {"language": "P", "steps": steps([
            {"op": "CALL_MACRO", "args": {"action_key": "macro.phys_opt"}}, *direct])}
    elif case == "p_typed":
        candidate = {"language": "P", "steps": steps([
            {"op": "SELECT_CRITICAL_NETS", "args": {"top_k": 1}, "bind": "target"},
            direct[0], {"op": "ROUTE_NET", "args": {"typed_net": {"ref": "target"}, "delay_mode": "AUTO_DELAY"}}])}
    elif case in {"g_spatial", "g_statements"}:
        candidate = {"language": "G", "steps": steps(spatial + direct)}
        if case == "g_statements":
            candidate = {"language": "G", "statements": [
                {"statement_id": "g" + row["step_id"][1:], "node": row["op"], "args": row["args"],
                 **({"bind": row["bind"]} if "bind" in row else {})}
                for row in candidate["steps"]]}
    elif case in {"g_nested_p", "g_nested_macro"}:
        nested = direct if case == "g_nested_p" else [
            {"op": "CALL_MACRO", "args": {"action_key": "macro.phys_opt"}}, *direct]
        candidate = {"language": "G", "steps": steps(spatial + [
            {"op": "P_PROGRAM", "args": {"program": {"language": "P", "steps": steps(nested)}}}])}
    elif case == "g_collections":
        candidate = {"language": "G", "steps": steps([
            {"op": "SELECT_WHERE", "args": {"source_type": "net", "predicate": {
                "op": "COMPARE", "field": "fanout", "operator": "GE", "value": 1}, "top_k": 3}, "bind": "all_nets"},
            {"op": "RANK", "args": {"input": {"ref": "all_nets"}, "field": "fanout", "direction": "DESC"}, "bind": "ranked"},
            {"op": "FILTER", "args": {"input": {"ref": "ranked"}, "predicate": {
                "op": "COMPARE", "field": "fanout", "operator": "GE", "value": 1}}, "bind": "filtered"},
            {"op": "TOPK", "args": {"input": {"ref": "filtered"}, "k": 1}, "bind": "head"},
            {"op": "DIFF", "args": {"left": {"ref": "filtered"}, "right": {"ref": "head"}}, "bind": "tail"},
            {"op": "BATCH_ROUTE_NETS", "args": {"typed_nets": {"ref": "tail"}, "delay_mode": "AUTO_DELAY"}},
        ])}
    else:
        candidate = {"language": "G", "steps": steps([
            {"op": "SELECT_WHERE", "args": {"source_type": "hardblock", "predicate": {
                "op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True}, "top_k": 2}, "bind": "targets"},
            {"op": "SELECT_WHERE", "args": {"source_type": "site", "predicate": {
                "op": "COMPARE", "field": "object_type", "operator": "EQ", "value": "site"}, "top_k": 2}, "bind": "sites"},
            {"op": "COUPLE_SITES", "args": {"hardblocks": {"ref": "targets"}, "sites": {"ref": "sites"}, "strategy": "FORWARD"}, "bind": "allocation"},
            direct[0], {"op": "APPLY_ALLOCATION", "args": {"allocation": {"ref": "allocation"}}}, direct[1],
        ])}
    return session, condition, candidate


@pytest.fixture(scope="module")
def actual_wp015():
    def source(name, git_path):
        external = os.environ.get("LATCH_WP015_SOURCE_DIR")
        if external:
            return (Path(external) / f"wp015-32d852a-{name}.py").read_text()
        result = subprocess.run(["git", "-C", str(ROOT), "show", f"32d852a:{git_path}"],
                                capture_output=True, text=True)
        if result.returncode:
            pytest.skip("Actual WP015 32d852a source unavailable; no historical equality claim")
        return result.stdout
    language = types.ModuleType("experiments.dof.protocol._wp015_32d852a")
    language.__package__ = "experiments.dof.protocol"
    sys.modules[language.__name__] = language
    text = source("language", "optimizer_pipeline/dof/single_submission/language.py")
    # Only adapt the relocated shared helper import, not executable definitions.
    text = text.replace("from ..hardblock_compatibility import (",
                        "from optimizer_pipeline.dof.hardblock_compatibility import (")
    exec(compile(text, "32d852a-language", "exec"), language.__dict__)
    def selected(text, names, namespace, current=None):
        nodes = [node for node in ast.parse(text).body if isinstance(node, ast.FunctionDef) and node.name in names]
        assert {node.name for node in nodes} == set(names)
        if current is not None:
            bodies = {node.name: ast.dump(node, include_attributes=False) for node in ast.parse(Path(current.__file__).read_text()).body
                      if isinstance(node, ast.FunctionDef) and node.name in names}
            assert bodies == {node.name: ast.dump(node, include_attributes=False) for node in nodes}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "32d852a-selected", "exec"), namespace)
        return namespace
    v2 = selected(source("contract_v2", "optimizer_pipeline/dof/single_submission/contract_v2.py"),
        {"_finish_v2", "compile_candidate_v2"}, dict(contract_v2.__dict__) | {
            "compile_p": language.compile_p, "compile_g": language.compile_g,
            "compile_macro": language.compile_macro, "HostStep": language.HostStep,
            "CompiledProgram": language.CompiledProgram}, contract_v2)
    v5 = selected(source("contract_v5", "optimizer_pipeline/dof/single_submission/contract_v5.py"),
        {"compile_candidate_v5"}, dict(contract_v5.__dict__) | {
            "compile_candidate_v2": v2["compile_candidate_v2"], "_finish_v2": v2["_finish_v2"],
            "compile_macro": language.compile_macro}, contract_v5)
    return v5["compile_candidate_v5"], source("planning", "scripts/run_wp015_cells.py"), selected


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("kind", ["DRAFT", "VALIDATE"])
def test_projection_retains_internal_evidence_and_next_turn(tmp_path, case, kind):
    session, condition, candidate = material(tmp_path, case)
    result = session.accept({"kind": kind, "candidate": candidate})
    assert result.status == "CONSTRUCTION_VALID", result_public(result)
    rich = result_public(result)
    before, transcript, private_hash = deepcopy(rich), deepcopy(session.transcript), result.compiled.private_plan_sha256
    request = planning_messages(session=session, condition=condition, previous_result=rich)
    public = json.loads(request[1]["content"])["previous_result"]
    assert public["compiled_sha256"] == hashlib.sha256(canonical_json_bytes(public["compiled"])).hexdigest()
    assert rich == before == result_public(result)
    assert session.transcript == transcript and result.compiled.private_plan_sha256 == private_hash
    assert result.compiled.public_sha256 == rich["compiled_sha256"]
    assert session.turns_remaining == 3 and session.valid_commits == 0 and session.terminal is None
    assert "01" * 32 not in json.dumps(request)
    if case.startswith("p_"):
        assert all(row["source_step_id"] is None and row["input_bindings"] == [] for row in public["compiled"]["host_steps"])
        assert any(row["source_step_id"] is not None for row in rich["compiled"]["host_steps"])
    elif case.startswith("g_"):
        assert all("ordered_member_hashes" in row for row in rich["compiled"]["binding_records"])
        assert all("ordered_member_hashes" not in row for row in public["compiled"]["binding_records"])
        assert any(row["source_step_id"] is not None for row in public["compiled"]["host_steps"])
    else:
        assert public == rich
    if case == "g_allocation":
        allocation = next(row for row in public["compiled"]["binding_records"] if row["binding_type"] == "allocation")
        assert {"matching_size", "compatibility_relation_sha256", "feasibility_query_sha256",
                "target_site_pair_hashes", "allocation_sha256", "root_sha256", "stage"} <= set(allocation)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("kind", ["DRAFT", "VALIDATE"])
def test_feedback_and_complete_next_request_equal_actual_wp015(tmp_path, case, kind, actual_wp015):
    compile_old, planning_source, selected = actual_wp015
    session, condition, candidate = material(tmp_path, case)
    accepted = session.accept({"kind": kind, "candidate": candidate})
    assert accepted.status == "CONSTRUCTION_VALID", result_public(accepted)
    rich = result_public(accepted)
    old = result_public(PlannerTurnResultV2("CONSTRUCTION_VALID", compiled=compile_old(candidate, session.compile_context)))
    projected = model_feedback(rich, source_constructs=session._feedback_constructs)
    assert projected == old
    assert (rich != old) == (case != "macro")
    job = conditioned_job(session, condition)
    variable = contextvars.ContextVar("actual_wp015_job", default=job)
    namespace = selected(planning_source, {"_objective", "_prompt", "_planner_context"}, {
        "Any": Any, "Mapping": Mapping, "legacy": types.SimpleNamespace(CURRENT_JOB=variable),
        "base": types.SimpleNamespace(canonical_json_bytes=canonical_json_bytes),
        "prompt_for_cell": prompt_for_cell, "PROTOCOL_ID": "WP015_LEAN_R1_V3", "PROTOCOL_EPOCH": PROTOCOL_EPOCH})
    admitted = tuple("macro." + key for key in session.compile_context.admitted_macros)
    contract = contract_for_cell(study_track=job["track"], coverage_regime=job["coverage_regime"],
        language_condition=job["language_condition"], level="C", admitted_action_keys=admitted)
    messages = planning_messages(session=session, condition=condition, previous_result=rich)
    assert messages[0]["content"].encode() == namespace["_prompt"](session.level)
    assert messages[1]["content"].encode() == namespace["_planner_context"](
        observation=session.observation, session=session, contract=contract, last_result=old)


@pytest.mark.parametrize("message", [
    {"kind": "QUERY", "queries": [{"name": "route_summary", "top_k": 1}]},
    {"kind": "DRAFT", "candidate": {"language": "M", "action_key": "macro.unknown"}},
    {"kind": "STOP"},
])
def test_noncompiled_feedback_clears_private_source_map_and_is_unchanged(tmp_path, message):
    session, condition, candidate = material(tmp_path, "g_nested_p")
    session.accept({"kind": "DRAFT", "candidate": candidate})
    assert session._feedback_constructs
    public = result_public(session.accept(message))
    assert session._feedback_constructs == {}
    before = deepcopy(public)
    assert model_feedback(public) == public == before
    assert json.loads(planning_messages(session=session, condition=condition,
        previous_result=public)[1]["content"])["previous_result"] == public


def test_exception_clears_private_source_map(tmp_path):
    session, _, candidate = material(tmp_path, "g_nested_p")
    session.accept({"kind": "COMMIT", "candidate": candidate})
    assert session._feedback_constructs
    with pytest.raises(Exception, match="session_already_terminal"):
        session.accept({"kind": "STOP"})
    assert session._feedback_constructs == {}


def test_no_feedback_copy_has_no_side_effects():
    assert model_feedback(None) is None


def test_legacy_g_statements_remain_ineligible_for_conditioned_session(tmp_path):
    session, condition, candidate = material(tmp_path, "g_statements")
    public = result_public(session.accept({"kind": "DRAFT", "candidate": candidate}))
    assert public["status"] == "CONSTRUCTION_INVALID"
    assert public["feedback"][0]["error_code"] == "LANGUAGE_CONDITION_NOT_MET"
    assert session._feedback_constructs == {} and model_feedback(public) == public
