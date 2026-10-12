"""Closed Vivado line-protocol worker for WP-004A technical mutation.

The worker inherits the WP-003 process-ownership implementation but exposes a
separate request schema and executable surface.  Callers select only typed,
allowlisted operations; they never provide Tcl.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any


WORKER_SOURCE_MANIFEST_SCHEMA = "dof.mutation-worker-source-manifest.v1"
WORKER_BINDING_VERSION = "dof.wp014h-mutation-worker-binding.v3"
WORKER_REQUEST_SCHEMA = "dof.vivado-mutation-worker-request.v1"
WORKER_VERSION = "dof.vivado-mutation-worker.v3"
WORKER_TRANSPORT_PROTOCOL_VERSION = "dof.vivado-mutation-line-protocol.v1"
WORKER_VERSION_PREFIX = "DOF_VIVADO_MUTATION_WORKER_VERSION_V1:"
WORKER_PROTOCOL_PREFIX = "DOF_VIVADO_MUTATION_PROTOCOL_VERSION_V1:"
READY_LINE = "DOF_VIVADO_MUTATION_READY_V1"
DONE_PREFIX = "DOF_VIVADO_MUTATION_CALL_DONE_V1:"
STATUS_PREFIX = "DOF_VIVADO_MUTATION_TCL_STATUS_V1:"


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_manifest_for_captured_bytes(
    entry: Path,
    entry_bytes: bytes,
    sibling_bytes: bytes,
) -> dict[str, Any]:
    files = []
    for role, path, raw in (
        ("isolated_worker_entry", entry, entry_bytes),
        (
            "shared_owned_vivado_implementation",
            entry.with_name("vivado_readonly_worker.py"),
            sibling_bytes,
        ),
    ):
        files.append(
            {
                "role": role,
                "relative_name": path.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
        )
    payload = {
        "schema_version": WORKER_SOURCE_MANIFEST_SCHEMA,
        "binding_version": WORKER_BINDING_VERSION,
        "entry_relative_name": entry.name,
        "files": files,
        "worker_version": WORKER_VERSION,
        "transport_protocol_version": WORKER_TRANSPORT_PROTOCOL_VERSION,
        "request_schema_version": WORKER_REQUEST_SCHEMA,
    }
    return payload | {"manifest_sha256": _canonical_hash(payload)}


def mutation_worker_source_manifest_for_entry(
    entry_path: str | Path,
) -> dict[str, Any]:
    """Hash the direct entry and the exact sibling bytes it will execute."""

    entry = Path(entry_path).resolve(strict=True)
    sibling = entry.with_name("vivado_readonly_worker.py")
    return _source_manifest_for_captured_bytes(
        entry,
        entry.read_bytes(),
        sibling.read_bytes(),
    )


def _required_bootstrap_sha256(name: str) -> str:
    positions = [index for index, item in enumerate(sys.argv) if item == name]
    if len(positions) != 1 or positions[0] + 1 >= len(sys.argv):
        raise RuntimeError("bound_worker_source_manifest_argument_invalid")
    value = sys.argv[positions[0] + 1]
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise RuntimeError("bound_worker_source_manifest_argument_invalid")
    return value


if __package__:
    from .vivado_readonly_worker import (
        EFFECTIVE_ENV_PREFIX,
        INIT_POLICY_LINE,
        LAUNCHED_LINE,
        PYTHON_VERSION_PREFIX,
        STARTED_LINE,
        VERSION_PREFIX,
        WorkerProtocolError,
        _bounded_stop,
        _live_verified,
        _same_owned_session,
        _spawn_owned_pty,
        _tree_is_owned,
        _validate_init_policy,
        effective_vivado_environment_hash,
    )
else:  # isolated direct-script entry: no cwd/PYTHONPATH/package-root trust
    _shared_path = Path(__file__).resolve().with_name("vivado_readonly_worker.py")
    try:
        _expected_source_manifest_sha256 = _required_bootstrap_sha256(
            "--source-manifest-sha256"
        )
        _entry_bytes = Path(__file__).resolve().read_bytes()
        _shared_bytes = _shared_path.read_bytes()
        _observed_source_manifest = _source_manifest_for_captured_bytes(
            Path(__file__).resolve(),
            _entry_bytes,
            _shared_bytes,
        )
        if (
            _observed_source_manifest["manifest_sha256"]
            != _expected_source_manifest_sha256
        ):
            raise RuntimeError("bound_worker_source_manifest_mismatch")
    except Exception as exc:
        print(
            f"worker source binding unavailable: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(71) from None
    _shared_spec = importlib.util.spec_from_file_location(
        "_dof_bound_vivado_readonly_worker", _shared_path
    )
    if _shared_spec is None or _shared_spec.loader is None:
        raise ImportError("bound_vivado_worker_spec_unavailable")
    _shared = importlib.util.module_from_spec(_shared_spec)
    # dataclasses and other runtime helpers require the executing module to be
    # registered.  Execute the same sibling bytes captured for the source
    # self-check so a path rewrite cannot race a second loader read.
    sys.modules[_shared_spec.name] = _shared
    exec(compile(_shared_bytes, os.fspath(_shared_path), "exec"), _shared.__dict__)
    EFFECTIVE_ENV_PREFIX = _shared.EFFECTIVE_ENV_PREFIX
    INIT_POLICY_LINE = _shared.INIT_POLICY_LINE
    LAUNCHED_LINE = _shared.LAUNCHED_LINE
    PYTHON_VERSION_PREFIX = _shared.PYTHON_VERSION_PREFIX
    STARTED_LINE = _shared.STARTED_LINE
    VERSION_PREFIX = _shared.VERSION_PREFIX
    WorkerProtocolError = _shared.WorkerProtocolError
    _bounded_stop = _shared._bounded_stop
    _live_verified = _shared._live_verified
    _same_owned_session = _shared._same_owned_session
    _spawn_owned_pty = _shared._spawn_owned_pty
    _tree_is_owned = _shared._tree_is_owned
    _validate_init_policy = _shared._validate_init_policy
    effective_vivado_environment_hash = _shared.effective_vivado_environment_hash


_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_ALLOWED_OPERATIONS = frozenset(
    {
        "vivado.open_checkpoint",
        "vivado.report_timing_summary",
        "vivado.report_route_status",
        "vivado.unroute_all",
        "vivado.unplace_all",
        "vivado.place_design",
        "vivado.route_design",
        "vivado.phys_opt_design",
        "vivado.generated_pblock_region",
        "vivado.wp013_query_objects",
        "vivado.wp013_apply_pblock",
        "vivado.wp013_create_pblock",
        "vivado.wp013_assign_pblock",
        "vivado.wp013_remove_pblock",
        "vivado.wp013_relocate_hardblocks",
        "vivado.wp014c_create_pblock",
        "vivado.wp014c_resize_pblock",
        "vivado.wp014c_assign_pblock",
        "vivado.wp014c_unassign_pblock",
        "vivado.wp014c_delete_pblock",
        "vivado.wp014c_relocate_hardblocks",
        "vivado.wp014c_replicate_net",
        "vivado.wp014c_route_net",
        "vivado.wp014c_place_cell",
        "vivado.wp014c_unplace_cell",
        "vivado.wp014c_swap_cells",
        "vivado.wp014c_set_cell_loc_fixed",
        "vivado.wp014c_set_net_dont_touch",
        "vivado.wp014c_set_pblock_exclude_placement",
        "vivado.wp014c_set_site_prohibit",
        "vivado.wp014h_batch_route_nets",
        "vivado.wp014h_apply_allocation",
        "vivado.write_checkpoint",
        "vivado.close_design",
        "worker.exit",
    }
)
_DIRECTIVES = {
    "vivado.place_design": frozenset({"Default", "Explore", "Quick", "RuntimeOptimized"}),
    "vivado.route_design": frozenset({
        "Default", "Explore", "AggressiveExplore", "NoTimingRelaxation",
        "MoreGlobalIterations", "HigherDelayCost", "AdvancedSkewModeling",
        "AlternateCLBRouting", "RuntimeOptimized", "Quick",
    }),
    # Legacy v1 wire compatibility.  Action Surface v3 uses the explicit
    # mode-bearing request below; this set is not its public directive enum.
    "vivado.phys_opt_design": frozenset({"Explore", "AddRetime", "AlternateFlowWithRetiming"}),
}
_PLACE_WEIGHTS = frozenset({"low", "medium", "high"})
_PHYS_DIRECTIVES = frozenset({
    "Default", "Explore", "AggressiveExplore", "ExploreWithHoldFix",
    "ExploreWithAggressiveHoldFix", "AlternateReplication", "AggressiveFanoutOpt",
    "AlternateFlowWithRetiming", "AddRetime", "RuntimeOptimized",
})
_PHYS_POST_ROUTE_OPTIONS = frozenset({
    "aggressive_hold_fix", "critical_cell_opt", "hold_fix", "placement_opt",
    "restruct_opt", "retime", "routing_opt", "slr_crossing_opt",
})
_PHYS_PLACED_OPTIONS = frozenset({
    "bram_enable_opt", "bram_register_opt", "dsp_register_opt", "fanout_opt",
    "shift_register_opt", "uram_register_opt",
})
_PHYS_OPTIONS = _PHYS_POST_ROUTE_OPTIONS | _PHYS_PLACED_OPTIONS | {"tns_cleanup"}
_DEPENDENCY_MANIFEST_SCHEMA = "dof.mutation-worker-dependency-manifest.v1"
_RUNTIME_DISTRIBUTIONS = ("pexpect", "ptyprocess")


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    operation_id: str
    call_token: str
    timeout_seconds: float
    arguments: dict[str, Any]


def parse_request_line(raw: bytes) -> WorkerRequest:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("request_not_exact_utf8_json") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "operation_id",
        "call_token",
        "timeout_seconds",
        "arguments",
    }:
        raise WorkerProtocolError("request_fields_not_exact")
    if value["schema_version"] != WORKER_REQUEST_SCHEMA:
        raise WorkerProtocolError("request_schema_mismatch")
    operation = value["operation_id"]
    if operation not in _ALLOWED_OPERATIONS:
        raise WorkerProtocolError("operation_not_allowed")
    token = value["call_token"]
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise WorkerProtocolError("call_token_invalid")
    timeout = value["timeout_seconds"]
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or not 0 < float(timeout) <= 900.0
    ):
        raise WorkerProtocolError("timeout_invalid")
    arguments = value["arguments"]
    if not isinstance(arguments, dict):
        raise WorkerProtocolError("arguments_not_object")
    if operation in {"vivado.open_checkpoint", "vivado.write_checkpoint"}:
        expected = {"dcp_path_b64"}
    elif operation == "vivado.place_design":
        expected = set(arguments)
        if expected not in ({"directive"}, {"directive", "net_delay_weight"}):
            raise WorkerProtocolError("operation_arguments_not_exact")
    elif operation == "vivado.route_design":
        expected = {"directive"}
    elif operation == "vivado.phys_opt_design":
        expected = set(arguments)
        if expected not in ({"directive"}, {"mode", "directive"}, {"mode", "options"}):
            raise WorkerProtocolError("operation_arguments_not_exact")
    elif operation == "vivado.generated_pblock_region":
        expected = {
            "pblock_id",
            "selector_kind",
            "cell_limit",
            "slice_x0",
            "slice_y0",
            "slice_x1",
            "slice_y1",
        }
    elif operation == "vivado.wp013_query_objects":
        expected = {"query_kind", "top_k", "resource_type", "target_cell_b64"}
    elif operation == "vivado.wp013_apply_pblock":
        expected = {"pblock_id", "cell_names_b64", "region_name_b64"}
    elif operation == "vivado.wp013_create_pblock":
        expected = {"pblock_id", "region_name_b64"}
    elif operation == "vivado.wp013_assign_pblock":
        expected = {"pblock_id", "cell_names_b64"}
    elif operation == "vivado.wp013_remove_pblock":
        expected = {"pblock_id"}
    elif operation == "vivado.wp013_relocate_hardblocks":
        expected = {"cell_names_b64", "site_names_b64"}
    elif operation in {"vivado.wp014c_create_pblock"}:
        expected = {"pblock_id", "region_name_b64"}
    elif operation == "vivado.wp014c_resize_pblock":
        expected = {"pblock_id", "region_name_b64", "mode"}
    elif operation in {"vivado.wp014c_assign_pblock", "vivado.wp014c_unassign_pblock"}:
        expected = {"pblock_id", "cell_names_b64"}
    elif operation == "vivado.wp014c_delete_pblock":
        expected = {"pblock_id"}
    elif operation == "vivado.wp014c_relocate_hardblocks":
        expected = {"cell_names_b64", "site_names_b64"}
    elif operation in {"vivado.wp014c_replicate_net"}:
        expected = {"net_name_b64"}
    elif operation == "vivado.wp014c_route_net":
        expected = {"net_name_b64", "delay_mode"}
    elif operation == "vivado.wp014h_batch_route_nets":
        expected = {"net_names_b64", "delay_mode"}
    elif operation == "vivado.wp014h_apply_allocation":
        expected = {
            "cell_names_b64", "site_names_b64", "target_site_pair_hashes",
            "compatibility_relation_sha256", "feasibility_query_sha256",
            "allocation_sha256", "target_identity_sha256",
            "dispatch_pairs_sha256",
        }
    elif operation == "vivado.wp014c_place_cell":
        expected = {"cell_name_b64", "site_name_b64"}
    elif operation == "vivado.wp014c_unplace_cell":
        expected = {"cell_name_b64"}
    elif operation == "vivado.wp014c_swap_cells":
        expected = {"first_cell_b64", "second_cell_b64"}
    elif operation in {
        "vivado.wp014c_set_cell_loc_fixed", "vivado.wp014c_set_net_dont_touch",
        "vivado.wp014c_set_pblock_exclude_placement", "vivado.wp014c_set_site_prohibit",
    }:
        expected = {"target_name_b64", "value"}
    else:
        expected = set()
    if set(arguments) != expected:
        raise WorkerProtocolError("operation_arguments_not_exact")
    if "dcp_path_b64" in arguments:
        encoded = arguments["dcp_path_b64"]
        if not isinstance(encoded, str) or len(encoded) > 32768:
            raise WorkerProtocolError("dcp_path_encoding_invalid")
        try:
            path_bytes = base64.b64decode(encoded.encode("ascii"), validate=True)
            path_bytes.decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError, ValueError) as exc:
            raise WorkerProtocolError("dcp_path_encoding_invalid") from exc
        if not path_bytes or b"\x00" in path_bytes:
            raise WorkerProtocolError("dcp_path_encoding_invalid")
    if operation in _DIRECTIVES and operation != "vivado.phys_opt_design":
        directive = arguments["directive"]
        if directive not in _DIRECTIVES[operation]:
            raise WorkerProtocolError("directive_not_allowed")
    if operation == "vivado.place_design" and "net_delay_weight" in arguments:
        if arguments["net_delay_weight"] not in _PLACE_WEIGHTS:
            raise WorkerProtocolError("net_delay_weight_not_allowed")
    if operation == "vivado.phys_opt_design":
        mode = arguments.get("mode")
        if mode is None:
            if arguments.get("directive") not in _DIRECTIVES[operation]:
                raise WorkerProtocolError("directive_not_allowed")
        elif mode == "DIRECTIVE":
            if arguments.get("directive") not in _PHYS_DIRECTIVES:
                raise WorkerProtocolError("directive_not_allowed")
        elif mode == "OPTIONS":
            options = arguments.get("options")
            if (
                not isinstance(options, list)
                or not 1 <= len(options) <= 4
                or len(options) != len(set(options))
                or not set(options) <= _PHYS_OPTIONS
            ):
                raise WorkerProtocolError("phys_opt_options_invalid")
            if "tns_cleanup" in options and "slr_crossing_opt" not in options:
                raise WorkerProtocolError("tns_cleanup_requires_slr_crossing_opt")
        else:
            raise WorkerProtocolError("phys_opt_mode_invalid")
    if operation == "vivado.generated_pblock_region":
        from_generated = _validate_generated_pblock_arguments(arguments)
        arguments = from_generated
    elif operation == "vivado.wp013_query_objects":
        arguments = _validate_wp013_query_arguments(arguments)
    elif operation == "vivado.wp013_apply_pblock":
        arguments = _validate_wp013_apply_pblock_arguments(arguments)
    elif operation == "vivado.wp013_create_pblock":
        arguments = {
            "pblock_id": _validate_wp013_pblock_id(arguments.get("pblock_id")),
            "region_name_b64": _validate_wp013_b64(arguments.get("region_name_b64"), "region_name"),
        }
    elif operation == "vivado.wp013_assign_pblock":
        arguments = {
            "pblock_id": _validate_wp013_pblock_id(arguments.get("pblock_id")),
            "cell_names_b64": _validate_wp013_encoded_list(arguments.get("cell_names_b64"), "cell_names"),
        }
    elif operation == "vivado.wp013_remove_pblock":
        arguments = {"pblock_id": _validate_wp013_pblock_id(arguments.get("pblock_id"))}
    elif operation == "vivado.wp013_relocate_hardblocks":
        cells = _validate_wp013_encoded_list(arguments.get("cell_names_b64"), "cell_names")
        sites = _validate_wp013_encoded_list(arguments.get("site_names_b64"), "site_names")
        if len(cells) != len(sites):
            raise WorkerProtocolError("wp013_relocation_cardinality_mismatch")
        arguments = {"cell_names_b64": cells, "site_names_b64": sites}
    elif operation.startswith("vivado.wp014c_"):
        arguments = _validate_wp014c_arguments(operation, arguments)
    elif operation == "vivado.wp014h_batch_route_nets":
        nets = _validate_wp013_encoded_list(arguments.get("net_names_b64"), "net_names")
        if not 2 <= len(nets) <= 32 or len(nets) != len(set(nets)):
            raise WorkerProtocolError("wp014h_batch_net_cardinality_invalid")
        mode = arguments.get("delay_mode")
        if mode not in {"DELAY", "AUTO_DELAY"}:
            raise WorkerProtocolError("wp014h_batch_delay_mode_invalid")
        arguments = {"net_names_b64": nets, "delay_mode": mode}
    elif operation == "vivado.wp014h_apply_allocation":
        cells = _validate_wp013_encoded_list(arguments.get("cell_names_b64"), "cell_names")
        sites = _validate_wp013_encoded_list(arguments.get("site_names_b64"), "site_names")
        if not 2 <= len(cells) <= 16 or len(cells) != len(sites) or len(sites) != len(set(sites)):
            raise WorkerProtocolError("wp014h_allocation_cardinality_invalid")
        pair_hashes = arguments.get("target_site_pair_hashes")
        if (
            not isinstance(pair_hashes, list)
            or len(pair_hashes) != len(cells)
            or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in pair_hashes)
        ):
            raise WorkerProtocolError("wp015b_allocation_pair_hashes_invalid")
        proof_fields = (
            "compatibility_relation_sha256", "feasibility_query_sha256",
            "allocation_sha256", "target_identity_sha256", "dispatch_pairs_sha256",
        )
        if any(
            not isinstance(arguments.get(key), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(arguments.get(key))) is None
            for key in proof_fields
        ):
            raise WorkerProtocolError("wp015b_allocation_proof_hash_invalid")
        arguments = {
            "cell_names_b64": cells, "site_names_b64": sites,
            "target_site_pair_hashes": pair_hashes,
            **{key: arguments[key] for key in proof_fields},
        }
    return WorkerRequest(operation, token, float(timeout), dict(arguments))


def _validate_generated_pblock_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Independent worker-side validation; importing repository code is forbidden."""

    pblock_id = arguments.get("pblock_id")
    if not isinstance(pblock_id, str) or re.fullmatch(
        r"g_[a-z][a-z0-9_]{0,22}", pblock_id
    ) is None:
        raise WorkerProtocolError("generated_pblock_id_invalid")
    selector = arguments.get("selector_kind")
    if selector not in {"sequential_cells", "high_fanout_cells"}:
        raise WorkerProtocolError("generated_selector_not_allowed")
    values: dict[str, int] = {}
    for name in (
        "cell_limit",
        "slice_x0",
        "slice_y0",
        "slice_x1",
        "slice_y1",
    ):
        item = arguments.get(name)
        if not isinstance(item, int) or isinstance(item, bool):
            raise WorkerProtocolError("generated_integer_invalid")
        values[name] = item
    if not 1 <= values["cell_limit"] <= 64:
        raise WorkerProtocolError("generated_cell_limit_invalid")
    if not 0 <= values["slice_x0"] <= values["slice_x1"] <= 999:
        raise WorkerProtocolError("generated_x_range_invalid")
    if not 0 <= values["slice_y0"] <= values["slice_y1"] <= 9999:
        raise WorkerProtocolError("generated_y_range_invalid")
    return {"pblock_id": pblock_id, "selector_kind": selector, **values}


def _validate_wp013_pblock_id(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"wp013_[a-z][a-z0-9_]{0,20}", value) is None:
        raise WorkerProtocolError("wp013_pblock_id_invalid")
    return value


def _validate_wp013_b64(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 32768:
        raise WorkerProtocolError(f"wp013_{label}_encoding_invalid")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
        text = decoded.decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError) as exc:
        raise WorkerProtocolError(f"wp013_{label}_encoding_invalid") from exc
    if (not text and not allow_empty) or "\x00" in text or any(ord(char) < 32 for char in text):
        raise WorkerProtocolError(f"wp013_{label}_encoding_invalid")
    return value


def _validate_wp013_encoded_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise WorkerProtocolError(f"wp013_{label}_list_invalid")
    return [_validate_wp013_b64(item, label) for item in value]


def _validate_wp013_query_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    kind = arguments.get("query_kind")
    if kind not in {
        "critical_cells", "critical_cell_locations", "timing_paths",
        "high_fanout_nets", "critical_hardblocks", "device_regions", "compatible_sites",
    }:
        raise WorkerProtocolError("wp013_query_kind_invalid")
    top_k = arguments.get("top_k")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 64:
        raise WorkerProtocolError("wp013_query_top_k_invalid")
    resource = arguments.get("resource_type")
    if resource not in {"", "DSP", "BRAM", "URAM", "ANY"}:
        raise WorkerProtocolError("wp013_query_resource_type_invalid")
    target = _validate_wp013_b64(arguments.get("target_cell_b64"), "target_cell", allow_empty=True)
    if kind == "compatible_sites" and not base64.b64decode(target).decode("utf-8"):
        raise WorkerProtocolError("wp013_query_target_required")
    if kind != "compatible_sites" and base64.b64decode(target).decode("utf-8"):
        raise WorkerProtocolError("wp013_query_target_forbidden")
    if kind != "critical_hardblocks" and resource:
        raise WorkerProtocolError("wp013_query_resource_forbidden")
    return {"query_kind": kind, "top_k": top_k, "resource_type": resource, "target_cell_b64": target}


def _validate_wp013_apply_pblock_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "pblock_id": _validate_wp013_pblock_id(arguments.get("pblock_id")),
        "cell_names_b64": _validate_wp013_encoded_list(arguments.get("cell_names_b64"), "cell_names"),
        "region_name_b64": _validate_wp013_b64(arguments.get("region_name_b64"), "region_name"),
    }


def _validate_wp014c_pblock_id(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"wp014c_[a-z][a-z0-9_]{0,19}", value) is None:
        raise WorkerProtocolError("wp014c_pblock_id_invalid")
    return value


def _validate_wp014c_arguments(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if operation == "vivado.wp014c_create_pblock":
        return {
            "pblock_id": _validate_wp014c_pblock_id(arguments["pblock_id"]),
            "region_name_b64": _validate_wp013_b64(arguments["region_name_b64"], "region_name"),
        }
    if operation == "vivado.wp014c_resize_pblock":
        mode = arguments["mode"]
        if mode not in {"ADD", "REMOVE"}:
            raise WorkerProtocolError("wp014c_resize_mode_invalid")
        return {
            "pblock_id": _validate_wp014c_pblock_id(arguments["pblock_id"]),
            "region_name_b64": _validate_wp013_b64(arguments["region_name_b64"], "region_name"),
            "mode": mode,
        }
    if operation in {"vivado.wp014c_assign_pblock", "vivado.wp014c_unassign_pblock"}:
        return {
            "pblock_id": _validate_wp014c_pblock_id(arguments["pblock_id"]),
            "cell_names_b64": _validate_wp013_encoded_list(arguments["cell_names_b64"], "cell_names"),
        }
    if operation == "vivado.wp014c_delete_pblock":
        return {"pblock_id": _validate_wp014c_pblock_id(arguments["pblock_id"])}
    if operation == "vivado.wp014c_relocate_hardblocks":
        cells = _validate_wp013_encoded_list(arguments["cell_names_b64"], "cell_names")
        sites = _validate_wp013_encoded_list(arguments["site_names_b64"], "site_names")
        if len(cells) != len(sites):
            raise WorkerProtocolError("wp014c_relocation_cardinality_mismatch")
        return {"cell_names_b64": cells, "site_names_b64": sites}
    if operation == "vivado.wp014c_replicate_net":
        return {"net_name_b64": _validate_wp013_b64(arguments["net_name_b64"], "net_name")}
    if operation == "vivado.wp014c_route_net":
        mode = arguments["delay_mode"]
        if mode not in {"DELAY", "AUTO_DELAY"}:
            raise WorkerProtocolError("wp014c_route_delay_mode_invalid")
        return {
            "net_name_b64": _validate_wp013_b64(arguments["net_name_b64"], "net_name"),
            "delay_mode": mode,
        }
    if operation == "vivado.wp014c_place_cell":
        return {
            "cell_name_b64": _validate_wp013_b64(arguments["cell_name_b64"], "cell_name"),
            "site_name_b64": _validate_wp013_b64(arguments["site_name_b64"], "site_name"),
        }
    if operation == "vivado.wp014c_unplace_cell":
        return {"cell_name_b64": _validate_wp013_b64(arguments["cell_name_b64"], "cell_name")}
    if operation == "vivado.wp014c_swap_cells":
        first = _validate_wp013_b64(arguments["first_cell_b64"], "first_cell")
        second = _validate_wp013_b64(arguments["second_cell_b64"], "second_cell")
        if first == second:
            raise WorkerProtocolError("wp014c_swap_cells_must_be_distinct")
        return {"first_cell_b64": first, "second_cell_b64": second}
    if operation in {
        "vivado.wp014c_set_cell_loc_fixed", "vivado.wp014c_set_net_dont_touch",
        "vivado.wp014c_set_pblock_exclude_placement", "vivado.wp014c_set_site_prohibit",
    }:
        value = arguments["value"]
        if not isinstance(value, bool):
            raise WorkerProtocolError("wp014c_property_value_invalid")
        return {
            "target_name_b64": _validate_wp013_b64(arguments["target_name_b64"], "target_name"),
            "value": value,
        }
    raise WorkerProtocolError("operation_not_allowed")


def request_bytes(
    operation_id: str,
    call_token: str,
    *,
    timeout_seconds: float,
    arguments: dict[str, Any] | None = None,
) -> bytes:
    payload = {
        "schema_version": WORKER_REQUEST_SCHEMA,
        "operation_id": operation_id,
        "call_token": call_token,
        "timeout_seconds": timeout_seconds,
        "arguments": dict(arguments or {}),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    parse_request_line(encoded)
    return encoded + b"\n"


def terminal_marker(call_token: str) -> bytes:
    if not _TOKEN_RE.fullmatch(call_token):
        raise WorkerProtocolError("call_token_invalid")
    return f"{DONE_PREFIX}{call_token}".encode("ascii")


def _path_expression(encoded: str) -> str:
    return (
        "[encoding convertfrom utf-8 "
        f"[binary decode base64 {{{encoded}}}]]"
    )


def build_fixed_tcl(request: WorkerRequest) -> str:
    operation = request.operation_id
    if operation == "worker.exit":
        return "exit"
    if operation == "vivado.open_checkpoint":
        body = "open_checkpoint " + _path_expression(
            request.arguments["dcp_path_b64"]
        )
    elif operation == "vivado.write_checkpoint":
        body = "write_checkpoint " + _path_expression(
            request.arguments["dcp_path_b64"]
        )
    elif operation == "vivado.report_timing_summary":
        body = "report_timing_summary -delay_type max -max_paths 10"
    elif operation == "vivado.report_route_status":
        body = "report_route_status"
    elif operation == "vivado.unroute_all":
        body = "route_design -unroute"
    elif operation == "vivado.unplace_all":
        body = "place_design -unplace"
    elif operation in _DIRECTIVES and operation != "vivado.phys_opt_design":
        directive = request.arguments["directive"]
        # The parser has already reduced this value to a finite literal set.
        command = operation.removeprefix("vivado.")
        body = f"{command} -directive {directive}"
        if operation == "vivado.place_design" and "net_delay_weight" in request.arguments:
            body += f" -net_delay_weight {request.arguments['net_delay_weight']}"
    elif operation == "vivado.phys_opt_design":
        if "mode" not in request.arguments:
            body = f"phys_opt_design -directive {request.arguments['directive']}"
        elif request.arguments["mode"] == "DIRECTIVE":
            body = f"phys_opt_design -directive {request.arguments['directive']}"
        else:
            body = "phys_opt_design " + " ".join(f"-{item}" for item in request.arguments["options"])
    elif operation == "vivado.generated_pblock_region":
        args = request.arguments
        selector = {
            "sequential_cells": "{IS_SEQUENTIAL}",
            "high_fanout_cells": "{IS_SEQUENTIAL && REF_PIN_NAME =~ *Q*}",
        }[args["selector_kind"]]
        last = args["cell_limit"] - 1
        region = (
            f"SLICE_X{args['slice_x0']}Y{args['slice_y0']}:"
            f"SLICE_X{args['slice_x1']}Y{args['slice_y1']}"
        )
        name = args["pblock_id"]
        body = (
            f"set __dof_g_cells [lrange [get_cells -hier -filter {selector}] 0 {last}]; "
            "if {[llength $__dof_g_cells] == 0} {error {generated_selector_empty}}; "
            f"create_pblock {name}; "
            f"add_cells_to_pblock [get_pblocks {name}] $__dof_g_cells; "
            f"resize_pblock [get_pblocks {name}] -add {{{region}}}; "
            f"puts \"DOF_GENERATED_PBLOCK_V1:{name}:{region}:[llength $__dof_g_cells]\""
        )
    elif operation == "vivado.wp013_query_objects":
        body = _build_wp013_query_tcl(request.arguments)
    elif operation == "vivado.wp013_apply_pblock":
        args = request.arguments
        cells = " ".join(_path_expression(item) for item in args["cell_names_b64"])
        region = _path_expression(args["region_name_b64"])
        name = args["pblock_id"]
        body = (
            f"set __dof_names [list {cells}]; "
            "set __dof_cells [get_cells -quiet $__dof_names]; "
            "if {[llength $__dof_cells] != [llength $__dof_names]} {error {wp013_cell_resolution_mismatch}}; "
            f"set __dof_region {region}; "
            "if {[llength [get_clock_regions -quiet $__dof_region]] != 1} {error {wp013_region_resolution_mismatch}}; "
            f"create_pblock {name}; add_cells_to_pblock [get_pblocks {name}] $__dof_cells; "
            f"resize_pblock [get_pblocks {name}] -add $__dof_region; "
            f"puts \"DOF_WP013_APPLY_PBLOCK_V1:{name}:[llength $__dof_cells]:$__dof_region\""
        )
    elif operation == "vivado.wp013_create_pblock":
        args = request.arguments
        region = _path_expression(args["region_name_b64"])
        name = args["pblock_id"]
        body = (
            f"set __dof_region {region}; "
            "if {[llength [get_clock_regions -quiet $__dof_region]] != 1} {error {wp013_region_resolution_mismatch}}; "
            "set __dof_region_range CLOCKREGION_${__dof_region}:CLOCKREGION_${__dof_region}; "
            f"create_pblock {name}; resize_pblock [get_pblocks {name}] -add $__dof_region_range; "
            f"puts \"DOF_WP013_CREATE_PBLOCK_V1:{name}:1:$__dof_region\""
        )
    elif operation == "vivado.wp013_assign_pblock":
        args = request.arguments
        cells = " ".join(_path_expression(item) for item in args["cell_names_b64"])
        name = args["pblock_id"]
        body = (
            f"set __dof_names [list {cells}]; set __dof_cells [get_cells -quiet $__dof_names]; "
            "if {[llength $__dof_cells] != [llength $__dof_names]} {error {wp013_cell_resolution_mismatch}}; "
            f"if {{[llength [get_pblocks -quiet {name}]] != 1}} {{error {{wp013_pblock_resolution_mismatch}}}}; "
            f"add_cells_to_pblock [get_pblocks {name}] $__dof_cells; "
            f"puts \"DOF_WP013_ASSIGN_PBLOCK_V1:{name}:[llength $__dof_cells]\""
        )
    elif operation == "vivado.wp013_remove_pblock":
        name = request.arguments["pblock_id"]
        body = (
            f"set __dof_pb [get_pblocks -quiet {name}]; "
            "if {[llength $__dof_pb] != 1} {error {wp013_pblock_resolution_mismatch}}; "
            f"delete_pblocks $__dof_pb; puts \"DOF_WP013_REMOVE_PBLOCK_V1:{name}:1\""
        )
    elif operation == "vivado.wp013_relocate_hardblocks":
        cells = " ".join(_path_expression(item) for item in request.arguments["cell_names_b64"])
        sites = " ".join(_path_expression(item) for item in request.arguments["site_names_b64"])
        body = (
            f"set __dof_cells [list {cells}]; set __dof_sites [list {sites}]; "
            "set __dof_n [llength $__dof_cells]; "
            "if {$__dof_n != [llength $__dof_sites]} {error {wp013_relocation_cardinality_mismatch}}; "
            "route_design -unroute; "
            "set __dof_moved 0; "
            "for {set __dof_i 0} {$__dof_i < $__dof_n} {incr __dof_i} {"
            "set __dof_cell [get_cells -quiet [lindex $__dof_cells $__dof_i]]; "
            "set __dof_site [get_sites -quiet [lindex $__dof_sites $__dof_i]]; "
            "if {[llength $__dof_cell] != 1 || [llength $__dof_site] != 1} {error {wp013_relocation_resolution_mismatch}}; "
            "set __dof_before [get_property NAME [get_sites -quiet -of_objects $__dof_cell]]; "
            "place_cell $__dof_cell $__dof_site; "
            "set __dof_after [get_property NAME [get_sites -quiet -of_objects $__dof_cell]]; "
            "if {$__dof_before eq $__dof_after} {error {wp013_relocation_no_effect}}; "
            "incr __dof_moved}; "
            "puts \"DOF_WP013_RELOCATE_V1:$__dof_n:$__dof_moved\""
        )
    elif operation == "vivado.wp014c_create_pblock":
        args = request.arguments
        name = args["pblock_id"]
        region = _path_expression(args["region_name_b64"])
        body = (
            f"set __dof_region {region}; if {{$__dof_region eq {{}}}} {{error {{wp014c_region_empty}}}}; "
            f"create_pblock {name}; resize_pblock [get_pblocks {name}] -add [list $__dof_region]; "
            f"puts \"DOF_WP014C_CREATE_PBLOCK_V1:{name}:1:$__dof_region\""
        )
    elif operation == "vivado.wp014c_resize_pblock":
        args = request.arguments
        name = args["pblock_id"]
        region = _path_expression(args["region_name_b64"])
        flag = "-add" if args["mode"] == "ADD" else "-remove"
        body = (
            f"set __dof_pb [get_pblocks -quiet {name}]; if {{[llength $__dof_pb] != 1}} {{error {{wp014c_pblock_resolution_mismatch}}}}; "
            f"set __dof_region {region}; resize_pblock $__dof_pb {flag} [list $__dof_region]; "
            f"puts \"DOF_WP014C_RESIZE_PBLOCK_V1:{name}:{args['mode']}:1\""
        )
    elif operation in {"vivado.wp014c_assign_pblock", "vivado.wp014c_unassign_pblock"}:
        args = request.arguments
        name = args["pblock_id"]
        cells = " ".join(_path_expression(item) for item in args["cell_names_b64"])
        command = "add_cells_to_pblock" if operation.endswith("assign_pblock") and "unassign" not in operation else "remove_cells_from_pblock"
        marker_name = "ASSIGN" if command.startswith("add") else "UNASSIGN"
        body = (
            f"set __dof_names [list {cells}]; set __dof_cells [get_cells -quiet $__dof_names]; "
            "if {[llength $__dof_cells] != [llength $__dof_names]} {error {wp014c_cell_resolution_mismatch}}; "
            f"set __dof_pb [get_pblocks -quiet {name}]; if {{[llength $__dof_pb] != 1}} {{error {{wp014c_pblock_resolution_mismatch}}}}; "
            f"{command} $__dof_pb $__dof_cells; puts \"DOF_WP014C_{marker_name}_PBLOCK_V1:{name}:[llength $__dof_cells]\""
        )
    elif operation == "vivado.wp014c_delete_pblock":
        name = request.arguments["pblock_id"]
        body = (
            f"set __dof_pb [get_pblocks -quiet {name}]; if {{[llength $__dof_pb] != 1}} {{error {{wp014c_pblock_resolution_mismatch}}}}; "
            f"delete_pblocks $__dof_pb; puts \"DOF_WP014C_DELETE_PBLOCK_V1:{name}:1\""
        )
    elif operation == "vivado.wp014c_relocate_hardblocks":
        cells = " ".join(_path_expression(item) for item in request.arguments["cell_names_b64"])
        sites = " ".join(_path_expression(item) for item in request.arguments["site_names_b64"])
        body = (
            f"set __dof_cells [list {cells}]; set __dof_sites [list {sites}]; set __dof_n [llength $__dof_cells]; "
            "if {$__dof_n != [llength $__dof_sites]} {error {wp014c_relocation_cardinality_mismatch}}; set __dof_moved 0; "
            "for {set __dof_i 0} {$__dof_i < $__dof_n} {incr __dof_i} {"
            "set __dof_cell [get_cells -quiet [lindex $__dof_cells $__dof_i]]; set __dof_site [get_sites -quiet [lindex $__dof_sites $__dof_i]]; "
            "if {[llength $__dof_cell] != 1 || [llength $__dof_site] != 1} {error {wp014c_relocation_resolution_mismatch}}; "
            "set __dof_before [get_property NAME [get_sites -quiet -of_objects $__dof_cell]]; place_cell $__dof_cell $__dof_site; "
            "set __dof_after [get_property NAME [get_sites -quiet -of_objects $__dof_cell]]; if {$__dof_before eq $__dof_after} {error {wp014c_relocation_no_effect}}; incr __dof_moved}; "
            "puts \"DOF_WP014C_RELOCATE_V1:$__dof_n:$__dof_moved\""
        )
    elif operation == "vivado.wp014c_replicate_net":
        net = _path_expression(request.arguments["net_name_b64"])
        body = (
            f"set __dof_net [get_nets -quiet {net}]; if {{[llength $__dof_net] != 1}} {{error {{wp014c_net_resolution_mismatch}}}}; "
            "set __dof_cells_before [llength [get_cells -hier]]; set __dof_nets_before [llength [get_nets -hier]]; "
            "phys_opt_design -force_replication_on_nets $__dof_net; "
            "set __dof_cells_after [llength [get_cells -hier]]; set __dof_nets_after [llength [get_nets -hier]]; "
            "if {$__dof_cells_before == $__dof_cells_after && $__dof_nets_before == $__dof_nets_after} {error {wp014c_replicate_net_no_effect}}; "
            "puts \"DOF_WP014C_REPLICATE_NET_V2:$__dof_cells_before:$__dof_cells_after:$__dof_nets_before:$__dof_nets_after\""
        )
    elif operation == "vivado.wp014c_route_net":
        net = _path_expression(request.arguments["net_name_b64"])
        flag = "-delay" if request.arguments["delay_mode"] == "DELAY" else "-auto_delay"
        body = (
            f"set __dof_net [get_nets -quiet {net}]; if {{[llength $__dof_net] != 1}} {{error {{wp014c_net_resolution_mismatch}}}}; "
            "set __dof_route_before [get_property ROUTE $__dof_net]; "
            # Targeted routing is a reroute operation.  Unroute only the
            # selected net first so the untouched design stays fully routed;
            # the implementation-heavy event remains the one route_design
            # call carrying the admitted delay mode.
            f"route_design -unroute -nets $__dof_net; route_design {flag} -nets $__dof_net; "
            "set __dof_route_after [get_property ROUTE $__dof_net]; "
            "if {$__dof_route_before eq $__dof_route_after} {error {wp014c_route_net_no_effect}}; "
            f"puts \"DOF_WP014C_ROUTE_NET_V2:{request.arguments['delay_mode']}:1:[string length $__dof_route_before]:[string length $__dof_route_after]\""
        )
    elif operation == "vivado.wp014h_batch_route_nets":
        nets = " ".join(_path_expression(item) for item in request.arguments["net_names_b64"])
        flag = "-delay" if request.arguments["delay_mode"] == "DELAY" else "-auto_delay"
        body = (
            f"set __dof_names [list {nets}]; set __dof_nets [get_nets -quiet $__dof_names]; "
            "if {[llength $__dof_nets] != [llength $__dof_names]} {error {wp014h_net_resolution_mismatch}}; "
            "set __dof_before {}; foreach __dof_net $__dof_nets {lappend __dof_before [get_property ROUTE $__dof_net]}; "
            f"route_design -unroute -nets $__dof_nets; route_design {flag} -nets $__dof_nets; "
            "set __dof_after {}; foreach __dof_net $__dof_nets {lappend __dof_after [get_property ROUTE $__dof_net]}; "
            "if {$__dof_before eq $__dof_after} {error {wp014h_batch_route_no_effect}}; "
            f"puts \"DOF_WP014H_BATCH_ROUTE_NETS_V1:{request.arguments['delay_mode']}:[llength $__dof_nets]:[string length $__dof_before]:[string length $__dof_after]\""
        )
    elif operation == "vivado.wp014h_apply_allocation":
        cells = " ".join(_path_expression(item) for item in request.arguments["cell_names_b64"])
        sites = " ".join(_path_expression(item) for item in request.arguments["site_names_b64"])
        body = (
            f"set __dof_cells [list {cells}]; set __dof_sites [list {sites}]; set __dof_n [llength $__dof_cells]; "
            "if {$__dof_n != [llength $__dof_sites]} {error {wp014h_allocation_cardinality_mismatch}}; set __dof_moved 0; "
            "for {set __dof_i 0} {$__dof_i < $__dof_n} {incr __dof_i} {"
            "set __dof_cell [get_cells -quiet [lindex $__dof_cells $__dof_i]]; set __dof_site [get_sites -quiet [lindex $__dof_sites $__dof_i]]; "
            "if {[llength $__dof_cell] != 1 || [llength $__dof_site] != 1} {error {wp014h_allocation_resolution_mismatch}}; "
            "set __dof_before [get_property NAME [get_sites -quiet -of_objects $__dof_cell]]; place_cell $__dof_cell $__dof_site; "
            "set __dof_after [get_property NAME [get_sites -quiet -of_objects $__dof_cell]]; if {$__dof_before eq $__dof_after} {error {wp014h_relocation_no_effect}}; incr __dof_moved}; "
            "puts \"DOF_WP014H_APPLY_ALLOCATION_V1:$__dof_n:$__dof_moved\"; "
            f"puts \"DOF_WP015B_ALLOCATION_EVIDENCE_V1:{request.arguments['dispatch_pairs_sha256']}:{request.arguments['target_identity_sha256']}:{request.arguments['compatibility_relation_sha256']}:{request.arguments['allocation_sha256']}\""
        )
    elif operation == "vivado.wp014c_place_cell":
        cell = _path_expression(request.arguments["cell_name_b64"])
        site = _path_expression(request.arguments["site_name_b64"])
        body = (
            f"set __dof_cell [get_cells -quiet {cell}]; set __dof_site [get_sites -quiet {site}]; "
            "if {[llength $__dof_cell] != 1 || [llength $__dof_site] != 1} {error {wp014c_place_resolution_mismatch}}; "
            "place_cell $__dof_cell $__dof_site; puts \"DOF_WP014C_PLACE_CELL_V1:1\""
        )
    elif operation == "vivado.wp014c_unplace_cell":
        cell = _path_expression(request.arguments["cell_name_b64"])
        body = (
            f"set __dof_cell [get_cells -quiet {cell}]; if {{[llength $__dof_cell] != 1}} {{error {{wp014c_cell_resolution_mismatch}}}}; "
            "unplace_cell $__dof_cell; puts \"DOF_WP014C_UNPLACE_CELL_V1:1\""
        )
    elif operation == "vivado.wp014c_swap_cells":
        first = _path_expression(request.arguments["first_cell_b64"])
        second = _path_expression(request.arguments["second_cell_b64"])
        body = (
            f"set __dof_first [get_cells -quiet {first}]; set __dof_second [get_cells -quiet {second}]; "
            "if {[llength $__dof_first] != 1 || [llength $__dof_second] != 1} {error {wp014c_swap_resolution_mismatch}}; "
            "swap_locs $__dof_first $__dof_second; puts \"DOF_WP014C_SWAP_CELLS_V1:2\""
        )
    elif operation in {
        "vivado.wp014c_set_cell_loc_fixed", "vivado.wp014c_set_net_dont_touch",
        "vivado.wp014c_set_pblock_exclude_placement", "vivado.wp014c_set_site_prohibit",
    }:
        target = _path_expression(request.arguments["target_name_b64"])
        getter, property_name = {
            "vivado.wp014c_set_cell_loc_fixed": ("get_cells", "IS_LOC_FIXED"),
            "vivado.wp014c_set_net_dont_touch": ("get_nets", "DONT_TOUCH"),
            "vivado.wp014c_set_pblock_exclude_placement": ("get_pblocks", "EXCLUDE_PLACEMENT"),
            "vivado.wp014c_set_site_prohibit": ("get_sites", "PROHIBIT"),
        }[operation]
        value = 1 if request.arguments["value"] else 0
        body = (
            f"set __dof_target [{getter} -quiet {target}]; if {{[llength $__dof_target] != 1}} {{error {{wp014c_property_target_resolution_mismatch}}}}; "
            f"set __dof_property_before [get_property {property_name} $__dof_target]; "
            f"set_property {property_name} {value} $__dof_target; "
            f"set __dof_property_after [get_property {property_name} $__dof_target]; "
            "if {$__dof_property_before eq $__dof_property_after} {error {wp014c_property_no_effect}}; "
            f"if {{$__dof_property_after ne {{{value}}}}} {{error {{wp014c_property_verification_failed}}}}; "
            f"puts \"DOF_WP014C_PROPERTY_V2:{property_name}:$__dof_property_before:$__dof_property_after:1\""
        )
    elif operation == "vivado.close_design":
        body = "close_design"
    else:
        raise WorkerProtocolError("operation_not_allowed")
    marker = f"{STATUS_PREFIX}{request.call_token}:"
    # Diagnostic metadata only: retain the caught Tcl result in the private
    # worker output without changing the command, status or retry behavior.
    error_marker = f"DOF_VIVADO_MUTATION_TCL_ERROR_V1:{request.call_token}:"
    return (
        f"set __dof_code [catch {{{body}}} __dof_result __dof_options]; "
        "if {$__dof_code != 0} {"
        f"puts \"{error_marker}[binary encode base64 -maxlen 0 "
        "[encoding convertto utf-8 $__dof_result]]\"}; "
        f"puts \"{marker}$__dof_code\"; flush stdout"
    )


def _build_wp013_query_tcl(arguments: dict[str, Any]) -> str:
    kind = arguments["query_kind"]
    last = arguments["top_k"] - 1
    encode = "binary encode base64 -maxlen 0 [encoding convertto utf-8"
    if kind == "timing_paths":
        return (
            "set __dof_objects [get_timing_paths -max_paths 32 -nworst 1]; "
            f"set __dof_objects [lrange $__dof_objects 0 {last}]; set __dof_i 0; "
            "foreach __dof_obj $__dof_objects {"
            "set __dof_slack [get_property -quiet SLACK $__dof_obj]; "
            "set __dof_delay [get_property -quiet DATAPATH_DELAY $__dof_obj]; "
            "set __dof_start [get_property -quiet STARTPOINT_PIN $__dof_obj]; "
            "set __dof_end [get_property -quiet ENDPOINT_PIN $__dof_obj]; "
            "if {$__dof_start eq \"\" || $__dof_end eq \"\"} {error {wp013_timing_path_endpoint_missing}}; "
            "set __dof_name [join [list path $__dof_i $__dof_start $__dof_end] {|}]; "
            "set __dof_attr [join [list $__dof_slack $__dof_delay $__dof_start $__dof_end] [format %c 31]]; "
            f"set __dof_name64 [{encode} $__dof_name]]; set __dof_attr64 [{encode} $__dof_attr]]; "
            f"puts \"DOF_WP013_QUERY_V1:{kind}:$__dof_i:$__dof_name64:$__dof_attr64\"; incr __dof_i}}; "
            f"puts \"DOF_WP013_QUERY_COUNT_V1:{kind}:$__dof_i\""
        )
    if kind in {"critical_cells", "critical_cell_locations"}:
        setup = (
            "set __dof_paths [get_timing_paths -max_paths 16 -nworst 1]; "
            "set __dof_objects [lsort -unique [get_cells -quiet -of_objects [get_pins -quiet -of_objects $__dof_paths]]]; "
        )
        if kind == "critical_cell_locations":
            attrs = (
                "set __dof_ref [get_property -quiet REF_NAME $__dof_obj]; "
                "set __dof_site [lindex [get_sites -quiet -of_objects $__dof_obj] 0]; "
                "set __dof_site_name [get_property -quiet NAME $__dof_site]; "
                "set __dof_region [lindex [get_clock_regions -quiet -of_objects $__dof_site] 0]; "
                "set __dof_region_name [get_property -quiet NAME $__dof_region]; "
                "set __dof_attr [join [list $__dof_ref $__dof_site_name $__dof_region_name] [format %c 31]]"
            )
        else:
            attrs = "set __dof_attr [get_property -quiet REF_NAME $__dof_obj]"
    elif kind == "critical_hardblocks":
        resource = arguments["resource_type"]
        patterns = {
            "DSP": "DSP*", "BRAM": "RAMB*", "URAM": "URAM*", "ANY": "{DSP* RAMB* URAM*}",
        }[resource]
        if resource == "ANY":
            filter_text = "{REF_NAME =~ DSP* || REF_NAME =~ RAMB* || REF_NAME =~ URAM*}"
        else:
            filter_text = f"{{REF_NAME =~ {patterns}}}"
        setup = (
            "set __dof_paths [get_timing_paths -max_paths 32 -nworst 1]; "
            f"set __dof_objects [lsort -unique [filter [get_cells -quiet -of_objects [get_pins -quiet -of_objects $__dof_paths]] {filter_text}]]; "
        )
        attrs = "set __dof_attr [get_property -quiet PRIMITIVE_TYPE $__dof_obj]"
    elif kind == "high_fanout_nets":
        # IS_CLOCK is blank rather than numeric on many ordinary nets in the
        # frozen 2025.1 DCPs.  Filtering with ``IS_CLOCK == 0`` therefore
        # produced a false empty set.  FLAT_PIN_COUNT is the installed-tool
        # property admitted by WP-014B and gives a deterministic, non-empty
        # candidate ordering without reconstructing targets after execution.
        setup = (
            "set __dof_objects [get_nets -hierarchical -filter {FLAT_PIN_COUNT >= 2}]; "
            "set __dof_pairs {}; foreach __dof_net $__dof_objects {set __dof_name [get_property NAME $__dof_net]; "
            "if {$__dof_name in {<const0> <const1>}} {continue}; lappend __dof_pairs [list [get_property FLAT_PIN_COUNT $__dof_net] $__dof_name]}; "
            "set __dof_pairs [lsort -integer -decreasing -index 0 $__dof_pairs]; set __dof_objects {}; "
            "foreach __dof_pair $__dof_pairs {lappend __dof_objects [get_nets -quiet [lindex $__dof_pair 1]]}; "
        )
        attrs = "set __dof_attr [get_property FLAT_PIN_COUNT $__dof_obj]"
    elif kind == "device_regions":
        setup = "set __dof_objects [lsort [get_clock_regions]]; "
        attrs = "set __dof_attr [get_property -quiet NAME $__dof_obj]"
    else:
        target = _path_expression(arguments["target_cell_b64"])
        setup = (
            f"set __dof_target [get_cells -quiet {target}]; "
            "if {[llength $__dof_target] != 1} {error {wp013_target_resolution_mismatch}}; "
            "set __dof_current [get_sites -quiet -of_objects $__dof_target]; "
            "if {[llength $__dof_current] != 1} {error {wp013_target_site_resolution_mismatch}}; "
            "set __dof_type [get_property SITE_TYPE $__dof_current]; "
            "set __dof_current_name [get_property NAME $__dof_current]; "
            "if {![regexp {^([A-Za-z0-9]+)_X[0-9]+Y[0-9]+$} $__dof_current_name __dof_match __dof_prefix]} {error {wp013_target_site_name_invalid}}; "
            "set __dof_all [get_sites -quiet ${__dof_prefix}_X*Y*]; "
            "set __dof_occupied [get_property LOC [get_cells -hier -quiet -filter {LOC != \"\"}]]; "
            "set __dof_objects {}; "
            "foreach __dof_site $__dof_all {"
            "set __dof_site_name [get_property NAME $__dof_site]; "
            "set __dof_site_type [get_property SITE_TYPE $__dof_site]; "
            "if {$__dof_site_type eq $__dof_type && [lsearch -exact $__dof_occupied $__dof_site_name] < 0 && $__dof_site_name ne $__dof_current_name} {lappend __dof_objects $__dof_site}}; "
            "set __dof_objects [lsort $__dof_objects]; "
        )
        attrs = "set __dof_attr $__dof_type"
    return (
        setup
        + f"set __dof_objects [lrange $__dof_objects 0 {last}]; set __dof_i 0; "
        + "foreach __dof_obj $__dof_objects {"
        + attrs
        + "; set __dof_name [get_property NAME $__dof_obj]; "
        + f"set __dof_name64 [{encode} $__dof_name]]; set __dof_attr64 [{encode} $__dof_attr]]; "
        + f"puts \"DOF_WP013_QUERY_V1:{kind}:$__dof_i:$__dof_name64:$__dof_attr64\"; incr __dof_i}}; "
        + f"puts \"DOF_WP013_QUERY_COUNT_V1:{kind}:$__dof_i\""
    )


def parse_tcl_result(call_token: str, output: bytes) -> tuple[int, bytes]:
    if not _TOKEN_RE.fullmatch(call_token):
        raise WorkerProtocolError("call_token_invalid")
    prefix = f"{STATUS_PREFIX}{call_token}:".encode("ascii")
    matches: list[int] = []
    retained: list[bytes] = []
    for line in output.splitlines(keepends=True):
        normalized = line.rstrip(b"\r\n")
        if normalized.startswith(prefix):
            suffix = normalized[len(prefix) :]
            if suffix not in {b"0", b"1", b"2", b"3", b"4", b"5"}:
                raise WorkerProtocolError("tcl_status_code_invalid")
            matches.append(int(suffix))
        else:
            retained.append(line)
    if len(matches) != 1:
        raise WorkerProtocolError("tcl_status_line_cardinality_invalid")
    return matches[0], b"".join(retained)


def _bound_dependency_manifest(root: Path) -> dict[str, Any]:
    dependencies = []
    for distribution_name in _RUNTIME_DISTRIBUTIONS:
        package = root / distribution_name
        metadata_matches = tuple(
            sorted(root.glob(f"{distribution_name}-*.dist-info"))
        )
        if not package.is_dir() or len(metadata_matches) != 1:
            raise WorkerProtocolError("bound_worker_dependency_layout_invalid")
        metadata = metadata_matches[0]
        version = None
        for line in (metadata / "METADATA").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("Version: "):
                version = line.removeprefix("Version: ").strip()
                break
        if not version:
            raise WorkerProtocolError("bound_worker_dependency_version_missing")
        candidates = [
            item
            for item in package.rglob("*")
            if item.is_file()
            and "__pycache__" not in item.parts
            and item.suffix != ".pyc"
        ]
        candidates.extend((metadata / "METADATA", metadata / "WHEEL"))
        records = []
        for candidate in candidates:
            resolved = candidate.resolve(strict=True)
            try:
                relative = resolved.relative_to(root)
            except ValueError as exc:
                raise WorkerProtocolError(
                    "bound_worker_dependency_escapes_root"
                ) from exc
            raw = resolved.read_bytes()
            records.append(
                {
                    "relative_name": relative.as_posix(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                }
            )
        records.sort(key=lambda item: item["relative_name"])
        dependencies.append(
            {
                "distribution": distribution_name,
                "version": version,
                "file_count": len(records),
                "total_size_bytes": sum(
                    item["size_bytes"] for item in records
                ),
                "content_sha256": _canonical_hash(records),
            }
        )
    payload = {
        "schema_version": _DEPENDENCY_MANIFEST_SCHEMA,
        "dependencies": dependencies,
    }
    return payload | {"manifest_sha256": _canonical_hash(payload)}


def _activate_bound_dependencies(
    dependency_root: str,
    expected_manifest_sha256: str,
) -> Any:
    root = Path(dependency_root).resolve(strict=True)
    if not root.is_dir():
        raise WorkerProtocolError("bound_worker_dependency_root_invalid")
    observed = _bound_dependency_manifest(root)
    if observed["manifest_sha256"] != expected_manifest_sha256:
        raise WorkerProtocolError("bound_worker_dependency_manifest_mismatch")
    # -S prevents .pth/sitecustomize execution.  Load both packages from their
    # exact hash-bound __init__.py paths instead of adding the broad
    # site-packages directory to sys.path, where a root-level pexpect.py or
    # extension module could otherwise shadow the attested package directory.
    loaded: dict[str, Any] = {}
    for package_name in ("ptyprocess", "pexpect"):
        package_dir = (root / package_name).resolve(strict=True)
        package_init = (package_dir / "__init__.py").resolve(strict=True)
        spec = importlib.util.spec_from_file_location(
            package_name,
            package_init,
            submodule_search_locations=[os.fspath(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise WorkerProtocolError("bound_worker_dependency_spec_unavailable")
        module = importlib.util.module_from_spec(spec)
        previous = sys.modules.get(package_name)
        sys.modules[package_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            if previous is None:
                sys.modules.pop(package_name, None)
            else:
                sys.modules[package_name] = previous
            raise
        loaded[package_name] = module
    return loaded["pexpect"]


def _run(
    vivado_exec: str,
    initialization_timeout: float,
    *,
    run_workspace: str,
    isolated_home: str,
    dependency_root: str,
    dependency_manifest_sha256: str,
) -> int:
    print(PYTHON_VERSION_PREFIX + platform.python_version(), flush=True)
    print(WORKER_VERSION_PREFIX + WORKER_VERSION, flush=True)
    print(
        WORKER_PROTOCOL_PREFIX + WORKER_TRANSPORT_PROTOCOL_VERSION,
        flush=True,
    )
    try:
        pexpect = _activate_bound_dependencies(
            dependency_root,
            dependency_manifest_sha256,
        )
    except Exception as exc:
        print(f"worker dependency unavailable: {type(exc).__name__}", file=sys.stderr)
        return 72
    try:
        resolved_exec, vivado_cwd, vivado_env = _validate_init_policy(
            vivado_exec,
            run_workspace=run_workspace,
            isolated_home=isolated_home,
        )
    except (OSError, WorkerProtocolError) as exc:
        print(f"Vivado init policy rejected: {type(exc).__name__}", file=sys.stderr)
        return 79
    print(INIT_POLICY_LINE, flush=True)
    print(
        EFFECTIVE_ENV_PREFIX
        + effective_vivado_environment_hash(vivado_env, vivado_cwd),
        flush=True,
    )
    try:
        owned = _spawn_owned_pty(
            resolved_exec,
            initialization_timeout,
            cwd=vivado_cwd,
            env=vivado_env,
        )
    except Exception as exc:
        print(f"Vivado process start failed: {type(exc).__name__}", file=sys.stderr)
        return 72
    child = owned.channel
    if not _same_owned_session(owned.identity):
        _bounded_stop(owned, known_identities=(owned.identity,))
        return 78
    print(LAUNCHED_LINE, flush=True)
    prompt = rb"\r?\nVivado% "
    try:
        child.expect(prompt, timeout=initialization_timeout)
    except (pexpect.TIMEOUT, pexpect.EOF):
        _bounded_stop(owned)
        return 73
    tree_owned, initial_tree = _tree_is_owned(owned.process.pid)
    if not tree_owned:
        _bounded_stop(owned, known_identities=initial_tree)
        return 78
    print(STARTED_LINE, flush=True)
    banner = bytes(child.before or b"").decode("utf-8", "replace")
    version_match = re.search(
        r"\bVivado\s+v([0-9]{4}\.[0-9]+(?:\.[0-9]+)?)", banner
    )
    print(
        VERSION_PREFIX
        + (version_match.group(1) if version_match is not None else "NOT_PARSED"),
        flush=True,
    )
    print(READY_LINE, flush=True)
    for raw in sys.stdin.buffer:
        try:
            request = parse_request_line(raw.rstrip(b"\r\n"))
        except WorkerProtocolError as exc:
            print(f"worker request rejected: {exc}", file=sys.stderr, flush=True)
            return 74
        if request.operation_id == "worker.exit":
            clean = _bounded_stop(
                owned,
                known_identities=initial_tree,
                graceful=True,
                graceful_timeout_seconds=request.timeout_seconds,
            )
            # Some Vivado builds close the PTY before pexpect observes EOF,
            # which makes the helper's diagnostic boolean conservative even
            # after it has reaped the direct child.  Accept that narrow case
            # only from independently verified zero-live identities.
            if not clean and (
                owned.process.poll() is None or _live_verified(initial_tree)
            ):
                return 75
            return 0
        tree_owned, observed_tree = _tree_is_owned(owned.process.pid)
        if not tree_owned:
            _bounded_stop(owned, known_identities=observed_tree)
            return 78
        child.sendline(build_fixed_tcl(request).encode("utf-8"))
        try:
            child.expect(prompt, timeout=request.timeout_seconds)
        except (pexpect.TIMEOUT, pexpect.EOF):
            _bounded_stop(owned, known_identities=observed_tree)
            return 76
        tree_owned, completed_tree = _tree_is_owned(owned.process.pid)
        if not tree_owned:
            _bounded_stop(owned, known_identities=completed_tree)
            return 78
        output = bytes(child.before or b"")
        sys.stdout.buffer.write(output)
        if output and not output.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.write(terminal_marker(request.call_token) + b"\n")
        sys.stdout.buffer.flush()
    _bounded_stop(owned, known_identities=initial_tree)
    return 77


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Owned fixed Vivado technical mutation worker for WP-004A."
    )
    parser.add_argument("--vivado-exec", required=True)
    parser.add_argument("--dependency-root", required=True)
    parser.add_argument("--dependency-manifest-sha256", required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--run-workspace", required=True)
    parser.add_argument("--isolated-home", required=True)
    parser.add_argument(
        "--initialization-timeout-seconds", type=float, default=60.0
    )
    args = parser.parse_args(argv)
    return _run(
        args.vivado_exec,
        args.initialization_timeout_seconds,
        run_workspace=args.run_workspace,
        isolated_home=args.isolated_home,
        dependency_root=args.dependency_root,
        dependency_manifest_sha256=args.dependency_manifest_sha256,
    )


if __name__ == "__main__":
    raise SystemExit(main())
