"""Ordinary path checks performed before a managed run writes any output."""
from pathlib import Path


def validate_new_run_paths(*, input_dcp: Path, run_dir: Path, output_dcp: Path | None = None) -> None:
    root, workspace = Path(input_dcp).resolve(), Path(run_dir).resolve()
    if root == workspace or workspace in root.parents:
        raise ValueError("run directory cannot contain the input DCP")
    if (workspace / "run.db").exists():
        raise FileExistsError("canonical run already exists; use resume_run() or choose a new run directory")
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise FileExistsError("new run directory must be absent or empty")
    if output_dcp is not None:
        output = Path(output_dcp).resolve()
        if output == root or (output.exists() and root.exists() and output.samefile(root)):
            raise ValueError("output cannot overwrite the input DCP")
        if output == workspace or output in workspace.parents:
            raise ValueError("output cannot replace the run directory or its parent")
        if output.exists() or Path(output_dcp).is_symlink():
            raise FileExistsError("output already exists; choose a new output path")
