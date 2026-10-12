"""Knowledgebase loader (stable across phases).

Reads + parses the on-disk KB JSON, fail-safe, cached so the file is read at
most once per run. This module is intentionally small and does NOT change as the
schema or projector grow — see ``projector.py`` (the tuning seam) and
``schema.py`` (card shape + validation).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _read_cached(resolved_path: str, _mtime_ns: int) -> Optional[Dict[str, Any]]:
    """Read + parse the KB JSON, cached by ``(path, mtime)``.

    The mtime is part of the cache key so an edited file is re-read, but within a
    single run the file is read at most once no matter how many policy iterations
    (or runners) occur. Fail-safe: returns ``None`` on any error.
    """
    try:
        with open(resolved_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "knowledgebase: failed to read %s (%s); continuing without knowledge_context",
            resolved_path,
            exc,
        )
        return None
    if isinstance(data, list):
        # A bare list is a flow-cards-only KB (no benchmark_history side).
        # projector.py already handles list-shaped flow_cards; wrap so it gets
        # the chance to. Provenance stays in config.json's knowledge_base_path.
        return {"flow_cards": data}
    if not isinstance(data, dict):
        logger.warning(
            "knowledgebase: %s is neither a JSON object nor an array; "
            "continuing without knowledge_context",
            resolved_path,
        )
        return None
    return data


def load_knowledge_base(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Load and parse the knowledgebase JSON file at ``path``.

    Fail-safe by contract: returns ``None`` (logging a warning) when ``path`` is
    falsy, the file is missing, or the contents are malformed. Never raises, so a
    knowledgebase problem can never crash an optimisation run. Reads at most once
    per run via an mtime-keyed cache.
    """
    if not path:
        return None
    resolved = Path(path).expanduser()
    try:
        mtime_ns = resolved.stat().st_mtime_ns
    except OSError as exc:
        logger.warning(
            "knowledgebase: cannot stat %s (%s); continuing without knowledge_context",
            path,
            exc,
        )
        return None
    return _read_cached(str(resolved), mtime_ns)
