"""Knowledgebase projector — THE TUNING SEAM.

Selects what goes into the LLM payload's ``knowledge_context``. The ``mode``
argument (config ``knowledge_mode``) is the source of truth for WHICH sides are
injected: off | reference_manual | benchmark_history | both. Each side is gated
independently, so reference_manual mode carries flow_cards and zero history, and
benchmark_history mode carries history and zero flow_cards.

When the mode includes flow_cards they are forwarded in full (~15 cards, no
compaction). When the mode includes benchmark_history it has two sub-modes:

  * ``params is None`` (default -- no caller opts in): FULL PASS-THROUGH -- every
    history record is forwarded, byte-identical to a bare
    ``build_knowledge_context(path)``.
  * ``params`` given (``history_retrieval_enabled=True`` in config): RETRIEVAL --
    a small relevant subset selected by the configured strategy (see
    ``retrieval.py``), field-projected, plus an optional
    ``history_retrieval_meta`` confidence block.

Handles both KB shapes:
  * dict  -> the real reference manual (``flow_cards`` keyed by flow id).
  * list  -> a list-shaped fixture (kept working for back-compat).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

from optimizer_pipeline.knowledge.retrieval import (
    DEFAULT_EXAMPLE_FIELDS,
    project_example,
    select_history,
)
from optimizer_pipeline.knowledge.schema import validate_flow_cards

logger = logging.getLogger(__name__)

# Generous caps: the full-pass-through path ships the full manual (~15 cards)
# and (today) ~60 history records with no compaction. These only bound payload
# size so a pathological KB cannot blow up the prompt. Retrieval mode bounds
# history separately via history_retrieval_max_examples.
_MAX_FLOW_CARDS = 100
_MAX_HISTORY_RECORDS = 500


def _project_flow_cards(raw: Any) -> Optional[Union[Dict[str, Any], List[Any]]]:
    """Pass flow_cards through (full manual), capped by count. Returns
    a dict for the real KB, a list for the legacy fixture, or ``None``."""
    if isinstance(raw, dict):
        for warning in validate_flow_cards(raw):
            logger.warning("knowledgebase: %s", warning)
        return dict(list(raw.items())[:_MAX_FLOW_CARDS])
    if isinstance(raw, list):
        return list(raw)[:_MAX_FLOW_CARDS]
    return None


_VALID_MODES = ("off", "reference_manual", "benchmark_history", "both")


def project_knowledge_context(
    knowledge_base: Optional[Dict[str, Any]],
    *,
    mode: str = "both",
    live_design_context: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Project a loaded KB into the compact ``knowledge_context`` payload dict.

    ``mode`` selects which sides are injected and is the SOURCE OF TRUTH for the
    split (config ``knowledge_mode``):

      * ``off``               -> ``None`` (no key at all; disabled-run invariant).
      * ``reference_manual``  -> flow_cards only. No ``benchmark_history`` key and
        NO history projection/retrieval is invoked at all (skipped, not computed
        then discarded).
      * ``benchmark_history`` -> history only (full pass-through, or Phase-3
        retrieval when ``params`` is given). No ``flow_cards`` key.
      * ``both``              -> both sides (the historical default).

    Returns ``None`` when there is nothing to inject in a valid mode, so the
    caller omits the key entirely and disabled runs stay byte-for-byte unchanged.

    ``live_design_context`` / ``params`` only affect the history side. Omitting
    both with ``mode="both"`` reproduces the full-pass-through payload exactly --
    no existing caller needs to change.
    """
    if mode not in _VALID_MODES:
        mode = "off"
    if mode == "off" or not isinstance(knowledge_base, dict):
        return None
    include_cards = mode in ("reference_manual", "both")
    include_history = mode in ("benchmark_history", "both")

    flow_cards = _project_flow_cards(knowledge_base.get("flow_cards")) if include_cards else None
    has_cards = bool(flow_cards)

    # History side is only touched when the mode includes it -- in
    # reference_manual mode select_history/project_example are never called.
    benchmark_history: List[Any] = []
    history_meta: Optional[Dict[str, Any]] = None
    history_is_retrieval = False
    if include_history:
        raw_history = knowledge_base.get("benchmark_history")
        history_records = list(raw_history) if isinstance(raw_history, list) else []
        if params is None:
            benchmark_history = history_records[:_MAX_HISTORY_RECORDS]
        else:
            examples, meta = select_history(history_records, live_design_context, params)
            fields = params.get("fields") or list(DEFAULT_EXAMPLE_FIELDS)
            benchmark_history = [project_example(record, fields) for record in examples]
            history_is_retrieval = True
            if params.get("include_confidence", True):
                history_meta = meta

    # Only the INCLUDED sides count toward emptiness: a deliberately-omitted side
    # (e.g. history in reference_manual mode) must not suppress the whole context.
    if not has_cards and not benchmark_history:
        return None

    result: Dict[str, Any] = {
        "phase": "retrieval_v1" if history_is_retrieval else "full_passthrough",
        "source_schema_version": knowledge_base.get("schema_version"),
    }
    if include_cards:
        result["flow_cards"] = flow_cards if flow_cards is not None else {}
    if include_history:
        result["benchmark_history"] = benchmark_history
        if history_meta is not None:
            result["history_retrieval_meta"] = history_meta
    return result
