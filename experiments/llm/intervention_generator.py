"""Compatibility import; canonical owner is integrations.llm.intervention_generator."""
import sys
from integrations.llm import intervention_generator as _owner
sys.modules[__name__] = _owner
