from optimizer_pipeline.dof.scientific_evidence import (
    CAUSAL_LINEAGE_VERSION,
    boundary_evidence,
    causal_lineage,
    strict_boundary_certificate,
)


def _child() -> dict:
    return {"sha256": "c" * 64, "size_bytes": 4096}


def test_unused_g_producer_plus_executed_macro_is_not_causal_g() -> None:
    candidate = {"language": "G", "steps": [
        {
            "step_id": "s1", "op": "SELECT_WHERE",
            "args": {
                "source_type": "net",
                "predicate": {
                    "op": "COMPARE", "field": "fanout",
                    "operator": "GE", "value": 2,
                },
                "top_k": 1,
            },
            "bind": "unused_net",
        },
        {
            "step_id": "s2", "op": "CALL_MACRO",
            "args": {"action_key": "macro.phys_opt"},
        },
    ]}
    result = boundary_evidence(
        candidate=candidate,
        execution_started=True,
        execution={
            "actual_heavy_calls": 3,
            "selected_count": 1,
            "transformed_count": 1,
            "child": _child(),
            "call_trace": [{"operation": "vivado.phys_opt_design"}],
        },
    )
    assert result["compiled_boundary"] == "G"
    assert result["boundary_runtime_exercised"] is False
    assert result["producer_consumer_lineage"]["nonexercise_reason"] == "G_OUTPUT_NOT_CONSUMED"


def test_bound_generated_net_requires_exact_consumer_runtime_and_delta() -> None:
    candidate = {"language": "G", "steps": [
        {
            "step_id": "s1", "op": "SELECT_WHERE",
            "args": {
                "source_type": "net",
                "predicate": {
                    "op": "COMPARE", "field": "fanout",
                    "operator": "GE", "value": 2,
                },
                "top_k": 1,
            },
            "bind": "net",
        },
        {
            "step_id": "s2", "op": "ROUTE_NET",
            "args": {"typed_net": {"ref": "net"}, "delay_mode": "AUTO_DELAY"},
        },
    ]}
    absent = causal_lineage(
        candidate=candidate,
        execution_started=True,
        execution={
            "selected_count": 1, "transformed_count": 1, "child": _child(),
            "call_trace": [{"operation": "vivado.route_design"}],
        },
    )
    assert absent["schema_version"] == CAUSAL_LINEAGE_VERSION
    assert absent["boundary_runtime_exercised"] is False
    assert absent["nonexercise_reason"] == "BOUND_CONSUMER_RUNTIME_NOT_OBSERVED"

    observed = causal_lineage(
        candidate=candidate,
        execution_started=True,
        execution={
            "selected_count": 1, "transformed_count": 1, "child": _child(),
            "call_trace": [{"operation": "vivado.wp014c_route_net"}],
        },
    )
    assert observed["boundary_runtime_exercised"] is True
    assert observed["nonexercise_reason"] is None
    assert observed["causal_paths"][0]["producer_node_id"] == "s1"
    assert observed["causal_paths"][0]["consumer_node_id"] == "s2"
    assert observed["causal_paths"][0]["consumer_argument_path"] == "args.typed_net"
    assert observed["causal_paths"][0]["source_step_exact"] is False


def test_strict_nonprefix_batch_certificate_requires_exact_dispatch_lineage() -> None:
    candidate = {"language": "G", "steps": [
        {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "net", "predicate": {"op": "COMPARE", "field": "fanout", "operator": "GE", "value": 1}, "top_k": 3}, "bind": "all_nets"},
        {"step_id": "s2", "op": "TOPK", "args": {"input": {"ref": "all_nets"}, "k": 1}, "bind": "head"},
        {"step_id": "s3", "op": "DIFF", "args": {"left": {"ref": "all_nets"}, "right": {"ref": "head"}}, "bind": "nonprefix"},
        {"step_id": "s4", "op": "BATCH_ROUTE_NETS", "args": {"typed_nets": {"ref": "nonprefix"}, "delay_mode": "AUTO_DELAY"}},
    ]}
    execution = {
        "selected_count": 2,
        "transformed_count": 2,
        "child": _child(),
        "binding_records": [{
            "binding_id": "nonprefix", "binding_type": "net",
            "binding_cardinality": 2, "binding_content_hash": "a" * 64,
        }],
        "call_trace": [{
            "operation": "vivado.wp014h_batch_route_nets",
            "source_step_id": "s4",
            "worker_dispatch_id": "wp014h-004",
            "actual_target_identity_hash": "b" * 64,
        }],
        "mechanism_state_deltas": [{
            "state_delta_id": "batch-route", "mechanism_family": "batch_targeted_route",
            "changed_count": 2, "changed_identity_hash": "d" * 64,
            "evidence_strength": "ROUTE_BEFORE_AFTER",
        }],
    }
    result = strict_boundary_certificate(
        candidate=candidate, execution_started=True, execution=execution,
    )
    assert result["event"] == "G_STRICT_BOUNDARY_EXERCISED"
    assert result["mechanism_family"] == "NONPREFIX_MULTI_NET_BATCH"
    certificate = result["certificates"][0]
    assert certificate["producer_step_id"] == "s3"
    assert certificate["consumer_step_id"] == "s4"
    assert certificate["worker_dispatch_id"] == "wp014h-004"


def test_runtime_call_without_nonempty_delta_does_not_exercise_boundary() -> None:
    candidate = {"language": "G", "steps": [
        {
            "step_id": "s1", "op": "SELECT_WHERE",
            "args": {
                "source_type": "net",
                "predicate": {"op": "COMPARE", "field": "fanout", "operator": "GE", "value": 2},
                "top_k": 1,
            },
            "bind": "net",
        },
        {
            "step_id": "s2", "op": "SET_NET_DONT_TOUCH",
            "args": {"target_handle": {"ref": "net"}, "value": True},
        },
        {"step_id": "s3", "op": "UNROUTE", "args": {}},
        {"step_id": "s4", "op": "ROUTE", "args": {"directive": "Default"}},
    ]}
    result = causal_lineage(
        candidate=candidate,
        execution_started=True,
        execution={
            "selected_count": 1,
            "transformed_count": 0,
            "child": _child(),
            "call_trace": [{"operation": "vivado.wp014c_set_net_dont_touch"}],
        },
    )
    assert result["boundary_reached"] is True
    assert result["boundary_runtime_exercised"] is False
    assert result["nonexercise_reason"] == "STATE_DELTA_NOT_EVIDENCED"


def test_versioned_mechanism_delta_is_preferred_over_dcp_fallback() -> None:
    candidate = {"language": "G", "steps": [
        {
            "step_id": "s1", "op": "SELECT_WHERE",
            "args": {
                "source_type": "net",
                "predicate": {"op": "COMPARE", "field": "fanout", "operator": "GE", "value": 2},
                "top_k": 1,
            },
            "bind": "net",
        },
        {
            "step_id": "s2", "op": "ROUTE_NET",
            "args": {"typed_net": {"ref": "net"}, "delay_mode": "AUTO_DELAY"},
        },
    ]}
    result = causal_lineage(
        candidate=candidate,
        execution_started=True,
        execution={
            "selected_count": 1,
            "transformed_count": 1,
            "call_trace": [{"operation": "vivado.wp014c_route_net"}],
            "mechanism_state_deltas": [{
                "state_delta_id": "route-net",
                "mechanism_family": "targeted_route",
                "before_hash": "a" * 64,
                "after_hash": "b" * 64,
                "changed_count": 1,
                "changed_identity_hash": "c" * 64,
                "evidence_strength": "ROUTE_BEFORE_AFTER",
            }],
        },
    )
    assert result["boundary_runtime_exercised"] is True
    assert result["state_deltas"][0]["mechanism_family"] == "targeted_route"
