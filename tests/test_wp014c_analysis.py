from scripts.analyze_wp014c import candidate_labels, final_compiled, outcome, paired_rows


def test_candidate_labels_preserve_macro_and_typed_operations() -> None:
    candidate = {
        "language": "G",
        "statements": [
            {"node": "CALL_MACRO", "args": {"action_key": "macro.phys_opt"}},
            {"node": "P_PROGRAM", "args": {"program": {"steps": [{"op": "REPLICATE_DRIVER"}]}}},
        ],
    }
    actions, operations = candidate_labels(candidate)
    assert actions == ["macro.phys_opt"]
    assert operations == ["CALL_MACRO", "P_PROGRAM", "REPLICATE_DRIVER"]


def test_final_compiled_requires_committed_turn() -> None:
    row = {"turns": [
        {"planner_result": {"status": "CONSTRUCTION_VALID", "compiled": {"schema_version": "draft"}}},
        {"planner_result": {"status": "COMMITTED", "compiled": {"schema_version": "final"}}},
    ]}
    assert final_compiled(row) == {"schema_version": "final"}


def test_paired_rows_accept_neutral_and_conditioned_level_binding() -> None:
    base = [{
        "root_id": "r", "model": "m", "level": "L3", "language_condition": "FREE_CHOICE",
        "canonical_terminal": "VALID_COMPLETION", "selected_language": "M", "delta_fmax_mhz": 1.0,
    }]
    reduced = [dict(base[0], delta_fmax_mhz=2.0)]
    conditioned = [dict(base[0], language_condition="P_BOUNDARY", selected_language="P", delta_fmax_mhz=3.0)]
    assert paired_rows(base, reduced)[0]["conditioned_minus_neutral_delta_fmax_mhz"] == 1.0
    assert paired_rows(base, conditioned)[0]["conditioned_minus_neutral_delta_fmax_mhz"] == 2.0


def test_outcome_reconstructs_typed_fmax_from_frozen_period_and_wns() -> None:
    row = {
        "canonical_terminal": "VALID_COMPLETION",
        "execution": {"pre_timing": {"wns": -1.0}, "post_timing": {"wns": -0.5}},
    }
    initial, final, improvement = outcome(row, 4.0)
    assert initial == 200.0
    assert final == 1000.0 / 4.5
    assert improvement == final - initial
