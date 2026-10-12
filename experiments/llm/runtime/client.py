"""Compatibility import; shared owner: integrations.llm.runtime.client."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("integrations.llm.runtime.client")
