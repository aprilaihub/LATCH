from __future__ import annotations

import hashlib

import pytest

from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry
from optimizer_pipeline.dof.single_submission.language import (
    CompileContext,
    ConstructionError,
    compile_g,
    compile_macro,
    compile_p,
    validate_construction,
)


def context() -> tuple[CompileContext, dict[str, str]]:
    registry = TrialHandleRegistry(
        trial_id="trial-language",
        root_sha256=hashlib.sha256(b"root-language").hexdigest(),
        secret=hashlib.sha256(b"test-language-secret").digest(),
    )
    handles = {
        "critical_cell": registry.register(object_type="cell", canonical_identity="private/cell/A", attributes={"object_type": "cell", "path_member": True, "path_rank": 1, "slack_ns": -0.2, "slr_id": 0}),
        "other_cell": registry.register(object_type="cell", canonical_identity="private/cell/B", attributes={"object_type": "cell", "path_member": False, "path_rank": 9, "slack_ns": 0.1, "slr_id": 1}),
        "net": registry.register(object_type="net", canonical_identity="private/net/N", attributes={"object_type": "net", "path_member": True, "path_rank": 1, "fanout": 100}),
        "hardblock": registry.register(object_type="hardblock", canonical_identity="private/dsp/D0", attributes={"object_type": "hardblock", "resource_type": "DSP", "path_member": True, "path_rank": 1, "slack_ns": -0.3, "slr_id": 0}),
        "region": registry.register(object_type="region", canonical_identity="private/region/R0", attributes={"bounds": [0, 2, 0, 2], "compatible_resource_types": ["DSP"]}),
        "site": registry.register(object_type="site", canonical_identity="private/site/DSP0", attributes={"compatible_resource_types": ["DSP"]}),
    }
    return CompileContext(registry, "ROUTED", (handles["region"],), (0, 10, 0, 10)), handles


def test_call_macro_embedding_has_identical_host_expansion() -> None:
    ctx, _ = context()
    direct = compile_macro("place_route", ctx)
    inherited = compile_p({"language": "P", "steps": [{"step_id": "s1", "op": "CALL_MACRO", "args": {"macro": "macro.place_route"}}]}, ctx)
    assert [step.public() for step in direct.host_steps] == [step.public() for step in inherited.host_steps]
    assert direct.heavy_calls == inherited.heavy_calls == 2
    assert direct.light_mutations == inherited.light_mutations == 2


def test_p_selector_executes_critical_semantics_and_spatial_program_is_nonempty() -> None:
    ctx, handles = context()
    program = {
        "language": "P",
        "steps": [
            {"step_id": "s1", "op": "SELECT_CRITICAL_CELLS", "args": {"top_k": 8}, "bind": "targets"},
            {"step_id": "s2", "op": "CREATE_PBLOCK", "args": {"predefined_region_id": handles["region"]}, "bind": "box"},
            {"step_id": "s3", "op": "ASSIGN", "args": {"target_handle": {"ref": "targets"}, "pblock_handle": {"ref": "box"}}},
            {"step_id": "s4", "op": "UNROUTE", "args": {}},
            {"step_id": "s5", "op": "UNPLACE", "args": {}},
            {"step_id": "s6", "op": "PLACE", "args": {"directive": "Default"}},
            {"step_id": "s7", "op": "ROUTE", "args": {"directive": "Default"}},
        ],
    }
    compiled = compile_p(program, ctx)
    assignment = next(step for step in compiled.host_steps if step.operation == "vivado.assign_pblock")
    assert assignment.selected_count == assignment.transformed_count == 1
    assert assignment.public_args["targets"] == [handles["critical_cell"]]
    assert handles["other_cell"] not in assignment.public_args["targets"]
    assert compiled.final_stage == "ROUTED" and compiled.heavy_calls == 2


def test_p_hardblock_relocation_is_typed_nonempty_and_requires_route() -> None:
    ctx, handles = context()
    program = {"language": "P", "steps": [
        {"step_id": "s1", "op": "SELECT_CRITICAL_HARDBLOCKS", "args": {"type": "DSP", "top_k": 4}, "bind": "hb"},
        {"step_id": "s2", "op": "RELOCATE_HARDBLOCK", "args": {"typed_target": {"ref": "hb"}, "destination": handles["site"]}},
        {"step_id": "s3", "op": "ROUTE", "args": {"directive": "Default"}},
    ]}
    compiled = compile_p(program, ctx)
    relocation = next(step for step in compiled.host_steps if step.operation == "vivado.relocate_hardblock")
    assert relocation.selected_count == relocation.transformed_count == 1
    assert compiled.final_stage == "ROUTED"


def test_g_executes_full_predicate_and_generated_geometry() -> None:
    ctx, handles = context()
    predicate = {"op": "AND", "children": [
        {"op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True},
        {"op": "COMPARE", "field": "slr_id", "operator": "EQ", "value": 0},
        {"op": "COMPARE", "field": "slack_ns", "operator": "LT", "value": 0.0},
    ]}
    program = {"language": "G", "statements": [
        {"statement_id": "g1", "node": "SELECT_WHERE", "args": {"source_type": "cell", "predicate": predicate, "top_k": 8}, "bind": "selected"},
        {"statement_id": "g2", "node": "CREATE_REGION", "args": {"x_min": 1, "x_max": 3, "y_min": 2, "y_max": 4}, "bind": "generated"},
        {"statement_id": "g3", "node": "ASSIGN_REGION", "args": {"target_handle": {"ref": "selected"}, "region_handle": {"ref": "generated"}}},
        {"statement_id": "g4", "node": "UNROUTE", "args": {}},
        {"statement_id": "g5", "node": "UNPLACE", "args": {}},
        {"statement_id": "g6", "node": "PLACE", "args": {"directive": "Default"}},
        {"statement_id": "g7", "node": "ROUTE", "args": {"directive": "Default"}},
    ]}
    compiled = compile_g(program, ctx)
    assignment = next(step for step in compiled.host_steps if step.operation == "vivado.assign_pblock")
    assert assignment.public_args["targets"] == [handles["critical_cell"]]
    assert compiled.transformed_count >= 2
    assert compile_g(program, ctx).public_bytes == compiled.public_bytes


def test_g_hardblock_generated_region_relocation_is_nonempty() -> None:
    ctx, handles = context()
    program = {"language": "G", "statements": [
        {"statement_id": "g1", "node": "SELECT_WHERE", "args": {"source_type": "hardblock", "predicate": {"op": "COMPARE", "field": "resource_type", "operator": "EQ", "value": "DSP"}, "top_k": 4}, "bind": "hb"},
        {"statement_id": "g2", "node": "RELOCATE_HARDBLOCK", "args": {"typed_target": {"ref": "hb"}, "candidate_sites": [handles["site"]], "site_index": 0}},
        {"statement_id": "g3", "node": "ROUTE", "args": {"directive": "Default"}},
    ]}
    compiled = compile_g(program, ctx)
    relocation = next(step for step in compiled.host_steps if step.operation == "vivado.relocate_hardblock")
    assert relocation.selected_count == relocation.transformed_count == 1


@pytest.mark.parametrize(
    "program,reason",
    [
        ({"language": "P", "steps": [{"step_id": "s1", "op": "SHELL", "args": {}}]}, "forbidden_operation"),
        ({"language": "G", "statements": [{"statement_id": "g1", "node": "EXEC", "args": {}}]}, "forbidden_generated_node"),
        ({"language": "G", "statements": [{"statement_id": "g1", "node": "CREATE_REGION", "args": {"x_min": -1, "x_max": 3, "y_min": 0, "y_max": 1}, "bind": "r"}]}, "generated_geometry_out_of_bounds"),
    ],
)
def test_containment_and_geometry_fail_closed(program, reason) -> None:
    ctx, _ = context()
    result = validate_construction(program, ctx)
    assert result["accepted"] is False
    assert result["feedback"] == [reason]


def test_predicate_node_and_static_heavy_budgets_fail_closed() -> None:
    ctx, _ = context()
    deep = {"op": "NOT", "child": {"op": "NOT", "child": {"op": "NOT", "child": {"op": "NOT", "child": {"op": "COMPARE", "field": "fanout", "operator": "GT", "value": 1}}}}}
    with pytest.raises(ConstructionError, match="predicate_depth"):
        compile_g({"language": "G", "statements": [{"statement_id": "g1", "node": "SELECT_WHERE", "args": {"source_type": "net", "predicate": deep, "top_k": 1}, "bind": "x"}]}, ctx)
    with pytest.raises(ConstructionError, match="static_heavy"):
        compile_p({"language": "P", "steps": [
            {"step_id": "s1", "op": "PHYS_OPT", "args": {"directive": "Explore", "options": []}},
            {"step_id": "s2", "op": "PHYS_OPT", "args": {"directive": "Explore", "options": []}},
            {"step_id": "s3", "op": "PHYS_OPT", "args": {"directive": "Explore", "options": []}},
            {"step_id": "s4", "op": "PHYS_OPT", "args": {"directive": "Explore", "options": []}},
            {"step_id": "s5", "op": "PHYS_OPT", "args": {"directive": "Explore", "options": []}},
        ]}, ctx)
