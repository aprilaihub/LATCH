from __future__ import annotations

import os

import pytest

from scripts.run_wp003_offline_probes import run_probes


@pytest.mark.skipif(os.name != "posix", reason="WP-003 process ownership is POSIX-only")
def test_wp003_offline_adversarial_probe_summary_is_fail_closed() -> None:
    result = run_probes()

    assert result["schema_version"] == (
        "research-flow.wp003-offline-adversarial-probes.v1"
    )
    assert result["all_gates_passed"] is True
    assert set(result["gates"]) == {
        "gate0_repository_provenance",
        "gate1_backend_calls",
        "gate2_process_ownership",
        "gate3_artifact_crash_safety",
        "gate4_provider_binding",
    }
    assert all(gate["passed"] is True for gate in result["gates"].values())

    gate0 = result["gates"]["gate0_repository_provenance"]["probes"]
    mismatch = gate0["declared_constraint_mismatch"]
    assert mismatch["rejected"] is True
    assert mismatch["endpoint"] == "DECLARED_CONSTRAINT_MISMATCH"
    assert mismatch["reasons"] == ["required_head_mismatch"]
    assert mismatch["backend_or_provider_entries"] == 0
    forged = gate0["forged_clean_provenance"]
    assert forged["rejected"] is True
    assert "repository_provenance_clean_diff_hash_mismatch" in forged["reasons"]
    assert forged["trusted_as_observation"] is False

    gate1 = result["gates"]["gate1_backend_calls"]["probes"]
    unknown = gate1["unknown_operation"]
    assert unknown["rejected"] is True
    assert unknown["host_callable_entries"] == 0
    assert set(unknown["budget_used"].values()) == {0}
    assert unknown["trace"][-1]["status"] == "REJECTED_BEFORE_CALL"
    extra = gate1["undeclared_extra_heavy"]
    assert extra["rejected"] is True
    assert extra["host_callable_entries"] == 1
    assert extra["budget_used"]["IMPLEMENTATION_HEAVY"] == 1
    assert extra["trace"][-1]["status"] == "REJECTED_BEFORE_CALL"
    assert extra["reconciliation_rejected"] is True
    l0 = gate1["l0_mutation"]
    assert l0["rejected"] is True
    assert l0["host_callable_entries"] == 0
    assert l0["heavy_budget_used"] == 0

    process = result["gates"]["gate2_process_ownership"]["probes"][
        "owned_short_lived_process"
    ]
    assert process["endpoint"] == "SUCCEEDED"
    assert process["session_owned"] is True
    assert process["cleanup_ok"] is True
    assert process["orphan_count"] == 0
    assert process["root_process_token_present"] is True
    assert process["lifecycle"][-1] == "PROCESS_EVIDENCE_FINALIZED"
    assert process["real_tool_process"] is False

    gate3 = result["gates"]["gate3_artifact_crash_safety"]["probes"]
    matrix = gate3["artifact_transaction_crash_matrix"]
    assert len(matrix) == 6
    assert all(row["passed"] is True for row in matrix)
    assert all(row["database_integrity_ok"] is True for row in matrix)
    assert all(row["lifecycle_safe"] is True for row in matrix)
    assert all(row["lifecycle_complete"] is False for row in matrix)
    assert all(row["incomplete_attempt"] is True for row in matrix)
    assert all(row["selection_count"] == 0 for row in matrix)
    assert all(row["terminal_success_count"] == 0 for row in matrix)
    finalizer = gate3["evidence_finalization_crash"]
    assert finalizer["passed"] is True
    assert finalizer["finalizer_called"] is False
    assert finalizer["selection_count"] == 0

    gate4 = result["gates"]["gate4_provider_binding"]["probes"]
    strict = gate4["strict_parser"]
    assert strict["all_rejected"] is True
    assert len(strict["cases"]) == 8
    assert all(row["rejected"] is True for row in strict["cases"])
    assert strict["repair_or_salvage_attempts"] == 0
    binding = gate4["response_binding"]
    assert binding["accepted"] is False
    assert binding["endpoint"] == "PROVIDER_BINDING_MISMATCH"
    assert binding["evidence_errors"] == []
    assert binding["backend_dispatches"] == 0
    budget = gate4["budget_across_bindings"]
    assert budget["first_accepted"] is True
    assert budget["second_accepted"] is False
    assert budget["second_endpoint"] == "BUDGET_REJECTION"
    assert budget["second_transport_calls"] == 0
    assert sorted(budget["request_counts_by_contract"].values()) == [0, 1]
    cancellation = gate4["structured_cancellation"]
    assert cancellation["accepted"] is False
    assert cancellation["endpoint"] == "CANCELLATION"
    assert cancellation["attempt_statuses"] == ["CANCELLED"]
    assert cancellation["transport_calls"] == 1
    assert cancellation["evidence_errors"] == []
    assert cancellation["backend_dispatches"] == 0

    assert result["execution_boundary"] == {
        "real_dcp_attempts": 0,
        "vivado_starts": 0,
        "rapidwright_starts": 0,
        "mcp_starts": 0,
        "external_model_requests": 0,
        "real_backend_calls": 0,
        "fake_owned_process_starts": 1,
        "fake_provider_transport_calls": 3,
        "l4_execution": False,
        "schema_migration": False,
    }
