import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from optimizer_pipeline.capabilities.intervention.recipe import (
    PrimitiveCatalog,
    RecipeValidator,
    RecipeSynthesisController,
    deterministic_place_reset_recipe,
)
from experiments.llm import ProviderInterventionGenerator
from optimizer_pipeline.capabilities.intervention import GeneratedInterventionProposal
from optimizer_pipeline.core.store import load_run_view


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_optimizer(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_dcp = tmp_path / "input.dcp"
    input_dcp.write_text("mock input\n")
    run_dir = tmp_path / "run"
    cmd = [
        sys.executable,
        "dcp_optimizer.py",
        str(input_dcp),
        "--mock",
        "--no-dotenv",
        "--run-dir",
        str(run_dir),
        *args,
    ]
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENROUTER_API_KEY", None)
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env, text=True, capture_output=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_valid_recipe_json_canonicalizes():
    payload = {
        "recipe_id": "try_place_reset",
        "summary": "Try a fresh place/route.",
        "steps": [
            {"primitive": "macro.unroute_design"},
            {"primitive": "macro.unplace_design"},
            {"primitive": "vivado.place_design", "args": {"directive": "Explore"}},
            {"primitive": "vivado.route_design", "args": {"directive": "Explore"}},
        ],
    }

    result = RecipeValidator(max_steps=8).validate(payload)

    assert result.valid
    assert result.recipe is not None
    assert result.recipe.recipe_id == "try_place_reset"
    assert [step.primitive for step in result.recipe.steps] == [
        "macro.unroute_design",
        "macro.unplace_design",
        "vivado.place_design",
        "vivado.route_design",
    ]


def test_recipe_validator_rejects_free_tcl_shell_and_paths():
    payload = {
        "recipe_id": "bad",
        "steps": [
            {"primitive": "run_tcl", "args": {"command": "place_design -unplace"}},
            {"primitive": "vivado.place_design", "args": {"directive": "Explore", "output_path": "/tmp/x"}},
            {"primitive": "shell", "args": {"command": "rm -rf /"}},
        ],
    }

    result = RecipeValidator(max_steps=8).validate(payload)

    assert not result.valid
    joined = "\n".join(result.errors)
    assert "forbidden_primitive:run_tcl" in joined
    assert "forbidden_arg_keys:output_path" in joined
    assert "forbidden_primitive:shell" in joined


def test_recipe_validator_accepts_existing_flow_primitives_without_args():
    payload = {
        "recipe_id": "try_existing_flows",
        "steps": [
            {"primitive": "flow.auto_pblock"},
            {"primitive": "flow.lut_cone"},
            {"primitive": "flow.retime"},
            {"primitive": "flow.memory_path_opt"},
        ],
    }

    result = RecipeValidator(max_steps=8).validate(payload)

    assert result.valid
    assert result.recipe is not None
    assert [step.primitive for step in result.recipe.steps] == [
        "flow.auto_pblock",
        "flow.lut_cone",
        "flow.retime",
        "flow.memory_path_opt",
    ]
    catalog = PrimitiveCatalog.default()
    assert catalog.get("flow.auto_pblock").flow_name == "auto_pblock"
    assert catalog.get("flow.retime").flow_name == "retime"


def test_recipe_validator_requires_flow_prefix_for_retime():
    payload = {
        "recipe_id": "bad_bare_retime",
        "steps": [
            {"primitive": "retime"},
        ],
    }

    result = RecipeValidator(max_steps=8).validate(payload)

    assert not result.valid
    assert any("unknown_primitive:retime" in error for error in result.errors)


def test_recipe_validator_rejects_flow_primitive_args():
    payload = {
        "recipe_id": "bad_flow_args",
        "steps": [
            {"primitive": "flow.phys_opt", "args": {"directive": "Explore"}},
        ],
    }

    result = RecipeValidator(max_steps=8).validate(payload)

    assert not result.valid
    assert any("flow_args_not_allowed:directive" in error for error in result.errors)


def test_recipe_validator_rejects_unknown_directive_and_too_many_steps():
    too_many = {
        "recipe_id": "too_many",
        "steps": [{"primitive": "macro.unroute_design"} for _ in range(9)],
    }
    bad_directive = {
        "recipe_id": "bad_directive",
        "steps": [{"primitive": "vivado.route_design", "args": {"directive": "AggressiveExplore"}}],
    }

    assert not RecipeValidator(max_steps=8).validate(too_many).valid
    result = RecipeValidator(max_steps=8).validate(bad_directive)
    assert not result.valid
    assert any("directive_not_allowed:AggressiveExplore" in error for error in result.errors)


def test_candidate_priorities_schema_is_not_a_recipe():
    payload = {"candidate_priorities": [{"flow": "rescue_place_reset_retime"}]}

    result = RecipeValidator(max_steps=8).validate(payload)

    assert not result.valid
    assert result.errors == ["old_candidate_priorities_schema_is_not_a_recipe"]


def test_synthesis_mock_returns_valid_deterministic_recipe():
    controller = RecipeSynthesisController(config=object(), max_steps=8)

    result = controller.synthesize_mock(context={"main_profile": {"best_delta_fmax_mhz": 0.0}})

    assert result.recipe is not None
    assert result.validation.valid
    assert result.recipe.recipe_id == deterministic_place_reset_recipe().recipe_id


def test_external_generator_payload_still_passes_managed_recipe_validation():
    class UnsafeGenerator:
        def generate_recipe(self, **_kwargs):
            return GeneratedInterventionProposal({
                "recipe_id": "unsafe",
                "steps": [{"primitive": "run_tcl", "args": {"command": "route_design"}}],
            })

        def usage_summary(self):
            return {}

    result = RecipeSynthesisController(config=object(), generator=UnsafeGenerator()).synthesize(context={})

    assert not result.validation.valid
    assert result.recipe is None
    assert any("forbidden_primitive:run_tcl" in error for error in result.validation.errors)


def test_recipe_synthesis_prompt_mentions_rescue_plan_and_attempt_diagnoses():
    captured = {}

    class CapturingClient:
        def complete_json(self, request):
            captured["request"] = request
            return SimpleNamespace(
                content="{}", parsed_json={}, prompt_tokens=1, completion_tokens=1,
                total_tokens=2, cost_usd=0.0, estimated_cost_usd=0.0,
                provider_reported_cost_usd=None, cost_source="estimated",
                pricing_profile_id="test", latency_sec=0.01,
            )

    generator = ProviderInterventionGenerator(
        SimpleNamespace(provider="openai", model="gpt-5.4-mini", llm_timeout_sec=None),
        client=CapturingClient(),
    )
    generator.generate_recipe(
        context={
            "rescue_attempt_plan": {"objective": "open sibling branch"},
            "attempt_diagnoses": [{"outcome_class": "invalid", "avoid_primitives": ["flow.fanout"]}],
        },
        catalog=PrimitiveCatalog.default().to_dict(),
    )

    request = captured["request"]
    system = request.messages[0]["content"]
    user = request.messages[1]["content"]
    assert "rescue_attempt_plan" in system
    assert "attempt_diagnoses" in system
    assert "avoid repeating" in system
    assert "flow.fanout" in user


def test_rescue_synthesis_always_runs_one_mock_transition(tmp_path: Path):
    proc = run_optimizer(
        tmp_path,
        "--pipeline",
        "fixed",
        "--plan",
        "phys_opt",
        "--budget-runs",
        "2",
        "--rescue-mode",
        "synthesis",
        "--rescue-trigger",
        "always",
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    run_dir = tmp_path / "run"
    view = load_run_view(run_dir)
    decision = view["intervention_decision"]
    assert decision["status"] == "executed"
    assert decision["selection_source"] == "synthesis_recipe"
    assert decision["fallback_used"] is False
    assert decision["selected_recipe"]["flow_name"] == "synthesized_recipe"
    assert decision["gate"]["reason"] == "always_trigger"

    records = view["attempts"]
    assert [record["action_type"] for record in records] == ["phys_opt", "synthesized_recipe"]
    rescue = records[-1]
    assert rescue["parameters"]["recipe_spec"]["recipe_id"] == "synth_place_reset_retime_v1"
    assert rescue["parent_state_id"] == "node_000_root"
