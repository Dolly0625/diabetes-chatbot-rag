"""Experimental TFDA tool-using agent harness.

This package is intentionally isolated from ``tfda_context_gate``.  It may read
the processed TFDA corpus, but it does not import or mutate the production path.
"""

from .graph import TFDAToolAgent, build_experimental_agent
from .models import LangChainToolCallingModel, RuleBasedTFDAModel
from .schemas import AgentLimits, RunResult

__all__ = [
    "AgentLimits",
    "LangChainToolCallingModel",
    "RuleBasedTFDAModel",
    "RunResult",
    "TFDAToolAgent",
    "build_experimental_agent",
]

