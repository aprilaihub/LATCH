import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_wp014i_validation_canary", ROOT / "scripts/run_wp014i_validation_canary.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_canary_plan_is_exact_and_balanced():
    plan = json.loads((ROOT / "configs/wp014i/validation-canary-v1.json").read_text())
    assert len(plan["cases"]) == 16
    assert len({row["case_id"] for row in plan["cases"]}) == 16
    assert sum(row["memory_class"] == "high" for row in plan["cases"]) >= 2
    kinds = {row["kind"] for row in plan["cases"]}
    assert kinds == {"legacy", "direct_self", "synthetic_self"}
    assert plan["limits"]["target_concurrency"] == 16
    assert plan["limits"]["reserve_fraction"] >= 0.20
    assert plan["limits"]["provider_or_llm_calls"] == 0
    assert plan["limits"]["optimization_calls"] == 0


def test_self_case_preserves_validation_only_external_flag(tmp_path):
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"immutable")
    sha = MODULE.file_sha256(dcp)
    row = MODULE._self_case({
        "case_id": "external", "root_id": "root", "memory_class": "normal",
        "validation_only_external_access": True,
    }, dcp, sha)
    assert row["golden_dcp"] == row["revised_dcp"]
    assert row["sealed_holdout_accesses"] == 1
    assert row["resolved_input_sha256"] == {"golden": sha, "revised": sha}


def test_resume_reuses_hashed_results_and_preserves_interrupted_directory(tmp_path, monkeypatch):
    dcp = tmp_path / "root.dcp"
    dcp.write_bytes(b"immutable")
    sha = MODULE.file_sha256(dcp)
    cases = [
        MODULE._self_case({
            "case_id": case_id, "root_id": case_id, "memory_class": "normal",
        }, dcp, sha)
        for case_id in ("complete", "interrupted")
    ]
    output = tmp_path / "cases"
    complete = output / "complete"
    complete.mkdir(parents=True)
    core = {
        "case": cases[0], "terminal": "MULTISEED_FUNCTIONAL_PASS",
        "wall_seconds": 1.0, "input_sha256_before": cases[0]["resolved_input_sha256"],
        "input_sha256_after": cases[0]["resolved_input_sha256"],
        "resource": {"process_group_telemetry": {}},
        "cleanup": {"audit_2": []},
    }
    result = core | {"result_sha256": MODULE.object_sha256(core)}
    (complete / "result.json").write_text(json.dumps(result))
    interrupted = output / "interrupted"
    interrupted.mkdir()
    (interrupted / "validator.log").write_text("terminated externally")

    monkeypatch.setattr(MODULE, "run_case", lambda case, *_args: {
        **core, "case": case, "input_sha256_before": case["resolved_input_sha256"],
        "input_sha256_after": case["resolved_input_sha256"],
    })
    aggregate = MODULE.run_memory_aware(
        cases, {"unused": True}, output,
        {"limits": {"maximum_complete_cases": 2, "target_concurrency": 2,
                    "reserve_fraction": 0.20},
         "memory_estimates_gib": {"normal": 1, "unknown": 1}},
        resume=True,
    )
    assert aggregate["executed_cases"] == 2
    assert aggregate["reused_case_ids"] == ["complete"]
    assert aggregate["newly_executed_case_ids"] == ["interrupted"]
    assert len(aggregate["interrupted_artifacts"]) == 1
    assert Path(aggregate["interrupted_artifacts"][0], "validator.log").is_file()
