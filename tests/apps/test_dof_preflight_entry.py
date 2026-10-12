from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from optimizer_pipeline.apps.dof_preflight.entry import (
    build_dof_preflight_parser,
    main,
    run_dof_preflight_entry,
)


def _argv() -> list[str]:
    return [
        "plan.json",
        "--dcp",
        "/private/checkpoints/root.dcp",
        "--run-dir",
        "/private/evidence/preflight",
        "--vivado-exec",
        "/private/tools/vivado",
        "--python-exec",
        "/private/env/bin/python",
    ]


def test_preflight_parser_requires_every_private_launch_binding() -> None:
    parser = build_dof_preflight_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["plan.json"])
    args = parser.parse_args(_argv())
    assert args.plan == Path("plan.json")
    assert args.dcp == Path("/private/checkpoints/root.dcp")
    assert args.run_dir == Path("/private/evidence/preflight")
    assert args.vivado_exec == Path("/private/tools/vivado")
    assert args.python_exec == Path("/private/env/bin/python")


def test_preflight_help_describes_fixed_read_only_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_dof_preflight_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "exactly one" in help_text
    assert "read-only L0" in help_text
    for option in ("--dcp", "--run-dir", "--vivado-exec", "--python-exec"):
        assert option in help_text


def test_preflight_entry_dispatches_exactly_once_and_emits_public_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Path, dict[str, Path]]] = []

    def fake_runner(plan: Path, **bindings: Path) -> dict[str, object]:
        calls.append((plan, bindings))
        return {
            "schema_version": "dof.real-preflight-result.v1",
            "endpoint": "VALID_COMPLETION",
            "endpoint_success": True,
            "run_id": "real-preflight-001",
        }

    args = build_dof_preflight_parser().parse_args(_argv())
    assert run_dof_preflight_entry(args, runner=fake_runner) == 0
    assert calls == [
        (
            Path("plan.json"),
            {
                "dcp_path": Path("/private/checkpoints/root.dcp"),
                "run_dir": Path("/private/evidence/preflight"),
                "vivado_exec": Path("/private/tools/vivado"),
                "python_exec": Path("/private/env/bin/python"),
            },
        )
    ]
    output = capsys.readouterr().out
    assert json.loads(output)["endpoint"] == "VALID_COMPLETION"
    assert "/private/" not in output


def test_preflight_entry_emits_failed_summary_and_returns_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = build_dof_preflight_parser().parse_args(_argv())

    def failed_runner(_plan: Path, **_bindings: Path) -> dict[str, object]:
        return {
            "schema_version": "dof.real-preflight-result.v1",
            "endpoint": "TIMEOUT",
            "endpoint_success": False,
        }

    assert run_dof_preflight_entry(args, runner=failed_runner) == 1
    assert json.loads(capsys.readouterr().out)["endpoint"] == "TIMEOUT"


def test_preflight_entry_rejects_non_object_runner_result() -> None:
    args = build_dof_preflight_parser().parse_args(_argv())

    def bad_runner(_plan: Path, **_bindings: Path) -> object:
        return ["not", "a", "public", "object"]

    with pytest.raises(TypeError, match="public JSON object"):
        run_dof_preflight_entry(args, runner=bad_runner)  # type: ignore[arg-type]


def test_preflight_entry_rejects_missing_programmatic_binding_before_dispatch() -> None:
    args = argparse.Namespace(
        plan=Path("plan.json"),
        dcp=Path("root.dcp"),
        run_dir=Path("run"),
        vivado_exec=Path("vivado"),
        python_exec=None,
    )
    called = False

    def forbidden_runner(_plan: Path, **_bindings: Path) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    with pytest.raises(ValueError, match="python_exec"):
        run_dof_preflight_entry(args, runner=forbidden_runner)
    assert called is False


def test_preflight_cli_error_does_not_echo_private_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_args: argparse.Namespace) -> int:
        raise FileNotFoundError("/private/checkpoints/secret-root.dcp")

    monkeypatch.setattr(
        "optimizer_pipeline.apps.dof_preflight.entry.run_dof_preflight_entry",
        fail,
    )
    assert main(_argv()) == 2
    error = capsys.readouterr().err
    assert error.strip() == "fpga-dof-preflight: FileNotFoundError"
    assert "/private/" not in error
