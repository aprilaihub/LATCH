from scripts.audit_wp013_admission import corrected_actual_counts
from scripts.run_wp013_admission import relocation_effect_count, route_status_clean


def test_route_status_accepts_vivado_2025_1_clean_format() -> None:
    report = b"""
       # of fully routed nets............. : 2918 :
       # of nets with routing errors...... : 0 :
    """
    assert route_status_clean(report) is True


def test_route_status_rejects_nonzero_errors_or_missing_positive_evidence() -> None:
    assert route_status_clean(b"# of fully routed nets: 10\n# of nets with routing errors: 2") is False
    assert route_status_clean(b"route command returned") is False


def test_create_pblock_legacy_count_correction_is_not_double_applied() -> None:
    compiled = {"transformed_count": 9}
    assert corrected_actual_counts({"transformed": 8}, compiled, 1)["transformed"] == 9
    assert corrected_actual_counts({"transformed": 9}, compiled, 1)["transformed"] == 9
    assert corrected_actual_counts({"transformed": 7}, compiled, 1)["transformed"] == 7


def test_relocation_effect_marker_accepts_crlf_but_not_echo_or_wrong_count() -> None:
    output = (
        b'puts "DOF_WP013_RELOCATE_V1:$__dof_n:$__dof_moved"\r\n'
        b"DOF_WP013_RELOCATE_V1:1:1\r\n"
    )
    assert relocation_effect_count(output, 1) == 1
    assert relocation_effect_count(b"DOF_WP014C_RELOCATE_V1:1:1\r\n", 1) == 1
    assert relocation_effect_count(b"DOF_WP014H_APPLY_ALLOCATION_V1:2:2\r\n", 2) == 2

    import pytest

    with pytest.raises(RuntimeError, match="relocation_effect_count_mismatch"):
        relocation_effect_count(b"DOF_WP013_RELOCATE_V1:1:0\r\n", 1)
