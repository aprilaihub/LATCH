"""Application-layer recording of external certification results."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from optimizer_pipeline.core.domain import ValidationLevel, ValidationRecord, ValidationStatus
from optimizer_pipeline.core.store import SQLiteRunStore


def record_certification(
    run_db: str | Path,
    report: Mapping[str, Any],
    *,
    evidence_uri: str | None = None,
) -> ValidationRecord:
    """Append an idempotent certification fact for the latest selected state."""

    payload = json.dumps(dict(report), sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    status = ValidationStatus.PASSED if report.get("overall_passed") is True else ValidationStatus.FAILED
    with SQLiteRunStore(Path(run_db), migrate=False) as store:
        run = store.get_run()
        selections = store.selections()
        if run is None or not selections:
            raise ValueError("run.db has no run or selected state")
        selected = selections[-1]
        validation_id = f"certification:{selected.state_id}:{digest[:16]}"
        existing = next((item for item in store.validations(subject_id=selected.state_id) if item.validation_id == validation_id), None)
        if existing is not None:
            return existing
        simulation = report.get("simulation_report") if isinstance(report.get("simulation_report"), Mapping) else {}
        details = {
            "evidence_uri": evidence_uri,
            "report_sha256": digest,
            "phase1_passed": report.get("phase1_passed"),
            "phase2_passed": report.get("phase2_passed"),
            "num_vectors": report.get("num_vectors"),
            "precheck_vectors": report.get("precheck_vectors"),
            "mismatch_count": simulation.get("mismatch_count"),
            "protocol_mismatch_count": simulation.get("protocol_mismatch_count"),
            "infrastructure_failure": report.get("infrastructure_failure"),
        }
        fact = ValidationRecord(
            validation_id, run.run_id, selected.scope_id, selected.state_id,
            ValidationLevel.CERTIFICATION, status, details=details,
        )
        with store.transaction():
            store.add_validation(fact)
        return fact
