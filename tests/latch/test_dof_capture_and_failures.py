import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from experiments.dof.runtime.captured_context import context_from_capture
from experiments.dof.runtime.post_commit import PostCommit
from experiments.dof.runtime.prepared import make_session, mock_material, run_prepared
from optimizer_pipeline.backends.xilinx.owned_process import run_owned
from optimizer_pipeline.scoring import file_sha256
from tests.latch.test_dof_post_commit import config, callbacks, ample_memory
from tests.latch.test_dof_live_adapters import cost
from experiments.dof.runtime.model_planning import ModelPlanner
from integrations.llm.runtime.types import LLMResponse


def captured(tmp_path):
    root = tmp_path / "root.dcp"
    root.write_text("synthetic root, no tools")
    capture = tmp_path / "capture"
    def write(name, value):
        path = capture / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    sha = file_sha256(root)
    write("measurement.json", dict(root_sha256=sha, timing=dict(wns_ns=-1, tns_ns=-2),
        route=dict(status="FULLY_ROUTED"), utilization={}))
    write("root-score/score.json", dict(artifact=dict(sha256=sha),
        score=dict(target_clock="clk", target_period_ns=10, fmax_mhz=100)))
    write("rapidwright-net-census.json", dict(root_sha256=sha,
        nets=[dict(name="net1", fanout=8)]))
    write("contract-probe/private/resolved-objects.json", dict(
        device_regions=[dict(identity="X1Y2")],
        timing_paths=[dict(identity="path1", index=0, attribute="-1\x1f11\x1fa/Q\x1fb/D")],
        critical_cell_locations=[dict(identity="a", index=0, attribute="SLICE\x1fSLICE_X1Y1\x1fX1Y2")]))
    write("hardblock-scan/aggregate.json", dict(rows=[dict(root_id="root", root_sha256=sha)]))
    write("hardblock-scan/probes/root/private/resolved-objects.json", dict(critical_hardblocks=[]))
    write("hardblock-scan/probes/root/private/hardblock-compatibility-queries.json", dict(targets=[]))
    return root, capture, write


def test_capture_registers_objects_and_real_query_surface(tmp_path):
    root, capture, _ = captured(tmp_path)
    context = context_from_capture(root=root, capture=capture, target_clock="clk")
    session = make_session(condition="direct", root=root, context=context, mode="real")
    result = session.accept(dict(kind="QUERY", queries=[dict(name="high_fanout_net_details", top_k=1)]))
    assert result.status == "QUERY_RESULT"
    assert context["queries"]["high_fanout_net_details"][0]["fanout"] == 8
    assert context["query_missing_reasons"]["critical_hardblock_candidates"] == "no_critical_hardblocks_returned"
    assert "net1" not in json.dumps(result.query_batch)


@pytest.mark.parametrize("condition", ["recipe", "direct", "constructed"])
def test_captured_observation_fixed_model_full_chain(tmp_path, condition):
    root, capture, _ = captured(tmp_path)
    context = context_from_capture(root=root, capture=capture, target_clock="clk")
    _, commits = mock_material(condition)
    requests = []
    class FixedClient:
        def complete_json(self, request):
            requests.append(request)
            message = dict(kind="QUERY", queries=[dict(name="high_fanout_net_details", top_k=1)]) if len(requests) == 1 else commits[0]
            return LLMResponse(content=json.dumps(message), parsed_json=message,
                provider="openai", model="fixture-model", prompt_tokens=100, completion_tokens=50)
    planner = ModelPlanner(condition=condition, provider="openai", model="fixture-model",
        cost_config=cost(), client=FixedClient())
    score, prepare, seed = callbacks(tmp_path)
    post = PostCommit(config=config(), golden=root, python=Path("python"), vivado=Path("vivado"),
        scorer_script=Path("scorer"), scorer=score, prepare=prepare, seed_runner=seed, memory_probe=ample_memory)
    executions = []
    async def execute(**kwargs):
        executions.append(kwargs)
        child = tmp_path / "child.dcp"; child.write_text("synthetic child")
        return dict(canonical_endpoint="VALID_COMPLETION", child_path=str(child))
    result = asyncio.run(run_prepared(condition=condition, mode="real", root=root,
        output=tmp_path / "chain", context=context, messages=[], planner=planner, execute=execute, post_commit=post))
    assert len(requests) == 2 and len(executions) == 1
    assert result["planning_terminal"] == "COMMITTED"
    assert result["post_commit_scoring"]["status"] == "MEASURED"
    assert result["functional_validation"]["passed"] is True
    assert len(result["functional_validation"]["seed_reports"]) == 3


@pytest.mark.parametrize("case", ["clock", "root", "fanout", "duplicate_root"])
def test_capture_missing_or_inconsistent_material_stops(tmp_path, case):
    root, capture, write = captured(tmp_path)
    clock = "other" if case == "clock" else "clk"
    if case == "root":
        root.write_text("different root")
    if case == "fanout":
        write("rapidwright-net-census.json", dict(root_sha256=file_sha256(root), nets=[]))
    if case == "duplicate_root":
        write("hardblock-scan/aggregate.json", dict(rows=[dict(root_id="root", root_sha256=file_sha256(root))]*2))
    with pytest.raises((ValueError, RuntimeError)):
        context_from_capture(root=root, capture=capture, target_clock=clock)


@pytest.mark.skipif(sys.platform != "linux", reason="physical process ownership is Linux-only")
def test_owned_subprocess_success_and_timeout_leave_unrelated_process_alive():
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        done = run_owned([sys.executable, "-c", "print('fixture')"], timeout=3, text=True)
        assert done.returncode == 0 and done.stdout.strip() == "fixture"
        with pytest.raises(subprocess.TimeoutExpired):
            run_owned([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


def test_budget_exhaustion_never_dispatches(tmp_path):
    root = tmp_path / "root.dcp"; root.write_text("fixture")
    context, _ = mock_material("direct")
    result = asyncio.run(run_prepared(condition="direct", mode="mock", root=root,
        output=tmp_path / "run", context=context, messages=[dict(kind="UNKNOWN")]*8))
    assert result["turns_used"] == 4
    assert result["execution_started"] is False
    assert result["planning_terminal"] == "NO_VALID_SUBMISSION"


@pytest.mark.parametrize("case, status", [("mismatch", "FUNCTIONAL_MISMATCH"), ("input", "INPUT_CHANGED")])
def test_mismatch_and_changed_input_are_not_passes(tmp_path, case, status):
    root, child = tmp_path / "root.dcp", tmp_path / "child.dcp"
    root.write_text("root"); child.write_text("child")
    score, prepare, seed = callbacks(tmp_path)
    def runner(**kwargs):
        report = seed(**kwargs)
        if case == "mismatch":
            report.update(passed=False, mismatch_count=1, result_fail_seen=True)
        else:
            root.write_text("unexpected mutation")
        return report
    result = PostCommit(config=config(), golden=root, python=Path("python"), vivado=Path("vivado"),
        scorer_script=Path("scorer"), scorer=score, prepare=prepare, seed_runner=runner, memory_probe=ample_memory)(
            root=root, child=child, output=tmp_path / "post")
    assert result["functional_validation"]["status"] == status
    assert result["functional_validation"]["passed"] is not True
    assert result["research_judgement"]["functional_scientific_delta_mhz"] is None


def test_memory_refusal_records_unexecuted_infrastructure_result(tmp_path):
    root, child = tmp_path / "root.dcp", tmp_path / "child.dcp"
    root.write_text("root"); child.write_text("child")
    score, prepare, _ = callbacks(tmp_path)
    def forbidden(**kwargs):
        pytest.fail("No seed may execute without memory admission")
    result = PostCommit(config=config(), golden=root, python=Path("python"), vivado=Path("vivado"),
        scorer_script=Path("scorer"), scorer=score, prepare=prepare, seed_runner=forbidden,
        memory_probe=lambda: (1024**4, 0))(root=root, child=child, output=tmp_path / "post")
    assert result["functional_validation"]["passed"] is None
    assert len(result["functional_validation"]["seed_reports"]) == 3
    assert not any(row["admitted"] for row in result["functional_validation"]["scheduler_events"])
