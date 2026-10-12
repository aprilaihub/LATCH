from __future__ import annotations

import json

import pytest

from optimizer_pipeline.dof.vivado_mutation_worker import (
    WorkerProtocolError,
    build_fixed_tcl,
    parse_request_line,
)


def request(kind: str, top_k: int = 8) -> bytes:
    return json.dumps(
        {
            "schema_version": "dof.vivado-mutation-worker-request.v1",
            "operation_id": "vivado.wp013_query_objects",
            "call_token": "wp013a-query",
            "timeout_seconds": 300.0,
            "arguments": {
                "query_kind": kind,
                "top_k": top_k,
                "resource_type": "",
                "target_cell_b64": "",
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize("kind", ["timing_paths", "critical_cell_locations"])
def test_wp013a_real_query_kinds_are_closed_and_bounded(kind: str) -> None:
    parsed = parse_request_line(request(kind))
    tcl = build_fixed_tcl(parsed)
    assert f"DOF_WP013_QUERY_V1:{kind}" in tcl
    assert "lrange $__dof_objects 0 7" in tcl
    if kind == "timing_paths":
        assert "get_timing_paths" in tcl
        assert "STARTPOINT_PIN" in tcl and "ENDPOINT_PIN" in tcl
        assert "DATAPATH_DELAY" in tcl and "SLACK" in tcl
    else:
        assert "get_sites -quiet -of_objects" in tcl
        assert "get_clock_regions -quiet -of_objects" in tcl
    # The worker writes Tcl through an interactive PTY.  A literal tab would
    # be consumed as terminal completion and truncate the command before its
    # status marker.  Construct a unit separator inside Tcl, after transport.
    assert "\t" not in tcl
    assert "[format %c 31]" in tcl


def test_wp013a_query_kinds_reject_extra_or_unbounded_fields() -> None:
    with pytest.raises(WorkerProtocolError, match="query_top_k_invalid"):
        parse_request_line(request("timing_paths", 65))
    value = json.loads(request("timing_paths"))
    value["arguments"]["raw_tcl"] = "exit"
    with pytest.raises(WorkerProtocolError, match="operation_arguments_not_exact"):
        parse_request_line(json.dumps(value).encode())
