"""Backend contracts and implementations.

The Xilinx backend owns execution/probing plus extracted observation, scoring,
validation and process adapters. Retained legacy and script bridges are explicit
compatibility resources, not a future extraction plan or research-policy owner.
"""

from .base import (
    ActionCatalog,
    ActionDefinition,
    ActionParameter,
    AnalysisProbeKind,
    AnalysisProbeRequest,
    AnalysisProbeResult,
    Backend,
    BackendContractError,
    BackendExecutionRequest,
    BackendExecutionResult,
    InvalidActionParameters,
    ParameterType,
    UnsafeManagedActionError,
    UnsupportedActionError,
    backend_session,
)

__all__ = [name for name in globals() if not name.startswith("_")]
