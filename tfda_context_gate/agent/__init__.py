"""Bounded Agent v0.1 contracts and LangChain adapters.

The Agent only chooses a bounded recovery action.  Workflow execution,
evidence approval and final output validation remain outside this package.

【繁中註解｜有界 Agent 套件總覽】
- 本套件僅提供「有界決策」能力：Planner 三選一（ASK_USER/REWRITE_QUERY/FALLBACK），不可覆蓋 A/B/C/D、不可批證據、不可選節點、不可改上限。
- 執行權歸 workflow/graph.py 的 StateGraph；僅 B=INSUFFICIENT 且已注入 agent_planner 時才進 Agent 分支。
- 有界上限 AGENT_LIMITS（max_agent_steps=2/max_rewrites=1/max_clarifications=1）由系統持有，圖節點強制覆蓋超限決策。
- 對外契約：AgentDecisionContext（窄化輸入，非 WorkflowState 全量）→ AgentDecision（判別聯合）→ 圖節點執行。
- 重寫安全：validate_meaning_preserving_rewrite 校驗重寫未新增未提供醫療事實；build_agent_question 僅映射追問句。
"""

from .config import AGENT_LIMITS, AgentLimits
from .clarification_policy import DeterministicClarificationPolicy
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
    "DeterministicClarificationPolicy",
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
