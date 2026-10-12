import json
from pathlib import Path


def test_development_manifest_has_fourteen_hash_bound_roots() -> None:
    repo = Path(__file__).resolve().parents[1]
    value = json.loads((repo / "configs/wp014i/development-known14-v1.json").read_text())
    assert len(value["roots"]) == 14
    assert len({row["candidate_id"] for row in value["roots"]}) == 14
    assert {row["resource_class"] for row in value["roots"]} == {"CORE", "LARGE"}
    assert sum(row["resource_class"] == "LARGE" for row in value["roots"]) == 3
    assert value["limits"]["optimization_calls"] == 0
    assert value["limits"]["holdout_or_transfer_accesses"] == 0
