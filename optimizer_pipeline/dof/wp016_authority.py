"""One-shot authority consumption and durable JSON journalling for WP-016A."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def sha256(value: bytes | Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical_bytes(value)).hexdigest()


def durable_write_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        raw = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def durable_write_replace(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o600)
    try:
        os.write(fd, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass(slots=True)
class AuthorityLease:
    token_sha256: str
    run_nonce: str
    consumption_path: Path
    lock_fd: int

    def close(self) -> None:
        if self.lock_fd >= 0:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = -1


def consume_once(*, token: dict[str, Any], authority_root: Path) -> AuthorityLease:
    authority_root.mkdir(parents=True, exist_ok=True)
    lock_path = authority_root / "wp016a-canary.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        token_hash = sha256(token)
        consumption = authority_root / ("consumed__" + token_hash + ".json")
        record = {
            "schema_version": "wp016a.authority-consumption.v1",
            "status": "CONSUMED_STARTING",
            "token_sha256": token_hash,
            "run_nonce": token["run_nonce"],
            "one_shot": True,
            "process_id": os.getpid(),
        }
        durable_write_exclusive(consumption, record)
        return AuthorityLease(token_hash, str(token["run_nonce"]), consumption, lock_fd)
    except Exception:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        raise


def create_runtime_root(*, runtime_base: Path, run_nonce: str,
                        cell_bindings: list[dict[str, str]],
                        expected_cell_count: int = 6) -> Path:
    runtime_root = runtime_base / ("wp016a-run__" + sha256(run_nonce.encode())[:24])
    runtime_root.mkdir(parents=True, exist_ok=False)
    for name in ("authority", "journal", "requests", "responses", "logs", "final"):
        (runtime_root / name).mkdir()
    cells = runtime_root / "cells"
    cells.mkdir()
    required = {"configuration_id", "cell_id", "session_id", "run_id", "log_id", "output_id"}
    if (
        len(cell_bindings) != expected_cell_count
        or expected_cell_count not in (5, 6)
        or any(set(row) != required for row in cell_bindings)
        or any(
            len({row[key] for row in cell_bindings}) != expected_cell_count
            for key in required
        )
    ):
        raise RuntimeError("exact_expected_unique_runtime_cells_required")
    path_bindings = []
    for row in cell_bindings:
        cell_id = row["cell_id"]
        cell = cells / cell_id
        cell.mkdir()
        session_root, run_root, log_root, output_root = (
            cell / "session", cell / "run", cell / "log", cell / "output",
        )
        for path in (session_root, run_root, log_root, output_root):
            path.mkdir()
        (session_root / row["session_id"]).mkdir()
        (run_root / row["run_id"]).mkdir()
        (log_root / row["log_id"]).mkdir()
        path_bindings.append({
            **row,
            "cell_path": str(Path("cells") / cell_id),
            "session_path": str(Path("cells") / cell_id / "session" / row["session_id"]),
            "run_path": str(Path("cells") / cell_id / "run" / row["run_id"]),
            "log_path": str(Path("cells") / cell_id / "log" / row["log_id"]),
            "output_path": str(Path("cells") / cell_id / "output" / "cell-result.json"),
        })
    durable_write_exclusive(runtime_root / "authority" / "identity-path-bindings.json", {
        "schema_version": "wp016a.runtime-identity-path-bindings.v1",
        "run_nonce_sha256": sha256(run_nonce.encode()),
        "cells": path_bindings,
    })
    return runtime_root


@dataclass(slots=True)
class DurableJournal:
    runtime_root: Path
    sequence: int = 0

    def append(self, *, kind: str, value: dict[str, Any]) -> Path:
        self.sequence += 1
        target = self.runtime_root / "journal" / f"{self.sequence:04d}__{kind}.json"
        record = {
            "schema_version": "wp016a.durable-journal-entry.v1",
            "sequence": self.sequence,
            "kind": kind,
            **value,
        }
        durable_write_exclusive(target, record)
        return target

    def terminal(self, *, status: str, value: dict[str, Any]) -> Path:
        target = self.runtime_root / "final" / "canary-result.json"
        durable_write_exclusive(target, {
            "schema_version": "wp016a.live-canary-terminal.v2",
            "status": status,
            **value,
        })
        return target

    def completion_terminal(self, *, status: str, value: dict[str, Any]) -> Path:
        target = self.runtime_root / "final" / "completion-run-result.json"
        durable_write_exclusive(target, {
            "schema_version": "wp016a.r08.live-completion-terminal.v1",
            "status": status,
            **value,
        })
        return target

    def request_receipt(self, *, cell_id: str, turn: int,
                        value: dict[str, Any]) -> Path:
        target = self.runtime_root / "requests" / f"{cell_id}__turn-{turn}.json"
        return _receipt(target, "wp016a.safe-request-receipt.v2", value)

    def response_receipt(self, *, cell_id: str, turn: int,
                         value: dict[str, Any]) -> Path:
        target = self.runtime_root / "responses" / f"{cell_id}__turn-{turn}.json"
        return _receipt(target, "wp016a.safe-response-receipt.v2", value)

    def cell_result(self, *, cell_id: str, value: dict[str, Any]) -> Path:
        target = self.runtime_root / "cells" / cell_id / "output" / "cell-result.json"
        return _receipt(target, "wp016a.durable-cell-result.v2", value)


def _receipt(path: Path, schema_version: str, value: dict[str, Any]) -> Path:
    durable_write_exclusive(path, {"schema_version": schema_version, **value})
    return path


__all__ = [
    "AuthorityLease", "DurableJournal", "consume_once", "create_runtime_root",
    "durable_write_exclusive", "durable_write_replace", "sha256",
]
