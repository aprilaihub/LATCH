import json
from pathlib import Path
import subprocess
import sys
import pytest
from datetime import datetime, timezone

from experiments.acceptance.config import WP015_SEEDS, VALIDATION, RW_REVISION, validate_config, TOOL_ORDER, PAID_ORDER
from experiments.acceptance.runner import AcceptanceBatch, dof_reached, public_dof
from experiments.acceptance.templates import fixed_messages
from tests.latch.test_acceptance_budget import limits
from tests.latch.test_dof_capture_and_failures import captured
from experiments.dof.runtime.captured_context import context_from_capture


def config(tmp_path):
    root = tmp_path / "logicnets_jscl_2025.1.dcp"; root.write_text("fixture only")
    return dict(input=str(root), golden=str(root), output=str(tmp_path / "out"),
        ledger_path=str(tmp_path / "out/batch.db"), period_ns=1.5, rapidwright_revision=RW_REVISION,
        tools=dict(python=sys.executable, vivado=sys.executable, rapidwright=str(tmp_path), java_home=str(tmp_path)),
        cost=limits(), validation=VALIDATION.copy())


def test_exact_wp015_seed_source_and_old_seed_rejected(tmp_path):
    cfg = config(tmp_path)
    assert WP015_SEEDS == [0xC0FFEE,0x5EED1234,0x1BADB002,0xD15EA5E,0xA11CE55]
    for seeds in ([0xDEADBEEF], WP015_SEEDS[:-1], WP015_SEEDS + [WP015_SEEDS[0]]):
        altered = cfg | {"validation": VALIDATION | {"seeds": seeds}}
        with pytest.raises(ValueError):
            validate_config(altered)


def test_checklist_and_report_do_not_create_outputs(tmp_path):
    cfg = config(tmp_path)
    batch = AcceptanceBatch(cfg, tmp_path / "config.json")
    assert batch.checklist()["executed"] is False
    assert batch.checklist()["condition_order_paid"] == PAID_ORDER
    assert batch.report()["accounting"] is None
    assert not Path(cfg["output"]).exists()


def test_cli_requires_separate_authorizations_before_reading_config(tmp_path):
    for phase, flags in [("tools", []),("paid", ["--authorized-tools"])]:
        proc = subprocess.run([sys.executable,"-m","experiments.acceptance","--config",str(tmp_path / "missing"),
                               "--phase",phase,*flags],capture_output=True,text=True)
        assert proc.returncode == 2 and "ValueError" in proc.stderr
    assert not list(tmp_path.iterdir())


def test_serial_order_reuses_s0_and_resume_skips_started_results(tmp_path, monkeypatch):
    # The fixture's price evidence is historical. Freeze only this offline
    # test clock; do not weaken production freshness or invent current prices.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 18, tzinfo=timezone.utc)
    monkeypatch.setattr("experiments.acceptance.runner.datetime", Clock)
    cfg = config(tmp_path)
    batch = AcceptanceBatch(cfg, tmp_path / "config.json")
    seen = []
    monkeypatch.setattr(batch,"versions",lambda: seen.append("versions"))
    monkeypatch.setattr(batch,"golden_self",lambda: seen.append("golden-self"))
    monkeypatch.setattr(batch,"capture",lambda: seen.append("capture"))
    monkeypatch.setattr(batch,"dof",lambda c,paid: seen.append(("dof-" if paid else "fixed-") + c))
    monkeypatch.setattr(batch,"ablation",lambda n: seen.append(n))
    from optimizer_pipeline.backends.xilinx import configured_environment as env
    monkeypatch.setattr(env,"configured_environment",lambda **kwargs: {})
    batch.run("tools")
    assert seen == TOOL_ORDER
    batch.run("tools")
    assert seen == TOOL_ORDER
    batch.run("paid")
    assert seen == TOOL_ORDER + PAID_ORDER
    assert seen.count("S0-baseline") == seen.count("S0-extension") == 1


@pytest.mark.parametrize("stamp", ["2026-09-30T17:59:59Z", "2026-10-01T18:00:01Z"])
def test_stale_or_future_price_evidence_stops_before_paid_calls(tmp_path, monkeypatch, stamp):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 18, tzinfo=timezone.utc)
    monkeypatch.setattr("experiments.acceptance.runner.datetime", Clock)
    cfg = config(tmp_path)
    cfg["cost"]["verified_at"] = stamp
    batch = AcceptanceBatch(cfg, tmp_path / "config.json")
    batch.prepare()
    for name in TOOL_ORDER:
        batch.once(name, lambda: "REACHED")
    with pytest.raises(RuntimeError, match="price"):
        batch.run("paid")
    assert batch.ledger.summary()["requests"] == 0
    assert not any(name in batch.ledger.summary()["conditions"] for name in PAID_ORDER)


def test_paid_phase_cannot_start_without_all_physical_capabilities(tmp_path):
    batch = AcceptanceBatch(config(tmp_path), tmp_path / "config.json")
    with pytest.raises(RuntimeError, match="capability"):
        batch.run("paid")
    assert batch.ledger.summary()["requests"] == 0


def test_once_partial_failure_is_not_replayed(tmp_path):
    batch = AcceptanceBatch(config(tmp_path), tmp_path / "config.json")
    batch.prepare()
    def failure():
        raise RuntimeError("fixture")
    with pytest.raises(RuntimeError):
        batch.once("capture", failure)
    calls = []
    with pytest.raises(RuntimeError):
        batch.once("capture", lambda: calls.append(1))
    assert calls == []


def test_model_invalid_proposal_is_not_labelled_reached(tmp_path):
    batch = AcceptanceBatch(config(tmp_path), tmp_path / "config.json")
    batch.prepare()
    batch.once("dof-recipe", lambda: "RESEARCH_RESULT")
    assert batch.ledger.summary()["conditions"]["dof-recipe"] == "RESEARCH_RESULT"


def test_interrupt_does_not_replay_started_condition_but_allows_unstarted(tmp_path):
    batch = AcceptanceBatch(config(tmp_path),tmp_path / "config.json")
    batch.prepare()
    def interrupted():
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        batch.once("dof-recipe", interrupted)
    calls = []
    batch.once("dof-recipe", lambda: calls.append("REPLAY"))
    batch.once("dof-direct", lambda: calls.append("NEW"))
    assert calls == ["NEW"]
    assert batch.ledger.summary()["conditions"]["dof-recipe"] == "INTERRUPTED"


def test_real_template_requires_root_bound_capture_and_strict_geometry(tmp_path):
    root,capture,write = captured(tmp_path)
    context = context_from_capture(root=root,capture=capture,target_clock="clk")
    for c in ("recipe","direct"):
        assert len(fixed_messages(c,root=root,context=context)) == 1
    with pytest.raises(ValueError, match="nondegenerate"):
        fixed_messages("constructed",root=root,context=context | {"geometry_bounds": [1,1,2,2]})
    # Synthetic capture with two actual observed regions, not a mock fallback.
    write("contract-probe/private/resolved-objects.json", dict(
        device_regions=[dict(identity="X1Y2"),dict(identity="X2Y2")],
        timing_paths=[], critical_cell_locations=[dict(identity="a",index=0,attribute="SLICE\x1fSLICE_X1Y1\x1fX1Y2")]))
    context = context_from_capture(root=root,capture=capture,target_clock="clk")
    messages = fixed_messages("constructed",root=root,context=context)
    assert messages[0]["candidate"]["steps"][1]["args"] == dict(x_min=1,x_max=2,y_min=2,y_max=2)
    with pytest.raises(ValueError):
        fixed_messages("recipe",root=root,context={})


def test_exit_code_is_not_a_physical_pass_and_public_report_omits_private_fields():
    facts = dict(planning_terminal="COMMITTED",execution_started=True,physical_valid=True,
        root_unchanged=True,post_commit_scoring=dict(status="MEASURED"),
        functional_validation=dict(passed=True,all_seeds_present_exactly_once=True,
            seed_reports=[dict(stimulus_seed=s,passed=True,infrastructure_failure=False,secret="private") for s in WP015_SEEDS]),
        handle_secret_hex="secret",feedback=["private"])
    assert dof_reached(facts)
    assert "secret" not in json.dumps(public_dof(facts))
    facts["functional_validation"]["seed_reports"].pop()
    assert not dof_reached(facts)
