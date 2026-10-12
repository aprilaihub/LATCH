from __future__ import annotations

import hashlib
import json

import jsonschema
import pytest

from experiments.dof.protocol.action_surface_v3 import (
    G_EXCLUSIVE_NODES,
    PHYS_OPT_DIRECTIVES,
    PHYS_OPT_OPTIONS,
    PLACE_DIRECTIVES,
    PLACE_NET_DELAY_WEIGHTS,
    P_OPERATION_REGISTRY,
    ROUTE_DIRECTIVES,
    public_registry,
    registry_sha256,
    worker_meanings,
)
from experiments.dof.protocol.contract_v5 import (
    G_PROGRAM_SCHEMA,
    P_CARDS,
    P_PROGRAM_SCHEMA,
    compile_candidate_v5,
    contract_for_level,
    compact_cards,
)
from experiments.dof.protocol.contract_v6 import (
    language_condition_error,
)
from experiments.dof.protocol.handles import TrialHandleRegistry
from experiments.dof.protocol.language import CompileContext, ConstructionError
from optimizer_pipeline.dof.hardblock_compatibility import build_relation
from optimizer_pipeline.dof.vivado_mutation_worker import (
    _ALLOWED_OPERATIONS,
    _DIRECTIVES,
    _PHYS_DIRECTIVES,
    _PHYS_OPTIONS,
    _PLACE_WEIGHTS,
    request_bytes,
)


def context() -> tuple[CompileContext, dict[str, str]]:
    registry = TrialHandleRegistry(
        trial_id="wp014c-test",
        root_sha256=hashlib.sha256(b"wp014c-root").hexdigest(),
        secret=hashlib.sha256(b"wp014c-secret").digest(),
    )
    values = {
        "cell1": registry.register(object_type="cell", canonical_identity="cell/A", attributes={"path_member": True, "path_rank": 1, "resource_type": "SLICE"}),
        "cell2": registry.register(object_type="cell", canonical_identity="cell/B", attributes={"path_member": True, "path_rank": 2, "resource_type": "SLICE"}),
        "net": registry.register(object_type="net", canonical_identity="net/N", attributes={"path_member": True, "path_rank": 1, "fanout": 64}),
        "net2": registry.register(object_type="net", canonical_identity="net/O", attributes={"path_member": True, "path_rank": 2, "fanout": 32}),
        "net3": registry.register(object_type="net", canonical_identity="net/P", attributes={"path_member": False, "path_rank": 3, "fanout": 16}),
        "site": registry.register(object_type="site", canonical_identity="DSP48E2_X0Y1", attributes={"compatible_resource_types": ["DSP", "SLICE"]}),
        "site2": registry.register(object_type="site", canonical_identity="DSP48E2_X0Y2", attributes={"compatible_resource_types": ["DSP", "SLICE"]}),
        "site3": registry.register(object_type="site", canonical_identity="DSP48E2_X0Y3", attributes={"compatible_resource_types": ["DSP", "SLICE"]}),
        "region": registry.register(object_type="region", canonical_identity="X0Y0", attributes={"bounds": [0, 0, 0, 0]}),
    }
    values["hardblock"] = registry.register(
        object_type="hardblock", canonical_identity="cell/DSP",
        attributes={"path_member": True, "path_rank": 1, "resource_type": "DSP", "compatible_site_handles": [values["site"], values["site2"]]},
    )
    values["hardblock2"] = registry.register(
        object_type="hardblock", canonical_identity="cell/DSP2",
        attributes={"path_member": True, "path_rank": 2, "resource_type": "DSP", "compatible_site_handles": [values["site"]]},
    )
    relation = build_relation(
        root_sha256=registry.root_sha256, stage="ROUTED",
        target_rows=[
            {"handle": values["hardblock"], "target_identity_sha256": "1" * 64, "resource_type": "DSP", "compatible_site_handles": [values["site"], values["site2"]]},
            {"handle": values["hardblock2"], "target_identity_sha256": "2" * 64, "resource_type": "DSP", "compatible_site_handles": [values["site"]]},
        ],
        target_query_sha256="3" * 64,
        target_site_query_sha256={values["hardblock"]: "4" * 64, values["hardblock2"]: "5" * 64},
    )
    return CompileContext(
        registry,
        "ROUTED",
        (values["region"],),
        (0, 4, 0, 4),
        fanout_admitted=True,
        action_surface_version="v3",
        hardblock_compatibility_relation=relation,
    ), values


def compile_p(steps, ctx):
    return compile_candidate_v5({"language": "P", "steps": steps}, ctx)


def test_registry_is_canonical_deterministic_and_complete() -> None:
    first = public_registry()
    assert first == public_registry()
    assert registry_sha256() == hashlib.sha256(
        json.dumps(first, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert set(first["P"]["operations"]) == set(P_OPERATION_REGISTRY)
    assert set(first["G"]["generated_nodes"]) == G_EXCLUSIVE_NODES
    assert len(P_OPERATION_REGISTRY) == len(P_CARDS) == 27
    assert "generic_set_property" not in P_OPERATION_REGISTRY


def test_public_contract_fits_common_ceiling_and_uses_closed_per_op_schemas() -> None:
    for level in ("L2", "L3", "L4"):
        raw = json.dumps(contract_for_level(level), sort_keys=True, separators=(",", ":")).encode()
        assert len(raw) <= 65_536
    assert len(P_PROGRAM_SCHEMA["properties"]["steps"]["items"]["oneOf"]) == len(P_OPERATION_REGISTRY)
    assert G_PROGRAM_SCHEMA["properties"]["steps"]["items"]["oneOf"][0] == {"$ref": "#/P/program_schema/properties/steps/items"}
    assert all(card["inputs"] == {
        "$ref": f"#/P/program_schema/properties/steps/items/oneOf/{index}/properties/args"
    } for index, card in enumerate(P_CARDS))
    packed = compact_cards(P_CARDS)
    assert [dict(zip(packed["field_order"], row, strict=True)) for row in packed["rows"]] == P_CARDS


@pytest.mark.parametrize("directive", PLACE_DIRECTIVES)
def test_every_place_directive_is_schema_and_compiler_reachable(directive: str) -> None:
    ctx, _ = context()
    candidate = {"language": "P", "steps": [
        {"step_id": "s1", "op": "UNROUTE", "args": {}},
        {"step_id": "s2", "op": "UNPLACE", "args": {}},
        {"step_id": "s3", "op": "PLACE", "args": {"directive": directive}},
        {"step_id": "s4", "op": "ROUTE", "args": {"directive": "Default"}},
    ]}
    jsonschema.validate(candidate, P_PROGRAM_SCHEMA)
    assert compile_candidate_v5(candidate, ctx).final_stage == "ROUTED"


@pytest.mark.parametrize("weight", PLACE_NET_DELAY_WEIGHTS)
def test_every_place_weight_is_schema_and_compiler_reachable(weight: str) -> None:
    ctx, _ = context()
    compile_p([
        {"step_id": "s1", "op": "UNROUTE", "args": {}},
        {"step_id": "s2", "op": "UNPLACE", "args": {}},
        {"step_id": "s3", "op": "PLACE", "args": {"directive": "Default", "net_delay_weight": weight}},
        {"step_id": "s4", "op": "ROUTE", "args": {"directive": "Default"}},
    ], ctx)


@pytest.mark.parametrize("directive", ROUTE_DIRECTIVES)
def test_every_route_directive_is_schema_and_compiler_reachable(directive: str) -> None:
    ctx, _ = context()
    compile_p([
        {"step_id": "s1", "op": "UNROUTE", "args": {}},
        {"step_id": "s2", "op": "ROUTE", "args": {"directive": directive}},
    ], ctx)


@pytest.mark.parametrize("directive", PHYS_OPT_DIRECTIVES)
def test_every_phys_directive_maps_to_one_heavy_meaning(directive: str) -> None:
    ctx, _ = context()
    compiled = compile_p([
        {"step_id": "s1", "op": "PHYS_OPT", "args": {"mode": "DIRECTIVE", "directive": directive}},
    ], ctx)
    assert compiled.heavy_calls == 1
    assert compiled.host_steps[0].public_args == {"mode": "DIRECTIVE", "directive": directive}


@pytest.mark.parametrize("option", PHYS_OPT_OPTIONS)
def test_every_phys_option_is_reachable_at_its_exact_stage(option: str) -> None:
    ctx, _ = context()
    phys = {"mode": "OPTIONS", "options": ["slr_crossing_opt", "tns_cleanup"]} if option == "tns_cleanup" else {"mode": "OPTIONS", "options": [option]}
    if option in {"bram_enable_opt", "bram_register_opt", "dsp_register_opt", "fanout_opt", "shift_register_opt", "uram_register_opt"}:
        steps = [
            {"step_id": "s1", "op": "UNROUTE", "args": {}},
            {"step_id": "s2", "op": "PHYS_OPT", "args": phys},
            {"step_id": "s3", "op": "ROUTE", "args": {"directive": "Default"}},
        ]
    else:
        steps = [{"step_id": "s1", "op": "PHYS_OPT", "args": phys}]
    assert compile_p(steps, ctx).heavy_calls >= 1


@pytest.mark.parametrize(
    "steps,reason",
    [
        ([{"step_id": "s1", "op": "PHYS_OPT", "args": {"directive": "Explore", "options": ["retime"]}}], "phys_opt_mode_invalid"),
        ([{"step_id": "s1", "op": "PHYS_OPT", "args": {"mode": "OPTIONS", "options": []}}], "phys_opt_options_invalid"),
        ([{"step_id": "s1", "op": "PHYS_OPT", "args": {"mode": "OPTIONS", "options": ["tns_cleanup"]}}], "tns_cleanup_requires_slr_crossing_opt"),
        ([{"step_id": "s1", "op": "PLACE", "args": {"directive": "AggressiveExplore"}}], "directive_not_allowed"),
        ([{"step_id": "s1", "op": "SET_PROPERTY", "args": {"name": "DONT_TOUCH"}}], "forbidden_operation"),
    ],
)
def test_excluded_and_incompatible_values_fail_closed(steps, reason: str) -> None:
    ctx, _ = context()
    with pytest.raises(ConstructionError, match=reason):
        compile_p(steps, ctx)


def test_boundary_checks_measure_constructs_and_preserve_inheritance() -> None:
    assert language_condition_error({"language": "P", "steps": [{"op": "CALL_MACRO"}, {"op": "ROUTE"}]}, "P_BOUNDARY") is None
    assert language_condition_error({"language": "P", "steps": [{"op": "CALL_MACRO"}]}, "P_BOUNDARY") == "LANGUAGE_CONDITION_NOT_MET"
    assert language_condition_error({"language": "G", "steps": [{"op": "CALL_MACRO"}, {"op": "SELECT_WHERE"}]}, "G_BOUNDARY") is None
    assert language_condition_error({"language": "G", "steps": [{"op": "CALL_MACRO"}]}, "G_BOUNDARY") == "LANGUAGE_CONDITION_NOT_MET"


@pytest.mark.parametrize(
    "steps,expected_operation",
    [
        ([{"step_id": "s1", "op": "UNROUTE", "args": {}}, {"step_id": "s2", "op": "REPLICATE_NET", "args": {"typed_net": "NET"}}, {"step_id": "s3", "op": "ROUTE", "args": {"directive": "Default"}}], "vivado.wp014c_replicate_net"),
        ([{"step_id": "s1", "op": "UNROUTE", "args": {}}, {"step_id": "s2", "op": "ROUTE_NET", "args": {"typed_net": "NET", "delay_mode": "DELAY"}}], "vivado.wp014c_route_net"),
        ([{"step_id": "s1", "op": "UNROUTE", "args": {}}, {"step_id": "s2", "op": "RELOCATE_HARDBLOCK", "args": {"typed_target": "HB", "destination": "SITE"}}, {"step_id": "s3", "op": "ROUTE", "args": {"directive": "Default"}}], "vivado.wp014c_relocate_hardblocks"),
        ([{"step_id": "s1", "op": "UNROUTE", "args": {}}, {"step_id": "s2", "op": "UNPLACE_CELL", "args": {"typed_cell": "CELL1"}}, {"step_id": "s3", "op": "ROUTE", "args": {"directive": "Default"}}], "vivado.wp014c_unplace_cell"),
        ([{"step_id": "s1", "op": "UNROUTE", "args": {}}, {"step_id": "s2", "op": "SWAP_CELLS", "args": {"first_cell": "CELL1", "second_cell": "CELL2"}}, {"step_id": "s3", "op": "ROUTE", "args": {"directive": "Default"}}], "vivado.wp014c_swap_cells"),
        ([{"step_id": "s1", "op": "SET_CELL_LOC_FIXED", "args": {"target_handle": "CELL1", "value": True}}, {"step_id": "s2", "op": "UNROUTE", "args": {}}, {"step_id": "s3", "op": "ROUTE", "args": {"directive": "Default"}}], "vivado.wp014c_set_cell_loc_fixed"),
        ([{"step_id": "s1", "op": "SET_NET_DONT_TOUCH", "args": {"target_handle": "NET", "value": True}}, {"step_id": "s2", "op": "UNROUTE", "args": {}}, {"step_id": "s3", "op": "ROUTE", "args": {"directive": "Default"}}], "vivado.wp014c_set_net_dont_touch"),
    ],
)
def test_targeted_and_property_families_compile_to_reviewed_meaning(steps, expected_operation: str) -> None:
    ctx, handles = context()
    replacements = {"NET": handles["net"], "HB": handles["hardblock"], "SITE": handles["site"], "CELL1": handles["cell1"], "CELL2": handles["cell2"]}
    encoded = json.dumps(steps)
    for name, handle in replacements.items():
        encoded = encoded.replace(f'"{name}"', f'"{handle}"')
    compiled = compile_p(json.loads(encoded), ctx)
    assert expected_operation in {step.operation for step in compiled.host_steps}


def test_rapidwright_fanout_uses_the_admitted_optimize_fanout_signature() -> None:
    ctx, handles = context()
    candidate = {"language": "P", "steps": [
        {"step_id": "s1", "op": "REPLICATE_DRIVER", "args": {"typed_net": handles["net"], "split_factor": 4}},
        {"step_id": "s2", "op": "ROUTE", "args": {"directive": "Default"}},
    ]}
    jsonschema.validate(candidate, P_PROGRAM_SCHEMA)
    compiled = compile_candidate_v5(candidate, ctx)
    step = compiled.host_steps[0]
    assert step.operation == "rapidwright.optimize_fanout"
    assert step.public_args == {"net": handles["net"], "split_factor": 4}
    assert step.private_args == {"net": "net/N", "split_factor": 4}
    with pytest.raises(ConstructionError, match="replicate_driver_fields_invalid"):
        compile_p([
            {"step_id": "s1", "op": "REPLICATE_DRIVER", "args": {"typed_net": handles["net"], "copies": 2, "max_loads_per_copy": 64}},
        ], ctx)


def test_g_inherits_exact_p_program_and_counts_real_generated_node() -> None:
    ctx, _ = context()
    candidate = {"language": "G", "steps": [
        {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "net", "predicate": {"op": "COMPARE", "field": "fanout", "operator": "GE", "value": 1}, "top_k": 1}, "bind": "target"},
        {"step_id": "s2", "op": "UNROUTE", "args": {}},
        {"step_id": "s3", "op": "ROUTE_NET", "args": {"typed_net": {"ref": "target"}, "delay_mode": "AUTO_DELAY"}},
    ]}
    compiled = compile_candidate_v5(candidate, ctx)
    assert compiled.language == "G"
    assert "vivado.wp014c_route_net" in {step.operation for step in compiled.host_steps}


def test_g_v2_nonprefix_collection_compiles_to_one_batch_dispatch() -> None:
    ctx, _ = context()
    candidate = {"language": "G", "steps": [
        {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "net", "predicate": {"op": "COMPARE", "field": "fanout", "operator": "GE", "value": 1}, "top_k": 3}, "bind": "all_nets"},
        {"step_id": "s2", "op": "TOPK", "args": {"input": {"ref": "all_nets"}, "k": 1}, "bind": "head"},
        {"step_id": "s3", "op": "DIFF", "args": {"left": {"ref": "all_nets"}, "right": {"ref": "head"}}, "bind": "nonprefix"},
        {"step_id": "s4", "op": "BATCH_ROUTE_NETS", "args": {"typed_nets": {"ref": "nonprefix"}, "delay_mode": "AUTO_DELAY"}},
    ]}
    compiled = compile_candidate_v5(candidate, ctx)
    assert compiled.heavy_calls == 1
    assert [step.operation for step in compiled.host_steps] == ["vivado.wp014h_batch_route_nets"]
    assert compiled.host_steps[0].source_step_id == "s4"
    records = {row["binding_id"]: row for row in compiled.binding_records}
    assert records["nonprefix"]["binding_type"] == "net"
    assert records["nonprefix"]["binding_cardinality"] == 2


def test_g_v2_couples_multiple_hardblocks_to_unique_sites() -> None:
    ctx, _ = context()
    candidate = {"language": "G", "steps": [
        {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "hardblock", "predicate": {"op": "COMPARE", "field": "resource_type", "operator": "EQ", "value": "DSP"}, "top_k": 2}, "bind": "blocks"},
        {"step_id": "s2", "op": "SELECT_WHERE", "args": {"source_type": "site", "predicate": {"op": "COMPARE", "field": "object_type", "operator": "EQ", "value": "site"}, "top_k": 3}, "bind": "sites"},
        {"step_id": "s3", "op": "COUPLE_SITES", "args": {"hardblocks": {"ref": "blocks"}, "sites": {"ref": "sites"}, "strategy": "REVERSE"}, "bind": "allocation"},
        {"step_id": "s4", "op": "UNROUTE", "args": {}},
        {"step_id": "s5", "op": "APPLY_ALLOCATION", "args": {"allocation": {"ref": "allocation"}}},
        {"step_id": "s6", "op": "ROUTE", "args": {"directive": "Default"}},
    ]}
    compiled = compile_candidate_v5(candidate, ctx)
    allocation = next(step for step in compiled.host_steps if step.operation == "vivado.wp014h_apply_allocation")
    assert len(allocation.public_args["targets"]) == 2
    assert len(set(allocation.public_args["sites"])) == 2
    assert allocation.source_step_id == "s5"


def test_worker_static_meanings_are_mechanically_covered() -> None:
    expected = {row["worker_operation"] for row in worker_meanings().values() if row["worker_operation"].startswith("vivado.")}
    translated = {"vivado.unroute_all" if item == "vivado.unroute" else "vivado.unplace_all" if item == "vivado.unplace" else "vivado.place_design" if item == "vivado.place" else "vivado.route_design" if item == "vivado.route" else "vivado.phys_opt_design" if item == "vivado.phys_opt" else item for item in expected}
    assert translated <= _ALLOWED_OPERATIONS
    assert set(PLACE_DIRECTIVES) == _DIRECTIVES["vivado.place_design"]
    assert set(ROUTE_DIRECTIVES) == _DIRECTIVES["vivado.route_design"]
    assert set(PHYS_OPT_DIRECTIVES) == _PHYS_DIRECTIVES
    assert set(PHYS_OPT_OPTIONS) == _PHYS_OPTIONS
    assert set(PLACE_NET_DELAY_WEIGHTS) == _PLACE_WEIGHTS


def test_worker_accepts_closed_g_v2_batch_requests() -> None:
    import base64

    encoded = [base64.b64encode(value).decode() for value in (b"net/a", b"net/b")]
    request_bytes(
        "vivado.wp014h_batch_route_nets", "batch-route", timeout_seconds=1,
        arguments={"net_names_b64": encoded, "delay_mode": "AUTO_DELAY"},
    )
    cells = [base64.b64encode(value).decode() for value in (b"cell/a", b"cell/b")]
    sites = [base64.b64encode(value).decode() for value in (b"DSP_X0Y1", b"DSP_X0Y2")]
    request_bytes(
        "vivado.wp014h_apply_allocation", "apply-allocation", timeout_seconds=1,
        arguments={
            "cell_names_b64": cells, "site_names_b64": sites,
            "target_site_pair_hashes": ["1" * 64, "2" * 64],
            "compatibility_relation_sha256": "3" * 64,
            "feasibility_query_sha256": "4" * 64,
            "allocation_sha256": "5" * 64,
            "target_identity_sha256": "6" * 64,
            "dispatch_pairs_sha256": "7" * 64,
        },
    )


def test_worker_accepts_every_strict_directive_and_option_wire_value() -> None:
    for directive in PLACE_DIRECTIVES:
        request_bytes("vivado.place_design", "p" + directive, timeout_seconds=1, arguments={"directive": directive})
    for directive in ROUTE_DIRECTIVES:
        request_bytes("vivado.route_design", "r" + directive, timeout_seconds=1, arguments={"directive": directive})
    for directive in PHYS_OPT_DIRECTIVES:
        request_bytes("vivado.phys_opt_design", "d" + directive, timeout_seconds=1, arguments={"mode": "DIRECTIVE", "directive": directive})
    for option in PHYS_OPT_OPTIONS:
        options = ["slr_crossing_opt", "tns_cleanup"] if option == "tns_cleanup" else [option]
        request_bytes("vivado.phys_opt_design", "o" + option, timeout_seconds=1, arguments={"mode": "OPTIONS", "options": options})


def test_targeted_route_worker_meaning_reroutes_only_the_selected_net() -> None:
    encoded = __import__("base64").b64encode(b"private/net/name").decode()
    raw = request_bytes(
        "vivado.wp014c_route_net", "target-route", timeout_seconds=1,
        arguments={"net_name_b64": encoded, "delay_mode": "AUTO_DELAY"},
    )
    from optimizer_pipeline.dof.vivado_mutation_worker import build_fixed_tcl, parse_request_line
    tcl = build_fixed_tcl(parse_request_line(raw.rstrip()))
    assert "route_design -unroute -nets $__dof_net" in tcl
    assert "route_design -auto_delay -nets $__dof_net" in tcl
    assert "wp014c_route_net_no_effect" in tcl


def test_property_worker_requires_verified_nonempty_state_delta() -> None:
    encoded = __import__("base64").b64encode(b"private/net/name").decode()
    raw = request_bytes(
        "vivado.wp014c_set_net_dont_touch", "property-delta", timeout_seconds=1,
        arguments={"target_name_b64": encoded, "value": True},
    )
    from optimizer_pipeline.dof.vivado_mutation_worker import build_fixed_tcl, parse_request_line
    tcl = build_fixed_tcl(parse_request_line(raw.rstrip()))
    assert "wp014c_property_no_effect" in tcl
    assert "wp014c_property_verification_failed" in tcl
    assert "DOF_WP014C_PROPERTY_V2" in tcl


def test_net_replication_worker_requires_topology_delta() -> None:
    encoded = __import__("base64").b64encode(b"private/net/name").decode()
    raw = request_bytes(
        "vivado.wp014c_replicate_net", "replication-delta", timeout_seconds=1,
        arguments={"net_name_b64": encoded},
    )
    from optimizer_pipeline.dof.vivado_mutation_worker import build_fixed_tcl, parse_request_line
    tcl = build_fixed_tcl(parse_request_line(raw.rstrip()))
    assert "wp014c_replicate_net_no_effect" in tcl
    assert "DOF_WP014C_REPLICATE_NET_V2" in tcl
