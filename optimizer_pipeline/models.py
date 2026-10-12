"""Public data model for DCP-state graph expansion."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class PipelineStatus(str, Enum):
    CREATED = "CREATED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    SELECTING = "SELECTING"
    VALIDATING_SELECTION = "VALIDATING_SELECTION"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VALIDATING_OUTPUT = "VALIDATING_OUTPUT"
    COMPLETED = "COMPLETED"
    PRUNED = "PRUNED"
    FAILED = "FAILED"




class ValidationStatus(str, Enum):
    CLEAN = "CLEAN"
    WARNING = "WARNING"
    FALLBACK_USED = "FALLBACK_USED"
    INVALID = "INVALID"
    REGRESSED = "REGRESSED"


class StopReason(str, Enum):
    MAX_ITERATIONS_REACHED = "MAX_ITERATIONS_REACHED"
    LLM_REQUESTED_STOP = "LLM_REQUESTED_STOP"
    NO_VALID_CANDIDATE = "NO_VALID_CANDIDATE"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    USER_ABORTED = "USER_ABORTED"
    USER_STOPPED = "USER_STOPPED"
    ANALYSIS_ONLY_COMPLETED = "ANALYSIS_ONLY_COMPLETED"


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses, enums, and paths into JSON-safe values."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {item.name: to_jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


class JsonModel:
    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class DcpStateNode(JsonModel):
    node_id: str
    parent_id: Optional[str]
    depth: int
    input_dcp: str
    output_dcp: Optional[str] = None
    flow_sequence: List[str] = field(default_factory=list)
    selected_flow: Optional[str] = None
    status: NodeStatus = NodeStatus.PENDING
    score: Optional[float] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    validation_status: Optional[str] = None
    validation_warnings: List[str] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)
    fallback_used: bool = False
    prune_reason: Optional[str] = None
    artifact_dir: Optional[str] = None


@dataclass
class PipelineState(JsonModel):
    status: PipelineStatus
    root_dcp: str
    active_node_ids: List[str] = field(default_factory=list)
    completed_node_ids: List[str] = field(default_factory=list)
    failed_node_ids: List[str] = field(default_factory=list)
    pruned_node_ids: List[str] = field(default_factory=list)
    best_node_id: Optional[str] = None
    stop_reason: Optional[StopReason] = None
    lifecycle_transitions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ExecutionResult(JsonModel):
    success: bool
    flow: str
    input_dcp: str
    output_dcp: str
    run_dir: str
    summary_path: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    exit_code: int = 1
    error: Optional[str] = None


@dataclass
class OutputValidationResult(JsonModel):
    valid: bool
    output_dcp: str
    summary_path: Optional[str]
    validation_mode: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    status: str = ValidationStatus.CLEAN.value
    fallback_used: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)
    log_paths: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult(JsonModel):
    success: bool
    run_dir: str
    final_output: Optional[str]
    summary_path: str
    state: PipelineState
