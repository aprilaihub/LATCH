#!/usr/bin/env python3
"""Historical WP-014H evidence helpers retained for import compatibility.

The original report-building command requires its external historical bundle.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def object_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    core = dict(value)
    core["manifest_sha256"] = object_hash(core)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(core, indent=2, sort_keys=True) + "\n")
    return core


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


if __name__ == "__main__":
    raise SystemExit("Use this module as evidence helpers; the historical report CLI requires its external bundle.")
