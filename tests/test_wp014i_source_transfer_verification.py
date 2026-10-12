import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_source_transfer_verification_is_hash_only_and_exact_five():
    plan = json.loads(
        (ROOT / "configs/wp014i/source-transfer-verification-v1.json").read_text()
    )
    assert len(plan["required_candidate_ids"]) == 5
    assert len(set(plan["required_candidate_ids"])) == 5
    assert all(value == 0 for value in plan["limits"].values())
    text = (ROOT / "scripts/verify_wp014i_source_transfer.py").read_text()
    assert "V0_PASS" in text
    assert "MULTISEED_FUNCTIONAL_PASS" in text
    assert "VIVADO_EXEC" not in text
