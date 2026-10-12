from __future__ import annotations

from dataclasses import replace

import pytest

import scripts.run_wp013b_e0 as synthetic
from optimizer_pipeline.dof.openrouter import build_openrouter_outbound
from optimizer_pipeline.dof.single_submission.contract_v5 import compile_candidate_v5
from optimizer_pipeline.dof.single_submission.contract_v6 import language_condition_error
from optimizer_pipeline.dof.single_submission.observation_v4 import CommonQuerySurfaceV4
from scripts.run_wp014c import (
    CASES, CASE_CONDITION, binding, candidate_for_case, case_is_satisfied,
)


@pytest.mark.parametrize("case", CASES)
def test_every_gate4_case_is_construct_and_compiler_admitted(case: str) -> None:
    _observation, _registry, context, _max_q = synthetic._synthetic_state("test-" + case)
    context = replace(context, fanout_admitted=True, action_surface_version="v3")
    candidate = candidate_for_case(case, context)
    assert language_condition_error(candidate, CASE_CONDITION[case]) is None
    compiled = compile_candidate_v5(candidate, context)
    assert compiled.heavy_calls <= 4
    assert compiled.light_mutations <= 8
    assert case_is_satisfied(case, candidate)


def test_openrouter_accepts_exact_wp014c_v3_interface_triple() -> None:
    request = binding(
        session_id="test", model="openai/gpt-5.6-luna",
        context=b"{}", prompt=b"{}", catalogue_hash="0" * 64, reserve=0.08,
    )
    assert build_openrouter_outbound(request).body["model"] == "openai/gpt-5.6-luna"


def test_wp014c_query_surface_preserves_eight_items_with_bounded_bytes() -> None:
    _observation, registry, _context, _max_q = synthetic._synthetic_state("test-query-v4")
    surface = CommonQuerySurfaceV4(registry)
    assert surface.max_queries == 8
    assert surface.catalogue["session_result_max_bytes"] == 16_384
