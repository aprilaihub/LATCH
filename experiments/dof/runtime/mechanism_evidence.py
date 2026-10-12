"""WP015 evidence augmentation, preserving the historical witness semantics.

The backend projects markers in memory; this adapter never opens restricted
worker logs. Missing markers remain missing evidence, not inferred success.
"""
from __future__ import annotations

from .scientific_evidence import _canonical_hash, strict_boundary_certificate

STRICT_WITNESS_IDS = {
    "SPATIAL_GENERATED_GEOMETRY": "spatial_generated_geometry",
    "NONPREFIX_MULTI_NET_BATCH": "nonprefix_multi_net_batch",
    "COUPLED_HARDBLOCK_SITE_ALLOCATION": "coupled_hardblock_site_allocation",
}


def mechanism_deltas(markers, witness_id):
    """Same marker selection and interpretation as the WP015 collector.

The historical min-one changed-count convention is deliberately retained;
changing that scientific convention is not a migration tidy-up.
"""
    concrete = sorted(set(marker for marker in markers if "$__dof_" not in marker))
    if not concrete:
        return []
    preferred = {
        "spatial_generated_geometry": "ASSIGN_PBLOCK",
        "nonprefix_multi_net_batch": "BATCH_ROUTE_NETS",
        "coupled_hardblock_site_allocation": "APPLY_ALLOCATION",
    }[witness_id]
    marker = next((row for row in concrete if preferred in row), concrete[-1])
    fields = marker.split(":")
    changed = int(fields[-3]) if witness_id == "nonprefix_multi_net_batch" else int(fields[-1]) if witness_id == "coupled_hardblock_site_allocation" else 1
    strength = "ROUTE_BEFORE_AFTER" if witness_id == "nonprefix_multi_net_batch" else "MECHANISM_EXACT"
    return [{
        "state_delta_id": witness_id,
        "mechanism_family": witness_id,
        "before_hash": _canonical_hash({"witness": witness_id, "phase": "before"}),
        "after_hash": _canonical_hash({"witness": witness_id, "marker": marker}),
        "changed_count": max(1, changed),
        "changed_identity_hash": _canonical_hash({"witness": witness_id, "marker": marker}),
        "evidence_strength": strength,
        "sanitized_marker": marker,
    }]


def augment_execution(*, candidate, compiled, execution):
    """Return a new evidence carrier; do not alter execution or candidates."""
    family = strict_boundary_certificate(candidate=candidate,
        execution_started=False, execution=None).get("mechanism_family")
    if family not in STRICT_WITNESS_IDS:
        return execution
    return dict(execution, binding_records=[dict(row) for row in compiled.binding_records],
        mechanism_state_deltas=mechanism_deltas(execution.get("instrumentation_markers", []),
            STRICT_WITNESS_IDS[family]))
