from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "audit_wp014e_gate0.py"
SPEC = importlib.util.spec_from_file_location("audit_wp014e_gate0", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_canonical_hash_is_stable_and_sensitive() -> None:
    assert MODULE.digest({"a": 1, "b": 2}) == MODULE.digest({"b": 2, "a": 1})
    assert MODULE.digest({"a": 1}) != MODULE.digest({"a": 2})


def test_required_key_names_are_exact() -> None:
    assert MODULE.REQUIRED_KEYS == (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
    )
