"""Provider-neutral proposal generation port for intervention capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class GeneratedInterventionProposal:
    payload: Dict[str, Any]
    audit: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class InterventionProposalGenerator(Protocol):
    def generate_recipe(self, *, context: Dict[str, Any], catalog: Dict[str, Any]) -> GeneratedInterventionProposal: ...

    def generate_manual(
        self,
        *,
        kind: str,
        context: Dict[str, Any],
        catalog: Dict[str, Any],
        plan: Optional[Dict[str, Any]],
        action_index: Optional[int],
    ) -> GeneratedInterventionProposal: ...

    def usage_summary(self) -> Dict[str, Any]: ...
