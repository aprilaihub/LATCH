"""Known legal proposals derived only from an actual root-bound capture.

Compilation (including strict-G) is checked before dispatch. No mock material
or fallback namespace is accepted here.
"""
from experiments.dof.runtime.prepared import make_session


def fixed_messages(condition, *, root, context):
    session = make_session(condition=condition, root=root, context=context, mode="prepared-real")
    if condition == "recipe":
        candidate = {"language": "M", "action_key": "macro.phys_opt"}
    elif condition == "direct":
        candidate = {"language": "P", "steps": [
            {"step_id": "s1", "op": "UNROUTE", "args": {}},
            {"step_id": "s2", "op": "ROUTE", "args": {"directive": "Default"}}]}
    else:
        # The retained query records Vivado REF_NAME (e.g. LUT6/MUXF7),
        # not a placement-site class such as SLICE. Use the original spatial
        # contract's critical-path selector without rewriting resource types.
        cells = [o for o in context["objects"] if o["object_type"] == "cell"
                 and o["attributes"].get("path_member") is True]
        if not cells:
            raise ValueError("No captured critical cell; spatial strict-G template is inapplicable")
        regions = [o["attributes"]["bounds"] for o in context["objects"] if o["object_type"] == "region"
                   and o["attributes"].get("row_semantics") == "observed_clock_region"]
        if not regions:
            raise ValueError("No observed clock regions; generated geometry is inapplicable")
        x0,x1,y0,y1 = min(r[0] for r in regions),max(r[1] for r in regions),min(r[2] for r in regions),max(r[3] for r in regions)
        if x0 == x1 and y0 == y1:
            raise ValueError("No captured nondegenerate generated geometry; strict-G template is inapplicable")
        candidate = {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "cell", "predicate":
                {"op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True}, "top_k": 1}, "bind": "targets"},
            {"step_id": "s2", "op": "CREATE_REGION", "args": dict(x_min=x0, x_max=x1, y_min=y0, y_max=y1), "bind": "region"},
            {"step_id": "s3", "op": "ASSIGN_REGION", "args": {"target_handle": {"ref": "targets"}, "region_handle": {"ref": "region"}}},
            {"step_id": "s4", "op": "UNROUTE", "args": {}},
            {"step_id": "s5", "op": "ROUTE", "args": {"directive": "Default"}}]}
    message = dict(kind="COMMIT", candidate=candidate)
    session.accept(message)
    if session.terminal != "COMMITTED" or session.valid_commits != 1:
        raise ValueError("Captured template failed the original language/strict-G checks")
    return [message]
