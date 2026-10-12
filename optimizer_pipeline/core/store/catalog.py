"""Rebuildable workspace catalog derived from canonical per-run databases."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .query import load_run_view


CATALOG_SCHEMA = """
CREATE TABLE catalog_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (
    run_db TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    selected_state_id TEXT,
    selected_output_uri TEXT,
    attempt_count INTEGER NOT NULL,
    integrity_errors_json TEXT NOT NULL
);
CREATE INDEX idx_catalog_run_id ON runs(run_id);
"""


def rebuild_workspace_catalog(output: str | Path, roots: Iterable[str | Path]) -> int:
    """Replace ``output`` from discovered run.db files and return run count."""

    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".rebuild")
    temporary.unlink(missing_ok=True)
    candidates: set[Path] = set()
    for root_value in roots:
        root = Path(root_value).resolve()
        if root.is_file() and root.name == "run.db":
            candidates.add(root)
        elif root.is_dir():
            candidates.update(path.resolve() for path in root.rglob("run.db"))
    candidates.discard(output_path)

    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(CATALOG_SCHEMA)
        connection.execute(
            "INSERT INTO catalog_metadata(key, value) VALUES (?, ?)",
            ("contract", "rebuildable-workspace-catalog.v1"),
        )
        count = 0
        for run_db in sorted(candidates):
            try:
                view = load_run_view(run_db)
            except Exception:
                continue
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(run_db), view["run_id"], view["mode"], view["run_status"],
                    view.get("selected_state_id"), view.get("selected_output_uri"),
                    int(view.get("attempt_count") or 0),
                    json.dumps(view.get("integrity_errors") or [], sort_keys=True),
                ),
            )
            count += 1
        connection.commit()
    finally:
        connection.close()
    temporary.replace(output_path)
    return count
