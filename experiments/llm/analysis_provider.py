"""Compatibility import; canonical owner is integrations.llm.analysis_provider."""
import sys
from integrations.llm import analysis_provider as _owner
sys.modules[__name__] = _owner
