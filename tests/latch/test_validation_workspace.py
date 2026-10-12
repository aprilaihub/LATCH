import json
from pathlib import Path
import subprocess

from optimizer_pipeline.backends.xilinx import functional_validation


def test_preparation_uses_explicit_smoke_workspace(tmp_path, monkeypatch):
    root = tmp_path / "root.dcp"; root.write_text("root")
    child = tmp_path / "child.dcp"; child.write_text("child")
    output = tmp_path / "isolated-validation"
    def fake_run(command, **kwargs):
        workspace = output / "dcp_validation_fixture"
        workspace.mkdir()
        (workspace / "validation_report.json").write_text(json.dumps(dict(phase1_passed=True,
            seed_workspace_prepared=True, simulation_report=dict(precheck_passed=True, prepared_seed_workspace=str(workspace)))))
        assert command[command.index("--workspace-dir") + 1] == str(output.resolve())
        assert command.count("--seed") == 3
        return subprocess.CompletedProcess(command, 0, "Working directory: " + str(workspace), "")
    monkeypatch.setattr(functional_validation, "run_owned", fake_run)
    result = functional_validation.prepare_workspace(golden=root, revised=child, output=output,
        python=Path("python"), vivado=Path("vivado"), seeds=[1, 2, 3], vectors=200,
        precheck_vectors=100, timeout_seconds=60)
    assert result["workspace"].is_relative_to(output)
    assert root.read_text() == "root" and child.read_text() == "child"


def test_validator_optional_workspace_preserves_default(tmp_path, monkeypatch):
    import validate_dcps
    monkeypatch.setattr(validate_dcps, "ensure_vivado_on_path", lambda: {})
    seen = []
    def temporary(*, prefix, dir):
        seen.append(Path(dir))
        return str(tmp_path / ("fixture-" + str(len(seen))))
    monkeypatch.setattr(validate_dcps.tempfile, "mkdtemp", temporary)
    validate_dcps.DCPValidator(tmp_path / "golden", tmp_path / "child")
    validate_dcps.DCPValidator(tmp_path / "golden", tmp_path / "child", workspace_dir=tmp_path / "smoke")
    assert seen == [Path(validate_dcps.__file__).parent, tmp_path / "smoke"]


def test_mcp_children_use_validator_owned_cwd(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import validate_dcps
    monkeypatch.setattr(validate_dcps, "ensure_vivado_on_path", lambda: {})
    seen = []
    def transport(params):
        seen.append(params)
        return ("read", "write")
    class Stack:
        async def enter_async_context(self, value): return value
    async def initialize(*args): pass
    monkeypatch.setattr(validate_dcps, "stdio_client", transport)
    monkeypatch.setattr(validate_dcps, "StdioServerParameters", SimpleNamespace)
    monkeypatch.setattr(validate_dcps, "ClientSession", lambda *args: object())
    validator = validate_dcps.DCPValidator(tmp_path / "golden", tmp_path / "child",
        workspace_dir=tmp_path / "owned")
    validator.exit_stack = Stack()
    monkeypatch.setattr(validator, "_initialize_mcp_session", initialize)
    asyncio.run(validator.start_servers())
    assert len(seen) == 2
    for p in seen:
        assert p.args[:1] == ["-c"], p.args
        assert Path(p.args[2]) == validator.temp_dir.resolve()
    assert all(Path(p.args[3]).is_absolute() for p in seen)


def test_workspace_wrapper_preserves_child_and_parent_cwd(tmp_path):
    import os
    import sys
    import validate_dcps
    before = Path.cwd()
    fixture = tmp_path / "fixture.py"
    fixture.write_text("import os; print(os.getcwd()); print(os.getpid())")
    process = subprocess.Popen([sys.executable, *validate_dcps.workspace_server_args(tmp_path, [str(fixture)])],
        stdout=subprocess.PIPE, text=True)
    stdout, _ = process.communicate(timeout=10)
    assert process.returncode == 0
    cwd, pid = stdout.strip().splitlines()
    assert Path(cwd) == tmp_path
    assert int(pid) == process.pid
    assert Path.cwd() == before
