"""WP015 single-COMMIT protocol without historical controller monkeypatches."""
from typing import Any, Mapping

from ..protocol.planner_v5 import SingleSubmissionSessionV5
from .wp015a_strict_g import strict_family_feedback
from .wp015_feedback import feedback_constructs


class StudySession(SingleSubmissionSessionV5):
    def __init__(self, *args: Any, strict_g_required: bool = False,
                 required_family: str | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.strict_g_required = strict_g_required
        self.required_family = required_family
        self._feedback_constructs = {}

    def accept(self, message: Mapping[str, Any]):
        self._feedback_constructs = {}
        if (self.strict_g_required and isinstance(message, Mapping)
                and message.get("kind") in {"DRAFT", "VALIDATE", "COMMIT"}
                and isinstance(message.get("candidate"), Mapping)):
            feedback = strict_family_feedback(message["candidate"], required_family=self.required_family)
            if feedback is not None:
                return self._feedback(feedback, "$.candidate", message)
        result = super().accept(message)
        if result.compiled is not None:
            self._feedback_constructs = feedback_constructs(message["candidate"])
        return result
