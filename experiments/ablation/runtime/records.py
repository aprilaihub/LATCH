from __future__ import annotations
from dataclasses import fields
from typing import Any, Dict

from optimizer_pipeline.domain.transition import DcpTransitionHandling, DcpTransitionIntent, DcpTransitionOutcome, DcpTransitionRecord

def record_from_dict(data: Dict[str, Any]) -> DcpTransitionRecord:
    if not isinstance(data, dict):
        raise TypeError("DcpTransitionRecord JSON object expected")
    intent_data = data.get("intent") or {}
    outcome_data = data.get("outcome")
    handling_data = data.get("handling")
    return DcpTransitionRecord(
        intent=DcpTransitionIntent(**_filter_dataclass_kwargs(DcpTransitionIntent, intent_data)),
        outcome=DcpTransitionOutcome(**_filter_dataclass_kwargs(DcpTransitionOutcome, outcome_data)) if isinstance(outcome_data, dict) else None,
        handling=DcpTransitionHandling(**_filter_dataclass_kwargs(DcpTransitionHandling, handling_data)) if isinstance(handling_data, dict) else None,
    )

def _filter_dataclass_kwargs(cls: Any, data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    allowed = {f.name for f in fields(cls)}
    return {key: value for key, value in data.items() if key in allowed}
