# Ported from a722b1e; import paths only adapted.
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from optimizer_pipeline import DeterministicAnalyzer, OutputValidator
from experiments.ablation.config import OptimizationConfig
from experiments.ablation.runtime.artifacts import ArtifactStore
from experiments.ablation.runtime.expansion.frontier_runner import FrontierExpansionRunner
from experiments.ablation.runtime.expansion.policies.core import DeterministicExpansionPolicy

from optimizer_pipeline.models import DcpStateNode, ExecutionResult, NodeStatus
from experiments.ablation.runtime.expansion.policies.core import BaseExpansionPolicy, TransitionCandidate, ExpansionDecision


class FakeExecutor:
    async def execute(self, input_dcp, output_dcp, run_dir, flow):
        run_dir.mkdir(parents=True, exist_ok=True)
        output_dcp.parent.mkdir(parents=True, exist_ok=True)
        output_dcp.write_text("fake dcp\n")
        gain = {"auto_pblock": 12.0, "phys_opt": 8.0, "retime": 5.0}.get(flow, 1.0)
        metrics = {
            "flow": flow,
            "clock_period_ns": 2.5,
            "initial_fmax_mhz": 400.0,
            "final_fmax_mhz": 400.0 + gain,
            "initial_wns_ns": -0.5,
            "final_wns_ns": -0.5 + gain / 100.0,
            "initial_tns_ns": -20.0,
            "final_tns_ns": -20.0 + gain / 2.0,
            "total_runtime_seconds": 0.01,
        }
        summary = run_dir / "run_summary.json"
        summary.write_text(json.dumps(metrics))
        return ExecutionResult(
            success=True,
            flow=flow,
            input_dcp=str(input_dcp),
            output_dcp=str(output_dcp),
            run_dir=str(run_dir),
            summary_path=str(summary),
            metrics=metrics,
            exit_code=0,
        )


class TimedExecutor(FakeExecutor):
    def __init__(self, delays, events):
        self.delays = delays
        self.events = events

    async def execute(self, input_dcp, output_dcp, run_dir, flow):
        self.events.append({"event": "start", "flow": flow, "time": time.monotonic()})
        await asyncio.sleep(self.delays.get(flow, 0.01))
        result = await super().execute(input_dcp, output_dcp, run_dir, flow)
        self.events.append({"event": "end", "flow": flow, "time": time.monotonic()})
        return result


class AsyncRefillPolicy(BaseExpansionPolicy):
    def __init__(self):
        self.requests = []

    def propose(self, request):
        self.requests.append(request)
        existing = {tuple(seq) for seq in request.get("existing_sequences") or []}
        frontier = request.get("frontier_node_ids") or []
        target = frontier[0] if frontier else None
        if not target:
            return ExpansionDecision(stop=True, reason="No frontier")
        candidates = []
        if not existing.intersection({("phys_opt",), ("auto_pblock",)}):
            candidates = [
                TransitionCandidate(target_node_id=target, flow="phys_opt", priority=0.9),
                TransitionCandidate(target_node_id=target, flow="auto_pblock", priority=0.8),
            ]
        elif ("retime",) not in existing:
            candidates = [TransitionCandidate(target_node_id=target, flow="retime", priority=0.7)]
        return ExpansionDecision(
            decision_type="expansion_plan",
            target_node_id=target,
            candidates=candidates,
            stop=not bool(candidates),
            reason="async refill test policy",
            model="test-policy",
        )


class SequentialRootPolicy(BaseExpansionPolicy):
    def __init__(self, flows):
        self.flows = flows
        self.requests = []

    def propose(self, request):
        self.requests.append(request)
        existing = {tuple(seq) for seq in request.get("existing_sequences") or []}
        frontier = request.get("frontier_node_ids") or []
        target = frontier[0] if frontier else None
        if not target:
            return ExpansionDecision(stop=True, reason="No frontier", model="test-policy")
        candidates = []
        for flow in self.flows:
            if (flow,) not in existing:
                candidates.append(TransitionCandidate(target_node_id=target, flow=flow, priority=1.0))
                break
        return ExpansionDecision(
            decision_type="expand_node",
            target_node_id=target,
            candidates=candidates,
            stop=not bool(candidates),
            reason="sequential root expansion",
            model="test-policy",
        )


class SlotAwareRootPolicy(BaseExpansionPolicy):
    def __init__(self, flows):
        self.flows = flows

    def propose(self, request):
        existing = {tuple(seq) for seq in request.get("existing_sequences") or []}
        frontier = request.get("frontier_node_ids") or []
        target = frontier[0] if frontier else None
        if not target:
            return ExpansionDecision(stop=True, reason="No frontier", model="test-policy")
        limit = int(request.get("free_slots") or request.get("batch_size") or 1)
        candidates = []
        for flow in self.flows:
            if (flow,) not in existing:
                candidates.append(TransitionCandidate(target_node_id=target, flow=flow, priority=1.0))
            if len(candidates) >= limit:
                break
        return ExpansionDecision(
            decision_type="expansion_plan",
            target_node_id=target,
            candidates=candidates,
            stop=not bool(candidates),
            reason="slot-aware root expansion",
            model="test-policy",
        )


async def fake_analysis_provider(input_dcp, config):
    return {
        "benchmark_name": Path(input_dcp).stem,
        "clock_period_ns": 2.5,
        "initial_fmax_mhz": 400.0,
        "initial_wns_ns": -0.5,
        "initial_tns_ns": -20.0,
        "failing_endpoints": 10,
        "utilization_summary": {"luts": 10, "ffs": 20, "dsps": 0, "brams": 0},
        "flow_scores": [
            {"flow": "auto_pblock", "flow_heuristic_score": 10.0},
            {"flow": "phys_opt", "flow_heuristic_score": 8.0},
            {"flow": "retime", "flow_heuristic_score": 6.0},
        ],
        "selected_top3": ["auto_pblock", "phys_opt", "retime"],
    }


class MockFrontierExpansionTest(unittest.TestCase):
    def test_deterministic_search_writes_research_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dcp = root / "input.dcp"
            input_dcp.write_text("fake\n")
            config = OptimizationConfig(
                policy_type="deterministic",
                max_depth=2,
                max_total_runs=4,
                branching_factor=2,
                beam_width=1,
                allowed_flows=["auto_pblock", "phys_opt", "retime"],
                fallback_flow="phys_opt",
            )
            runner = FrontierExpansionRunner(
                config=config,
                artifact_store=ArtifactStore(root / "run"),
                analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
                executor=FakeExecutor(),
                output_validator=OutputValidator(),
                policy=DeterministicExpansionPolicy(),
            )
            result = asyncio.run(runner.run(input_dcp))
            self.assertTrue(result.success)
            run_dir = Path(result.run_dir)
            self.assertTrue((run_dir / "pipeline_summary.json").is_file())
            self.assertTrue((run_dir / "results_summary.csv").is_file())
            self.assertTrue((run_dir / "best_trajectory.md").is_file())
            self.assertTrue((run_dir / "transition_journal_summary.json").is_file())
            self.assertTrue((run_dir / "policy_candidates_summary.json").is_file())
            summary = json.loads((run_dir / "pipeline_summary.json").read_text())
            self.assertEqual(summary["policy_type"], "deterministic")
            self.assertGreaterEqual(summary["eda_runs"], 1)
            self.assertIn("best_flow_sequence", summary)
            journal_summary = json.loads((run_dir / "transition_journal_summary.json").read_text())
            self.assertEqual(journal_summary["schema_version"], "transition_record.v2.sqlite_export")
            self.assertTrue(journal_summary["derived_export_only"])
            self.assertTrue((run_dir / "run.db").is_file())
            self.assertFalse((run_dir / "transition_journal.jsonl").exists())
            self.assertGreaterEqual(journal_summary["journal"]["materialized_transition_records"], 1)

    def test_async_scheduler_refills_slot_before_slow_task_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dcp = root / "input.dcp"
            input_dcp.write_text("fake\n")
            events = []
            policy = AsyncRefillPolicy()
            config = OptimizationConfig(
                policy_type="analysis_priority",
                scheduler_mode="async",
                max_depth=1,
                max_total_runs=3,
                branching_factor=3,
                batch_size=3,
                max_parallel_jobs=2,
                beam_width=1,
                allowed_flows=["phys_opt", "auto_pblock", "retime"],
                fallback_flow="phys_opt",
            )
            runner = FrontierExpansionRunner(
                config=config,
                artifact_store=ArtifactStore(root / "run"),
                analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
                executor=TimedExecutor({"phys_opt": 0.12, "auto_pblock": 0.01, "retime": 0.01}, events),
                output_validator=OutputValidator(),
                policy=policy,
            )
            result = asyncio.run(runner.run(input_dcp))
            self.assertTrue(result.success)
            times = {(item["event"], item["flow"]): item["time"] for item in events}
            self.assertLess(times[("start", "retime")], times[("end", "phys_opt")])
            self.assertEqual(runner.scheduled_run_count, 3)
            self.assertTrue(any(request.get("active_node_ids") for request in policy.requests[1:]))

    def test_async_scheduler_allows_partial_expansion_with_one_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dcp = root / "input.dcp"
            input_dcp.write_text("fake\n")
            config = OptimizationConfig(
                policy_type="analysis_priority",
                scheduler_mode="async",
                max_depth=1,
                max_total_runs=3,
                branching_factor=3,
                batch_size=3,
                max_parallel_jobs=1,
                beam_width=1,
                allowed_flows=["auto_pblock", "phys_opt", "retime"],
                fallback_flow="phys_opt",
            )
            runner = FrontierExpansionRunner(
                config=config,
                artifact_store=ArtifactStore(root / "run"),
                analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
                executor=FakeExecutor(),
                output_validator=OutputValidator(),
                policy=SequentialRootPolicy(["auto_pblock", "phys_opt", "retime"]),
            )
            result = asyncio.run(runner.run(input_dcp))
            self.assertTrue(result.success)
            root_children = [node for node in runner.nodes.values() if node.parent_id == "node_000_root"]
            self.assertEqual(len(root_children), 3)
            self.assertEqual(runner.run_count, 3)

    def test_async_scheduler_does_not_oversubscribe_run_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dcp = root / "input.dcp"
            input_dcp.write_text("fake\n")
            config = OptimizationConfig(
                policy_type="analysis_priority",
                scheduler_mode="async",
                max_depth=1,
                max_total_runs=3,
                branching_factor=4,
                batch_size=4,
                max_parallel_jobs=4,
                beam_width=1,
                allowed_flows=["auto_pblock", "phys_opt", "retime", "congestion_spread"],
                fallback_flow="phys_opt",
            )
            runner = FrontierExpansionRunner(
                config=config,
                artifact_store=ArtifactStore(root / "run"),
                analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
                executor=FakeExecutor(),
                output_validator=OutputValidator(),
                policy=SlotAwareRootPolicy(["auto_pblock", "phys_opt", "retime", "congestion_spread"]),
            )
            result = asyncio.run(runner.run(input_dcp))
            self.assertTrue(result.success)
            self.assertEqual(runner.scheduled_run_count, 3)
            self.assertEqual(runner.run_count, 3)
            non_root = [node for node in runner.nodes.values() if node.depth > 0]
            self.assertEqual(len(non_root), 3)

    def test_analysis_priority_analyzes_all_visible_frontier_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = OptimizationConfig(
                policy_type="analysis_priority",
                online_deep_analysis=True,
                analysis_level="read_only",
                max_depth=3,
                beam_width=1,
                allowed_flows=["auto_pblock", "phys_opt", "retime"],
                fallback_flow="phys_opt",
            )
            runner = FrontierExpansionRunner(
                config=config,
                artifact_store=ArtifactStore(root / "run"),
                analyzer=DeterministicAnalyzer(analysis_provider=fake_analysis_provider),
                executor=FakeExecutor(),
                output_validator=OutputValidator(),
                policy=DeterministicExpansionPolicy(),
            )
            node_a = DcpStateNode(
                node_id="node_001_auto_pblock",
                parent_id="node_000_root",
                depth=1,
                input_dcp="in_a.dcp",
                flow_sequence=["auto_pblock"],
                status=NodeStatus.COMPLETED,
                score=1.0,
            )
            node_b = DcpStateNode(
                node_id="node_002_phys_opt",
                parent_id="node_000_root",
                depth=1,
                input_dcp="in_b.dcp",
                flow_sequence=["phys_opt"],
                status=NodeStatus.COMPLETED,
                score=100.0,
            )
            runner.nodes = {node_a.node_id: node_a, node_b.node_id: node_b}

            asyncio.run(runner._maybe_run_online_analysis([node_a, node_b], type("State", (), {})()))

            analyzed = {session.node_id for session in runner.analysis_controller.memory.sessions}
            priority_nodes = {priority.node_id for priority in runner.analysis_controller.memory.candidate_priorities}
            self.assertEqual(analyzed, {"node_001_auto_pblock", "node_002_phys_opt"})
            self.assertEqual(priority_nodes, analyzed)


if __name__ == "__main__":
    unittest.main()
