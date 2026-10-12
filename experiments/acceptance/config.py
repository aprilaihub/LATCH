"""The explicitly approved WP015 smoke parameters, separate from old defaults."""
from pathlib import Path
from integrations.llm.runtime.batch_budget import validate_limits

WP015_SEEDS = [0xC0FFEE, 0x5EED1234, 0x1BADB002, 0xD15EA5E, 0xA11CE55]
VALIDATION = dict(seeds=WP015_SEEDS, vectors=200, precheck_vectors=100,
    target_clock="clk_fpl26contest", epsilon_fmax=0, prepare_timeout_seconds=10800,
    seed_timeout_seconds=3600, score_timeout_seconds=900, memory_reserve_fraction=.20,
    seed_memory_class="unknown")
RW_REVISION = "2fc8b6aec859c20c04218573f9dda09db0a72e3f"
TOOL_ORDER = ["versions", "golden-self", "capture", "fixed-recipe", "fixed-direct",
              "fixed-constructed", "S0-baseline", "S0-extension"]
PAID_ORDER = ["dof-recipe", "dof-direct", "dof-constructed", "S1", "S2", "S3"]


def validate_config(cfg):
    validate_limits(cfg["cost"])
    return validate_tools_config(cfg)


def validate_tools_config(cfg):
    """Validate approved physical scope without requiring unused model prices.

    Paid batch entrypoints and BatchBudget retain validate_limits. The generic
    no-model harness never prepares a request ledger or uses model settings.
    """
    if cfg.get("validation") != VALIDATION:
        raise ValueError("The smoke requires the exact WP015 seed/vector/timeout configuration")
    if cfg.get("rapidwright_revision") != RW_REVISION or cfg.get("period_ns") != 1.5:
        raise ValueError("Unapproved tool revision or clock period")
    root, golden, output = (Path(cfg[key]) for key in ("input", "golden", "output"))
    if not all(path.is_absolute() for path in (root, golden, output)) or root != golden:
        raise ValueError("Absolute input/golden/output required; golden must be the original DCP")
    if root.name != "logicnets_jscl_2025.1.dcp" or not root.is_file():
        raise ValueError("The approved design must exist")
    if output == root.parent or root.is_relative_to(output):
        raise ValueError("Outputs cannot enclose or overwrite the input")
    for key in ("python", "vivado", "rapidwright", "java_home"):
        path = Path(cfg["tools"][key])
        if not path.is_absolute() or not path.exists():
            raise ValueError("Missing explicit tool path: " + key)
    if cfg.get("ledger_path") != str(output / "batch.db"):
        raise ValueError("The ledger must belong to this independent output directory")
    return cfg
