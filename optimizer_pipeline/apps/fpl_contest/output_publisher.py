"""Centralized publication of the contest output DCP."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

@dataclass
class BestDcpPublisher:
    """Owns the single contest output path and best-output history."""

    output_path: Path
    run_dir: Path
    run_mode: str
    contest_eligible: bool
    history: List[Dict[str, Any]] = field(default_factory=list)
    best_fmax_mhz: Optional[float] = None
    best_delta_fmax_mhz: Optional[float] = None
    best_node_id: Optional[str] = None
    best_transition_id: Optional[str] = None

    def __post_init__(self) -> None:
        self.output_path = Path(self.output_path)
        self.run_dir = Path(self.run_dir)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def publish_baseline(self, input_dcp: Path, *, root_fmax_mhz: Optional[float] = None) -> Dict[str, Any]:
        event = self._publish(
            source_dcp=Path(input_dcp),
            node_id="node_000_root",
            transition_id="baseline",
            optimizer_name="baseline",
            fmax_output_mhz=root_fmax_mhz,
            delta_fmax_mhz=0.0 if root_fmax_mhz is not None else None,
            reason="baseline published before expansion",
        )
        return event

    def consider_transition(self, trace_row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not trace_row.get("valid_for_scoring"):
            return None
        source = trace_row.get("output_dcp")
        if not source or not Path(source).exists():
            return None
        publication = trace_row.get("publication_selection") if isinstance(trace_row.get("publication_selection"), dict) else {}
        fmax = _safe_float(publication.get("fmax_output_mhz") if publication else trace_row.get("fmax_output_mhz"))
        delta_fmax = _safe_float(publication.get("delta_fmax_mhz") if publication else trace_row.get("delta_fmax_mhz"))
        if self.best_fmax_mhz is not None and fmax is not None and fmax <= self.best_fmax_mhz:
            return None
        if self.best_fmax_mhz is not None and fmax is None:
            return None
        return self._publish(
            source_dcp=Path(source),
            node_id=trace_row.get("node_id"),
            transition_id=trace_row.get("transition_id"),
            optimizer_name=trace_row.get("optimizer_name"),
            fmax_output_mhz=fmax,
            delta_fmax_mhz=delta_fmax,
            reason="valid Fmax publication improvement",
        )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "best_node_id": self.best_node_id,
            "best_transition_id": self.best_transition_id,
            "selection_metric": "fmax_output_mhz",
            "published_best_fmax_mhz": self.best_fmax_mhz,
            "published_best_delta_fmax_mhz": self.best_delta_fmax_mhz,
        }

    def _publish(
        self,
        *,
        source_dcp: Path,
        node_id: Optional[str],
        transition_id: Optional[str],
        optimizer_name: Optional[str],
        fmax_output_mhz: Optional[float],
        delta_fmax_mhz: Optional[float],
        reason: str,
    ) -> Dict[str, Any]:
        if not source_dcp.exists():
            raise FileNotFoundError(f"Cannot publish missing DCP: {source_dcp}")
        shutil.copyfile(source_dcp, self.output_path)
        now = time.time()
        self.output_path.touch()
        self.best_fmax_mhz = fmax_output_mhz if fmax_output_mhz is not None else self.best_fmax_mhz
        self.best_delta_fmax_mhz = delta_fmax_mhz if delta_fmax_mhz is not None else self.best_delta_fmax_mhz
        self.best_node_id = node_id
        self.best_transition_id = transition_id
        event = {
            "schema_version": "best_dcp_publisher.v1",
            "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "run_mode": self.run_mode,
            "contest_eligible": self.contest_eligible,
            "output_path": str(self.output_path),
            "source_dcp": str(source_dcp),
            "node_id": node_id,
            "transition_id": transition_id,
            "optimizer_name": optimizer_name,
            "selection_metric": "fmax_output_mhz",
            "published_best_fmax_mhz": fmax_output_mhz,
            "published_best_delta_fmax_mhz": delta_fmax_mhz,
            "reason": reason,
        }
        self.history.append(event)
        return event


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None
