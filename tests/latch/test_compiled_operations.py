"""Boundary checks for the extracted compiled-operation dispatcher."""

import ast
import asyncio
import base64
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from optimizer_pipeline.backends.xilinx import compiled_operations as operations
from optimizer_pipeline.dof.vivado_mutation_worker import STATUS_PREFIX


ROOT = Path(__file__).resolve().parents[2]


def _top_level_function(
    tree: ast.Module, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _class_method(
    tree: ast.Module, class_name: str, method_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )


def _body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ast.dump(
        ast.Module(body=node.body, type_ignores=[]), include_attributes=False
    )


def test_extracted_dispatch_and_helpers_keep_historical_bodies():
    run_tree = ast.parse((ROOT / "scripts/run_wp013a_e0.py").read_text())
    admission_tree = ast.parse((ROOT / "scripts/run_wp013_admission.py").read_text())
    mutation_tree = ast.parse(
        (ROOT / "optimizer_pipeline/dof/real_mutation.py").read_text()
    )
    specs_tree = ast.parse(
        (ROOT / "optimizer_pipeline/dof/single_submission/specs.py").read_text()
    )
    extracted_tree = ast.parse(Path(operations.__file__).read_text())

    for name in (
        "_macro_attempt",
        "_execute_macro",
        "_typed_supported",
        "_execute_rapidwright_typed",
    ):
        assert _body(_top_level_function(extracted_tree, name)) == _body(
            _top_level_function(run_tree, name)
        )

    historical_typed = copy.deepcopy(_top_level_function(run_tree, "_execute_typed"))

    class RenameAdmissionClient(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.AST:
            if node.id == "Admission":
                node.id = "_WorkerClient"
            return node

    historical_typed = RenameAdmissionClient().visit(historical_typed)
    current_typed = copy.deepcopy(_top_level_function(extracted_tree, "_execute_typed"))
    # The only added field projects existing instrumentation in memory instead
    # of reopening restricted logs. Keep every dispatch/body assertion intact.
    result = current_typed.body[-1]
    assert isinstance(result, ast.Return) and isinstance(result.value, ast.Dict)
    indexes = [i for i, key in enumerate(result.value.keys)
               if isinstance(key, ast.Constant) and key.value == "instrumentation_markers"]
    assert len(indexes) == 1
    index = indexes[0]
    expected = ast.parse("instrumentation_markers(backend.raw_call_outputs)", mode="eval").body
    assert ast.dump(result.value.values[index]) == ast.dump(expected)
    del result.value.keys[index], result.value.values[index]
    assert _body(current_typed) == _body(
        historical_typed
    )

    assert _body(_class_method(extracted_tree, "_WorkerClient", "call")) == _body(
        _class_method(admission_tree, "Admission", "call")
    )
    assert _body(
        _top_level_function(extracted_tree, "_verify_worker_attestation")
    ) == _body(_top_level_function(mutation_tree, "_verify_worker_attestation"))

    for name in ("digest", "file_digest", "read_object", "write_atomic"):
        assert _body(_top_level_function(extracted_tree, name)) == _body(
            _top_level_function(run_tree, name)
        )
    for name in (
        "b64",
        "relocation_effect_count",
        "allocation_effect_evidence",
        "route_status_clean",
    ):
        assert _body(_top_level_function(extracted_tree, name)) == _body(
            _top_level_function(admission_tree, name)
        )
    assert _body(_top_level_function(extracted_tree, "canonical_json_bytes")) == _body(
        _top_level_function(specs_tree, "canonical_json_bytes")
    )


def test_public_dispatch_rejects_unsupported_host_operations_before_tool_setup(
    tmp_path,
):
    compiled = SimpleNamespace(
        host_steps=[SimpleNamespace(operation="vivado.exec_tcl")]
    )
    result = asyncio.run(
        operations.execute_compiled(
            compiled,
            object(),
            tmp_path / "missing.dcp",
            tmp_path / "out",
            Path(sys.executable),
            Path(sys.executable),
        )
    )
    assert result["canonical_endpoint"] == "CAPABILITY_ACTION_REJECTION"
    assert result["unsupported_host_operations"] == ["vivado.exec_tcl"]
    assert result["actual_heavy_calls"] == result["actual_light_mutations"] == 0


def test_embedded_macro_after_mutation_is_rejected_before_tool_setup(tmp_path):
    compiled = SimpleNamespace(
        host_steps=[
            SimpleNamespace(operation="vivado.place"),
            SimpleNamespace(operation="macro.execute"),
        ]
    )
    result = asyncio.run(
        operations.execute_compiled(
            compiled,
            object(),
            tmp_path / "missing.dcp",
            tmp_path / "out",
            Path(sys.executable),
            Path(sys.executable),
        )
    )
    assert result["canonical_endpoint"] == "CAPABILITY_ACTION_REJECTION"
    assert result["error"] == "embedded_macro_must_precede_mutating_host_steps"
    assert result["actual_heavy_calls"] == result["actual_light_mutations"] == 0


def test_macro_fallback_uses_fake_owned_process_runner(tmp_path, monkeypatch):
    observed = {}

    class FakeProcess:
        endpoint = SimpleNamespace(value="SUCCEEDED")
        cleanup_ok = True

        def to_dict(self, *, include_output_text=False):
            return {"cleanup_ok": True, "endpoint": "SUCCEEDED"}

    class FakeRunner:
        def __init__(self, limits):
            observed["limits"] = limits

        async def run(self, command, *, cwd, env):
            observed.update(command=command, cwd=cwd, env=env)
            return FakeProcess()

    monkeypatch.setattr(operations, "OwnedProcessRunner", FakeRunner)
    monkeypatch.setattr(
        operations,
        "_macro_attempt",
        lambda _out, _flow: {
            "canonical_endpoint": "VALID_COMPLETION",
            "actual_heavy_calls": 2,
        },
    )
    result = asyncio.run(
        operations._execute_macro(
            root=tmp_path / "root.dcp",
            flow="place_route",
            out=tmp_path / "macro-out",
            python=Path(sys.executable),
        )
    )

    assert observed["command"][1] == str(
        operations.REPO / "scripts" / "run_flow_matrix.py"
    )
    assert observed["cwd"] == operations.REPO
    assert observed["env"]["PYTHONUNBUFFERED"] == "1"
    assert result["canonical_endpoint"] == "VALID_COMPLETION"
    assert result["process"]["cleanup_ok"] is True


def test_typed_dispatch_uses_fake_attested_worker_session(tmp_path, monkeypatch):
    root = tmp_path / "root.dcp"
    root.write_bytes(b"immutable root")
    observed_calls = []

    class FakeProcessResult:
        def to_dict(self, *, include_output_text=False):
            return {"cleanup_ok": True, "endpoint": "SUCCEEDED"}

    class FakeSession:
        def __init__(self, *args, **kwargs):
            argv = args[0]
            environment = kwargs["env"]
            home = Path(argv[argv.index("--isolated-home") + 1])
            env_hash = operations.effective_vivado_environment_hash(environment, home)
            markers = [
                operations.INIT_POLICY_LINE,
                operations.LAUNCHED_LINE,
                operations.STARTED_LINE,
                operations.READY_LINE,
                "DOF_VIVADO_VERSION_V1:2025.1",
                f"{operations.PYTHON_VERSION_PREFIX}{__import__('platform').python_version()}",
                f"{operations.EFFECTIVE_ENV_PREFIX}{env_hash}",
                f"{operations.WORKER_VERSION_PREFIX}{operations.WORKER_VERSION}",
                f"{operations.WORKER_PROTOCOL_PREFIX}{operations.WORKER_TRANSPORT_PROTOCOL_VERSION}",
            ]
            self.startup_output = SimpleNamespace(
                data=("\n".join(markers) + "\n").encode()
            )
            self.result = None

        async def start(self):
            return self

        async def call(self, raw, *, terminal_marker, timeout_seconds):
            request = json.loads(raw)
            operation = request["operation_id"]
            arguments = request["arguments"]
            observed_calls.append(operation)
            if operation == "vivado.write_checkpoint":
                child = Path(os_fsdecode(base64.b64decode(arguments["dcp_path_b64"])))
                child.write_bytes(b"fake child")
            if operation == "vivado.report_route_status":
                output = b"# of nets with routing errors: 0\n# of unrouted nets: 0\nfully routed\n"
            elif operation == "vivado.report_timing_summary":
                output = b"fake timing report\n"
            else:
                output = b""
            payload = f"{STATUS_PREFIX}{request['call_token']}:0\n".encode() + output
            return SimpleNamespace(stdout=SimpleNamespace(data=payload))

        async def close(self):
            return FakeProcessResult()

    def os_fsdecode(value):
        return value.decode(sys.getfilesystemencoding())

    monkeypatch.setattr(operations, "OwnedLineProtocolSession", FakeSession)
    compiled = SimpleNamespace(host_steps=[])
    result = asyncio.run(
        operations.execute_compiled(
            compiled,
            object(),
            root,
            tmp_path / "typed-out",
            Path(sys.executable),
            Path(sys.executable),
        )
    )

    assert result["canonical_endpoint"] == "VALID_COMPLETION"
    assert result["root_unchanged"] is True
    assert result["process"]["cleanup_ok"] is True
    assert result["child"]["size_bytes"] == len(b"fake child")
    assert observed_calls == [
        "vivado.open_checkpoint",
        "vivado.report_timing_summary",
        "vivado.write_checkpoint",
        "vivado.report_timing_summary",
        "vivado.report_route_status",
    ]
