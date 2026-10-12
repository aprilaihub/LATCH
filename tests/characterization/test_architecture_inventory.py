from __future__ import annotations

from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[2]


def run_audit(script: str) -> str:
    return subprocess.run(
        [sys.executable, str(REPO / "tools" / script), "--repo", str(REPO)],
        cwd=REPO,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout


def test_generated_entry_inventory_is_current_and_covers_primary_surfaces():
    report = run_audit("audit_entries.py")
    # Inspect live entry surfaces, not a checked-in internal audit snapshot.
    assert report.strip()
    for expected in (
        "dcp_optimizer.py",
        "validate_dcps.py",
        "scripts/run_fixed_sequence_expansion.py",
        "scripts/run_frontier_expansion.py",
        "VivadoMCP/vivado_mcp_server.py",
        "experiments/promptsearch/cli.py",
        "Makefile",
    ):
        assert f"| {expected} |" in report


def test_generated_artifact_inventory_is_current_and_covers_hardcut_owners():
    report = run_audit("audit_artifact_consumers.py")
    # Keep structural owner/record coverage after retiring report documents.
    assert report.strip()
    for expected in (
        "optimizer_pipeline/run/context.py",
        "optimizer_pipeline/apps/fpl_contest/entry.py",
        "optimizer_pipeline/capabilities/intervention/controller.py",
        "tests/test_contest_entry.py",
    ):
        assert f"| {expected} |" in report
    for family in (
        "transition journal",
        "state graph",
        "runtime projection",
        "execution result",
        "pipeline summary",
        "contest publication",
        "analysis records",
        "rescue records",
    ):
        assert family in report
