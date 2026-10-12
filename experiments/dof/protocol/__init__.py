"""WP015-derived contracts for the migrated study implementation.

Class and function bodies are retained from the historical single-submission
library. The original optimizer_pipeline.dof.single_submission path remains
unchanged for version-bound historical runners; this package was not the owner
used by the original paper's collection run.
"""

from .specs import (
    SPEC_BUNDLE_VERSION,
    FrozenProtocolSpec,
    build_protocol_freeze,
    protocol_freeze_bytes,
    protocol_freeze_hash,
)

__all__ = [
    "SPEC_BUNDLE_VERSION",
    "FrozenProtocolSpec",
    "build_protocol_freeze",
    "protocol_freeze_bytes",
    "protocol_freeze_hash",
]
