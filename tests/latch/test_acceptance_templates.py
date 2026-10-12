"""Real primitive names must not be confused with placement-site classes."""
import pytest

from experiments.acceptance.templates import fixed_messages
from experiments.dof.runtime.captured_context import context_from_capture
from experiments.dof.runtime.prepared import make_session
from tests.latch.test_dof_capture_and_failures import captured


def context(tmp_path, resource):
    root, capture, write = captured(tmp_path)
    write("contract-probe/private/resolved-objects.json", dict(
        device_regions=[dict(identity="X1Y2"), dict(identity="X2Y2")],
        timing_paths=[], critical_cell_locations=[dict(
            identity="cell", index=0,
            attribute=f"{resource}\x1fSLICE_X1Y1\x1fX1Y2")]))
    return root, context_from_capture(root=root, capture=capture, target_clock="clk")


@pytest.mark.parametrize("resource", ["LUT6", "LUT4", "MUXF7", "FDRE", "SLICE"])
def test_critical_primitive_compiles_under_original_strict_g(tmp_path, resource):
    root, ctx = context(tmp_path, resource)
    messages = fixed_messages("constructed", root=root, context=ctx)
    predicate = messages[0]["candidate"]["steps"][0]["args"]["predicate"]
    assert predicate == dict(op="COMPARE", field="path_member", operator="EQ", value=True)
    assert [o["attributes"]["resource_type"] for o in ctx["objects"]
            if o["object_type"] == "cell"] == [resource]
    session = make_session(condition="constructed", root=root, context=ctx, mode="prepared-real")
    session.accept(messages[0])
    assert session.strict_g_required is True
    assert session.terminal == "COMMITTED" and session.valid_commits == 1


def test_noncritical_cell_cannot_substitute_for_captured_critical_cell(tmp_path):
    root, ctx = context(tmp_path, "LUT6")
    for obj in ctx["objects"]:
        if obj["object_type"] == "cell":
            obj["attributes"]["path_member"] = False
    with pytest.raises(ValueError, match="No captured critical cell"):
        fixed_messages("constructed", root=root, context=ctx)


def test_generated_geometry_is_still_required(tmp_path):
    root, ctx = context(tmp_path, "LUT6")
    ctx["objects"] = [o for o in ctx["objects"] if o["object_type"] != "region"]
    with pytest.raises(ValueError, match="No observed clock regions"):
        fixed_messages("constructed", root=root, context=ctx)
