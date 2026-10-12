import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mlcad_retry_is_single_bounded_class_rule():
    plan = json.loads((ROOT / "configs/wp014i/synthetic-mlcad-retry-v1.json").read_text())
    assert plan["identity"].endswith("CONTINUATION-03")
    assert [row["candidate_id"] for row in plan["priority_candidates"]] == ["mlcad_d181"]
    assert plan["limits"]["routing_attempts"] == 1
    assert plan["limits"]["route_workers"] == 1
    assert plan["limits"]["route_timeout_seconds"] == 7200
    rule = plan["route_timeout_class_policy"]
    assert rule["applies_to_every_candidate_meeting_predicate"] is True
    assert rule["qor_used"] is False
    assert rule["retry_count_authorised"] == 1
    assert plan["limits"]["provider_or_llm_calls"] == 0
    assert plan["limits"]["optimization_calls"] == 0
