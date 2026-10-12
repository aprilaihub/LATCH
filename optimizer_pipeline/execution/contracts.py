"""Reusable single-step DCP optimisation loop contracts.

The outer run coordination layer (frontier expansion, fixed plans, portfolio/bandit, or a
hand-written experiment loop) should choose which DCP and optimiser to try.
This package owns only the transition executor: input DCP + optimiser -> validated/scored
child DCP result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from optimizer_pipeline.models import ExecutionResult, OutputValidationResult, to_jsonable
from optimizer_pipeline.qor import QoRScore


@dataclass
class DcpTransitionRequest:
    input_dcp: Path
    optimizer_name: str
    output_dir: Path
    output_dcp: Optional[Path] = None
    root_metrics: Dict[str, Any] = field(default_factory=dict)
    parent_metrics: Dict[str, Any] = field(default_factory=dict)
    optimizer_params: Dict[str, Any] = field(default_factory=dict)
    run_id: Optional[str] = None
    parent_node_id: Optional[str] = None
    child_node_id: Optional[str] = None
    candidate_id: Optional[str] = None
    flow_sequence_before: List[str] = field(default_factory=list)
    mode: str = "experiment"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def resolved_output_dcp(self) -> Path:
        return Path(self.output_dcp) if self.output_dcp is not None else Path(self.output_dir) / "output.dcp"

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class DcpTransitionResult:
    success: bool
    accepted: bool
    input_dcp: str
    output_dcp: Optional[str]
    optimizer_name: str
    optimizer_params: Dict[str, Any]
    validation_status: str
    rejection_reason: Optional[str]
    execution: ExecutionResult
    output_validation: OutputValidationResult
    analysis_context: Dict[str, Any]
    metrics: Dict[str, Any]
    qor_score: QoRScore
    runtime_seconds: Optional[float]
    artifacts: Dict[str, str] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    transition_record: Dict[str, Any] = field(default_factory=dict)

    @property
    def pure_qor_score(self) -> Optional[float]:
        return self.qor_score.total_score if self.qor_score is not None else None

    def to_dict(self) -> Dict[str, Any]:
        data = to_jsonable(self)
        data["pure_qor_score"] = self.pure_qor_score
        return data
