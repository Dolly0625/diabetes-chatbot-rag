from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentLimits(StrictModel):
    max_agent_steps: int = Field(default=4, ge=1, le=20)
    max_total_tool_calls: int = Field(default=6, ge=1, le=50)
    deadline_seconds: float = Field(default=15.0, gt=0.0, le=300.0)


class ToolCall(StrictModel):
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class CandidateEvidence(StrictModel):
    evidence_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source: str = Field(min_length=1)
    ingredient: Optional[str] = None
    published_date: Optional[str] = None
    score: float = Field(ge=0.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


ToolStatus = Literal["OK", "ERROR", "BLOCKED"]


class ToolResult(StrictModel):
    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    status: ToolStatus
    payload: Dict[str, Any] = Field(default_factory=dict)
    candidate_evidence: List[CandidateEvidence] = Field(default_factory=list)
    latency_ms: float = Field(default=0.0, ge=0.0)
    cache_hit: bool = False
    error_code: Optional[str] = None


MessageRole = Literal["user", "assistant", "tool", "system"]


class AgentMessage(StrictModel):
    role: MessageRole
    content: str = ""
    tool_calls: List[ToolCall] = Field(default_factory=list)
    tool_result: Optional[ToolResult] = None
    run_id: str = Field(min_length=1)


class AssistantTurn(StrictModel):
    content: str = ""
    tool_calls: List[ToolCall] = Field(default_factory=list)


class TraceEvent(StrictModel):
    run_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    event: str = Field(min_length=1)
    status: str = Field(min_length=1)
    timestamp: float = Field(ge=0.0)
    data: Dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(StrictModel):
    allowed: bool
    reason_code: str = Field(min_length=1)


class EvidenceDecision(StrictModel):
    decision: Literal["PASS", "INSUFFICIENT", "UNSAFE"]
    approved_evidence_ids: List[str] = Field(default_factory=list)
    reason_codes: List[str] = Field(default_factory=list)


class OutputDecision(StrictModel):
    decision: Literal["PASS", "BLOCK"]
    reason_codes: List[str] = Field(default_factory=list)


class RunResult(StrictModel):
    run_id: str
    thread_id: str
    status: Literal["COMPLETED", "BLOCKED", "FALLBACK"]
    final_response: str
    termination_reason: str
    approved_evidence_ids: List[str] = Field(default_factory=list)
    tool_results: List[ToolResult] = Field(default_factory=list)
    trace: List[TraceEvent] = Field(default_factory=list)
    agent_steps: int = Field(ge=0)
    tool_call_counts: Dict[str, int] = Field(default_factory=dict)

