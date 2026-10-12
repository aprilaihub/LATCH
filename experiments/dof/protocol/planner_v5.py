"""Action Surface v3 planner with construct-based boundary enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contract_v5 import PLANNER_MESSAGE_V5, compile_candidate_v5, schema_ref_for_error
from .contract_v6 import LANGUAGE_CONDITIONS, PLANNER_MESSAGE_V6, language_condition_error
from .language import CompiledProgram
from .planner_v2 import PlannerTurnResultV2, result_public
from .planner_v3 import SingleSubmissionSessionV3


@dataclass(slots=True)
class SingleSubmissionSessionV5(SingleSubmissionSessionV3):
    language_condition: str = "FREE_CHOICE"
    planner_message_version = PLANNER_MESSAGE_V6

    def __post_init__(self) -> None:
        super(SingleSubmissionSessionV5, self).__post_init__()
        if self.language_condition not in LANGUAGE_CONDITIONS:
            raise ValueError("language_condition_invalid")

    def _schema_ref_for_error(self, error_code: str) -> str:
        return schema_ref_for_error(error_code)

    def _compile_candidate(self, candidate: Mapping[str, Any]) -> CompiledProgram:
        return compile_candidate_v5(candidate, self.compile_context)

    def accept(self, message: Mapping[str, Any]) -> PlannerTurnResultV2:
        if (
            isinstance(message, Mapping)
            and message.get("kind") in {"DRAFT", "VALIDATE", "COMMIT"}
            and isinstance(message.get("candidate"), Mapping)
        ):
            error = language_condition_error(message["candidate"], self.language_condition)
            if error is not None:
                return self._feedback(error, "$.candidate", message)
        return super(SingleSubmissionSessionV5, self).accept(message)


__all__ = [
    "PLANNER_MESSAGE_V5", "SingleSubmissionSessionV5",
    "PlannerTurnResultV2", "result_public",
]
