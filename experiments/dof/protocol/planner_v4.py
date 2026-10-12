"""WP-014 planner overlay enforcing public language eligibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contract_v4 import LANGUAGE_CONDITIONS, PLANNER_MESSAGE_V4, language_condition_error
from .planner_v2 import PlannerTurnResultV2, result_public
from .planner_v3 import SingleSubmissionSessionV3


@dataclass(slots=True)
class SingleSubmissionSessionV4(SingleSubmissionSessionV3):
    language_condition: str = "FREE_CHOICE"
    planner_message_version = PLANNER_MESSAGE_V4

    def __post_init__(self) -> None:
        super(SingleSubmissionSessionV4, self).__post_init__()
        if self.language_condition not in LANGUAGE_CONDITIONS:
            raise ValueError("language_condition_invalid")

    def accept(self, message: Mapping[str, Any]) -> PlannerTurnResultV2:
        if (
            isinstance(message, Mapping)
            and message.get("kind") in {"DRAFT", "VALIDATE", "COMMIT"}
            and isinstance(message.get("candidate"), Mapping)
        ):
            error = language_condition_error(message["candidate"], self.language_condition)
            if error is not None:
                return self._feedback(error, "$.candidate", message)
        return super(SingleSubmissionSessionV4, self).accept(message)


__all__ = ["SingleSubmissionSessionV4", "PlannerTurnResultV2", "result_public"]
