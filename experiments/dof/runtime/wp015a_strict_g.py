"""WP-015A strict-G construction diagnostics and feasibility checks.

These checks are deliberately QoR-blind.  They operate only on the frozen
typed registry, generated geometry and a development-only device-capacity
manifest.  A negative result is construction feedback before COMMIT; it is not
an optimization outcome.
"""

from __future__ import annotations

import collections
from typing import Any, Mapping

from .scientific_evidence import strict_boundary_certificate
from ..protocol.language import CompileContext, CompiledProgram


STRICT_FAMILIES = (
    "SPATIAL_GENERATED_GEOMETRY",
    "NONPREFIX_MULTI_NET_BATCH",
    "COUPLED_HARDBLOCK_SITE_ALLOCATION",
)

FEEDBACK_CODES = {
    "SPATIAL_GENERATED_GEOMETRY": "g_strict_spatial_requires_nonempty_selector_generated_nonpredefined_geometry_assign_region_and_implementation_followup",
    "NONPREFIX_MULTI_NET_BATCH": "g_strict_nonprefix_requires_diff_output_cardinality_at_least_two_consumed_by_batch_route_nets",
    "COUPLED_HARDBLOCK_SITE_ALLOCATION": "g_strict_coupled_allocation_requires_at_least_two_hardblocks_compatible_unique_sites_couple_sites_and_apply_allocation",
}


def strict_family_feedback(
    candidate: Mapping[str, Any], *, required_family: str | None,
) -> str | None:
    """Return an exact non-QoR construction diagnostic, or ``None``."""

    certificate = strict_boundary_certificate(
        candidate=candidate, execution_started=False, execution=None,
    )
    observed = certificate.get("mechanism_family")
    if observed in STRICT_FAMILIES and (required_family is None or observed == required_family):
        return None
    if required_family in FEEDBACK_CODES:
        return FEEDBACK_CODES[str(required_family)]
    return "g_strict_requires_one_complete_causally_connected_admitted_family_certificate"


def spatial_generated_region_precheck(
    *, compiled: CompiledProgram, context: CompileContext,
    capacity_by_region: Mapping[str, Mapping[str, int]],
) -> dict[str, Any]:
    """Check generated-region coordinates and resource capacity without QoR.

    ``capacity_by_region`` is a hash-bound development query product.  Keys are
    ``x_min:x_max:y_min:y_max`` and values count compatible sites by resource
    type.  The check rejects unknown regions instead of guessing capacity.
    """

    generated = [
        step for step in compiled.host_steps
        if step.operation == "vivado.wp014c_create_pblock"
        and step.source_construct == "ASSIGN_REGION"
    ]
    assignments = [
        step for step in compiled.host_steps
        if step.operation == "vivado.wp014c_assign_pblock"
        and step.source_construct == "ASSIGN_REGION"
    ]
    if len(generated) != 1 or len(assignments) != 1:
        return {"accepted": False, "reason": "spatial_generated_region_consumer_not_unique"}
    region_handle = generated[0].public_args.get("region")
    region = context.registry.resolve(str(region_handle), expected_type="region")
    bounds = region.attributes.get("bounds")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return {"accepted": False, "reason": "generated_region_bounds_missing"}
    key = ":".join(str(int(value)) for value in bounds)
    capacity = capacity_by_region.get(key)
    if not isinstance(capacity, Mapping):
        return {"accepted": False, "reason": "generated_region_capacity_unknown", "region_key": key}
    target_handles = assignments[0].public_args.get("targets")
    if not isinstance(target_handles, list) or not target_handles:
        return {"accepted": False, "reason": "generated_region_selector_empty", "region_key": key}
    demand: collections.Counter[str] = collections.Counter()
    for handle in target_handles:
        target = context.registry.resolve(str(handle))
        resource = target.attributes.get("resource_type")
        if not isinstance(resource, str) or not resource:
            return {"accepted": False, "reason": "generated_region_target_resource_unknown", "region_key": key}
        demand[resource] += 1
    deficits = {
        resource: {"required": count, "available": int(capacity.get(resource, 0))}
        for resource, count in demand.items() if int(capacity.get(resource, 0)) < count
    }
    return {
        "accepted": not deficits,
        "reason": None if not deficits else "generated_region_compatible_site_capacity_insufficient",
        "region_key": key,
        "target_count": len(target_handles),
        "resource_demand": dict(sorted(demand.items())),
        "capacity_checked": {key: int(capacity.get(key, 0)) for key in sorted(demand)},
        "deficits": deficits,
        "qor_fields_used": [],
    }


__all__ = [
    "FEEDBACK_CODES", "STRICT_FAMILIES", "spatial_generated_region_precheck",
    "strict_family_feedback",
]
