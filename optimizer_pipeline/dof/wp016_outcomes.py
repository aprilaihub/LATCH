"""Frozen WP-016 integration-status versus proposal-outcome taxonomy.

The taxonomy deliberately classifies a finite model/protocol budget outcome as
complete integration evidence without relabelling it as a successful proposal.
Unknown terminals fail closed as shared integration defects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TerminalOrigin(StrEnum):
    MODEL_OUTPUT = "MODEL_OUTPUT"
    FINITE_PROTOCOL_BUDGET = "FINITE_PROTOCOL_BUDGET"
    VALID_PROPOSAL_TERMINAL = "VALID_PROPOSAL_TERMINAL"
    PROVIDER_OR_TRANSPORT = "PROVIDER_OR_TRANSPORT"
    CONTROLLER_INVARIANT = "CONTROLLER_INVARIANT"
    AUTHORITY_OR_ENVIRONMENT = "AUTHORITY_OR_ENVIRONMENT"
    UNKNOWN = "UNKNOWN"


class IntegrationStatus(StrEnum):
    COMPLETE_TYPED_CELL_OUTCOME = "COMPLETE_TYPED_CELL_OUTCOME"
    SHARED_INTEGRATION_FAILURE = "SHARED_INTEGRATION_FAILURE"


@dataclass(frozen=True, slots=True)
class OutcomeClassification:
    terminal: str
    terminal_origin: TerminalOrigin
    integration_status: IntegrationStatus
    proposal_outcome: str
    shared_integration_defect: bool

    def public(self) -> dict[str, Any]:
        return {
            "integration_status": self.integration_status.value,
            "proposal_outcome": self.proposal_outcome,
            "terminal_origin": self.terminal_origin.value,
            "shared_integration_defect": self.shared_integration_defect,
            "scientific_analysis_eligible": False,
        }


_VALID_PROPOSAL = {
    "COMMIT_RECEIVED": "PROPOSAL_COMMIT",
    "NO_VALID_SUBMISSION_RECEIVED": "NO_VALID_SUBMISSION",
}

_FINITE_PROTOCOL = {
    "CONTEXT_LIMIT_AFTER_MODEL_TURN": "NO_PROPOSAL_CONTEXT_LIMIT",
    "TURN_LIMIT_EXHAUSTED": "NO_PROPOSAL_TURN_LIMIT",
    "SEMANTIC_RESERVE_EXHAUSTED": "NO_PROPOSAL_SEMANTIC_LIMIT",
    "QUERY_COUNT_EXHAUSTED": "NO_PROPOSAL_QUERY_LIMIT",
    "QUERY_RESPONSE_TRUNCATED": "NO_PROPOSAL_QUERY_RESPONSE_TRUNCATED",
    "OUTPUT_LIMIT_EXHAUSTED": "NO_PROPOSAL_OUTPUT_LIMIT",
    "OUTPUT_LIMIT_EXCEEDED": "NO_PROPOSAL_OUTPUT_LIMIT",
    "TRUNCATED_RESPONSE": "NO_PROPOSAL_TRUNCATED_RESPONSE",
}

_MODEL_OUTPUT = {
    "MODEL_CONTENT_JSON_INVALID": "NO_PROPOSAL_MODEL_FORMAT",
    "MODEL_CONTENT_SCHEMA_INVALID": "NO_PROPOSAL_MODEL_FORMAT",
    "MALFORMED_RETRIEVAL": "NO_PROPOSAL_MODEL_FORMAT",
    "RETRIEVAL_NOT_FOUND": "NO_PROPOSAL_RETRIEVAL_NOT_FOUND",
    "CAPABILITY_UNAVAILABLE": "NO_PROPOSAL_CAPABILITY_UNAVAILABLE",
    "MULTIPLE_COMMIT_REJECTED": "NO_PROPOSAL_MULTIPLE_COMMIT",
    "PROGRAM_VALIDATION_REQUIRED": "NO_PROPOSAL_VALIDATION_REQUIRED",
}

_CONTROLLER_INVARIANT = {
    "MALFORMED_OUTPUT_ACCOUNTING": "NO_PROPOSAL_CONTROLLER_INVARIANT",
}


KNOWN_TERMINALS = frozenset(
    _VALID_PROPOSAL | _FINITE_PROTOCOL | _MODEL_OUTPUT | _CONTROLLER_INVARIANT
)
INTEGRATION_COMPLETE_TERMINALS = frozenset(
    _VALID_PROPOSAL | _FINITE_PROTOCOL | _MODEL_OUTPUT
)
PROPOSAL_TERMINALS = frozenset(_VALID_PROPOSAL)


def classify_terminal(terminal: str) -> OutcomeClassification:
    if terminal in _VALID_PROPOSAL:
        return OutcomeClassification(
            terminal, TerminalOrigin.VALID_PROPOSAL_TERMINAL,
            IntegrationStatus.COMPLETE_TYPED_CELL_OUTCOME,
            _VALID_PROPOSAL[terminal], False,
        )
    if terminal in _FINITE_PROTOCOL:
        return OutcomeClassification(
            terminal, TerminalOrigin.FINITE_PROTOCOL_BUDGET,
            IntegrationStatus.COMPLETE_TYPED_CELL_OUTCOME,
            _FINITE_PROTOCOL[terminal], False,
        )
    if terminal in _MODEL_OUTPUT:
        return OutcomeClassification(
            terminal, TerminalOrigin.MODEL_OUTPUT,
            IntegrationStatus.COMPLETE_TYPED_CELL_OUTCOME,
            _MODEL_OUTPUT[terminal], False,
        )
    if terminal in _CONTROLLER_INVARIANT:
        return OutcomeClassification(
            terminal, TerminalOrigin.CONTROLLER_INVARIANT,
            IntegrationStatus.SHARED_INTEGRATION_FAILURE,
            _CONTROLLER_INVARIANT[terminal], True,
        )
    return OutcomeClassification(
        terminal, TerminalOrigin.UNKNOWN,
        IntegrationStatus.SHARED_INTEGRATION_FAILURE,
        "NO_PROPOSAL_UNKNOWN_TERMINAL", True,
    )


def shared_failure_origin(status: str) -> TerminalOrigin:
    upper = status.upper()
    if any(fragment in upper for fragment in (
        "PROVIDER", "TRANSPORT", "HTTP", "SERVICE_TIER", "CACHE_POLICY",
        "REQUEST_ID", "MODEL_IDENTITY",
    )):
        return TerminalOrigin.PROVIDER_OR_TRANSPORT
    if any(fragment in upper for fragment in (
        "AUTHORITY", "TOKEN", "CREDENTIAL", "ENVIRONMENT", "MANIFEST",
        "RUNTIME", "WORKTREE",
    )):
        return TerminalOrigin.AUTHORITY_OR_ENVIRONMENT
    if any(fragment in upper for fragment in (
        "CONTROLLER", "ADAPTER", "INVARIANT", "SCHEMA", "GUARD", "BUDGET",
    )):
        return TerminalOrigin.CONTROLLER_INVARIANT
    return TerminalOrigin.UNKNOWN


def taxonomy_manifest() -> dict[str, Any]:
    return {
        "schema_version": "wp016a.terminal-outcome-taxonomy.v1",
        "terminal_origins": [item.value for item in TerminalOrigin],
        "integration_statuses": [item.value for item in IntegrationStatus],
        "known_terminal_table": {
            terminal: classify_terminal(terminal).public()
            for terminal in sorted(KNOWN_TERMINALS)
        },
        "unknown_terminal_policy": {
            "integration_status": IntegrationStatus.SHARED_INTEGRATION_FAILURE.value,
            "terminal_origin": TerminalOrigin.UNKNOWN.value,
            "shared_integration_defect": True,
            "action": "STOP_ALL_LATER_CELLS",
        },
        "scientific_analysis_eligible": False,
    }


__all__ = [
    "INTEGRATION_COMPLETE_TERMINALS", "KNOWN_TERMINALS", "PROPOSAL_TERMINALS",
    "IntegrationStatus", "OutcomeClassification", "TerminalOrigin",
    "classify_terminal", "shared_failure_origin", "taxonomy_manifest",
]
