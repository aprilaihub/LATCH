"""Compatibility import; shared owner: integrations.llm.runtime.registry."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("integrations.llm.runtime.registry")
