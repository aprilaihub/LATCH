"""Fixed, bounded-process SHA-256 worker for real-preflight root evidence.

The parent owns this short-lived process with ``OwnedProcessRunner``.  Keeping
file I/O in a killable child means a pathological filesystem object cannot
silently defeat the preflight's async call and whole-run deadlines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Sequence


HASH_WORKER_SCHEMA = "dof.bounded-file-hash.v1"


def _fingerprint(path: Path) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    # O_NONBLOCK prevents a pre-existing FIFO/device from hanging before the
    # regular-file check.  It has no effect on ordinary DCP files.
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(os.fspath(path), flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("input_is_not_regular_file")
        if before.st_size <= 0:
            raise ValueError("input_file_is_empty")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or size != after.st_size:
            raise ValueError("input_changed_while_hashing")
        return {
            "schema_version": HASH_WORKER_SCHEMA,
            "sha256": digest.hexdigest(),
            "size_bytes": size,
        }
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = _fingerprint(args.path)
    except (OSError, ValueError):
        return 2
    print(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
