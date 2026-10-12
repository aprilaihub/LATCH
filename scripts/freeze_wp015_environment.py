#!/usr/bin/env python3
"""Capture a public-safe WP-015 host/tool environment manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
from typing import Any


REPO = Path(__file__).resolve().parents[1]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def command(args: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
    completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=120)
    output = (completed.stdout + "\n" + completed.stderr).strip()
    # Version commands must never carry environment/license endpoints into the
    # manifest.  Retain only non-empty lines without server/path-like tokens.
    public_lines = [
        line.strip() for line in output.splitlines() if line.strip()
        and "LICENSE" not in line.upper() and "SERVER" not in line.upper()
        and "@" not in line and not line.strip().startswith("/")
    ]
    return {
        "returncode": completed.returncode,
        "public_output": public_lines[:20],
        "raw_output_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> int:
    git_head = command(["git", "rev-parse", "HEAD"], cwd=REPO)
    git_tree = command(["git", "rev-parse", "HEAD^{tree}"], cwd=REPO)
    git_status = command(["git", "status", "--porcelain"], cwd=REPO)
    rapidwright_git = command(["git", "rev-parse", "HEAD"], cwd=args.rapidwright) if (args.rapidwright / ".git").exists() else None
    tool_files = []
    for path in (args.python, args.vivado):
        resolved = path.resolve()
        tool_files.append({
            "name": path.name, "sha256": file_sha256(resolved) if resolved.is_file() else None,
            "size_bytes": resolved.stat().st_size if resolved.is_file() else None,
        })
    core = {
        "schema_version": "dof.wp015-public-environment-manifest.v1",
        "repository": {"head": git_head["public_output"], "tree": git_tree["public_output"]},
        "repository_clean": git_status["public_output"] == [],
        "platform": {
            "system": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "logical_cpu_count": os.cpu_count(),
        },
        "tools": {
            "python": command([str(args.python), "--version"]),
            "vivado": command([str(args.vivado), "-version"]),
            "java": command(["java", "-version"]),
            "rapidwright_git": rapidwright_git,
            "executable_files": tool_files,
        },
        "required_environment_presence": {
            key: bool(os.environ.get(key)) for key in (
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
                "GEMINI_API_KEY", "PYTHON", "VIVADO_EXEC", "RAPIDWRIGHT_PATH",
            )
        },
        "secrets_or_license_endpoints_persisted": False,
        "site_specific_absolute_tool_paths_persisted": False,
    }
    result = core | {"manifest_sha256": digest(core)}
    write(args.output, result)
    print(json.dumps({
        "repository_clean": result["repository_clean"],
        "vivado_returncode": result["tools"]["vivado"]["returncode"],
        "manifest_sha256": result["manifest_sha256"],
    }, sort_keys=True))
    return 0 if result["repository_clean"] and result["tools"]["vivado"]["returncode"] == 0 else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("--python", type=Path, required=True)
    value.add_argument("--vivado", type=Path, required=True)
    value.add_argument("--rapidwright", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
