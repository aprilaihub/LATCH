"""Offline historical-source differential checks, never model/tool execution.

Git-backed comparisons require the selected source revisions. A source-only
distribution without those revisions skips them explicitly, not as a pass.
"""
from __future__ import annotations

import ast
import asyncio
import contextvars
import json
import subprocess
import types
from pathlib import Path
from typing import Any, Mapping

import pytest

from experiments.dof.runtime.prepared import make_session, mock_material
from experiments.dof.runtime.planning_request import conditioned_job, planning_messages
from experiments.dof.protocol.contract_v6 import PROTOCOL_EPOCH, prompt_for_cell, contract_for_cell
from experiments.dof.protocol.specs import canonical_json_bytes
from experiments.dof.protocol.planner_v2 import result_public
from optimizer_pipeline.dof.single_submission.planner_v5 import SingleSubmissionSessionV5
from optimizer_pipeline.dof.wp015a_strict_g import strict_family_feedback

ROOT = Path(__file__).resolve().parents[2]


def historical(path, revision="652aea7"):
    result = subprocess.run(["git", "-C", str(ROOT), "show", f"{revision}:{path}"],
                            capture_output=True, text=True)
    if result.returncode:
        pytest.skip(f"Historical source {revision}:{path} unavailable; no fidelity claim")
    return result.stdout


def definitions(source, names, namespace):
    tree = ast.parse(source)
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and node.name in names]
    assert {node.name for node in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "historical-selected-definitions", "exec"), namespace)
    return namespace


def session(tmp_path, condition):
    root = tmp_path / "root.dcp"
    root.write_text("offline differential root")
    context, messages = mock_material(condition)
    context.update(trial_id="differential", handle_secret_hex="01" * 32)
    return make_session(condition=condition, root=root, context=context, mode="mock"), messages


@pytest.mark.parametrize("condition", ["recipe", "direct", "constructed"])
@pytest.mark.parametrize("required_family", [None, "SPATIAL_GENERATED_GEOMETRY"])
@pytest.mark.parametrize("prior_query", [False, True])
def test_wp015_request_matches_original_functions(tmp_path, condition, required_family, prior_query):
    current, _ = session(tmp_path, condition)
    current.required_family = required_family
    job = conditioned_job(current, condition)
    variable = contextvars.ContextVar("historical_job", default=job)
    namespace = definitions(historical("scripts/run_wp015_cells.py"),
        ["_objective", "_prompt", "_planner_context"], {
            "Any": Any, "Mapping": Mapping, "legacy": types.SimpleNamespace(CURRENT_JOB=variable),
            "base": types.SimpleNamespace(canonical_json_bytes=canonical_json_bytes),
            "prompt_for_cell": prompt_for_cell, "PROTOCOL_ID": "WP015_LEAN_R1_V3",
            "PROTOCOL_EPOCH": PROTOCOL_EPOCH,
        })
    previous = None
    if prior_query:
        previous = result_public(current.accept({"kind": "QUERY", "queries": [{"name": "route_summary", "top_k": 1}]}))
    messages = planning_messages(session=current, condition=condition, previous_result=previous)
    admitted = tuple("macro." + key for key in current.compile_context.admitted_macros)
    contract = contract_for_cell(study_track=job["track"], coverage_regime=job["coverage_regime"],
        language_condition=job["language_condition"], level="C", admitted_action_keys=admitted)
    assert messages[0]["content"].encode() == namespace["_prompt"](current.level)
    assert messages[1]["content"].encode() == namespace["_planner_context"](
        observation=current.observation, session=current, contract=contract, last_result=previous)
    assert "01" * 32 not in json.dumps(messages)


@pytest.mark.parametrize("condition", ["recipe", "direct", "constructed"])
@pytest.mark.parametrize("scenario", ["commit", "query_commit", "invalid_budget", "stop", "wrong_language", "draft_commit"])
def test_wp015_session_trace_matches_original_firewall(tmp_path, condition, scenario):
    current, legal = session(tmp_path, condition)
    job = conditioned_job(current, condition)
    variable = contextvars.ContextVar("historical_job", default=job)
    namespace = {"Any": Any, "Mapping": Mapping, "SingleSubmissionSessionV5": SingleSubmissionSessionV5,
                 "legacy": types.SimpleNamespace(CURRENT_JOB=variable), "strict_family_feedback": strict_family_feedback}
    definitions(historical("scripts/run_wp014c_scientific.py"), ["ContextSessionV3"], namespace)
    namespace["wp014c"] = types.SimpleNamespace(ContextSessionV3=namespace["ContextSessionV3"])
    definitions(historical("scripts/run_wp015_cells.py"), ["WP015Session"], namespace)
    old = namespace["WP015Session"](current.level, current.observation, current.queries,
        current.compile_context, current.binding, current.query_provider)
    wrong = {"language": "M", "action_key": "macro.phys_opt"} if condition != "recipe" else {
        "language": "P", "steps": [{"step_id": "s", "op": "UNROUTE", "args": {}}]}
    cases = {
        "commit": legal + legal,
        "query_commit": [{"kind": "QUERY", "queries": [{"name": "route_summary", "top_k": 1}]}] + legal,
        "invalid_budget": [{"kind": "COMMIT", "candidate": {"language": "M", "action_key": "macro.unknown"}}] * 4,
        "stop": [{"kind": "STOP"}] + legal,
        "wrong_language": [{"kind": "COMMIT", "candidate": wrong}] * 4,
        "draft_commit": [dict(legal[0], kind="DRAFT")] + legal,
    }
    traces = []
    for implementation in (old, current):
        trace = []
        for message in cases[scenario]:
            if implementation.terminal is not None:
                break
            trace.append(result_public(implementation.accept(message)))
        traces.append({"feedback": trace, "status": implementation.public_status(),
            "terminal": implementation.terminal, "commits": implementation.valid_commits,
            "compiled": implementation.committed.public_payload() if implementation.committed else None})
    assert traces[0] == traces[1]


def test_protocol_definitions_are_from_selected_git_revision():
    for path in (ROOT / "experiments/dof/protocol").glob("*.py"):
        if path.name == "__init__.py":
            continue
        old = historical("optimizer_pipeline/dof/single_submission/" + path.name)
        def bodies(text):
            return {node.name: ast.dump(node, include_attributes=False) for node in ast.parse(text).body
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
        assert bodies(old) == bodies(path.read_text()), path.name


def test_strict_g_definitions_are_from_selected_git_revision():
    old = historical("optimizer_pipeline/dof/wp015a_strict_g.py")
    current = ROOT / "experiments/dof/runtime/wp015a_strict_g.py"
    def bodies(text):
        return {node.name: ast.dump(node, include_attributes=False) for node in ast.parse(text).body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    assert bodies(old) == bodies(current.read_text())


@pytest.mark.parametrize("condition", ["recipe", "direct", "constructed"])
def test_capture_observation_and_query_match_original_projection(tmp_path, condition):
    # Synthetic material only. No historical private capture or tool is read.
    from tests.latch.test_dof_capture_and_failures import captured
    from experiments.dof.runtime.captured_context import context_from_capture
    from experiments.dof.protocol.observation import build_common_observation
    root, capture, _ = captured(tmp_path)
    context = context_from_capture(root=root, capture=capture, target_clock="clk")
    current = make_session(condition=condition, root=root, context=context, mode="real")
    measurements = json.loads((capture / "measurement.json").read_text())
    namespace = definitions(historical("scripts/run_wp013a_e0.py"),
        ["_common_observation", "_query_provider"], {
            "Any": Any, "Mapping": Mapping, "Path": Path,
            "read_object": lambda _: {"source": {"state": {"measurements": measurements}}, "observation_sha256": "fixture"},
            "_registry_rows": lambda *args: (None, context["queries"], context["query_missing_reasons"],
                context["predefined_regions"], context["geometry_bounds"], context["hardblock_compatibility_relation"]),
            "build_common_observation": build_common_observation,
            "audit_query_semantics": lambda *args, **kwargs: {},
            "EXPECTED_OPENROUTER_COST_CAP_USD": 12.0,  # WP014C.configure applied by WP015.configure.
            "QUERY_EVIDENCE_TAG": "wp014c-frozen-read-only-capture",
        })
    old, private = namespace["_common_observation"](dict(sha256=context["root_sha256"],
        target_period_ns=10, target_period_evidence="read-only same-clock scorer"), capture)
    assert dict(old.payload) == dict(current.observation.payload)
    assert current.observation.payload["timing"]["fmax_mhz"] == 1000 / 11
    # The supplied rows/missing state is identical; evidence tags deliberately
    # identify new capture vs historical freeze, not falsely claim old identity.
    old_provider = namespace["_query_provider"](private)
    for name in context["queries"]:
        before = old_provider(name, 1)
        after = current.query_provider(name, 1)
        assert before[0] == after[0] and before[2:] == after[2:]


@pytest.mark.parametrize("path", ["scripts/run_wp013a_e0.py", "scripts/run_wp013_admission.py",
    "optimizer_pipeline/dof/real_mutation.py"])
def test_retained_dispatch_sources_match_original_executable_definitions(path):
    # Existing compiled-operation tests compare extracted dispatch/worker
    # bodies against these retained sources. Anchor that evidence in Git, not
    # two potentially co-edited copies. Bootstrap paths/comments are not code.
    def bodies(source):
        return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(source).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    assert bodies(historical(path)) == bodies((ROOT / path).read_text())
