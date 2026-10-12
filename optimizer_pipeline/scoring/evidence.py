"""Shared score helpers extracted without changing the historical schema."""
from __future__ import annotations
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping

# Preserve the existing result schema consumed by historical score readers.
SCIENTIFIC_EVIDENCE_VERSION = "dof.scientific-evidence.v2"

def file_sha256(path: str | Path | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def score_fmax_delta(
    *,
    root_fmax_mhz: float | None,
    child_fmax_mhz: float | None,
    root_sha256: str | None,
    child_sha256: str | None,
    epsilon_fmax: float,
) -> dict[str, Any]:
    epsilon = float(epsilon_fmax)
    if not math.isfinite(epsilon) or epsilon < 0:
        raise ValueError("epsilon_fmax_must_be_finite_and_nonnegative")
    byte_identical = bool(root_sha256 and child_sha256 and root_sha256 == child_sha256)
    if root_fmax_mhz is None or child_fmax_mhz is None:
        raw_delta = effective_delta = None
    else:
        raw_delta = float(child_fmax_mhz) - float(root_fmax_mhz)
        if not math.isfinite(raw_delta):
            raise ValueError("fmax_delta_nonfinite")
        effective_delta = 0.0 if byte_identical or abs(raw_delta) <= epsilon else raw_delta
    return {
        "schema_version": SCIENTIFIC_EVIDENCE_VERSION,
        "root_sha256": root_sha256,
        "child_sha256": child_sha256,
        "byte_identical_child": byte_identical,
        "epsilon_fmax": epsilon,
        "measurement_delta_fmax_mhz": raw_delta,
        "delta_fmax_mhz": effective_delta,
    }
