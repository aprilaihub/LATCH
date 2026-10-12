from __future__ import annotations

import hashlib

from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry
from scripts.run_wp014c_admission import select_compatible_cell_pair


def test_cell_pair_prefers_largest_resource_group_then_canonical_identity() -> None:
    registry = TrialHandleRegistry(
        trial_id="test", root_sha256="0" * 64, secret=hashlib.sha256(b"test").digest(),
    )

    def cell(name: str, resource: str) -> tuple[str, str, str]:
        handle = registry.register(
            object_type="cell", canonical_identity=name, attributes={"resource_type": resource},
        )
        return handle, "site-handle", resource

    rows = [
        cell("z_lut6", "LUT6"), cell("a_lut6", "LUT6"),
        cell("z_lut3", "LUT3"), cell("b_lut3", "LUT3"), cell("a_lut3", "LUT3"),
    ]
    selected = select_compatible_cell_pair(rows, registry)
    assert [registry.resolve(row[0], expected_type="cell").canonical_identity for row in selected] == [
        "a_lut3", "b_lut3",
    ]
