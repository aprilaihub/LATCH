"""Keep the acceptance code reviewable without exposing local run artifacts."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE_FILES = (
    "README.md", "__init__.py", "__main__.py", "config.py", "runner.py",
    "smoke-april.example.json", "templates.py",
)


def ignored(path: str) -> bool:
    if shutil.which("git") is None:
        pytest.skip("Git ignore checks require Git")
    probe = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip("Git ignore checks require a repository checkout")
    result = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "--no-index", path],
        capture_output=True, text=True,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


@pytest.mark.parametrize("name", ACCEPTANCE_FILES)
def test_acceptance_sources_are_visible_to_git(name):
    path = f"experiments/acceptance/{name}"
    assert (ROOT / path).is_file()
    assert not ignored(path), path


@pytest.mark.parametrize("path", (
    "experiments/acceptance/data/trials.csv",
    "experiments/acceptance/outputs/batch.db",
    "experiments/acceptance/__pycache__/runner.cpython-312.pyc",
    "outputs/acceptance/batch.db",
    "experiments/acceptance/input.dcp",
    "experiments/acceptance/tool.log",
    "experiments/acceptance/.env",
))
def test_local_inputs_outputs_and_secrets_stay_ignored(path):
    assert ignored(path), path
