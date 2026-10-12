"""Compatibility import; shared owner: integrations.llm.runtime.adapters.openai_compatible."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("integrations.llm.runtime.adapters.openai_compatible")
