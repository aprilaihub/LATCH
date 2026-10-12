import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module,option", [
    ("experiments.ablation.analysis", "--input-root"),
    ("experiments.dof.analysis.plot", "--input"),
])
def test_plot_output_cannot_overwrite_input(tmp_path, module, option):
    result = subprocess.run([sys.executable, "-m", module, option, str(tmp_path),
                             "--output", str(tmp_path / "figures")], capture_output=True, text=True)
    assert result.returncode == 2
    assert "Input and output trees must be separate" in result.stderr
    assert not (tmp_path / "figures").exists()


def test_dof_nested_mock_output_is_created(tmp_path):
    output = tmp_path / "new-parent" / "run"
    result = subprocess.run([sys.executable, "-m", "experiments.dof", "--condition", "recipe",
                             "--mode", "mock", "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    assert facts["physical_valid"] is None
    assert facts["functional_validation"]["status"] == "NOT_RUN"
    assert (output / "run.db").is_file()


def test_knowledge_data_is_in_package_metadata():
    root = Path(__file__).resolve().parents[2]
    assert '"experiments.ablation" = ["configs/*.json", "runtime/knowledge/data/*.json"]' in (root / "pyproject.toml").read_text()
