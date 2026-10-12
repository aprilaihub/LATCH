"""Organizer-only mocks: no EDA, model, network or historical run reads."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from experiments.acceptance import prepared


@pytest.mark.parametrize("fail", [None, "capture", "direct"])
def test_prepared_acceptance_no_ledger_no_replay(tmp_path, fail):
    root = tmp_path / "root.dcp"
    root.write_bytes(b"synthetic")
    output = tmp_path / "fresh"
    cfg = dict(input=str(root), output=str(tmp_path / "original"), tools={})
    calls = []
    class Batch:
        def __init__(self, config, path):
            self.output = Path(config["output"])
            assert config["ledger_path"] == str(self.output / "batch.db")
        def versions(self): calls.append("versions")
        def golden_self(self): calls.append("golden")
        def capture(self):
            calls.append("capture")
            if fail == "capture": raise RuntimeError("fixture")
        def dof(self, condition, *, paid):
            assert paid is False
            calls.append(condition)
            if fail == condition: raise RuntimeError("fixture")
            child = self.output / ("fixed-" + condition)
            child.mkdir()
            (child / "summary.json").write_text(json.dumps(dict(root_unchanged=True)))
    with patch.object(prepared, "validate_tools_config"), patch.object(prepared, "AcceptanceBatch", Batch):
        result = prepared.run(cfg, tmp_path / "config.json", output)
        assert result["model_requests"] == 0
        assert not (output / "batch.db").exists()
        assert result["original_input_unchanged"] is True
        assert result["status"] == ("CONDITIONS_FINISHED_COVERAGE_INCOMPLETE" if fail is None else "INFRASTRUCTURE_OR_UNREACHED")
        assert calls == (["versions", "golden", "capture"] if fail == "capture" else
                         ["versions", "golden", "capture", "recipe", "direct"] if fail else
                         ["versions", "golden", "capture", "recipe", "direct", "constructed"])
        with pytest.raises(ValueError, match="Fresh independent"):
            prepared.run(cfg, tmp_path / "config.json", output)
