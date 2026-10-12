"""Production contracts for the WP-013 single-submission protocol.

The package is intentionally separate from ``action_language_probe``.  The
probe remains historical feasibility evidence; this package is the only
production protocol surface used by WP-013 and later experiments.
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
