# Ported from a722b1e; import paths only adapted.
"""auto_top8: determinism and isolation.

Determinism matters because the fitted weights are a paper artifact - a rerun
of the fitting script must reproduce them byte for byte, and the scorer must
return the same flow for the same state every time.

Isolation matters because the whole point of the module is that a replacement
heuristic can be dropped in as another registry entry. If anything outside the
module and its single registration point reaches into auto_top8, that property
is gone.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEIGHTS = os.path.join(REPO, "experiments", "ablation", "configs", "auto_top8_weights_default.json")
FITTER = os.path.join(REPO, "scripts", "fit_auto_top8.py")
DATASET = os.environ.get("LATCH_S0_DATASET", "__not_supplied__")

NINE_FLOWS = [
    "phys_opt", "auto_pblock", "lut_cone", "retime", "congestion_spread",
    "memory_path_opt", "hardblock_relocate", "net_swap", "fanout",
]

SAMPLE = {
    "route_delay_ratio": 0.821065,
    "max_logic_levels": 5,
    "cp_spread_avg_distance": 112.28,
    "total_hardblocks": 0,
}


def _load():
    from experiments.ablation.runtime.auto_top8 import load_policy
    return load_policy()


def test_weights_carry_a_version():
    with open(WEIGHTS) as fh:
        payload = json.load(fh)
    assert payload.get("auto_top8_version"), "weights must carry a version field"
    assert payload.get("auto_top8_lambda") is not None
    assert len(payload["auto_top8_flows"]) == len(NINE_FLOWS)


def test_refitting_is_byte_identical(tmp_path):
    """Two runs of the fitter on the same CSV produce identical bytes."""
    if not os.path.exists(DATASET):
        pytest.skip("External S0 fitting dataset unavailable; refitting not verified")
    outs = []
    for i in range(2):
        dest = str(tmp_path / f"auto_top8_determinism_{i}.json")
        subprocess.run(
            [sys.executable, FITTER, DATASET, "--out", dest],
            check=True, capture_output=True,
        )
        with open(dest, "rb") as fh:
            outs.append(fh.read())
    assert outs[0] == outs[1], "refitting the same CSV changed the weights"


def test_scoring_is_deterministic():
    policy = _load()
    first = policy.select(SAMPLE, NINE_FLOWS)
    for _ in range(5):
        assert policy.select(SAMPLE, NINE_FLOWS) == first
    scores_a = policy.score(SAMPLE)
    scores_b = policy.score(SAMPLE)
    assert scores_a == scores_b


def test_returns_exactly_one_flow_from_allowed():
    policy = _load()
    chosen = policy.select(SAMPLE, NINE_FLOWS)
    assert isinstance(chosen, str)
    assert chosen in NINE_FLOWS


def test_respects_allowed_flows_restriction():
    policy = _load()
    subset = ["retime", "net_swap"]
    assert policy.select(SAMPLE, subset) in subset


def test_missing_features_still_select():
    """Usable at any node, including states with no extracted features."""
    policy = _load()
    assert policy.select({}, NINE_FLOWS) in NINE_FLOWS


def test_tie_break_is_alphabetical():
    from experiments.ablation.runtime.auto_top8 import AutoTop8Policy
    tied = AutoTop8Policy({
        "auto_top8_version": "test",
        "auto_top8_feature_order": ["route_delay_ratio"],
        "auto_top8_standardisation": {"route_delay_ratio": {"mean": 0.0, "std": 1.0}},
        "auto_top8_flows": {
            "zebra": {"weights": [0.0], "intercept": 1.0},
            "alpha": {"weights": [0.0], "intercept": 1.0},
        },
    })
    assert tied.select({"route_delay_ratio": 0.0}, ["zebra", "alpha"]) == "alpha"


def test_isolation_single_reference_point():
    """Only the module itself and the registry may mention auto_top8."""
    allowed = {
        # Source-delivery file listing is metadata, not another policy owner.
        "SOURCE-SCOPE.json",
        os.path.join("optimizer_pipeline", "auto_top8.py"),
        os.path.join("optimizer_pipeline", "expansion", "policies", "registry.py"),
        os.path.join("scripts", "fit_auto_top8.py"),
        os.path.join("tests", "latch", "test_auto_top8.py"),
        # Historical policy replay is an offline audit, not another owner.
        os.path.join("tests", "latch", "test_historical_frontier.py"),
    }
    # Fitted-weight variants are data, not code references: B1/B2 and any later
    # refit may sit alongside the default without breaking isolation.
    allowed |= {os.path.join('configs', n)
                for n in os.listdir(os.path.join(REPO, 'configs'))
                if n.startswith('auto_top8_weights_') and n.endswith('.json')}
    pattern = re.compile(r"auto_top8", re.IGNORECASE)
    offenders = []
    for root, dirs, files in os.walk(REPO):
        # Skip transient run artefacts (gitignored): they legitimately record
        # which policy produced them, which is not a code dependency.
        dirs[:] = [d for d in dirs
                   if not d.startswith(('dcp_validation_', 'dcp_optimizer_run-'))
                   and d not in
                   {".git", "__pycache__", ".venv", "RapidWright", "experiments",
                    "docs", "prompt_exploration", "results", "shared"}]
        for name in files:
            if not name.endswith((".py", ".json", ".yaml", ".yml")):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, REPO)
            if rel in allowed:
                continue
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    if pattern.search(fh.read()):
                        offenders.append(rel)
            except OSError:
                continue
    assert not offenders, f"auto_top8 referenced outside its module: {offenders}"


def test_registered_in_policy_registry():
    from experiments.ablation.runtime.expansion.policies.registry import build_policy_registry
    assert "auto_top8" in build_policy_registry().names()
