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

# Mirrors reference_manual_schema.json -> flow_cards.<id>.* (see data/).
REQUIRED_CARD_FIELDS = (
    "id",
    "display_name",
    "description",
    "runtime_class",
    "risk_level",
    "use_when",
    "avoid_when",
    "composable_with",
    "redundant_after",
    "typical_fmax_delta_mhz",
    "notes",
)

# The future concise-projection cut (not applied yet).
SHORT_CARD_FIELDS = (
    "id",
    "display_name",
    "runtime_class",
    "risk_level",
    "typical_fmax_delta_mhz",
    "use_when",
    "avoid_when",
)
LONG_CARD_FIELDS = ("description", "composable_with", "redundant_after", "notes")


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
