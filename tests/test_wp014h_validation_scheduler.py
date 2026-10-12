from optimizer_pipeline.dof.validation_scheduler import (
    GIB,
    MemoryAwareValidationScheduler,
    MemoryAwareSeedScheduler,
    ValidationCase,
    ValidationSeed,
    root_round_robin,
    seed_root_round_robin,
)


def test_round_robin_interleaves_roots() -> None:
    cases = [
        ValidationCase("a1", "a", "normal"),
        ValidationCase("a2", "a", "normal"),
        ValidationCase("b1", "b", "high"),
        ValidationCase("c1", "c", "unknown"),
    ]
    assert [row.case_id for row in root_round_robin(cases)] == ["a1", "b1", "c1", "a2"]


def test_refill_targets_16_but_preserves_twenty_percent() -> None:
    scheduler = MemoryAwareValidationScheduler(
        [ValidationCase(f"c{i}", f"r{i % 4}", "high" if i < 2 else "normal") for i in range(16)],
    )
    selected = scheduler.refill(running=[], total_memory=1500 * GIB, available_memory=1400 * GIB)
    assert len(selected) == 16
    constrained = MemoryAwareValidationScheduler([ValidationCase("large", "r", "high")])
    assert constrained.refill(running=[], total_memory=100 * GIB, available_memory=70 * GIB) == []


def test_seed_scheduler_uses_case_seed_units_and_root_round_robin() -> None:
    units = [
        ValidationSeed("a", "root-a", "s0", 1, "normal"),
        ValidationSeed("a", "root-a", "s1", 2, "normal"),
        ValidationSeed("b", "root-b", "s0", 1, "normal"),
        ValidationSeed("b", "root-b", "s1", 2, "normal"),
    ]
    assert [(row.case_id, row.seed_id) for row in seed_root_round_robin(units)] == [
        ("a", "s0"), ("b", "s0"), ("a", "s1"), ("b", "s1"),
    ]
    scheduler = MemoryAwareSeedScheduler(units, maximum=4, target=4)
    selected = scheduler.refill(
        running=[], total_memory=100 * GIB, available_memory=90 * GIB,
    )
    assert len(selected) == 4


def test_seed_scheduler_accepts_twenty_but_rejects_twenty_one() -> None:
    units = [ValidationSeed(f"c{i}", f"r{i}", "s0", i, "normal") for i in range(21)]
    scheduler = MemoryAwareSeedScheduler(units[:20], maximum=20, target=20)
    assert len(scheduler.refill(
        running=[], total_memory=1500 * GIB, available_memory=1400 * GIB,
    )) == 20
    try:
        MemoryAwareSeedScheduler(units, maximum=21, target=21)
    except ValueError as error:
        assert str(error) == "validation_seed_concurrency_must_be_1_to_20"
    else:
        raise AssertionError("twenty-one seed workers must remain rejected")
