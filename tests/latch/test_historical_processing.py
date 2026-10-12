"""Numerical replay of Matt's ordered processing on synthetic raw records.

Only filesystem location expressions are adapted in the original code. This
does not reconstruct the unavailable complete historical raw corpus.
"""
import ast
import contextlib
import csv
import io
import json
from pathlib import Path
import subprocess
import sys

from experiments.ablation.processing.run import STAGES, run_processing
from tests.latch.test_ablation_processing import _multi_participant_config
from tests.latch.test_historical_semantics import historical


def original_at_fixture(source, config):
    roots = config["participant_roots"]
    users = {"csestito": "cristian", "myoungman": "matt", "shugu": "shu"}

    class LocationsOnly(ast.NodeTransformer):
        def visit_Assign(self, node):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name == "S0_SWEEP":
                    node.value = ast.Constant(config["s0_sweep_root"])
                elif name == "S0_EXTRA":
                    node.value = ast.parse(repr(config["s0_extra_roots"]), mode="eval").body
                elif name == "parts" and ast.unparse(node.value) == "p.split('/')":
                    node.value = ast.parse("os.path.relpath(p, _fixture_matt_root).split(os.sep)", mode="eval").body
            return self.generic_visit(node)

        def visit_JoinedStr(self, node):
            if ast.unparse(node) in {"f'/home/{user}/shared/timing_closure_agent/ablation_study'",
                                    'f"/home/{user}/shared/timing_closure_agent/ablation_study"'}:
                return ast.parse("_fixture_roots[_fixture_users[user]]", mode="eval").body
            return self.generic_visit(node)

        def visit_Constant(self, node):
            prefix = "/home/myoungman/shared/timing_closure_agent/ablation_study/"
            if isinstance(node.value, str) and node.value.startswith(prefix):
                return ast.Constant(roots["matt"] + "/" + node.value[len(prefix):])
            return node

        def visit_Subscript(self, node):
            # Only absolute-location path indices, never scientific columns.
            if (isinstance(node.value, ast.Name) and node.value.id == "parts"
                    and isinstance(node.slice, ast.Constant) and node.slice.value in (6, 7, 8, 9)):
                node.slice = ast.Constant(node.slice.value - 6)
            return self.generic_visit(node)

    tree = ast.fix_missing_locations(LocationsOnly().visit(ast.parse(source)))
    return tree, dict(_fixture_roots=roots, _fixture_users=users, _fixture_matt_root=roots["matt"])


def test_original_ordered_processing_matches_all_csv_fields(tmp_path, monkeypatch):
    config_path, _ = _multi_participant_config(tmp_path)
    config = json.loads(config_path.read_text())
    current, original = tmp_path / "current", tmp_path / "original"
    # Fail/skip missing Git evidence before producing fixture processing output.
    sources = {name: historical("paper_analysis/scripts/" + name, "a722b1e") for _, name in STAGES}

    def old_executor(script, env):
        tree, namespace = original_at_fixture(sources[script.name], config)
        namespace.update(__name__="__main__", __file__=str(script))
        with monkeypatch.context() as patch, contextlib.redirect_stdout(io.StringIO()):
            for key, value in env.items(): patch.setenv(key, value)
            exec(compile(tree, "historical-processing-locations-adapted-only", "exec"), namespace)

    def current_executor(script, env):
        result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, script.name + " failed: " + result.stderr[-1000:]

    old_report = run_processing(config_path, original, executor=old_executor)
    new_report = run_processing(config_path, current, executor=current_executor)
    assert old_report["completed_stages"] == new_report["completed_stages"]
    old_tables = {p.name for p in (original / "data").glob("*.csv")}
    assert old_tables == {p.name for p in (current / "data").glob("*.csv")}
    assert len(old_tables) >= 15
    for name in sorted(old_tables):
        def table(root):
            with (root / "data" / name).open(newline="") as stream:
                reader = csv.reader(stream)
                headers = next(reader)
                return headers, sorted(reader)
        assert table(original) == table(current), name
    assert new_report["key_totals"]["num_runs"] == 30
