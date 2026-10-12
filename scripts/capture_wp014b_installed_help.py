#!/usr/bin/env python3
"""Capture and hash the authoritative installed Vivado 2025.1 help surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any


REPO = Path(__file__).resolve().parents[1]
SCHEMA = "dof.wp014b-installed-help.v1"
COMMANDS = (
    "opt_design", "place_design", "phys_opt_design", "route_design",
    "power_opt_design", "place_cell", "unplace_cell", "swap_locs",
    "create_pblock", "resize_pblock", "move_pblock", "add_cells_to_pblock",
    "remove_cells_from_pblock", "delete_pblocks", "set_property",
    "reset_property", "list_property", "list_property_value", "report_property",
    "get_timing_paths", "report_timing", "report_timing_summary",
    "report_high_fanout_nets", "report_design_analysis", "report_qor_assessment",
    "report_qor_suggestions", "get_cells", "get_nets", "get_sites",
    "get_pblocks", "get_slrs", "get_clock_regions",
)
MODES = ("help", "syntax", "args")
TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")
ARGUMENT = re.compile(r"(?<![A-Za-z0-9_])-([A-Za-z][A-Za-z0-9_]*)")
BULLET_VALUE = re.compile(r"^\s*\*\s+([A-Za-z][A-Za-z0-9_+.-]*)\b", re.MULTILINE)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def repository_binding() -> dict[str, str]:
    status = subprocess.run(
        ("git", "status", "--porcelain=v1"), cwd=REPO,
        check=True, capture_output=True, text=True,
    ).stdout
    if status:
        raise RuntimeError("tracked_or_untracked_repository_not_clean")
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=REPO,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ("git", "rev-parse", "HEAD^{tree}"), cwd=REPO,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return {"head_commit": head, "tree": tree}


def tcl() -> bytes:
    lines = [
        'puts "DOF_VERSION_BEGIN"',
        "puts [version -short]",
        'puts "DOF_VERSION_END"',
    ]
    for command in COMMANDS:
        if not TOKEN.fullmatch(command):
            raise RuntimeError("unsafe_help_command")
        for mode in MODES:
            invocation = f"help {command}" if mode == "help" else f"help -{mode} {command}"
            lines.extend(
                (
                    f'puts "DOF_HELP_BEGIN:{command}:{mode}"',
                    f"set __dof_code [catch {{{invocation}}} __dof_result __dof_options]",
                    "puts $__dof_result",
                    f'puts "DOF_HELP_STATUS:{command}:{mode}:$__dof_code"',
                    f'puts "DOF_HELP_END:{command}:{mode}"',
                )
            )
    lines.append("exit")
    return ("\n".join(lines) + "\n").encode()


def sections(stdout: bytes) -> tuple[str, dict[tuple[str, str], tuple[int, bytes]]]:
    text = stdout.decode("utf-8", errors="replace")
    version_match = re.search(r"DOF_VERSION_BEGIN\r?\n(.*?)\r?\nDOF_VERSION_END", text, re.S)
    if version_match is None:
        raise RuntimeError("vivado_version_marker_missing")
    version = version_match.group(1).strip()
    parsed: dict[tuple[str, str], tuple[int, bytes]] = {}
    for command in COMMANDS:
        for mode in MODES:
            pattern = re.compile(
                rf"DOF_HELP_BEGIN:{re.escape(command)}:{mode}\r?\n(.*?)\r?\n"
                rf"DOF_HELP_STATUS:{re.escape(command)}:{mode}:([0-9]+)\r?\n"
                rf"DOF_HELP_END:{re.escape(command)}:{mode}", re.S,
            )
            match = pattern.search(text)
            if match is None:
                raise RuntimeError(f"help_marker_missing:{command}:{mode}")
            parsed[(command, mode)] = (int(match.group(2)), match.group(1).encode())
    return version, parsed


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--vivado", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    binding = repository_binding()
    vivado = args.vivado.resolve(strict=True)
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.mkdir(parents=True, mode=0o700)
    script = tcl()
    write(args.out / "raw" / "capture.tcl", script)
    completed = subprocess.run(
        (
            os.fspath(vivado), "-mode", "batch", "-nolog", "-nojournal",
            "-notrace", "-source", os.fspath(args.out / "raw" / "capture.tcl"),
        ),
        cwd=args.out, capture_output=True, timeout=1800,
    )
    write(args.out / "raw" / "session.stdout.txt", completed.stdout)
    write(args.out / "raw" / "session.stderr.txt", completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"vivado_help_capture_failed:{completed.returncode}")
    version, parsed = sections(completed.stdout)
    if not version.startswith("2025.1"):
        raise RuntimeError("installed_vivado_version_not_2025_1")

    rows = []
    for (command, mode), (status, raw) in parsed.items():
        relative = Path("raw") / command / f"{mode}.txt"
        write(args.out / relative, raw)
        text = raw.decode(errors="replace")
        rows.append(
            {
                "command": command,
                "mode": mode,
                "status": status,
                "relative_path": relative.as_posix(),
                "sha256": digest(raw),
                "size_bytes": len(raw),
                "arguments": sorted(set(ARGUMENT.findall(text))),
                "bullet_value_tokens": sorted(set(BULLET_VALUE.findall(text))),
            }
        )
    normalised = {
        "schema_version": SCHEMA,
        "authority": "installed_vivado_help",
        "vivado_version_short": version,
        "commands": list(COMMANDS),
        "modes": list(MODES),
        "rows": rows,
        "all_status_zero": all(row["status"] == 0 for row in rows),
    }
    write(args.out / "normalised" / "installed-help.json", canonical(normalised))
    license_names = ("XILINXD_LICENSE_FILE", "LM_LICENSE_FILE")
    environment = {
        "schema_version": "dof.wp014b-environment-binding.v1",
        "repository": binding,
        "vivado_version_short": version,
        "vivado_executable_resolved": os.fspath(vivado),
        "vivado_executable_sha256": digest(vivado.read_bytes()),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "target_device": "xcvu3p-ffvc1517-2-e",
        "license_environment": {
            name: {
                "present": bool(os.environ.get(name)),
                "value_sha256": digest(os.environ[name].encode()) if os.environ.get(name) else None,
            }
            for name in license_names
        },
    }
    write(args.out / "environment-binding.json", canonical(environment))
    files = []
    for path in sorted(item for item in args.out.rglob("*") if item.is_file()):
        raw = path.read_bytes()
        files.append({
            "relative_path": path.relative_to(args.out).as_posix(),
            "sha256": digest(raw), "size_bytes": len(raw),
        })
    hashes = {
        "schema_version": "dof.wp014b-installed-help-hashes.v1",
        "repository": binding,
        "file_count": len(files),
        "files": files,
    }
    hashes["manifest_sha256"] = digest(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode())
    write(args.out / "hashes.json", canonical(hashes))
    print(json.dumps({
        "accepted": normalised["all_status_zero"], "version": version,
        "command_count": len(COMMANDS), "row_count": len(rows),
        "manifest_sha256": hashes["manifest_sha256"],
    }, sort_keys=True))
    return 0 if normalised["all_status_zero"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
