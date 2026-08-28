from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


E_SCHEMA_VERSION = "e.v0.1"
TraceStatus = Literal[
    "STARTED",
    "COMPLETED",
    "BLOCKED",
    "INSUFFICIENT",
    "FALLBACK",
    "ERROR",
    "SKIPPED",
    "NEEDS_CLARIFICATION",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestMetadata(StrictModel):
    """Request-level metadata shared by every E record."""

    request_id: str = Field(min_length=1)
    trace_id: str | None = None
    thread_id: str | None = None
    schema_version: str = Field(default=E_SCHEMA_VERSION, min_length=1)
    timestamp: datetime = Field(default_factory=utc_now)
    declared_role: str | None = None
    # This is redacted text when supplied. The original raw text is never
    # required for trace persistence; query_hash supports correlation without
    # storing the raw query in a production sink.
    original_query: str | None = None
    query_hash: str | None = None


class TraceEvent(StrictModel):
    """One structured component execution event.

    The optional fields intentionally cover A/RAG/B/C/D and reserve Agent
    fields without requiring an Agent implementation.
    """

    record_type: Literal["trace_event"] = "trace_event"
    request_id: str = Field(min_length=1)
    trace_id: str | None = None
    thread_id: str | None = None
    schema_version: str = Field(default=E_SCHEMA_VERSION, min_length=1)
    timestamp: datetime = Field(default_factory=utc_now)
    declared_role: str | None = None
    original_query: str | None = None
    query_hash: str | None = None

    component: str = Field(min_length=1)
    node_name: str = Field(min_length=1)
    status: TraceStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    latency_ms: float | None = Field(default=None, ge=0)

    # A: Input Router + Policy Gate
    router_status: str | None = None
    intent_tags: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    rag_allowed: bool | None = None
    prompt_guard_result: Any | None = None

    # RAG
    retrieval_query: str | None = None
    retrieved_count: int | None = Field(default=None, ge=0)
    retrieved_evidence_ids: list[str] = Field(default_factory=list)
    retrieval_latency_ms: float | None = Field(default=None, ge=0)
    retrieval_attempt: int | None = Field(default=None, ge=1)
    retrieved_evidence: list[dict[str, Any]] = Field(default_factory=list)

    # B: Contract Gate + Context Gate
    decision: str | None = None
    outcome: str | None = None
    approved_evidence_ids: list[str] = Field(default_factory=list)
    approved_evidence_count: int | None = Field(default=None, ge=0)
    b_attempt: int | None = Field(default=None, ge=1)
    relevance: str | None = None
    sufficiency: str | None = None
    conflict: str | None = None
    safety: str | None = None

    # C: Evidence-aware Generator
    candidate_decision: str | None = None
    claim_count: int | None = Field(default=None, ge=0)
    evidence_ids: list[str] = Field(default_factory=list)

    # D: Mandatory Output Gate
    failure_type: str | None = None
    failed_claims: list[Any] = Field(default_factory=list)
    invalid_evidence_ids: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None

    # Query rewrite / clarification display fields. These are structured
    # observations only; hidden model reasoning is never recorded.
    current_query: str | None = None
    rewritten_query: str | None = None
    rewrite_attempt: int | None = Field(default=None, ge=1)
    missing_information: list[str] = Field(default_factory=list)
    identified_missing_information: list[str] = Field(default_factory=list)
    planner_context: dict[str, Any] | None = None
    question: str | None = None

    # System/dependency metadata
    model_name: str | None = None
    token_usage: dict[str, int] | None = None
    error_type: str | None = None
    error_message: str | None = None

    # Agent v0.1 fields: optional so the same E contract covers the baseline.
    agent_action: str | None = None
    requested_action: str | None = None
    requested_reason_code: str | None = None
    reason_code: str | None = None
    actions_taken: list[str] = Field(default_factory=list)
    agent_step: int | None = Field(default=None, ge=0)
    step_count: int | None = Field(default=None, ge=0)
    retry_count: int | None = Field(default=None, ge=0)
    rewrite_count: int | None = Field(default=None, ge=0)
    clarification_count: int | None = Field(default=None, ge=0)
    tool_name: str | None = None
    termination_reason: str | None = None


class EvaluationRecord(StrictModel):
    """Evaluation data collected for later human/offline analysis."""

    record_type: Literal["evaluation"] = "evaluation"
    request_id: str = Field(min_length=1)
    thread_id: str | None = None
    schema_version: str = Field(default=E_SCHEMA_VERSION, min_length=1)
    timestamp: datetime = Field(default_factory=utc_now)
    original_query: str | None = None
    query_hash: str | None = None
    expected_decision: str | None = None
    actual_decision: str | None = None
    outcome: str | None = None
    failure_type: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LatencySummary(StrictModel):
    count: int = Field(default=0, ge=0)
    total_ms: float = Field(default=0, ge=0)
    average_ms: float | None = Field(default=None, ge=0)


class MetricsSnapshot(StrictModel):
    """Small in-process metrics snapshot; export systems can replace it later."""

    record_type: Literal["metrics"] = "metrics"
    request_count: int = Field(default=0, ge=0)
    event_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    fallback_count: int = Field(default=0, ge=0)
    blocked_count: int = Field(default=0, ge=0)
    by_component: dict[str, int] = Field(default_factory=dict)
    latency_by_component: dict[str, LatencySummary] = Field(default_factory=dict)
