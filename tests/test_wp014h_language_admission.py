import hashlib

from scripts.run_wp014h_language_admission import CONDITIONS, MODELS, _classify
from scripts.run_wp014h_strict_witnesses import candidate
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry
from optimizer_pipeline.dof.single_submission.language import CompileContext


def test_exact_seven_by_three_panel() -> None:
    assert len(MODELS) == 7
    assert CONDITIONS == ("M_ONLY", "P_BOUNDARY", "G_STRICT_BOUNDARY")


def test_static_strict_admission_accepts_only_conservative_family() -> None:
    registry = TrialHandleRegistry(
        trial_id="wp014h-admission-test",
        root_sha256=hashlib.sha256(b"root").hexdigest(),
        secret=hashlib.sha256(b"secret").digest(),
    )
    registry.register(object_type="net", canonical_identity="n1", attributes={"fanout": 8, "path_rank": 1})
    registry.register(object_type="net", canonical_identity="n2", attributes={"fanout": 7, "path_rank": 2})
    registry.register(object_type="net", canonical_identity="n3", attributes={"fanout": 6, "path_rank": 3})
    wp014h_private = {
        "registry": registry,
        "predefined_regions": (),
        "geometry_bounds": (0, 4, 0, 4),
    }
    context = CompileContext(
        wp014h_private["registry"], "ROUTED", wp014h_private["predefined_regions"],
        wp014h_private["geometry_bounds"], fanout_admitted=True,
        action_surface_version="v3",
    )
    value = candidate("nonprefix_multi_net_batch", wp014h_private)
    import json
    result = _classify(
        json.dumps({"kind": "COMMIT", "candidate": value}).encode(),
        condition="G_STRICT_BOUNDARY", compile_context=context,
    )
    assert result["terminal"] == "MODEL_OUTPUT_COMPATIBLE"
    assert result["strict_construct_family"] == "NONPREFIX_MULTI_NET_BATCH"
