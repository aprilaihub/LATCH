"""Offline evidence projections; no live logs, tools or model requests."""
import ast
import base64
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from optimizer_pipeline.backends.xilinx.compiled_operations import instrumentation_markers
from experiments.dof.runtime.mechanism_evidence import mechanism_deltas, augment_execution
from experiments.dof.runtime.scientific_evidence import _canonical_hash
from experiments.acceptance.runner import public_dof


@pytest.mark.parametrize("witness,marker", [
    ("spatial_generated_geometry", "DOF_WP014C_ASSIGN_PBLOCK_V1:synthetic:1"),
    ("nonprefix_multi_net_batch", "DOF_WP014H_BATCH_ROUTE_NETS_V1:2:3:4"),
    ("coupled_hardblock_site_allocation", "DOF_WP014H_APPLY_ALLOCATION_V1:2:2"),
])
def test_historical_marker_projection_exact(tmp_path, witness, marker):
    # Extract only the pure historical function: never import its controllers.
    source = Path(__file__).resolve().parents[2] / "scripts/run_wp014h_strict_witnesses.py"
    tree = ast.parse(source.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "mechanism_deltas")
    namespace = dict(Path=Path, Any=Any, Mapping=Mapping, json=json, base64=base64,
        MARKER=re.compile(r'DOF_WP014[CH]_[A-Z0-9_]+_V[0-9]+:[^\r\n\"]+'), digest=_canonical_hash)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "historical-function", "exec"), namespace)
    rows = [{"output_b64": base64.b64encode(("irrelevant secret\n" + marker + "\n").encode()).decode()}]
    fixture = tmp_path / "private"
    fixture.mkdir()
    (fixture / "worker-call-outputs.json").write_text(json.dumps(rows))
    projected = instrumentation_markers(rows)
    assert projected == [marker]
    assert mechanism_deltas(projected, witness) == namespace["mechanism_deltas"](tmp_path, witness)


def test_augmentation_missing_markers_not_success():
    candidate = {"steps": [
        {"op": "CREATE_REGION", "bind": "region", "args": dict(x_min=0,x_max=1,y_min=0,y_max=0)},
        {"op": "ASSIGN_REGION", "args": {}}]}
    execution = {"canonical_endpoint": "VALID_COMPLETION"}
    compiled = SimpleNamespace(binding_records=({"binding_id": "region"},))
    result = augment_execution(candidate=candidate, compiled=compiled, execution=execution)
    assert result["binding_records"] == [{"binding_id": "region"}]
    assert result["mechanism_state_deltas"] == []
    assert execution == {"canonical_endpoint": "VALID_COMPLETION"}


def test_public_projection_keeps_strict_failure_without_private_evidence():
    value = public_dof({"strict_boundary_certificate": {"strict_boundary_exercised": False,
        "noncertificate_reason": "MECHANISM_SPECIFIC_DELTA_MISSING", "private_fixture": "secret"}})
    assert value["strict_g"]["strict_boundary_exercised"] is False
    assert "secret" not in json.dumps(value)
