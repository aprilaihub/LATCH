"""Small, dependency-free contracts for degree-of-freedom experiments.

These enums are intentionally closed.  Parsing code must reject values outside
the declared surface instead of silently mapping them to a legacy capability.
"""

from __future__ import annotations

from enum import Enum


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class DoFLevel(_StringEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class LevelStatus(_StringEnum):
    ACTIVE = "ACTIVE"
    REFERENCE_PENDING = "REFERENCE_PENDING"


class UnknownRegime(_StringEnum):
    KNOWN = "known"
    FORCED_UNKNOWN = "forced_unknown"
    NATURAL_UNKNOWN = "natural_unknown"


class EventClass(_StringEnum):
    READ_ONLY = "READ_ONLY"
    MUTATION_LIGHT = "MUTATION_LIGHT"
    IMPLEMENTATION_HEAVY = "IMPLEMENTATION_HEAVY"
    VALIDATION = "VALIDATION"
    LLM_REQUEST = "LLM_REQUEST"
    OUTER_ATTEMPT = "OUTER_ATTEMPT"


class EndpointCode(_StringEnum):
    VALID_COMPLETION = "VALID_COMPLETION"
    NO_IMPROVEMENT = "NO_IMPROVEMENT"
    INVALID_POLICY_OUTPUT = "INVALID_POLICY_OUTPUT"
    CAPABILITY_ACTION_REJECTION = "CAPABILITY_ACTION_REJECTION"
    MASK_VIOLATION = "MASK_VIOLATION"
    BUDGET_REJECTION = "BUDGET_REJECTION"
    BACKEND_ERROR = "BACKEND_ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLATION = "CANCELLATION"
    INVALID_ARTIFACT = "INVALID_ARTIFACT"
    L4_REFERENCE_PENDING = "L4_REFERENCE_PENDING"


class DesignStage(_StringEnum):
    UNPLACED = "UNPLACED"
    PLACED = "PLACED"
    ROUTED = "ROUTED"
