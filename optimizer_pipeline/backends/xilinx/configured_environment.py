"""Resolve explicit tool paths without starting a tool or discovering credentials."""
from __future__ import annotations
import os
import json
from pathlib import Path


def load_tool_config(path):
    """Read the common four tool paths; relative paths are config-relative.

    Additional batch fields are left to their owner, not passed as tool options.
    No environment file, credentials, defaults or alternate tool is discovered.
    """
    config_path = Path(path).resolve(strict=True)
    payload = json.loads(config_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("Tool configuration must be a JSON object")
    result = {}
    for key in ("python", "vivado", "rapidwright", "java_home"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Tool configuration requires a non-empty path: " + key)
        selected = Path(value)
        result[key] = selected.absolute() if selected.is_absolute() else config_path.parent / selected
    return result


def configured_environment(*, python, vivado, rapidwright, java_home):
    values = dict(python=python, vivado=vivado, rapidwright=rapidwright, java_home=java_home)
    if any(not value for value in values.values()):
        raise ValueError("Explicit Python, Vivado, RapidWright and Java home paths are required")
    paths = {key: Path(value).resolve(strict=True) for key, value in values.items()}
    java = paths["java_home"] / "bin/java"
    if any(not p.is_file() or not os.access(p, os.X_OK) for p in (paths["python"], paths["vivado"], java)):
        raise ValueError("Configured Python, Vivado and Java executables must exist")
    jars = sorted((paths["rapidwright"] / "jars").glob("*.jar"))
    if not jars:
        raise ValueError("Configured RapidWright jars are missing; no alternate installation is selected")
    return dict(PYTHON=str(Path(python).absolute()), VIVADO_EXEC=str(paths["vivado"]), VIVADO_BIN=str(paths["vivado"]),
        JAVA_HOME=str(paths["java_home"]), RAPIDWRIGHT_PATH=str(paths["rapidwright"]),
        CLASSPATH=os.pathsep.join([str(paths["rapidwright"] / "bin"), *(str(p) for p in jars)]),
        PATH=os.pathsep.join([str(java.parent), str(paths["vivado"].parent), os.environ.get("PATH", "")]))
