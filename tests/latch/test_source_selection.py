"""Source-copy boundaries; no tools, network or credential contents."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("latch_source_selection", ROOT / "tools/export_source.py")
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


@pytest.mark.parametrize("path", ["optimizer_pipeline/core/store.py", "validate_dcps.py",
    "mcp_stdio_asyncio.py", "scripts/e1_score_dcp.tcl", "experiments/ablation/runtime/knowledge/data/reference_manual_final.json",
    "integrations/llm/runtime/data/providers/openai.json", "examples/latch/tools.example.json",
    "configs/wp014e/provider-profiles-v1.json", "configs/wp014e/pricing-snapshot-2026-08-12.json",
    ".research-flow/source/WP015-Pricing-Snapshot-2026-08-14.json",
    "configs/experiments/dof_contract/mock_root.mockdcp", "docs/latch-harness.md",
    "experiments/dof/prompts/request.md", "experiments/acceptance/README.md",
    "docs/reference/cli-reference.md", "docs/reference/glossary.md",
    "experiments/tuning/runner.py", "LICENSE-MCP-MIT.txt", "LICENSING.md", "CONTRIBUTORS.md",
    "docs/assets/README.md", "docs/assets/latch-overview.png", "docs/assets/latch-overview.pdf",
    "docs/assets/dof-results.png", "docs/assets/dof-results.pdf",
    "docs/assets/ablation-results.png", "docs/assets/ablation-results.pdf"])
def test_runtime_source_and_resources_are_selected(path):
    assert source.source_path(path)


@pytest.mark.parametrize("path", [".git/config", "private/notes.md", "experiments/dof/outputs/run.db",
    "experiments/llm/data/trials.json", "experiments/dof/context.json", "scripts/.env", "scripts/input.dcp",
    "tests/fixtures/capture.json", "experiments/ablation/raw/input.json", "RapidWright/jars/secret.jar",
    "configs/wp014e/secret-context.json", "../outside.py", "/outside.py",
    "archive/README.md", "docs/refactor/entry-inventory.md", "docs/dev_journal/new.md",
    "docs/new-internal-report.md", "experiments/acceptance/CONTINUATION.md",
    "experiments/acceptance/new-run-report.md", "docs/assets/other.png",
    "docs/assets/full-manuscript.pdf", "docs/assets/internal.md",
    "scripts/package_wp014h.py", "scripts/package_wp014i.py",
    "scripts/package_wp015_handoff.py", "scripts/build_paper_evidence_capsule_pass2.py",
    "scripts/new_internal_review.py"])
def test_runtime_data_credentials_and_history_are_not_selected(path):
    assert not source.source_path(path)


def test_existing_or_nested_destination_is_rejected_before_copy(tmp_path):
    with pytest.raises(ValueError, match="independent"):
        source.export(tmp_path, root=tmp_path)
    with pytest.raises(ValueError, match="independent"):
        source.export(tmp_path / "copy", root=tmp_path)


def test_export_returns_internal_metadata_without_delivering_it(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "README.md").write_text("# Public usage\n")
    def git_output(argv, **kwargs):
        if "ls-files" in argv:
            return b"README.md\0"
        if "rev-parse" in argv:
            return "test-revision\n"
        return ""
    monkeypatch.setattr(source.subprocess, "check_output", git_output)
    target = tmp_path / "delivery"
    report = source.export(target, root=root)
    assert report["source_revision"] == "test-revision"
    assert report["included_files"] == ["README.md"]
    assert sorted(p.name for p in target.iterdir()) == ["README.md"]
    assert (target / "README.md").read_text() == (root / "README.md").read_text()


def test_source_inventory_refuses_worktree_report_file(tmp_path, monkeypatch):
    from tools import source_inventory
    monkeypatch.setattr(source_inventory, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["source_inventory", "--output", str(tmp_path / "internal.md")])
    with pytest.raises(SystemExit) as stopped:
        source_inventory.main()
    assert stopped.value.code == 2
    assert not (tmp_path / "internal.md").exists()


def test_source_inventory_writes_only_new_external_report(tmp_path, monkeypatch):
    from tools import source_inventory
    monkeypatch.setattr(source_inventory, "ROOT", tmp_path / "source")
    monkeypatch.setattr(source_inventory, "inventory", lambda: [])
    target = tmp_path / "external.md"
    monkeypatch.setattr("sys.argv", ["source_inventory", "--format", "markdown", "--output", str(target)])
    source_inventory.main()
    assert "outside the worktree" in target.read_text()
    with pytest.raises(FileExistsError):
        source_inventory.main()


def test_fresh_git_keeps_runtime_bridges_not_generated_data(tmp_path):
    import subprocess
    (tmp_path / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    selected = ["experiments/__init__.py", "experiments/llm/runtime/client.py",
                "experiments/tuning/collector.py", "experiments/dof/runtime/prepared.py"]
    forbidden = ["experiments/llm/data/results.json", "experiments/tuning/outputs/result.json",
                 "run.db", "run.sqlite3", "capture.dcp", ".env"]
    for name in selected + forbidden:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("synthetic fixture")
    result = subprocess.run(["git", "-C", str(tmp_path), "-c", "core.excludesFile=/dev/null",
        "check-ignore", "--no-index", "--stdin"], input="\n".join(selected + forbidden) + "\n",
        capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert set(result.stdout.splitlines()) == set(forbidden)
