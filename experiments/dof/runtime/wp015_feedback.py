"""Model-only WP015 feedback view; execution and evidence keep rich lineage."""
from __future__ import annotations

from copy import deepcopy
import hashlib

from ..protocol.specs import canonical_json_bytes


# The binding fields emitted by the actual WP015 compiler (32d852a).
# Allocation feasibility fields were already present and remain model-visible.
_BINDING_FIELDS = frozenset({
    "binding_id", "binding_type", "binding_cardinality", "binding_content_hash",
    "matching_size", "compatibility_relation_sha256", "feasibility_query_sha256",
    "target_site_pair_hashes", "allocation_sha256", "root_sha256", "stage",
})


def feedback_constructs(candidate):
    """Keep only source node types/IDs, never proposals or private captures."""
    rows = candidate.get("steps", candidate.get("statements", []))
    return {
        row.get("step_id", row.get("statement_id")): row.get("op", row.get("node"))
        for row in rows if row.get("op", row.get("node")) == "P_PROGRAM"
    }


def model_feedback(previous_result, *, source_constructs=None):
    """Copy a result into the 32d852a public feedback shape, without compiling.

    The current compiled object, public digest, transcript and result ledger
    remain untouched. Only this copy's existing compiled digest is recomputed.
    """
    result = deepcopy(previous_result)
    if result is None or not isinstance(result.get("compiled"), dict):
        return result
    compiled = result["compiled"]
    schema = compiled.get("schema_version", "")
    is_p = schema.startswith("dof.single-submission.compiled-p.")
    is_g = schema.startswith("dof.single-submission.compiled-g.")
    for step in compiled["host_steps"]:
        if is_p and step.get("source_construct") != "CALL_MACRO":
            step.update(source_construct=None, source_step_id=None, input_bindings=[])
        elif (is_g and (source_constructs or {}).get(step.get("source_step_id")) == "P_PROGRAM"
              and step.get("source_construct") != "CALL_MACRO"):
            # Nested P gained annotations after WP015. At WP015 its nonmacro
            # host steps were annotated by the outer G P_PROGRAM instead.
            step["source_construct"] = "P_PROGRAM"
    compiled["binding_records"] = [
        {key: value for key, value in row.items() if key in _BINDING_FIELDS}
        for row in compiled["binding_records"]
    ]
    result["compiled_sha256"] = hashlib.sha256(canonical_json_bytes(compiled)).hexdigest()
    return result
