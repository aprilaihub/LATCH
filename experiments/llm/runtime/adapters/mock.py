"""Compatibility import; shared owner: integrations.llm.runtime.adapters.mock."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("integrations.llm.runtime.adapters.mock")
