from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel


AgentAction = Literal["ASK_USER", "REWRITE_QUERY", "FALLBACK"]
AgentReasonCode = Literal[
    "MISSING_REQUIRED_CONTEXT",
    "QUERY_FORMULATION_NEEDS_REWRITE",
    "RECOVERY_EXHAUSTED",
    "LIMIT_EXCEEDED",
    "PLANNER_FAILURE",
    "REWRITER_FAILURE",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AskUserDecision(StrictModel):
    action: Literal["ASK_USER"]
    reason_code: AgentReasonCode
    missing_information: list[str] = Field(min_length=1, max_length=4)


class RewriteQueryDecision(StrictModel):
    action: Literal["REWRITE_QUERY"]
    reason_code: AgentReasonCode


class FallbackDecision(StrictModel):
    action: Literal["FALLBACK"]
    reason_code: AgentReasonCode


AgentDecisionUnion = Union[AskUserDecision, RewriteQueryDecision, FallbackDecision]

AgentDecision = Annotated[
    AgentDecisionUnion,
    Field(discriminator="action"),
]


class AgentDecisionStructuredOutput(RootModel[AgentDecision]):
    """Pydantic root wrapper used by LangChain provider JSON-schema APIs."""

    pass


class EvidenceSummary(StrictModel):
    """Small, deterministic projection of B-visible retrieval metadata."""

    evidence_id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    score: float | None = None
    ingredient: str | None = None
    title: str | None = None
    source: str | None = None
    date: str | None = None
    snippet: str | None = Field(default=None, max_length=240)


class AgentAttempt(StrictModel):
    """System-written history item; the Planner can only read it."""

    query: str = Field(min_length=1, max_length=8_000)
    completed_agent_action: AgentAction
    b_decision: str = Field(min_length=1)
    b_reason_codes: list[str] = Field(default_factory=list, max_length=8)
    retrieval_outcome: dict[str, object] = Field(default_factory=dict)


class AgentDecisionContext(StrictModel):
    """Narrow Planner input. It is deliberately not WorkflowState."""

    original_query: str = Field(min_length=1, max_length=8_000)
    current_query: str = Field(min_length=1, max_length=8_000)
    b_decision: str = Field(min_length=1)
    b_reason_codes: list[str] = Field(default_factory=list, max_length=8)
    # Neutral B observation. The Planner interprets it; B never selects an
    # Agent action or emits a control instruction here.
    identified_missing_information: list[str] = Field(default_factory=list, max_length=8)
    retrieval_feedback: dict[str, object] = Field(default_factory=dict)
    evidence_summaries: list[EvidenceSummary] = Field(default_factory=list, max_length=5)
    previous_attempts: list[AgentAttempt] = Field(default_factory=list, max_length=2)
