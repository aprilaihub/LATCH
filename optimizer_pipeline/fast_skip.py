"""Interfaces for future pre-execution fast-skip policies.

Fast skip is intentionally a no-op for now. The interface exists so that
retrieval, analysis, and FPGA-team strategies can later decide to skip
expensive Vivado execution while still recording the skipped candidate and
reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol


@dataclass
class FastSkipDecision:
    should_skip: bool = False
    reason: str = "no_fast_skip"
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "should_skip": self.should_skip,
            "reason": self.reason,
            "confidence": self.confidence,
            "metadata": self.metadata,
        }


class FastSkipPolicy(Protocol):
    name: str

    def evaluate(self, *, candidate: Any, parent_node: Any, context: Dict[str, Any]) -> FastSkipDecision:
        ...


class NoOpFastSkipPolicy:
    name = "noop_fast_skip"

    def evaluate(self, *, candidate: Any, parent_node: Any, context: Dict[str, Any]) -> FastSkipDecision:
        return FastSkipDecision(False, "noop_fast_skip")
