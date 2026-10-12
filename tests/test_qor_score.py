import unittest

from optimizer_pipeline.qor import compute_qor_score, fill_missing_qor_baseline, has_qor_baseline
from optimizer_pipeline.scoring.official_contest import compute_fpl26_official_contest_score


class QoRScoreTest(unittest.TestCase):
    def test_score_uses_pure_timing_qor_and_reports_runtime_separately(self):
        root = {"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5, "initial_tns_ns": -20.0}
        node = {"final_fmax_mhz": 410.0, "final_wns_ns": -0.35, "final_tns_ns": -12.0}
        score = compute_qor_score(root, node, path_runtime_seconds=600.0)
        self.assertEqual(score.status, "improved")
        self.assertGreater(score.total_score, 0)
        self.assertAlmostEqual(score.components["runtime_minutes"], 10.0)
        self.assertAlmostEqual(score.weights["runtime_minutes"], 0.0)
        self.assertAlmostEqual(score.weighted_components["runtime_minutes"], 0.0)

    def test_invalid_output_is_penalized(self):
        root = {"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5, "initial_tns_ns": -20.0}
        node = {"final_fmax_mhz": 410.0, "final_wns_ns": -0.35, "final_tns_ns": -12.0}
        score = compute_qor_score(root, node, valid_output=False)
        self.assertEqual(score.status, "invalid")
        self.assertLess(score.weighted_components["invalid_output"], 0)

    def test_severe_regression_status(self):
        root = {"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5}
        node = {"final_fmax_mhz": 390.0, "final_wns_ns": -0.7}
        score = compute_qor_score(root, node)
        self.assertEqual(score.status, "regressed")

    def test_missing_root_baseline_can_be_inferred_from_node_initial_metrics(self):
        root = {}
        node = {
            "clock_period_ns": 2.5,
            "initial_fmax_mhz": 400.0,
            "final_fmax_mhz": 420.0,
            "initial_wns_ns": -0.5,
            "final_wns_ns": -0.25,
            "initial_tns_ns": -20.0,
            "final_tns_ns": -10.0,
        }
        filled = fill_missing_qor_baseline(root, node)
        self.assertTrue(has_qor_baseline(filled))
        self.assertEqual(filled["initial_fmax_mhz"], 400.0)
        score = compute_qor_score(filled, node)
        self.assertEqual(score.status, "improved")
        self.assertGreater(score.total_score, 0.0)
        self.assertEqual(score.components["root_fmax_mhz"], 400.0)

    def test_inferred_baseline_does_not_overwrite_existing_root_metrics(self):
        root = {"initial_fmax_mhz": 390.0, "initial_wns_ns": -0.6}
        node = {"initial_fmax_mhz": 400.0, "initial_wns_ns": -0.5, "initial_tns_ns": -20.0}
        filled = fill_missing_qor_baseline(root, node)
        self.assertEqual(filled["initial_fmax_mhz"], 390.0)
        self.assertEqual(filled["initial_wns_ns"], -0.6)
        self.assertEqual(filled["initial_tns_ns"], -20.0)

    def test_fpl26_official_contest_formula_example(self):
        score = compute_fpl26_official_contest_score(
            fmax_input_mhz=400.0,
            fmax_output_mhz=450.0,
            openrouter_cost_usd=0.25,
            runtime_seconds=1200.0,
        )
        self.assertAlmostEqual(score.score, 47.083333, places=6)
        self.assertEqual(score.formula_version, "fpl26.official.v1")
        self.assertIsNone(score.zero_reason)

    def test_fpl26_official_contest_runtime_gamma_caps_at_one_hour(self):
        score = compute_fpl26_official_contest_score(
            fmax_input_mhz=400.0,
            fmax_output_mhz=450.0,
            openrouter_cost_usd=0.0,
            runtime_seconds=7200.0,
        )
        self.assertEqual(score.gamma_runtime_hours, 1.0)
        self.assertTrue(score.gamma_capped)

    def test_fpl26_official_contest_zero_reasons(self):
        cases = [
            ({"valid_for_scoring": False}, "validation_failed"),
            ({"produced_output_dcp": False}, "missing_output_dcp"),
            ({"fmax_output_mhz": None}, "missing_fmax"),
            ({"fmax_output_mhz": 400.0}, "no_fmax_improvement"),
            ({"openrouter_cost_usd": 20.0}, "formula_nonpositive"),
        ]
        for overrides, reason in cases:
            kwargs = {
                "fmax_input_mhz": 400.0,
                "fmax_output_mhz": 450.0,
                "openrouter_cost_usd": 0.0,
                "runtime_seconds": 1.0,
                "valid_for_scoring": True,
                "produced_output_dcp": True,
            }
            kwargs.update(overrides)
            score = compute_fpl26_official_contest_score(**kwargs)
            self.assertEqual(score.score, 0.0)
            self.assertEqual(score.zero_reason, reason)


if __name__ == "__main__":
    unittest.main()
