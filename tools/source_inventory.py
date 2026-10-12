"""Emit a source/notice inventory from the existing local Git revisions.

Read-only: does not stage files, fetch upstream, initialize dependencies, or
decide that an author credit grants a redistribution license. Generated output
contains paths and source classification only, never source or runtime records.
"""
from __future__ import annotations
import argparse
import ast
import contextlib
import io
import json
import subprocess
from pathlib import Path
try:
    from .export_source import source_path
except ImportError:  # Direct documented script invocation.
    from export_source import source_path

ROOT = Path(__file__).resolve().parents[1]
BASE = "652aea7"
MATT = "a722b1e"
AMD = "d21b17d"


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.DEVNULL)


def blob(revision, path):
    try: return git("show", f"{revision}:{path}")
    except subprocess.CalledProcessError: return None


def possible_sources(path):
    yield BASE, path
    if path.startswith("integrations/llm/"):
        tail = path.removeprefix("integrations/llm/")
        yield BASE, "experiments/llm/" + tail
        if tail.startswith("runtime/"):
            yield MATT, "optimizer_pipeline/llm/" + tail.removeprefix("runtime/")
    if path.startswith("experiments/ablation/runtime/"):
        yield MATT, "optimizer_pipeline/" + path.removeprefix("experiments/ablation/runtime/")
    if path.startswith("experiments/ablation/configs/"):
        yield MATT, "configs/" + Path(path).name
    if path.startswith("experiments/ablation/analysis/") or path.startswith("experiments/ablation/processing/scripts/"):
        yield MATT, "paper_analysis/scripts/" + Path(path).name
    if path == "experiments/ablation/config.py": yield MATT, "optimizer_pipeline/run/config.py"
    if path == "experiments/ablation/runner_cli.py": yield MATT, "scripts/run_frontier_expansion.py"
    if path.startswith("experiments/dof/protocol/"):
        yield BASE, "optimizer_pipeline/dof/single_submission/" + Path(path).name
    if path.endswith("/wp015_analysis.py"): yield BASE, "scripts/analyze_wp015.py"
    if path.endswith("/wp015a_strict_g.py"): yield BASE, "scripts/run_wp015a_strict_g.py"
    if path.endswith("/named_family.py"): yield BASE, "scripts/wp015_named_family.py"
    if path.endswith("/compiled_operations.py"): yield BASE, "scripts/run_wp013a_e0.py"
    if path.endswith("/planning_request.py"): yield BASE, "scripts/run_wp015_cells.py"


def relation(current, original, path):
    if current == original: return "byte-identical"
    if path.endswith(".py"):
        try:
            old, new = (ast.parse(text.decode()) for text in (original, current))
            bodies = lambda tree: {node.name: ast.dump(node, include_attributes=False)
                for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
            a, b = bodies(old), bodies(new)
            common = a.keys() & b.keys()
            same = sum(a[key] == b[key] for key in common)
            return f"modified; {same}/{len(common)} same named top-level definitions"
        except (SyntaxError, UnicodeDecodeError): pass
    return "modified"


def inventory():
    candidates = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    rows = []
    for path in sorted(set(candidates)):
        # Reports are emitted to stdout for external archival, not source files.
        if not path or not source_path(path): continue
        file = ROOT / path
        if not file.is_file() or file.is_symlink(): continue
        current = file.read_bytes()
        head = current[:4096].decode(errors="replace")
        declared_amd = any(line.lstrip().startswith("#") and "Copyright" in line
            and "Advanced Micro Devices" in line for line in head.splitlines())
        source = None
        if declared_amd:
            upstream_path = "dcp_optimizer.py" if path.endswith("/legacy.py") else path
            original = blob(AMD, upstream_path)
            if original is not None:
                source = (AMD, upstream_path, original)
        if source is None:
            for revision, original_path in possible_sources(path):
                original = blob(revision, original_path)
                if original is not None:
                    source = (revision, original_path, original)
                    break
        if declared_amd and source and source[0] == AMD:
            provenance = "verified AMD upstream"
            license_status = "file Apache-2.0 declaration; upstream license verified; retain modifications/notices"
        elif declared_amd:
            provenance = "AMD declaration present; external upstream unverified"
            license_status = "do not treat header alone as verified upstream redistribution clearance"
        elif source:
            provenance = "Matt selected source" if source[0] == MATT else "Shu base/history source"
            license_status = "authorship/source identified; separate file-wide redistribution grant not established"
        else:
            provenance = "integration addition; no matching selected-source path"
            license_status = "no separate redistribution license granted by this inventory"
        rows.append(dict(path=path, provenance=provenance, revision=source[0] if source else "current integration",
            source_path=source[1] if source else "-", change=relation(current, source[2], path) if source else "new",
            notice="AMD/Apache header retained" if declared_amd else "no AMD declaration observed",
            license_status=license_status))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path, help="Optional report file outside the source worktree; otherwise stdout")
    args = parser.parse_args()
    if args.output and args.output.resolve().is_relative_to(ROOT.resolve()):
        parser.error("Archive generated reports outside the source worktree")
    rows = inventory()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render(rows, args.format)
    if args.output:
        with args.output.open("x", encoding="utf-8") as target:
            target.write(buffer.getvalue())
    else:
        print(buffer.getvalue(), end="")


def render(rows, output_format):
    if output_format == "json": print(json.dumps(rows, indent=2))
    else:
        print("# File-level LATCH source inventory\n")
        print("Generated for the selected source scope; archive this report outside the worktree.")
        print("Requires the retained historical Git objects; not an ownership or legal-clearance certificate.\n")
        print("| File | Source | Revision:path | Local relationship | Notice / license boundary |")
        print("|---|---|---|---|---|")
        for row in rows:
            print(f"| `{row['path']}` | {row['provenance']} | `{row['revision']}:{row['source_path']}` | {row['change']} | {row['notice']}; {row['license_status']} |")


if __name__ == "__main__": main()
