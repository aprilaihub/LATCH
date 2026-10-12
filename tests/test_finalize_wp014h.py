from scripts.finalize_wp014h import object_hash


def test_manifest_object_hash_is_order_independent() -> None:
    assert object_hash({"b": 2, "a": 1}) == object_hash({"a": 1, "b": 2})


def test_wp014h_minimum_and_target_portfolio_arithmetic() -> None:
    development = 378
    transfer_per_root = 42
    heldout_per_root = 42
    assert development + 7 * transfer_per_root + 4 * heldout_per_root == 840
    assert development + 10 * transfer_per_root + 6 * heldout_per_root == 1050
