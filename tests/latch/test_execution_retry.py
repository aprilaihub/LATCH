# Ported from a722b1e; import paths only adapted.
"""Retry-on-hard-execution-failure.

Every test drives a real FrontierExpansionRunner with an executor rigged to fail
in a specific way, and asserts on what the run actually did.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from optimizer_pipeline import DeterministicAnalyzer, OutputValidator
from experiments.ablation.config import OptimizationConfig
from experiments.ablation.runtime.artifacts import ArtifactStore
from experiments.ablation.runtime.expansion.frontier_runner import FrontierExpansionRunner
from experiments.ablation.runtime.expansion.policies.core import DeterministicExpansionPolicy

from optimizer_pipeline.models import ExecutionResult

ALLOWED = ["auto_pblock", "phys_opt", "retime", "lut_cone"]
# ranking the analysis provider hands the deterministic policy, best first
RANKED = ["auto_pblock", "phys_opt", "retime", "lut_cone"]


class RiggedExecutor:
    """Executor that fails chosen flows in a chosen way, and records every call."""

    def __init__(self, fail_flows, mode="missing_dcp"):
        self.fail_flows = set(fail_flows)
        self.mode = mode
        self.calls = []

    async def execute(self, input_dcp, output_dcp, run_dir, flow):
        self.calls.append(flow)
        run_dir.mkdir(parents=True, exist_ok=True)
        output_dcp.parent.mkdir(parents=True, exist_ok=True)
        summary = run_dir / "run_summary.json"
        metrics = {
            "flow": flow, "clock_period_ns": 2.5,
            "initial_fmax_mhz": 400.0, "final_fmax_mhz": 400.0 + {"phys_opt": 8.0}.get(flow, 2.0),
            "initial_wns_ns": -0.5, "final_wns_ns": -0.4,
            "initial_tns_ns": -20.0, "final_tns_ns": -18.0,
            "total_runtime_seconds": 0.01,
        }
        summary.write_text(json.dumps(metrics))

        if flow in self.fail_flows:
            if self.mode == "missing_dcp":
                # The observed real failure: executor claims success, exit 0,
                # but no checkpoint is ever written.
                return ExecutionResult(True, flow, str(input_dcp), str(output_dcp),
                                       str(run_dir), str(summary), metrics, 0)
            if self.mode == "nonzero_exit":
                output_dcp.write_text("partial\n")
                return ExecutionResult(False, flow, str(input_dcp), str(output_dcp),
                                       str(run_dir), str(summary), metrics, 2, "tool exploded")

        output_dcp.write_text("fake dcp\n")
        return ExecutionResult(True, flow, str(input_dcp), str(output_dcp),
                               str(run_dir), str(summary), metrics, 0)


async def analysis_provider(input_dcp, config):
    return {
        "benchmark_name": Path(input_dcp).stem,
        "clock_period_ns": 2.5, "initial_fmax_mhz": 400.0,
        "initial_wns_ns": -0.5, "initial_tns_ns": -20.0, "failing_endpoints": 10,
        "utilization_summary": {"luts": 10, "ffs": 20},
        "critical_path_features": {"route_delay_ratio": 0.72},
        "flow_scores": [
            {"flow": "auto_pblock", "flow_heuristic_score": 10.0},
            {"flow": "phys_opt", "flow_heuristic_score": 8.0},
            {"flow": "retime", "flow_heuristic_score": 6.0},
            {"flow": "lut_cone", "flow_heuristic_score": 4.0},
        ],
        "selected_top3": ["auto_pblock", "phys_opt", "retime"],
    }


def build(tmp_path, executor, *, max_depth=3, max_runs=6):
    config = OptimizationConfig(
        policy_type="deterministic",
        max_depth=max_depth, max_total_runs=max_runs,
        branching_factor=1, beam_width=1, batch_size=1, max_parallel_jobs=1,
        scheduler_mode="batch",
        allowed_flows=list(ALLOWED), fallback_flow="phys_opt",
        disallow_consecutive_repeat=False,
    )
    return FrontierExpansionRunner(
        config=config,
        artifact_store=ArtifactStore(tmp_path / "tree"),
        analyzer=DeterministicAnalyzer(analysis_provider=analysis_provider),
        executor=executor,
        output_validator=OutputValidator(),
        policy=DeterministicExpansionPolicy(policy_name="deterministic"),
    )


def run(tmp_path, executor, **kw):
    dcp = tmp_path / "input.dcp"
    dcp.write_text("fake\n")
    runner = build(tmp_path, executor, **kw)
    result = asyncio.run(runner.run(dcp))
    summary = json.loads(Path(result.summary_path).read_text())
    return runner, result, summary


def test_retry_fires_on_missing_output_dcp_and_uses_next_ranked_flow(tmp_path):
    """The observed real failure mode: success=True, exit 0, no checkpoint."""
    ex = RiggedExecutor(fail_flows={"auto_pblock"}, mode="missing_dcp")
    runner, _result, summary = run(tmp_path, ex, max_depth=1, max_runs=2)

    assert runner.retry_count == 1
    fired = [e for e in runner.retry_events if not e.get("skipped")]
    assert len(fired) == 1
    ev = fired[0]
    assert ev["failed_flow"] == "auto_pblock"
    # next-best in the ranking after excluding the failed flow
    assert ev["substituted_flow"] == "phys_opt"
    assert ev["rank_index"] == 0
    assert "output DCP missing" in ev["reason"]
    assert ev["ranking_source"] in {"deterministic_flow_ranking", "analysis_candidate_priorities"}
    # the substitute really executed
    assert "phys_opt" in ex.calls
    assert summary["execution_retries"]["count"] == 1


def test_retry_fires_on_nonzero_exit(tmp_path):
    ex = RiggedExecutor(fail_flows={"auto_pblock"}, mode="nonzero_exit")
    runner, _result, _summary = run(tmp_path, ex, max_depth=1, max_runs=2)
    fired = [e for e in runner.retry_events if not e.get("skipped")]
    assert len(fired) == 1
    assert "exit_code" in fired[0]["reason"] or "non-zero" in fired[0]["reason"]
    assert fired[0]["substituted_flow"] == "phys_opt"


def test_retry_adds_no_llm_call_and_no_extra_policy_decision(tmp_path):
    """Guardrail 5: the retry must not buy extra reasoning budget."""
    ex = RiggedExecutor(fail_flows={"auto_pblock"}, mode="missing_dcp")
    runner, _result, summary = run(tmp_path, ex, max_depth=1, max_runs=2)

    assert runner.retry_count == 1
    assert summary.get("total_llm_calls", 0) == 0
    assert summary.get("analysis_llm_calls", 0) == 0
    # one policy decision per scheduling round; the retry reuses that decision
    # rather than provoking another one
    executed = [n for n in runner.nodes.values() if n.depth > 0]
    assert len(runner.policy_events) < len(executed)


def test_second_failure_at_same_node_terminates_exactly_as_before(tmp_path):
    """Guardrail 2: one retry per decision point, then dead end."""
    ex = RiggedExecutor(fail_flows={"auto_pblock", "phys_opt"}, mode="missing_dcp")
    runner, _result, _summary = run(tmp_path, ex, max_depth=1, max_runs=4)

    fired = [e for e in runner.retry_events if not e.get("skipped")]
    assert len(fired) == 1, "the retry must not itself be retried"
    assert fired[0]["failed_flow"] == "auto_pblock"
    assert fired[0]["substituted_flow"] == "phys_opt"
    # both attempts failed, so the chain is a dead end and the run stops
    assert all(n.status.value == "FAILED" for n in runner.nodes.values() if n.depth == 1)
    assert ex.calls.count("auto_pblock") == 1
    assert ex.calls.count("phys_opt") == 1


def test_global_retry_cap_of_three(tmp_path):
    """Guardrail 3: a design where everything fails cannot spend the budget retrying."""
    ex = RiggedExecutor(fail_flows=set(ALLOWED), mode="missing_dcp")
    runner, _result, summary = run(tmp_path, ex, max_depth=6, max_runs=20)

    fired = [e for e in runner.retry_events if not e.get("skipped")]
    assert runner.retry_count <= FrontierExpansionRunner.MAX_EXECUTION_RETRIES_PER_RUN
    assert len(fired) == runner.retry_count
    assert summary["execution_retries"]["cap_per_run"] == 3


def test_valid_but_poor_result_never_retries(tmp_path):
    """Guardrail 1: a flow that ran and scored badly is evidence, not a retry trigger."""
    ex = RiggedExecutor(fail_flows=set(), mode="missing_dcp")  # nothing fails
    runner, _result, _summary = run(tmp_path, ex, max_depth=1, max_runs=2)
    assert runner.retry_count == 0
    assert runner.retry_events == []


def test_both_attempts_count_against_the_run_budget(tmp_path):
    """Guardrail 7: failed work consumed real compute and must be accounted."""
    ex = RiggedExecutor(fail_flows={"auto_pblock"}, mode="missing_dcp")
    runner, _result, _summary = run(tmp_path, ex, max_depth=1, max_runs=2)
    # one failed attempt + one retry = the whole 2-run budget
    assert runner.run_count == 2
    assert runner.scheduled_run_count == 2
    assert len(ex.calls) == 2


def test_phase_runtime_breakdown_separates_failed_execution(tmp_path):
    ex = RiggedExecutor(fail_flows={"auto_pblock"}, mode="missing_dcp")
    _runner, _result, summary = run(tmp_path, ex, max_depth=1, max_runs=2)
    breakdown = summary["phase_runtime_breakdown"]
    assert set(breakdown) >= {
        "analysis_seconds", "execution_productive_seconds",
        "execution_failed_seconds", "total_wall_seconds",
    }
    assert breakdown["execution_failed_seconds"] > 0.0
    assert breakdown["execution_productive_seconds"] > 0.0


def test_hard_failure_classifier_ignores_log_scan_invalidity(tmp_path):
    """A checkpoint that exists but fails log-scan validation is NOT a hard failure."""
    from optimizer_pipeline.execution.contracts import DcpTransitionResult

    dcp = tmp_path / "written.dcp"
    dcp.write_text("real\n")
    ok = ExecutionResult(True, "phys_opt", "in.dcp", str(dcp), str(tmp_path), None, {}, 0)
    step = DcpTransitionResult(
        success=True, accepted=False, input_dcp="in.dcp", output_dcp=str(dcp),
        optimizer_name="phys_opt", optimizer_params={}, validation_status="INVALID",
        rejection_reason="validation_failed", execution=ok, output_validation=None,
        analysis_context={}, metrics={}, qor_score=None, runtime_seconds=1.0,
    )
    assert FrontierExpansionRunner._hard_execution_failure_reason(step) is None


def test_each_decision_point_gets_its_own_retry_until_the_cap(tmp_path):
    """Guardrail 2 is per decision point, not per run -- which is why 3 exists.

    auto_pblock is top-ranked at every node and always fails, so every depth is
    a fresh decision point that earns one retry, until the global cap stops it.
    """
    ex = RiggedExecutor(fail_flows={"auto_pblock"}, mode="missing_dcp")
    runner, _result, _summary = run(tmp_path, ex, max_depth=6, max_runs=20)

    fired = [e for e in runner.retry_events if not e.get("skipped")]
    assert len(fired) == 3 == runner.retry_count
    # distinct decision points, never the same one twice
    parents = [e["parent_node_id"] for e in fired]
    assert len(set(parents)) == len(parents)
    # and once capped, later failures are recorded as skipped with the reason
    skipped = [e for e in runner.retry_events if e.get("skipped")]
    assert any(e["skipped_because"] == "global retry cap reached" for e in skipped)
