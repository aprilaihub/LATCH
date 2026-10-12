import hashlib
import json
from pathlib import Path
import subprocess
import sys

from optimizer_pipeline.core.store import load_run_view, SQLiteRunStore


REPO = Path(__file__).resolve().parents[1]


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def test_historical_import_is_read_only_queryable_and_idempotent(tmp_path: Path):
    # Keep this unit test self-contained.  The original multi-megabyte golden
    # experiment directory is intentionally ignored/untracked; importer
    # correctness must not depend on a developer's local archive.
    source = tmp_path / "legacy-valid"
    source.mkdir()
    (source / "pipeline_summary.json").write_text(json.dumps({
        "mode": "legacy-test",
        "input_dcp": "missing-root.dcp",
        "root_metrics": {"wns": -1.0},
    }))
    (source / "transition_journal.jsonl").write_text(json.dumps({
        "intent": {
            "transition_id": "node_001_phys_opt",
            "parent_node_id": "node_000_root",
            "optimizer_name": "phys_opt",
            "optimizer_params": {"directive": "Explore"},
            "source": "legacy",
        },
        "outcome": {
            "success": True,
            "child_node_id": "node_001_phys_opt",
            "output_dcp": "missing-child.dcp",
            "validation_status": "CLEAN",
            "metrics": {"wns": -0.5},
        },
        "handling": {"updated_incumbent": True},
    }) + "\n")
    output = tmp_path / "imported.db"
    before = _tree_digest(source)
    command = [sys.executable, "tools/migrate_v1_run.py", str(source), str(output)]

    first = subprocess.run(command, cwd=REPO, text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    view = load_run_view(output)
    assert view["attempt_count"] == 1
    assert view["attempts"][0]["action_type"] == "phys_opt"
    assert view["attempts"][0]["validation_status"] == "CLEAN"
    assert view["selected_state_id"] == "node_001_phys_opt"
    assert view["integrity_errors"] == []
    with SQLiteRunStore(output, migrate=False) as store:
        metadata = store.get_run().metadata.to_dict()
        assert metadata["historical_import"] is True
        assert metadata["historical_source"] == str(source.resolve())
        fact_counts = tuple(store.connection.execute(
            "SELECT (SELECT count(*) FROM attempts), (SELECT count(*) FROM states), (SELECT count(*) FROM selections)"
        ).fetchone())

    second = subprocess.run(command, cwd=REPO, text=True, capture_output=True)
    assert second.returncode == 0, second.stderr
    with SQLiteRunStore(output, migrate=False) as store:
        repeated_counts = tuple(store.connection.execute(
            "SELECT (SELECT count(*) FROM attempts), (SELECT count(*) FROM states), (SELECT count(*) FROM selections)"
        ).fetchone())
    assert repeated_counts == fact_counts
    assert _tree_digest(source) == before


def test_historical_import_preserves_unknowns_and_orphan_attempt(tmp_path: Path):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "pipeline_summary.json").write_text(json.dumps({"mode": "legacy-test", "root_metrics": {}}))
    (source / "transition_journal.jsonl").write_text(json.dumps({
        "intent": {
            "transition_id": "node_001_unknown",
            "parent_node_id": "node_000_root",
            "optimizer_name": "unknown_action",
            "source": "legacy",
        },
        "outcome": None,
        "handling": {"schedule_stage": "scheduled"},
    }) + "\n")
    output = tmp_path / "orphan.db"

    proc = subprocess.run(
        [sys.executable, "tools/migrate_v1_run.py", str(source), str(output)],
        cwd=REPO, text=True, capture_output=True,
    )

    assert proc.returncode == 0, proc.stderr
    view = load_run_view(output)
    assert view["selected_output_uri"] == "unknown://input-dcp"
    assert view["orphan_attempt_ids"] == ["attempt:node_001_unknown"]
    assert view["budget"] == {"reserved": 1, "completed": 0, "failed": 0}
