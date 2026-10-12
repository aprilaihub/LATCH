# Ported from a722b1e; import paths only adapted.
import unittest

from experiments.ablation.runtime.expansion.policies.core import AnalysisPriorityExpansionPolicy, BaseExpansionPolicy, DeterministicExpansionPolicy, TransitionCandidate, ExpansionDecision
from optimizer_pipeline.models import NodeStatus, DcpStateNode
from experiments.ablation.runtime.expansion.frontier_runner import OptimizationConfig, FrontierExpansionRunner


class BadTargetPolicy(BaseExpansionPolicy):
    def propose(self, request):
        payload = {
            "decision_type": "expand_node",
            "target_node_id": "node_999_not_frontier",
            "candidates": [{"flow": "phys_opt", "risk": "medium", "confidence": "medium"}],
            "prune_node_ids": [],
            "stop": False,
            "reason": "bad target for test",
        }
        return ExpansionDecision(
            target_node_id=payload["target_node_id"],
            candidates=[TransitionCandidate(flow="phys_opt")],
            reason=payload["reason"],
            raw_payload=payload,
            model="bad-policy",
        )


class RetryThenValidPolicy(BaseExpansionPolicy):
    def __init__(self):
        self.requests = []

    def propose(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            payload = {
                "decision_type": "expand_node",
                "target_node_id": "node_999_not_frontier",
                "candidates": [{"flow": "phys_opt", "risk": "medium", "confidence": "medium"}],
                "prune_node_ids": [],
                "stop": False,
                "reason": "bad target for first attempt",
            }
            return ExpansionDecision(
                target_node_id=payload["target_node_id"],
                candidates=[TransitionCandidate(flow="phys_opt")],
                reason=payload["reason"],
                raw_payload=payload,
                model="retry-policy",
            )
        payload = {
            "decision_type": "expand_node",
            "target_node_id": "node_001_phys_opt",
            "candidates": [{"flow": "auto_pblock", "risk": "medium", "confidence": "medium"}],
            "prune_node_ids": [],
            "stop": False,
            "reason": "fixed target after validation feedback",
        }
        return ExpansionDecision(
            target_node_id=payload["target_node_id"],
            candidates=[TransitionCandidate(flow="auto_pblock")],
            reason=payload["reason"],
            raw_payload=payload,
            model="retry-policy",
        )


class DeterministicExpansionPolicyTest(unittest.TestCase):
    def test_uses_deterministic_flow_scores(self):
        policy = DeterministicExpansionPolicy()
        decision = policy.propose(
            {
                "allowed_flows": ["phys_opt", "auto_pblock", "retime"],
                "branching_factor": 2,
                "frontier": [{"node_id": "node_000_root", "pure_qor_score": 0.0, "flow_sequence": []}],
                "analysis_context_by_node": {
                    "node_000_root": {
                        "candidate_flows": {
                            "deterministic_scores": [
                                {"flow": "auto_pblock", "flow_heuristic_score": 10.0},
                                {"flow": "phys_opt", "flow_heuristic_score": 9.0},
                                {"flow": "retime", "flow_heuristic_score": 1.0},
                            ]
                        }
                    }
                },
            }
        )
        self.assertEqual(decision.target_node_id, "node_000_root")
        self.assertEqual([c.flow for c in decision.candidates], ["auto_pblock", "phys_opt"])

    def _runner_and_frontier(self, policy):
        config = OptimizationConfig(
            allowed_flows=["phys_opt", "auto_pblock"],
            branching_factor=1,
            beam_width=1,
            max_llm_decision_attempts=1,
        )
        runner = FrontierExpansionRunner(
            config=config,
            artifact_store=None,
            analyzer=None,
            executor=None,
            output_validator=None,
            policy=policy,
            fallback_policy=DeterministicExpansionPolicy(),
        )
        frontier_node = DcpStateNode(
            node_id="node_001_phys_opt",
            parent_id="node_000_root",
            depth=1,
            input_dcp="in.dcp",
            flow_sequence=["phys_opt"],
            status=NodeStatus.COMPLETED,
            score=1.0,
        )
        runner.nodes = {frontier_node.node_id: frontier_node}
        request = {
            "allowed_flows": ["phys_opt", "auto_pblock"],
            "branching_factor": 1,
            "frontier_node_ids": ["node_001_phys_opt"],
            "frontier": [{"node_id": "node_001_phys_opt", "pure_qor_score": 1.0, "flow_sequence": ["phys_opt"]}],
            "existing_sequences": [["phys_opt"]],
            "analysis_context_by_node": {},
        }
        return runner, frontier_node, request

    def test_invalid_policy_decision_falls_back_without_llm_retry(self):
        runner, frontier_node, request = self._runner_and_frontier(BadTargetPolicy())
        decision, validation = runner._get_validated_decision(request, [frontier_node])
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.target_node_id, "node_001_phys_opt")
        self.assertIsNotNone(validation.rejected_payload)
        attempts = validation.rejected_payload["llm_attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(
            attempts[0]["validation"]["rejected_payload"]["target_node_id"],
            "node_999_not_frontier",
        )
        self.assertTrue(
            any("not in current frontier" in err for err in attempts[0]["validation"]["errors"])
        )

    def test_analysis_priority_policy_uses_candidate_priorities(self):
        policy = AnalysisPriorityExpansionPolicy()
        decision = policy.propose({
            "frontier": [{"node_id": "node_000_root", "pure_qor_score": 0.0, "flow_sequence": []}],
            "allowed_flows": ["phys_opt", "auto_pblock"],
            "branching_factor": 2,
            "candidate_priorities": [
                {
                    "node_id": "node_000_root",
                    "flow": "auto_pblock",
                    "priority_score": 0.9,
                    "risk_score": 0.1,
                    "confidence": 0.8,
                    "reason": "analysis priority",
                },
                {
                    "node_id": "node_000_root",
                    "flow": "phys_opt",
                    "priority_score": 0.4,
                    "risk_score": 0.7,
                    "confidence": 0.3,
                },
            ],
            "existing_sequences": [],
        })
        self.assertEqual([candidate.flow for candidate in decision.candidates], ["auto_pblock", "phys_opt"])
        self.assertEqual(decision.candidates[0].confidence, "high")
        self.assertEqual(decision.candidates[1].risk, "high")

    def test_analysis_priority_policy_globally_ranks_node_flow_transitions(self):
        policy = AnalysisPriorityExpansionPolicy()
        decision = policy.propose({
            "frontier": [
                {"node_id": "node_001_high_score", "pure_qor_score": 100.0, "flow_sequence": ["phys_opt"]},
                {"node_id": "node_002_low_score", "pure_qor_score": 1.0, "flow_sequence": ["auto_pblock"]},
            ],
            "allowed_flows": ["retime", "congestion_spread"],
            "branching_factor": 1,
            "candidate_priorities": [
                {"node_id": "node_001_high_score", "flow": "retime", "priority_score": 0.4, "confidence": 0.9, "risk_score": 0.0},
                {"node_id": "node_002_low_score", "flow": "congestion_spread", "priority_score": 0.9, "confidence": 0.5, "risk_score": 0.0},
            ],
            "existing_sequences": [["phys_opt"], ["auto_pblock"]],
        })
        self.assertEqual(decision.target_node_id, "node_002_low_score")
        self.assertEqual([(candidate.target_node_id, candidate.flow) for candidate in decision.candidates], [("node_002_low_score", "congestion_spread")])

    def test_analysis_priority_policy_preserves_missing_scores_with_low_neutral_priority(self):
        policy = AnalysisPriorityExpansionPolicy(neutral_missing_priority=0.2)
        decision = policy.propose({
            "frontier": [{"node_id": "node_000_root", "pure_qor_score": 0.0, "flow_sequence": []}],
            "allowed_flows": ["phys_opt", "auto_pblock"],
            "branching_factor": 2,
            "candidate_priorities": [
                {"node_id": "node_000_root", "flow": "auto_pblock", "priority_score": 0.9}
            ],
            "existing_sequences": [],
        })
        self.assertEqual([candidate.flow for candidate in decision.candidates], ["auto_pblock", "phys_opt"])
        self.assertEqual(decision.candidates[1].priority, 0.2)
        self.assertEqual(decision.candidates[1].confidence, "low")

    def test_analysis_priority_frontier_nodes_are_not_beam_pruned(self):
        config = OptimizationConfig(
            policy_type="analysis_priority",
            allowed_flows=["phys_opt", "auto_pblock"],
            max_depth=3,
            beam_width=1,
        )
        runner = FrontierExpansionRunner(
            config=config,
            artifact_store=None,
            analyzer=None,
            executor=None,
            output_validator=None,
            policy=AnalysisPriorityExpansionPolicy(),
        )
        high = DcpStateNode(
            node_id="node_002_high_score",
            parent_id="node_000_root",
            depth=1,
            input_dcp="in.dcp",
            flow_sequence=["phys_opt"],
            status=NodeStatus.COMPLETED,
            score=100.0,
        )
        low = DcpStateNode(
            node_id="node_001_low_score",
            parent_id="node_000_root",
            depth=1,
            input_dcp="in.dcp",
            flow_sequence=["auto_pblock"],
            status=NodeStatus.COMPLETED,
            score=1.0,
        )
        runner.nodes = {high.node_id: high, low.node_id: low}
        self.assertEqual([node.node_id for node in runner._frontier_nodes()], ["node_001_low_score", "node_002_high_score"])


if __name__ == "__main__":
    unittest.main()
