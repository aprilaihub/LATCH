import hashlib
import json
from pathlib import Path

from optimizer_pipeline.dof.single_submission.contract_v5 import (
    SYNTAX_EXAMPLES, compile_candidate_v5, contract_for_level,
)
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry
from optimizer_pipeline.dof.single_submission.language import CompileContext
from optimizer_pipeline.dof.hardblock_compatibility import build_relation
from optimizer_pipeline.dof.wp015_provider import (
    FINAL_ANSWER_CAPACITY_TOKENS, TOTAL_OUTPUT_TOKEN_CEILING,
)
from optimizer_pipeline.dof.wp015a_strict_g import (
    STRICT_FAMILIES, spatial_generated_region_precheck, strict_family_feedback,
)


ROOT = Path(__file__).resolve().parents[1]


def context() -> CompileContext:
    registry = TrialHandleRegistry(
        trial_id="wp015a-strict-tests",
        root_sha256=hashlib.sha256(b"root").hexdigest(),
        secret=hashlib.sha256(b"secret").digest(),
    )
    registry.register(object_type="cell", canonical_identity="cell/0", attributes={"path_member": True, "path_rank": 1, "resource_type": "LUT3"})
    for index in range(4):
        registry.register(object_type="net", canonical_identity=f"net/{index}", attributes={"fanout": 8 - index, "path_rank": index + 1})
    sites = [
        registry.register(object_type="site", canonical_identity=f"RAMB36_X0Y{index}", attributes={"compatible_resource_types": ["BRAM"], "site_class": "RAMB36"})
        for index in range(4)
    ]
    hardblocks = [
        registry.register(object_type="hardblock", canonical_identity=f"bram/{index}", attributes={"resource_type": "BRAM", "path_rank": index + 1, "compatible_site_handles": sites})
        for index in range(2)
    ]
    relation = build_relation(
        root_sha256=registry.root_sha256, stage="ROUTED",
        target_rows=[
            {"handle": handle, "target_identity_sha256": str(index + 1) * 64, "resource_type": "BRAM", "compatible_site_handles": sites}
            for index, handle in enumerate(hardblocks)
        ],
        target_query_sha256="3" * 64,
        target_site_query_sha256={handle: str(index + 4) * 64 for index, handle in enumerate(hardblocks)},
    )
    return CompileContext(
        registry, "ROUTED", (), (0, 1, 0, 4), fanout_admitted=True,
        action_surface_version="v3",
        hardblock_compatibility_relation=relation,
    )


def test_all_three_public_strict_examples_compile_with_production_validator() -> None:
    examples = {
        "SPATIAL_GENERATED_GEOMETRY": "G_BOUNDARY_SPATIAL",
        "NONPREFIX_MULTI_NET_BATCH": "G_BOUNDARY_NONPREFIX_MULTI_NET",
        "COUPLED_HARDBLOCK_SITE_ALLOCATION": "G_BOUNDARY_COUPLED_HARDBLOCK",
    }
    for family, name in examples.items():
        candidate = SYNTAX_EXAMPLES[name]["candidate"]
        compiled = compile_candidate_v5(candidate, context())
        assert compiled.language == "G"
        assert strict_family_feedback(candidate, required_family=family) is None


def test_contract_has_equal_nonrecommendational_strict_cards() -> None:
    contract = contract_for_level("L4")
    cards = contract["G"]["strict_family_cards"]
    assert tuple(card["mechanism_family"] for card in cards) == STRICT_FAMILIES
    assert all(card["qor_or_target_recommendation_present"] is False for card in cards)
    assert all(len(card["legal_structure"]) >= 3 for card in cards)


def test_spatial_precheck_rejects_unknown_or_insufficient_capacity() -> None:
    ctx = context()
    candidate = SYNTAX_EXAMPLES["G_BOUNDARY_SPATIAL"]["candidate"]
    compiled = compile_candidate_v5(candidate, ctx)
    unknown = spatial_generated_region_precheck(compiled=compiled, context=ctx, capacity_by_region={})
    assert unknown["reason"] == "generated_region_capacity_unknown"
    insufficient = spatial_generated_region_precheck(
        compiled=compiled, context=ctx,
        capacity_by_region={"0:0:0:0": {"LUT3": 0}},
    )
    assert insufficient["reason"] == "generated_region_compatible_site_capacity_insufficient"
    accepted = spatial_generated_region_precheck(
        compiled=compiled, context=ctx,
        capacity_by_region={"0:0:0:0": {"LUT3": 1}},
    )
    assert accepted["accepted"] is True and accepted["qor_fields_used"] == []


def test_output_envelope_is_common_bounded_and_zero_repair() -> None:
    value = json.loads((ROOT / "configs/wp015a/provider-output-envelope-v1.json").read_text())
    assert FINAL_ANSWER_CAPACITY_TOKENS == value["common_final_answer_capacity_tokens"] == 4096
    assert TOTAL_OUTPUT_TOKEN_CEILING == value["hard_total_generated_token_ceiling"] == 12288
    assert all(row["value"] == 12288 for row in value["profiles"].values())
    assert value["retry_count"] == value["fallback_count"] == value["response_repair_count"] == 0


def test_runtime_model_order_is_fixed_diverse_and_bounded() -> None:
    value = json.loads((ROOT / "configs/wp015a/strict-runtime-model-order-v1.json").read_text())
    assert tuple(value["orders"]) == STRICT_FAMILIES
    assert value["max_backend_candidates_per_mechanism"] == 2
    assert [value["orders"][family][0].split("/", 1)[0] for family in STRICT_FAMILIES] == [
        "openai", "anthropic", "deepseek",
    ]


def test_scientific_controller_instruments_strict_state_deltas_before_projection() -> None:
    source = (ROOT / "scripts/run_wp015_cells.py").read_text()
    assert 'execution["mechanism_state_deltas"] = mechanism_deltas' in source
    assert source.index('execution["mechanism_state_deltas"] = mechanism_deltas') < source.index(
        'strict = strict_boundary_certificate(candidate=candidate, execution_started=True, execution=execution)'
    )
