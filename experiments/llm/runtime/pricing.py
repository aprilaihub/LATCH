"""Compatibility import; shared owner: integrations.llm.runtime.pricing."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("integrations.llm.runtime.pricing")
