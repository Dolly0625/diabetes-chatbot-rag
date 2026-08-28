from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


B_SCHEMA_VERSION = "b.v0.1"
BDecision = Literal["PASS", "INSUFFICIENT", "UNSAFE", "REVIEW", "FALLBACK"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CanonicalEvidence(StrictModel):
    """One retrieved evidence item at the B workflow boundary.

    Optional source/score/date/version remain ``None`` when the upstream
    retriever did not provide them; the adapter never invents provenance.
    """

    evidence_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None
    date: str | None = None
    version: str | None = None


class CanonicalBInput(StrictModel):
    request_id: str = Field(min_length=1)
    schema_version: str = Field(default=B_SCHEMA_VERSION, min_length=1)
    original_query: str = Field(min_length=1)
    retrieval_queries: list[str] = Field(min_length=1)
    evidence: list[CanonicalEvidence] = Field(default_factory=list)


class CanonicalBResult(StrictModel):
    """The only B shape visible to the formal workflow."""

    request_id: str = Field(min_length=1)
    schema_version: str = Field(default=B_SCHEMA_VERSION, min_length=1)
    decision: BDecision
    approved_evidence_ids: list[str] = Field(default_factory=list)
    evidence: list[CanonicalEvidence] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    # Neutral observation only: B may report which user facts remain
    # unidentified. It does not recommend an Agent action.
    identified_missing_information: list[str] = Field(default_factory=list, max_length=8)
    retrieval_feedback: dict[str, Any] = Field(default_factory=dict)
    relevance: str | None = None
    sufficiency: str | None = None
    conflict: str | None = None
    safety: str | None = None
