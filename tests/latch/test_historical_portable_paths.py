"""Offline location checks without importing or dispatching historical tools."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNERS = ("run_wp016b_core.py", "run_wp016b_expansion.py", "run_wp016b_expansion_a2.py")
PROTECTED = ("wp015-runs", "wp015c-runs", "wp015c-runtime-deps", "wp015-scientific-source-32d852a")


def _guard(name: str):
    tree = ast.parse((ROOT / "scripts" / name).read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_protected_prefixes")
    namespace = {"Path": Path, "os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), name, "exec"), namespace)
    return namespace["_protected_prefixes"]


@pytest.mark.parametrize("name", RUNNERS)
def test_missing_historical_workspace_refuses_protection_setup(name, monkeypatch):
    monkeypatch.delenv("LATCH_HISTORICAL_WORKSPACE", raising=False)
    with pytest.raises(RuntimeError, match="required for historical input protection"):
        _guard(name)()


@pytest.mark.parametrize("name", RUNNERS)
def test_missing_protected_material_refuses_protection_setup(name, tmp_path, monkeypatch):
    monkeypatch.setenv("LATCH_HISTORICAL_WORKSPACE", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        _guard(name)()


@pytest.mark.parametrize("name", RUNNERS)
def test_configured_historical_layout_preserves_protected_prefixes(name, tmp_path, monkeypatch):
    names = PROTECTED + (("wp016b-expansion-runtime",) if name.endswith("_a2.py") else ())
    for relative in names:
        (tmp_path / relative).mkdir()
    monkeypatch.setenv("LATCH_HISTORICAL_WORKSPACE", str(tmp_path))
    prefixes = _guard(name)()
    assert prefixes == tuple((tmp_path / relative).resolve() for relative in names)
    candidate = prefixes[0] / "new-output"
    assert any(candidate == path or candidate.is_relative_to(path) for path in prefixes)


def test_setup_helper_installs_existing_pyproject_from_independent_cwd(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "pip-arguments.txt"
    python = fake_bin / "python-fixture"
    python.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then\n  echo "Python 3.12.0"\nelse\n  printf "%s\\n" "$@" >> "$LATCH_TEST_PIP_ARGUMENTS"\nfi\n')
    python.chmod(0o700)
    java = fake_bin / "java"
    java.write_text('#!/bin/sh\necho \'openjdk version "21.0.1"\' >&2\n')
    java.chmod(0o700)
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "PYTHON_EXE": str(python), "LATCH_TEST_PIP_ARGUMENTS": str(calls),
    }
    result = subprocess.run(["bash", str(ROOT / "RapidWrightMCP/setup.sh")], cwd=tmp_path,
        env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["-m", "pip", "install", "-e", str(ROOT) + "[tools]"]
    assert (ROOT / "pyproject.toml").is_file()
    assert "requirements.txt" not in result.stdout
