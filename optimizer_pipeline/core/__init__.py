"""Stable domain and runtime contracts.

Immutable facts and canonical SQLite storage are the active managed-run truth.
Historical JSON collectors are separate version-bound evidence. Application policy,
provider metadata, prompts, and model configuration do not belong here.
"""

from . import domain, store, validation

__all__ = ["domain", "store", "validation"]
