# Ported from a722b1e; import paths only adapted.
"""Opt-in DCP pruning: keeps the winning ancestor path, never touches records."""
import json
from pathlib import Path

from experiments.ablation.runtime.expansion.frontier_runner import FrontierExpansionRunner


class _Node:
    def __init__(self, node_id, parent_id, artifact_dir):
        self.node_id = node_id
        self.parent_id = parent_id
        self.artifact_dir = str(artifact_dir)


class _Stub:
    pass


def _make_node_dir(root: Path, name: str) -> Path:
    d = root / name
    (d / "output.dcp.edf").mkdir(parents=True)
    (d / "output.dcp").write_bytes(b"x" * 2048)
    (d / "output.dcp.edf" / "output.dcp.md5").write_bytes(b"y" * 1024)
    (d / "node_state.json").write_text(json.dumps({"node_id": name}))
    (d / "executor_run").mkdir()
    (d / "executor_run" / "vivado.log").write_text("log text")
    return d


def _run(tmp_path, best_id):
    runner = _Stub()
    runner.nodes = {}
    for nid, parent in [("n0", None), ("n1", "n0"), ("n2", "n1"), ("n3", "n0"), ("n4", "n3")]:
        runner.nodes[nid] = _Node(nid, parent, _make_node_dir(tmp_path, nid))
    state = _Stub()
    state.best_node_id = best_id
    record = FrontierExpansionRunner._prune_dcps_keep_winning_path(runner, state)
    return runner, record


def test_prune_keeps_winning_path_and_deletes_edf(tmp_path):
    runner, record = _run(tmp_path, "n2")

    assert record["enabled"] is True
    assert record["kept_node_ids"] == ["n0", "n1", "n2"]
    assert record["pruned_node_ids"] == ["n3", "n4"]
    assert record["errors"] == []

    for nid in ("n0", "n1", "n2"):
        d = Path(runner.nodes[nid].artifact_dir)
        assert (d / "output.dcp").exists(), nid
        assert (d / "output.dcp.edf").is_dir(), nid

    # the .edf export goes with its DCP -- it is derived design data, not a record
    for nid in ("n3", "n4"):
        d = Path(runner.nodes[nid].artifact_dir)
        assert not (d / "output.dcp").exists(), nid
        assert not (d / "output.dcp.edf").exists(), nid

    # records survive for EVERY node, pruned or not
    for nid in runner.nodes:
        d = Path(runner.nodes[nid].artifact_dir)
        assert (d / "node_state.json").exists(), nid
        assert (d / "executor_run" / "vivado.log").read_text() == "log text", nid

    # 2048 (dcp) + 1024 (edf contents), twice
    assert record["bytes_freed"] == (2048 + 1024) * 2


def test_prune_refuses_to_act_when_winner_unknown(tmp_path):
    """A run that never published a best result must not lose data."""
    runner, record = _run(tmp_path, None)

    assert record["pruned_node_ids"] == []
    assert "skipped_reason" in record
    for nid in runner.nodes:
        assert (Path(runner.nodes[nid].artifact_dir) / "output.dcp").exists(), nid
