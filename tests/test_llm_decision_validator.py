import unittest

from optimizer_pipeline.capabilities.search.policies.core import TransitionCandidate, ExpansionDecision, validate_expansion_decision
from optimizer_pipeline.models import DcpStateNode


class LLMDecisionValidatorTest(unittest.TestCase):
    def setUp(self):
        self.root = DcpStateNode(node_id="node_000_root", parent_id=None, depth=0, input_dcp="in.dcp")

    def test_accepts_allowlisted_candidate(self):
        decision = ExpansionDecision(
            target_node_id="node_000_root",
            candidates=[TransitionCandidate(flow="auto_pblock", confidence="high", risk="medium")],
        )
        result = validate_expansion_decision(
            decision,
            allowed_flows=["auto_pblock", "phys_opt"],
            existing_node_ids=["node_000_root"],
            branching_factor=2,
            target_node=self.root,
            existing_sequences=[],
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.decision.candidates[0].flow, "auto_pblock")

    def test_rejects_scripts_and_unknown_flows(self):
        decision = ExpansionDecision(
            target_node_id="node_000_root",
            candidates=[TransitionCandidate(flow="auto_pblock", expected_effect="```tcl open_checkpoint foo```")],
        )
        result = validate_expansion_decision(
            decision,
            allowed_flows=["phys_opt"],
            existing_node_ids=["node_000_root"],
            branching_factor=2,
            target_node=self.root,
            existing_sequences=[],
        )
        self.assertFalse(result.valid)
        self.assertTrue(any("forbidden" in err for err in result.errors))
        self.assertTrue(any("unknown flow" in err for err in result.errors))

    def test_rejects_consecutive_repeat(self):
        parent = DcpStateNode(
            node_id="node_001_phys_opt",
            parent_id="node_000_root",
            depth=1,
            input_dcp="in.dcp",
            flow_sequence=["phys_opt"],
        )
        decision = ExpansionDecision(
            target_node_id="node_001_phys_opt",
            candidates=[TransitionCandidate(flow="phys_opt")],
        )
        result = validate_expansion_decision(
            decision,
            allowed_flows=["phys_opt"],
            existing_node_ids=["node_001_phys_opt"],
            branching_factor=1,
            target_node=parent,
            existing_sequences=[["phys_opt"]],
        )
        self.assertFalse(result.valid)
        self.assertTrue(any("consecutive" in err for err in result.errors))

    def test_rejects_target_outside_current_frontier(self):
        decision = ExpansionDecision(
            target_node_id="node_005_auto_pblock",
            candidates=[TransitionCandidate(flow="phys_opt")],
        )
        result = validate_expansion_decision(
            decision,
            allowed_flows=["phys_opt"],
            existing_node_ids=["node_004_congestion_spread"],
            branching_factor=1,
            target_node=None,
            existing_sequences=[],
        )
        self.assertFalse(result.valid)
        self.assertTrue(any("not in current frontier" in err for err in result.errors))
        self.assertTrue(any("node_004_congestion_spread" in err for err in result.errors))

    def test_rejects_pruning_nodes_outside_completed_frontier(self):
        decision = ExpansionDecision(
            target_node_id="node_000_root",
            candidates=[TransitionCandidate(flow="phys_opt")],
            prune_node_ids=["node_002_running"],
        )
        result = validate_expansion_decision(
            decision,
            allowed_flows=["phys_opt"],
            existing_node_ids=["node_000_root"],
            branching_factor=1,
            target_node=self.root,
            existing_sequences=[],
            prunable_node_ids=["node_000_root"],
        )
        self.assertFalse(result.valid)
        self.assertTrue(any("not in current prunable frontier" in err for err in result.errors))


if __name__ == "__main__":
    unittest.main()
