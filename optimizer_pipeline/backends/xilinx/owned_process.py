"""Synchronous adapter to the existing Linux owned-process runner.

New capture/scoring/validation adapters use the historical process-identity and
descendant cleanup implementation, rather than weakening it to PID/group-only
cleanup. Call from synchronous code or a worker thread, not an active event loop.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

from optimizer_pipeline.dof.processes import OwnedProcessRunner, ProcessEndpoint, ProcessLimits


def run_owned(argv, *, cwd=None, env=None, timeout=None, text=False):
    if sys.platform != "linux":
        raise RuntimeError("physical tool adapters require Linux process ownership support")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("run_owned must run outside the event loop or in a worker thread")
    duration = float(timeout) if timeout is not None else 1200.0
    limits = ProcessLimits(initialization_timeout_seconds=min(30.0, duration),
                           whole_run_timeout_seconds=duration,
                           output_limit_bytes=16 * 1024 * 1024)
    result = asyncio.run(OwnedProcessRunner(limits).run(argv, cwd=cwd, env=env))
    stdout, stderr = result.stdout.data, result.stderr.data
    if not result.cleanup_ok:
        raise RuntimeError("owned tool process cleanup failed")
    if result.endpoint in {ProcessEndpoint.INITIALIZATION_TIMEOUT, ProcessEndpoint.WHOLE_RUN_TIMEOUT}:
        raise subprocess.TimeoutExpired(argv, duration, output=stdout, stderr=stderr)
    if not result.session_owned or result.endpoint not in {ProcessEndpoint.SUCCEEDED, ProcessEndpoint.FAILED}:
        raise RuntimeError("owned tool process failed before safe completion")
    if result.returncode is None or result.stdout.truncated or result.stderr.truncated:
        raise RuntimeError("owned tool process produced incomplete execution evidence")
    if text:
        stdout, stderr = stdout.decode(errors="replace"), stderr.decode(errors="replace")
    return subprocess.CompletedProcess(argv, result.returncode, stdout, stderr)
