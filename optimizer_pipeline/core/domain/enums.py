"""Stable vocabulary for immutable optimization-domain facts."""

from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class RunStatus(StringEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    FINALIZED = "FINALIZED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class ScopeKind(StringEnum):
    ROOT = "ROOT"
    FIXED = "FIXED"
    LINEAR = "LINEAR"
    FRONTIER = "FRONTIER"
    INTERVENTION = "INTERVENTION"
    CERTIFICATION = "CERTIFICATION"
    CUSTOM = "CUSTOM"


class ArtifactKind(StringEnum):
    DCP = "DCP"
    LOG = "LOG"
    REPORT = "REPORT"
    MANIFEST = "MANIFEST"
    OTHER = "OTHER"


class AttemptEventKind(StringEnum):
    SCHEDULED = "SCHEDULED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ORPHANED = "ORPHANED"


class OutcomeStatus(StringEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class ValidationLevel(StringEnum):
    ATTEMPT = "ATTEMPT"
    CERTIFICATION = "CERTIFICATION"
    FORMAL = "FORMAL"


class ValidationStatus(StringEnum):
    NOT_RUN = "NOT_RUN"
    CLEAN = "CLEAN"
    WARNING = "WARNING"
    FALLBACK_USED = "FALLBACK_USED"
    INVALID = "INVALID"
    PASSED = "PASSED"
    FAILED = "FAILED"


class BudgetEventKind(StringEnum):
    RESERVED = "RESERVED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RELEASED = "RELEASED"


class SelectionKind(StringEnum):
    BASELINE = "BASELINE"
    BEST_VALID = "BEST_VALID"
    EXPLICIT = "EXPLICIT"


class ParentUseMode(StringEnum):
    NORMAL = "NORMAL"
    EXPLICIT_INTERVENTION = "EXPLICIT_INTERVENTION"

