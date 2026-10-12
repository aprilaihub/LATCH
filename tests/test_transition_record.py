from pathlib import Path

from optimizer_pipeline.domain.transition import DcpTransitionRecord
from optimizer_pipeline.domain.transition import (
    candidate_priority_audit_fields,
)
from optimizer_pipeline.models import ExecutionResult, OutputValidationResult
from optimizer_pipeline.qor import QoRScore
from optimizer_pipeline.execution.contracts import DcpTransitionResult


class DummyNode:
    def __init__(self, node_id, parent_id=None, selected_flow=None, status="COMPLETED", score=1.0):
        self.node_id = node_id
        self.parent_id = parent_id
        self.selected_flow = selected_flow
        self.status = status
        self.score = score
        self.validation_status = "CLEAN" if status == "COMPLETED" else "INVALID"
        self.input_dcp = f"{node_id}_in.dcp"
        self.output_dcp = f"{node_id}_out.dcp"
        self.metrics = {
            "dcp_transition": {
                "accepted": status == "COMPLETED",
                "rejection_reason": None if status == "COMPLETED" else "validation_failed",
                "transition_record": {"transition_id": node_id, "child_node_id": node_id},
            },
            "node_runtime_seconds": 12.0,
        }


def test_priority_audit_preserves_raw_priority_before_clipping():
    fields = candidate_priority_audit_fields(
        {"priority": 1.0, "raw_base_priority": 0.92, "raw_analysis_adjusted_priority": 1.18},
        analysis_priority_delta=0.2,
    )
    assert fields["clipped_priority"] == 1.0
    assert fields["raw_base_priority"] == 0.92
    assert fields["raw_analysis_adjusted_priority"] == 1.18
    assert fields["priority_clip_applied"] is True


def test_manual_transition_record_from_transition_result():
    execution = ExecutionResult(True, "phys_opt", "in.dcp", "out.dcp", "run", metrics={"total_runtime_seconds": 1.0}, exit_code=0)
    validation = OutputValidationResult(True, "CLEAN", [], [], False, {}, [])
    qos = QoRScore(total_score=2.5, status="improved", components={}, weighted_components={}, weights={})
    result = DcpTransitionResult(
        success=True,
        accepted=True,
        input_dcp="in.dcp",
        output_dcp="out.dcp",
        optimizer_name="phys_opt",
        optimizer_params={"flow": "phys_opt"},
        validation_status="CLEAN",
        rejection_reason=None,
        execution=execution,
        output_validation=validation,
        analysis_context={},
        metrics={},
        qor_score=qos,
        runtime_seconds=1.0,
        transition_record={"transition_id": "t0"},
    )
    record = DcpTransitionRecord.from_fixed_sequence_transition(run_id="manual", step_index=0, input_dcp=Path("in.dcp"), optimizer_name="phys_opt", result=result)
    assert record.intent.source == "manual_plan"
    assert record.outcome.validation_status == "CLEAN"
    assert record.handling.final_status == "accepted"
    assert record.to_policy_candidate_row()["source"] == "manual_plan"
