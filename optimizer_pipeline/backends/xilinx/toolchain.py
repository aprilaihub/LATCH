"""Shared environment pre-flight check.

One function, called by every condition/level through the common entry point in
scripts/run_frontier_expansion.py. Nothing is reimplemented per policy.

The check interrogates the tools that will *actually be invoked* rather than
trusting PATH: Vivado is queried through the binary named by VIVADO_EXEC, and
RapidWright through the jar on CLASSPATH plus the importable Python package.

Two RapidWright versions coexist on this system and both are genuinely in use
by the same MCP server -- the Java classpath drives DCP manipulation, the pip
package is imported by RapidWrightMCP/rapidwright_tools.py. Both are recorded
and both are asserted against the floor in requirements.txt rather than
pretending a single version exists.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

REQUIRED_VIVADO = "2025.1"
# requirements.txt: rapidwright>=2025.2.1
REQUIRED_RAPIDWRIGHT_MIN = (2025, 2, 1)


# Flag a run as contended when the 1-minute load average reaches this fraction
# of the machine's cores. 0.70 leaves headroom for the bursty phases of an
# implementation run; above it, runnable work is queuing machine-wide and the
# shared resources a taskset cannot partition -- memory bandwidth, last-level
# cache, disk -- start distorting wall-clock even for a pinned process. At or
# above 1.0 the machine is oversubscribed outright.
#
# Load average is machine-wide, so this is a proxy for shared-resource
# contention rather than a measurement of the specific cores a run is pinned to.
# It is advisory only: nothing here delays, blocks or reschedules a run.
# Vivado's own worker-thread ceiling. Previously unset, so runs inherited
# Vivado's default -- observed as 8 in the logs, which happened to match the
# 8-core taskset but was never actually pinned by us. Setting it explicitly
# makes it a controlled variable rather than a coincidence, and keeps Vivado
# from spawning more workers than the core set it is confined to.
VIVADO_MAX_THREADS = 8

LOAD_CONTENDED_FRACTION = 0.70
LOAD_OVERSUBSCRIBED_FRACTION = 1.0


class EnvironmentMismatch(RuntimeError):
    """Raised when the invoked toolchain does not match the pinned versions."""


def _parse_version(text: str) -> Optional[tuple]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _vivado_version(vivado_bin: str, timeout: int = 120, *, owned: bool = False) -> Dict[str, Any]:
    """Query the binary itself. Never inferred from the path."""
    try:
        if owned:
            from .owned_process import run_owned
            proc = run_owned([vivado_bin, "-version"], text=True, timeout=timeout)
        else:
            proc = subprocess.run([vivado_bin, "-version"], capture_output=True,
                                  text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"binary": vivado_bin, "version": None, "error": f"{type(exc).__name__}: {exc}"}
    first = (proc.stdout or proc.stderr or "").strip().splitlines()
    banner = first[0] if first else ""
    # Banner is lowercase in 2025.1 ("vivado v2025.1 (64-bit)"); match either case.
    m = re.search(r"vivado\s+v?(\d+\.\d+)", banner, re.IGNORECASE)
    return {"binary": vivado_bin, "version": m.group(1) if m else None, "banner": banner}


def _rapidwright_java_version() -> Dict[str, Any]:
    """Version of the jar actually on CLASSPATH."""
    classpath = os.environ.get("CLASSPATH", "")
    for entry in classpath.split(os.pathsep):
        if not entry:
            continue
        base = Path(entry).parent if entry.endswith("*") else Path(entry)
        if not base.exists():
            continue
        # Explicit jar entries are also used by the configured smoke environment.
        # Inspect only the named jar, not unrelated jars beside it.
        jars = [base] if base.is_file() else sorted(base.glob("rapidwright-api-lib-*.jar"))
        for jar in jars:
            if not jar.name.startswith("rapidwright-api-lib-") or jar.suffix != ".jar":
                continue
            if "javadoc" in jar.name or "sources" in jar.name:
                continue
            v = _parse_version(jar.name)
            if v:
                return {"source": str(jar), "version": ".".join(str(x) for x in v), "parsed": v}
    return {"source": classpath or None, "version": None, "parsed": None}


def _rapidwright_python_version() -> Dict[str, Any]:
    try:
        import importlib.metadata as md
        raw = md.version("rapidwright")
        return {"source": "pip:rapidwright", "version": raw, "parsed": _parse_version(raw)}
    except Exception as exc:  # noqa: BLE001 - absence is a reportable state, not a crash
        return {"source": "pip:rapidwright", "version": None, "parsed": None,
                "error": f"{type(exc).__name__}: {exc}"}


def check_environment(*, strict: bool = True, owned: bool = False) -> Dict[str, Any]:
    """Verify the invoked toolchain. Raises EnvironmentMismatch when strict.

    Returns a record suitable for embedding in a run summary so results stay
    traceable to the conditions that produced them.
    """
    vivado_bin = os.environ.get("VIVADO_EXEC") or os.environ.get("VIVADO_BIN") or "vivado"
    vivado = _vivado_version(vivado_bin, owned=True) if owned else _vivado_version(vivado_bin)
    rw_java = _rapidwright_java_version()
    rw_py = _rapidwright_python_version()

    problems = []
    if vivado.get("version") != REQUIRED_VIVADO:
        problems.append(
            f"Vivado {vivado.get('version') or 'UNKNOWN'} at {vivado_bin}, required {REQUIRED_VIVADO}"
        )
    for label, rec in (("RapidWright(java)", rw_java), ("RapidWright(python)", rw_py)):
        parsed = rec.get("parsed")
        if parsed is None:
            problems.append(f"{label} version could not be determined ({rec.get('source')})")
        elif parsed < REQUIRED_RAPIDWRIGHT_MIN:
            need = ".".join(str(x) for x in REQUIRED_RAPIDWRIGHT_MIN)
            problems.append(f"{label} {rec.get('version')} below required minimum {need}")

    record = {
        "vivado": vivado,
        "rapidwright_java": rw_java,
        "rapidwright_python": rw_py,
        "required": {
            "vivado": REQUIRED_VIVADO,
            "rapidwright_min": ".".join(str(x) for x in REQUIRED_RAPIDWRIGHT_MIN),
        },
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "problems": problems,
        "passed": not problems,
    }
    if problems and strict:
        raise EnvironmentMismatch(
            "Environment pre-flight failed:\n  - " + "\n  - ".join(problems)
        )
    return record


def _cgroup_memory_limits() -> Dict[str, Any]:
    """Memory limits from this process's own cgroup, if readable."""
    try:
        cgroup = Path("/proc/self/cgroup").read_text().strip().splitlines()[0].split(":")[-1]
    except (OSError, IndexError):
        return {}
    base = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
    out: Dict[str, Any] = {"cgroup": cgroup}
    for name in ("memory.max", "memory.high"):
        try:
            out[name] = (base / name).read_text().strip()
        except OSError:
            out[name] = None
    return out


def _rlimits() -> Dict[str, Any]:
    try:
        import resource
    except ImportError:
        return {}
    out: Dict[str, Any] = {}
    for label, key in (("address_space", "RLIMIT_AS"), ("data", "RLIMIT_DATA"),
                       ("rss", "RLIMIT_RSS"), ("cpu_seconds", "RLIMIT_CPU")):
        const = getattr(resource, key, None)
        if const is None:
            continue
        try:
            soft, hard = resource.getrlimit(const)
        except (ValueError, OSError):
            continue
        out[label] = {
            "soft": "unlimited" if soft == resource.RLIM_INFINITY else soft,
            "hard": "unlimited" if hard == resource.RLIM_INFINITY else hard,
        }
    return out


def _compact_cores(cores):
    """Render a core set as ranges ("0-7,16-23") so a 192-core record stays readable."""
    if not cores:
        return None
    parts, start, prev = [], cores[0], cores[0]
    for c in list(cores[1:]) + [None]:
        if c is not None and c == prev + 1:
            prev = c
            continue
        parts.append(str(start) if start == prev else "{0}-{1}".format(start, prev))
        if c is not None:
            start = prev = c
    return ",".join(parts)


def system_load_snapshot(label: str) -> Dict[str, Any]:
    """Machine load and the resource envelope this run is executing under.

    Recorded at run start and run end so a run record is self-describing about
    the conditions its timings were produced under. Reporting only.
    """
    import time

    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = None

    total_cores = os.cpu_count()
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except AttributeError:
        affinity = None

    ratio = (load1 / total_cores) if (load1 is not None and total_cores) else None
    return {
        "label": label,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "load_average": {"1min": load1, "5min": load5, "15min": load15},
        "total_cores": total_cores,
        "pinned_cores": _compact_cores(affinity),
        "pinned_core_count": len(affinity) if affinity is not None else None,
        "pinned": (affinity is not None and total_cores is not None and len(affinity) < total_cores),
        "load_per_core": round(ratio, 4) if ratio is not None else None,
        "contended": bool(ratio is not None and ratio >= LOAD_CONTENDED_FRACTION),
        "oversubscribed": bool(ratio is not None and ratio >= LOAD_OVERSUBSCRIBED_FRACTION),
        "contended_threshold": LOAD_CONTENDED_FRACTION,
        "memory_limits": {"rlimits": _rlimits(), "cgroup": _cgroup_memory_limits()},
        "vivado_threads": vivado_thread_settings(),
    }


def format_load_line(snapshot: Dict[str, Any]) -> str:
    """One-line pre-run statement of load, flagging contention without blocking."""
    load = snapshot.get("load_average") or {}
    one = load.get("1min")
    cores = snapshot.get("total_cores")
    pinned = snapshot.get("pinned_core_count")
    pin_txt = (f"{pinned} of {cores} cores" if snapshot.get("pinned")
               else f"unpinned, {cores} cores visible")
    head = (
        "System load:  {a:.2f} / {b:.2f} / {c:.2f} (1/5/15min), {pin}, "
        "load per core {r}".format(
            a=one or 0.0, b=load.get("5min") or 0.0, c=load.get("15min") or 0.0,
            pin=pin_txt, r=snapshot.get("load_per_core"),
        )
    )
    if snapshot.get("oversubscribed"):
        return head + "\n              *** MACHINE OVERSUBSCRIBED -- treat this run's timing figures as contended ***"
    if snapshot.get("contended"):
        return head + "\n              *** load at/above {t:.0%} of cores -- treat this run's timing figures as contended ***".format(
            t=snapshot.get("contended_threshold", LOAD_CONTENDED_FRACTION))
    return head


def vivado_thread_settings() -> Dict[str, Any]:
    """The thread cap this run applies, for the record.

    Applied as `set_param general.maxThreads` when each Vivado session starts
    (VivadoMCP.start_vivado). An environment variable was tried first and does
    not work: Vivado reported maxThreads=8 both with and without
    XIL_MAX_THREADS set, so only the Tcl parameter actually controls it.
    """
    return {
        "general_max_threads": VIVADO_MAX_THREADS,
        "applied_via": "set_param general.maxThreads at Vivado session start",
        "previously": "unset (Vivado default, observed 8)",
        "env_var_ineffective": "XIL_MAX_THREADS (tested, no effect)",
    }


def repo_revision() -> Dict[str, Any]:
    """The commit a run executed from, so results trace to specific code.

    `dirty` matters as much as the hash: a run from a modified tree is not
    reproducible from the hash alone and is flagged rather than presented as if
    it were.
    """
    repo = Path(__file__).resolve().parents[3]
    def _git(*args: str) -> Optional[str]:
        try:
            out = subprocess.run(["git", "-C", str(repo), *args],
                                 capture_output=True, text=True, timeout=30, check=False)
            return (out.stdout or "").strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "commit": commit,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status),
        "dirty_files": [ln[3:] for ln in (status or "").splitlines()][:20],
    }
