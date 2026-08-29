"""Versioned LLM <-> RAG boundary contract.

This module adapts the cross-team JSON contract without replacing the
internal ``QueryExpansionResult -> RAGResult`` protocol.  External RAG output
is still normalized into ``CanonicalEvidence`` and must pass Context Gate B.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tfda_context_gate.a_router.labels import (
    IntentTag,
    LanguageCode,
    PolicyReasonCode,
    Polarity,
    RiskFlag,
    RouterStatus,
    TargetSubject,
    TimeFrame,
)
from tfda_context_gate.a_router.schemas import AResult
from tfda_context_gate.b_context_gate.adapters import normalize_evidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult

from .schemas import RAGResult


RETRIEVAL_REQUEST_SCHEMA_VERSION = "rag.request.v0.1"
RETRIEVAL_RESPONSE_SCHEMA_VERSION = "rag.response.v0.1"

RetrievalStatus = Literal["SUCCESS", "EMPTY", "PARTIAL", "STALE", "CONFLICT", "ERROR"]
RetrievalRoute = Literal["VECTOR", "GRAPH", "HYBRID"]
GraphPathStatus = Literal["COMPLETE", "PARTIAL"]
EvidenceRiskLevel = Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalContextModifiers(StrictModel):
    time_frame: TimeFrame
    target_subject: TargetSubject
    polarity: Polarity
    language: LanguageCode


class RetrievalGuardrailResult(StrictModel):
    """Read-only A-layer decision supplied to RAG for routing/filtering."""

    intent_tags: list[IntentTag] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    context_modifiers: RetrievalContextModifiers
    router_status: RouterStatus
    reason_codes: list[PolicyReasonCode] = Field(default_factory=list)

    @model_validator(mode="after")
    def only_general_education_reaches_rag(self) -> "RetrievalGuardrailResult":
        if self.router_status is not RouterStatus.G_GENERAL_EDUCATION:
            raise ValueError("only G_GENERAL_EDUCATION may enter the general RAG boundary")
        return self


class RetrievalRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=256)
    schema_version: str = Field(default=RETRIEVAL_REQUEST_SCHEMA_VERSION, min_length=1)
    user_raw_input: str = Field(min_length=1, max_length=8_000)
    retrieval_queries: list[str] = Field(min_length=1, max_length=8)
    guardrail_result: RetrievalGuardrailResult
    language: LanguageCode
    timestamp: datetime

    @field_validator("retrieval_queries")
    @classmethod
    def non_empty_queries(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("retrieval_queries cannot contain empty strings")
        return normalized

    @field_validator("timestamp")
    @classmethod
    def timestamp_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value


class RetrievedChunk(StrictModel):
    """RAG-owned chunk shape agreed in the alignment document."""

    chunk_id: str = Field(min_length=1)
    source: str | None = None
    version: str | None = None
    date: str | None = None
    score: float | None = None
    score_type: str | None = None
    status: str | None = None
    content: str = Field(min_length=1)
    retriever: str | None = None
    entities: list[Any] = Field(default_factory=list)
    relations: list[Any] = Field(default_factory=list)
    evidence_risk_level: EvidenceRiskLevel | None = None
    safety_signal_types: list[str] = Field(default_factory=list)
    risk_basis: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResponse(StrictModel):
    request_id: str = Field(min_length=1, max_length=256)
    schema_version: str = Field(default=RETRIEVAL_RESPONSE_SCHEMA_VERSION, min_length=1)
    retrieval_route: RetrievalRoute | None = None
    retrieval_status: RetrievalStatus
    graph_path_status: GraphPathStatus | None = None
    rerun_suggested: bool = False
    warnings: list[str] = Field(default_factory=list, max_length=32)
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    retrieval_latency_ms: float | None = Field(default=None, ge=0)

    @field_validator("warnings")
    @classmethod
    def bounded_warning_codes(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 200 for value in cleaned):
            raise ValueError("warnings must contain non-empty bounded codes/messages")
        return cleaned

    @model_validator(mode="after")
    def status_matches_chunks(self) -> "RetrievalResponse":
        if self.retrieval_status == "SUCCESS" and not self.chunks:
            raise ValueError("SUCCESS requires at least one chunk")
        if self.retrieval_status in {"EMPTY", "ERROR"} and self.chunks:
            raise ValueError(f"{self.retrieval_status} must return chunks=[]")
        return self


def retrieval_request_from_results(
    a_result: AResult,
    expansion: QueryExpansionResult,
    *,
    timestamp: datetime | None = None,
) -> RetrievalRequest:
    """Build the external request only after A explicitly allows RAG."""

    if not a_result.rag_allowed:
        raise ValueError("A gate did not allow RAG retrieval")
    if a_result.request_id != expansion.request_id:
        raise ValueError("A result and query expansion request_id mismatch")
    return RetrievalRequest(
        request_id=a_result.request_id,
        user_raw_input=a_result.user_raw_input,
        retrieval_queries=expansion.retrieval_queries,
        guardrail_result=RetrievalGuardrailResult(
            intent_tags=a_result.intent_tags,
            risk_flags=a_result.risk_flags,
            context_modifiers=RetrievalContextModifiers.model_validate(
                a_result.context_modifiers.model_dump(mode="python")
            ),
            router_status=a_result.router_status,
            reason_codes=a_result.reason_codes,
        ),
        language=a_result.language,
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def retrieval_response_to_rag_result(
    response: RetrievalResponse | dict[str, Any],
    *,
    request: RetrievalRequest,
) -> RAGResult:
    """Normalize an external response into the existing RAG -> B boundary."""

    parsed = response if isinstance(response, RetrievalResponse) else RetrievalResponse.model_validate(response)
    if parsed.request_id != request.request_id:
        raise ValueError("retrieval response request_id mismatch")
    evidence = [normalize_evidence(chunk.model_dump(mode="python")) for chunk in parsed.chunks]
    return RAGResult(
        request_id=request.request_id,
        original_query=request.user_raw_input,
        retrieval_queries=request.retrieval_queries,
        evidence=evidence,
        retrieval_latency_ms=parsed.retrieval_latency_ms,
        retrieval_status=parsed.retrieval_status,
        retrieval_route=parsed.retrieval_route,
        graph_path_status=parsed.graph_path_status,
        rerun_suggested=parsed.rerun_suggested,
        warnings=parsed.warnings,
    )


__all__ = [
    "RETRIEVAL_REQUEST_SCHEMA_VERSION",
    "RETRIEVAL_RESPONSE_SCHEMA_VERSION",
    "RetrievedChunk",
    "RetrievalGuardrailResult",
    "RetrievalRequest",
    "RetrievalResponse",
    "retrieval_request_from_results",
    "retrieval_response_to_rag_result",
]
