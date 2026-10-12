"""Flow-card schema + light validation.

The authoritative card shape is ``reference_manual_schema.json`` (by-example).
Its field set is mirrored here so the projector can warn on drift without taking
a JSON-Schema dependency. Validation is WARN-ONLY and never raises (fail-safe) —
a malformed card must never crash a run.

``SHORT_CARD_FIELDS`` / ``LONG_CARD_FIELDS`` mark the clean cut a later *concise*
projection will use (keep short, drop long). The projector currently forwards the
full card.
"""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)

# Mirrors the deployed manual, data/flow_cards_C2_deploy.json -> <flow_id>.*.
# The older reference_manual_kb.json carried extra measured fields
# (runtime_class, risk_level, composable_with, redundant_after,
# typical_fmax_delta_mhz, notes); those were benchmark-derived and are
# deliberately absent from the generalised deploy manual, so they are not
# required here.
REQUIRED_CARD_FIELDS = (
    "id",
    "display_name",
    "description",
    "use_when",
    "avoid_when",
    "ceiling_note",
)

# The future concise-projection cut (not applied yet).
SHORT_CARD_FIELDS = (
    "id",
    "display_name",
    "use_when",
    "avoid_when",
)
LONG_CARD_FIELDS = ("description", "ceiling_note")


def validate_flow_cards(flow_cards: Any) -> List[str]:
    """Return human-readable warnings about card-shape drift; never raises.

    Only the dict (schema) shape is validated. List-shaped flow_cards (legacy
    list fixtures) are intentionally skipped — they predate this schema and are
    not expected to conform.
    """
    warnings: List[str] = []
    if not isinstance(flow_cards, dict):
        return warnings
    for fid, card in flow_cards.items():
        if not isinstance(card, dict):
            warnings.append(f"flow_card '{fid}' is not an object")
            continue
        missing = [f for f in REQUIRED_CARD_FIELDS if f not in card]
        if missing:
            warnings.append(f"flow_card '{fid}' missing fields: {missing}")
    return warnings
