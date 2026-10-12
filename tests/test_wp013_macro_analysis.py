from scripts.analyze_wp013_macro_panel import best_constant, fit_stump, predict


def test_transparent_candidates_use_features_not_root_identity() -> None:
    roots = ["a", "b", "c"]
    actions = ["m0", "m1"]
    values = {
        ("a", "m0"): 10.0, ("a", "m1"): 5.0,
        ("b", "m0"): 4.0, ("b", "m1"): 9.0,
        ("c", "m0"): 3.0, ("c", "m1"): 8.0,
    }
    features = {"a": 0.0, "b": 10.0, "c": 11.0}
    panel = {}
    for root in roots:
        for action in actions:
            panel[root, action] = {
                "final_fmax_mhz": values[root, action],
                "features": {
                    "initial_wns_ns": features[root], "initial_tns_ns": 0,
                    "failing_endpoints": 0, "initial_fmax_mhz": 0,
                    "brams": 0, "dsps": 0, "ffs": 0, "luts": 0,
                },
            }
    oracle = {root: max(values[root, action] for action in actions) for root in roots}
    assert best_constant(roots, actions, panel, oracle) == "m1"
    model = fit_stump(roots, actions, panel, oracle)
    assert model["kind"] == "decision_stump"
    assert predict(model, "a", actions, panel) == "m0"
    assert predict(model, "b", actions, panel) == "m1"
