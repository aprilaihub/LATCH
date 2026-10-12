from __future__ import annotations

import json

from scripts import build_wp015_m10_panel as panel
from scripts import run_wp015_seed_validation_batch as batch
from scripts import freeze_wp015_inventory as inventory
from scripts import freeze_wp015_core as core_freeze
from optimizer_pipeline.dof.validation_scheduler import ValidationSeed


def test_validation_inventory_keeps_only_physical_valid_cells() -> None:
    cells = [
        {
            "cell_id": "r__phys_opt", "root_id": "r", "resource_class": "CORE",
            "action_key": "macro.phys_opt", "physical_endpoint": "VALID_COMPLETION",
            "root_dcp": "/r.dcp", "child_dcp": "/c.dcp", "root_sha256": "a",
            "child_sha256": "b",
        },
        {
            "cell_id": "r__fanout", "root_id": "r", "resource_class": "LARGE",
            "action_key": "macro.fanout", "physical_endpoint": "ACTION_INVALID",
            "root_dcp": "/r.dcp", "child_dcp": None, "root_sha256": "a",
            "child_sha256": None,
        },
    ]
    cases = panel.validation_cases(cells)
    assert len(cases) == 1
    assert cases[0]["case_id"] == "m10__r__phys_opt"
    assert cases[0]["memory_class"] == "normal"


def test_m10_stress_rule_uses_frozen_priority_and_mechanism_coverage() -> None:
    winners = {
        "logicnets_jscl": "macro.place_route",
        "corescore_500_mod": "macro.memory_path_opt",
        "finn_radioml": "macro.fanout",
        "amd_mini-isp": "macro.lut_cone",
    }
    rows = []
    roots = {}
    for root in panel.PRIORITY:
        roots[root] = {"family": root, "root_sha256": "0" * 64}
        for action in panel.M10:
            rows.append({
                "root_id": root, "action_key": action,
                "root_fallback_gain_mhz": 10.0 if winners.get(root) == action else 0.0,
            })
    result = panel._stress(rows, roots)
    assert result["selected_root_ids"] == [
        "logicnets_jscl", "corescore_500_mod", "finn_radioml", "amd_mini-isp"
    ]
    selected = {row["root_id"]: row for row in result["selected_stress_roots"]}
    assert selected["logicnets_jscl"]["mechanism_flags"]["spatial_or_flow_removed"]
    assert selected["corescore_500_mod"]["mechanism_flags"]["hardblock_or_memory_removed"]
    assert len(selected["finn_radioml"]["retained_action_keys"]) == 9


def test_stress_selection_preserves_all_mechanism_flags_after_replacement() -> None:
    winners = {
        root: "macro.fanout" for root in panel.PRIORITY[:5]
    } | {
        panel.PRIORITY[5]: "macro.place_route",
        panel.PRIORITY[6]: "macro.memory_path_opt",
    }
    rows, roots = [], {}
    for root in panel.PRIORITY:
        roots[root] = {"family": root, "root_sha256": "0" * 64}
        for action in panel.M10:
            rows.append({
                "root_id": root, "action_key": action,
                "root_fallback_gain_mhz": 10.0 if winners.get(root) == action else 0.0,
            })
    result = panel._stress(rows, roots)
    selected = result["selected_stress_roots"]
    assert any(row["mechanism_flags"]["spatial_or_flow_removed"] for row in selected)
    assert any(row["mechanism_flags"]["hardblock_or_memory_removed"] for row in selected)
    assert len({row["root_id"] for row in selected}) == 4


def test_seed_runner_exception_is_typed_not_raised(tmp_path, monkeypatch) -> None:
    def fail(**_kwargs):
        raise RuntimeError("synthetic xsim transport")

    monkeypatch.setattr(batch, "run_prepared_seed", fail)
    unit = ValidationSeed("case", "root", "seed-00", 0xC0FFEE, "normal")
    result = batch._run_seed_unit(unit, {"prepared_workspace": str(tmp_path / "prepared")},
                                  tmp_path / "seed", 200, 60)
    assert result["infrastructure_failure"] is True
    assert result["infrastructure_reason"] == "seed_runner_exception:RuntimeError"
    assert result["passed"] is False
    assert (tmp_path / "seed" / "seed-result.json").is_file()


def test_precheck_mismatch_is_not_mislabeled_as_preparation_failure(
    tmp_path, monkeypatch,
) -> None:
    validator_dir = tmp_path / "dcp_validation_test"
    validator_dir.mkdir()
    (validator_dir / "validation_report.json").write_text(json.dumps({
        "phase1_passed": True,
        "simulation_report": {
            "precheck_passed": False, "mismatch_count": 7,
            "infrastructure_failure": False,
        },
    }))

    def fail(case, _plan, output):
        case_dir = output / "prepared" / case["case_id"]
        case_dir.mkdir(parents=True)
        (case_dir / "validator.log").write_text(
            "Working directory: " + str(validator_dir) + "\n"
        )
        raise RuntimeError("seed_workspace_preparation_failed:" + case["case_id"])

    monkeypatch.setattr(batch, "prepare_case", fail)
    result = batch._prepare_one(
        {"case_id": "mismatch", "resolved_input_sha256": {}},
        {"functional": {"seeds": []}}, tmp_path / "batch",
    )
    assert result["terminal"] == "FUNCTIONAL_PRECHECK_MISMATCH"
    assert result["terminal_reason"] == "precheck_completed_with_functional_mismatch"


def test_lean_inventory_is_exact_and_balanced() -> None:
    portfolio = inventory.read(inventory.PORTFOLIO_PATH)
    models = inventory.read(inventory.MODELS_PATH)
    matrix = inventory.read(inventory.MATRIX_PATH)
    gate0 = {
        "status": "PASS",
        "roots": [
            {**row, "dcp": f"/{row['root_id']}.dcp", "sha256": "0" * 64}
            for row in portfolio["roots"]
        ],
    }
    selected = matrix["roots"][:4]
    stress = {
        "m10": list(panel.M10), "stress_masks_sha256": "1" * 64,
        "selected_root_ids": selected,
        "selected_stress_roots": [
            {"root_id": root, "retained_action_keys": list(panel.M10[:-1])}
            for root in selected
        ],
    }
    jobs = inventory.generate(
        gate0=gate0, stress=stress, models_value=models, matrix=matrix,
        portfolio=portfolio, core_hash="2" * 64,
    )
    assert len(jobs) == 1134
    assert len({row["job_id"] for row in jobs}) == 1134
    assert sum(row["track"] == "N_NEUTRAL" for row in jobs) == 525
    assert sum(row["track"] == "C_CONDITIONED" for row in jobs) == 525
    assert sum(row["track"] == "S_NEUTRAL_REDUCED" for row in jobs) == 84
    assert all(row["scientific_cell_reuse"] is False for row in jobs)


def test_shared_core_manifest_has_all_required_groups() -> None:
    groups = core_freeze.component_hashes()
    assert set(groups) == set(core_freeze.COMPONENT_GROUPS)
    assert all(len(row["group_sha256"]) == 64 for row in groups.values())
    assert all(row["files"] for row in groups.values())
