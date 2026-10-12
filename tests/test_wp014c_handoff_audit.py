from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "audit_wp014c_handoff.py"
SPEC = importlib.util.spec_from_file_location("audit_wp014c_handoff", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_embedded_hash_round_trip() -> None:
    body = {"accepted": True, "rows": [1, 2, 3]}
    value = body | {"aggregate_sha256": MODULE.digest(body)}
    assert MODULE.verify_embedded(value, "aggregate_sha256")


def test_embedded_hash_detects_mutation() -> None:
    body = {"accepted": True}
    value = body | {"audit_sha256": MODULE.digest(body)}
    value["accepted"] = False
    assert not MODULE.verify_embedded(value, "audit_sha256")
