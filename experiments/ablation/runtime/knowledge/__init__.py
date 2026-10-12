"""Cross-benchmark knowledgebase.

Public API is a single entry point:

    from experiments.ablation.runtime.knowledge import build_knowledge_context

The module is split by where future growth lands:
  * ``loader``    — file read, fail-safe, caching (small and stable).
  * ``schema``    — card shape + light validation (grows as history arrives).
  * ``projector`` — the tuning seam: selection / (future) compaction.
  * ``signals``   — live-design derivation rules, shared with the offline
    history extractor.
  * ``retrieval`` — the strategy registry the projector dispatches into when
    history retrieval is enabled.

``build_knowledge_context`` is the single entry point the frontier runner calls.
Callers that pass only ``path`` get the full-pass-through payload: omitting
``live_design_context``/``retrieval_params`` reproduces it exactly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from experiments.ablation.runtime.knowledge.loader import load_knowledge_base
from experiments.ablation.runtime.knowledge.projector import project_knowledge_context
from experiments.ablation.runtime.knowledge.retrieval import retrieval_params_from_config

# The tuned, catalogue-matched deploy manual: one card per allowed flow.
# reference_manual_final.json is flow_cards_C2_deploy.json plus the fanout card
# rewrite (2026-08-19); the older flow_cards_C2_deploy.json and reference_manual_kb.json
# are still selectable via --knowledge-base-path.
DEFAULT_KNOWLEDGE_BASE_PATH = Path(__file__).resolve().parent / "data" / "reference_manual_final.json"


def build_knowledge_context(
    path: Optional[str],
    *,
    mode: str = "off",
    live_design_context: Optional[Dict[str, Any]] = None,
    retrieval_params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Load (cached) + project. Returns ``None`` when disabled or unavailable.

    Loads the KB file once per run (mtime-cached) and reuses it for every
    policy iteration. When ``path`` is unset/missing/malformed, ``mode`` is
    ``"off"``, or the selected side has no content, the result is ``None`` and
    the runner adds no ``knowledge_context`` key (disabled runs stay byte-for-byte
    unchanged).

    ``mode`` (config ``knowledge_mode``) is the source of truth for which sides
    are injected: off | reference_manual | benchmark_history | both. This branch
    defaults to ``"off"``; callers must explicitly request the reference manual.

    ``retrieval_params`` is ``None`` by default -> full pass-through for the
    history side. Pass a dict (see ``retrieval.retrieval_params_from_config``) to
    opt into history retrieval;
    ``live_design_context`` then supplies whatever is known about the design being
    optimised right now. Both are ignored in ``reference_manual`` mode.
    """
    return project_knowledge_context(
        load_knowledge_base(path),
        mode=mode,
        live_design_context=live_design_context,
        params=retrieval_params,
    )


__all__ = [
    "load_knowledge_base",
    "project_knowledge_context",
    "build_knowledge_context",
    "retrieval_params_from_config",
    "DEFAULT_KNOWLEDGE_BASE_PATH",
]
