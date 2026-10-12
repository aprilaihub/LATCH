"""WP-013B planner overlay; runtime compilation remains the accepted v2 path."""

from __future__ import annotations

from .contract_v3 import PLANNER_MESSAGE_V3, QUERY_TOP_K_MAX, schema_ref_for_error
from .planner_v2 import PlannerTurnResultV2, SingleSubmissionSessionV2, result_public


class SingleSubmissionSessionV3(SingleSubmissionSessionV2):
    query_top_k_max = QUERY_TOP_K_MAX
    planner_message_version = PLANNER_MESSAGE_V3

    def _schema_ref_for_error(self, error_code: str) -> str:
        return schema_ref_for_error(error_code)


PlannerTurnResultV3 = PlannerTurnResultV2

__all__ = ["PlannerTurnResultV3", "SingleSubmissionSessionV3", "result_public"]
