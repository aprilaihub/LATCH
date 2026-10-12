"""Artifact paths and JSON persistence for pipeline runs."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

from optimizer_pipeline.models import PipelineState, DcpStateNode, to_jsonable


class ArtifactStore:
    """Owns the on-disk layout for one optimizer pipeline run."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.nodes_dir = self.run_dir / "nodes"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def create_default(cls, base_dir: Optional[Path] = None) -> "ArtifactStore":
        root = Path.cwd() if base_dir is None else Path(base_dir)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        return cls(root / f"dcp_optimizer_run-{timestamp}")

    @staticmethod
    def safe_label(label: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", label.strip())
        return cleaned.strip("_") or "node"

    def node_dir(self, index: int, label: str) -> Path:
        path = self.nodes_dir / f"node_{index:03d}_{self.safe_label(label)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, path: Path, data: Any) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(to_jsonable(data), handle, indent=2, sort_keys=True)
            handle.write("\n")
        return path

    def read_json(self, path: Path) -> Dict[str, Any]:
        with open(path, "r") as handle:
            return json.load(handle)

    def write_pipeline_state(self, state: PipelineState) -> Path:
        return self.write_json(self.run_dir / "pipeline_state.json", state)

    def write_state_graph(self, nodes: Dict[str, DcpStateNode]) -> Path:
        edges = [
            {"parent_id": node.parent_id, "child_id": node.node_id}
            for node in nodes.values()
            if node.parent_id
        ]
        return self.write_json(
            self.run_dir / "state_graph.json",
            {"nodes": [node_artifact_dict(node) for node in nodes.values()], "edges": edges},
        )

    def write_node_state(self, node: DcpStateNode) -> Path:
        if not node.artifact_dir:
            raise ValueError(f"Node {node.node_id} has no artifact_dir")
        return self.write_json(Path(node.artifact_dir) / "node_state.json", node_artifact_dict(node))

    def copy_final_output(self, source_dcp: Path) -> Path:
        final_output = self.run_dir / "final_output.dcp"
        shutil.copy2(source_dcp, final_output)
        return final_output


def node_artifact_dict(node: DcpStateNode) -> Dict[str, Any]:
    data = node.to_dict()
    pure_qor_score = data.pop("score", None)
    data["pure_qor_score"] = pure_qor_score
    data["trajectory_objective_profile"] = "pure_qor_v1"
    data["trajectory_objective_score"] = pure_qor_score
    return data
