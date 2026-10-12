"""Study options extend the shared config without changing global defaults."""
from dataclasses import dataclass
from optimizer_pipeline.run.config import OptimizationConfig as SharedConfig


@dataclass
class OptimizationConfig(SharedConfig):
    analysis_reasoning_effort: str | None = None
    prune_dcps_keep_winning_path: bool = False
