from pathlib import Path

from optimizer_pipeline.dof.single_submission.contract_v5 import compile_candidate_v5
from optimizer_pipeline.dof.single_submission.handles import TrialHandleRegistry
from optimizer_pipeline.dof.single_submission.language import CompileContext
from scripts.run_wp014g_probes import (
    PROBE_IDS,
    _candidate,
    mechanism_state_deltas,
    parser,
)


def context():
    registry = TrialHandleRegistry(trial_id="wp014g-test", root_sha256="a" * 64, secret=b"x" * 32)
    registry.register(object_type="cell", canonical_identity="cell/A", attributes={"path_member": True, "resource_type": "LUT"})
    registry.register(object_type="net", canonical_identity="net/A", attributes={"fanout": 8})
    registry.register(object_type="hardblock", canonical_identity="ram/A", attributes={"path_member": True, "resource_type": "BRAM"})
    registry.register(object_type="site", canonical_identity="RAMB36_X0Y1", attributes={"compatible_resource_types": ["BRAM"]})
    return {
        "registry": registry,
        "predefined_regions": (),
        "geometry_bounds": (0, 1, 0, 4),
    }


def test_all_probe_fixtures_compile_with_exact_static_budget() -> None:
    private = context()
    compile_context = CompileContext(
        private["registry"], "ROUTED", private["predefined_regions"],
        private["geometry_bounds"], fanout_admitted=True,
        action_surface_version="v3",
    )
    for probe_id in PROBE_IDS:
        compiled = compile_candidate_v5(_candidate(probe_id, private), compile_context)
        assert compiled.language == "G"
        assert 1 <= compiled.heavy_calls <= 4
        assert compiled.light_mutations <= 8


def test_probe_controller_declares_no_provider_surface() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "run_wp014g_probes.py").read_text()
    assert "OPENAI_API_KEY" not in source
    assert "DEEPSEEK_API_KEY" not in source
    assert "OPENROUTER_API_KEY" not in source
    assert "ANTHROPIC_API_KEY" not in source


def test_probe_subset_is_explicit_and_repeatable() -> None:
    args = parser().parse_args([
        "--bundle", "bundle.json", "--capture", "capture", "--out", "out",
        "--python", "python", "--vivado", "vivado",
        "--probe-id", "net_generated_target_route",
        "--probe-id", "property_generated_net_target",
    ])
    assert args.probe_id == ["net_generated_target_route", "property_generated_net_target"]


def test_mechanism_state_delta_projection_is_identity_free(tmp_path) -> None:
    import base64
    import json

    private = tmp_path / "private"
    private.mkdir()
    marker = "DOF_WP014C_ROUTE_NET_V2:AUTO_DELAY:1:10952:12108"
    (private / "worker-call-outputs.json").write_text(json.dumps([{
        "operation": "vivado.wp014c_route_net",
        "status": "0",
        "output_b64": base64.b64encode(marker.encode()).decode(),
    }]))
    rows = mechanism_state_deltas(tmp_path, probe_id="net_generated_target_route")
    assert rows[0]["evidence_strength"] == "ROUTE_BEFORE_AFTER"
    assert rows[0]["before_hash"] != rows[0]["after_hash"]
    assert "private/net/name" not in json.dumps(rows)
