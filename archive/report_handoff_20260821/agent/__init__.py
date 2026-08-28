"""Bounded Agent v0.1 contracts and LangChain adapters.

The Agent only chooses a bounded recovery action.  Workflow execution,
evidence approval and final output validation remain outside this package.
"""

from .config import AGENT_LIMITS, AgentLimits
from .context import build_agent_decision_context, evidence_summaries
from .planner import (
    AgentPlanner,
    LangChainAgentPlanner,
    PlannerError,
    ScriptedAgentPlanner,
)
from .openrouter import AGENT_MODEL, build_agent_openrouter_llm
from .ollama import OLLAMA_AGENT_MODEL, build_agent_ollama_llm
from .rewriter import (
    DeterministicQueryRewriter,
    LangChainQueryRewriter,
    QueryRewriter,
    RewrittenQuery,
)
from .schemas import (
    AgentAction,
    AgentAttempt,
    AgentDecision,
    AgentDecisionContext,
    AgentDecisionStructuredOutput,
    AgentDecisionUnion,
    AskUserDecision,
    FallbackDecision,
    RewriteQueryDecision,
)

__all__ = [
    "AGENT_LIMITS",
    "AGENT_MODEL",
    "OLLAMA_AGENT_MODEL",
    "AgentAction",
    "AgentAttempt",
    "AgentDecision",
    "AgentDecisionContext",
    "AgentDecisionStructuredOutput",
    "AgentDecisionUnion",
    "AgentLimits",
    "AgentPlanner",
    "AskUserDecision",
    "DeterministicQueryRewriter",
    "FallbackDecision",
    "LangChainAgentPlanner",
    "LangChainQueryRewriter",
    "PlannerError",
    "QueryRewriter",
    "RewriteQueryDecision",
    "RewrittenQuery",
    "ScriptedAgentPlanner",
    "build_agent_decision_context",
    "build_agent_openrouter_llm",
    "build_agent_ollama_llm",
    "evidence_summaries",
]
