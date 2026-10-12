"""Opt-in limited act-probe tools.

These tools are deliberately fail-closed. They are visible only when analysis
budget allows act probes. In v1 they validate the requested primitive and write a
scratch evidence artifact; a future executor can attach real isolated Vivado
probe execution behind the same interface.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Tuple

from .base import ToolContext


class _DirectiveActProbeBase:
    tool_class = "act_probe"
    mutates_design = True
    default_timeout_sec = 300.0
    tool_name = ""
    allowed_directives: Tuple[str, ...] = ()

    def run(self, args: Dict[str, Any], context: ToolContext) -> Dict[str, Any]:
        errors = _validate_args(args, self.allowed_directives)
        run_dir = Path(context.run_dir) / "analysis" / "act_probes"
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact = run_dir / f"{int(time.time() * 1000)}_{self.name}.json"
        record = {
            "schema_version": "act_probe_result.v1",
            "tool_name": self.name,
            "vivado_tool": self.tool_name,
            "directive": args.get("directive"),
            "node_id": args.get("node_id") or getattr(context.node, "node_id", None),
            "status": "rejected" if errors else "executor_unavailable",
            "errors": errors,
            "published": False,
            "graph_node_created": False,
            "note": "Act probe execution is opt-in infrastructure; no scratch executor is attached in this runtime.",
        }
        artifact.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        record["artifact_path"] = str(artifact)
        return record


class ProbePlaceDirectiveTool(_DirectiveActProbeBase):
    name = "probe_place_directive"
    tool_name = "place_design"
    allowed_directives = ("Default", "Explore")


class ProbePhysOptDirectiveTool(_DirectiveActProbeBase):
    name = "probe_phys_opt_directive"
    tool_name = "phys_opt_design"
    allowed_directives = ("Explore", "AddRetime", "AlternateFlowWithRetiming")


class ProbeRouteDirectiveTool(_DirectiveActProbeBase):
    name = "probe_route_directive"
    tool_name = "route_design"
    allowed_directives = ("Default", "Explore")


def _validate_args(args: Dict[str, Any], allowed_directives: Tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    forbidden = {"tcl", "cmd", "command", "shell", "script", "path", "file", "output", "checkpoint", "write_checkpoint"}
    for key in args:
        lowered = str(key).lower()
        if any(item in lowered for item in forbidden):
            errors.append(f"forbidden_arg:{key}")
    directive = str(args.get("directive") or "")
    if directive not in allowed_directives:
        errors.append(f"directive_not_allowed:{directive}")
    return errors
