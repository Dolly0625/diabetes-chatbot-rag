from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicySnapshot(StrictModel):
    """The final A decision copied into the D boundary.

    D treats these fields as facts from A. It does not infer or replace A's
    policy decision.
    """

    router_status: str = Field(min_length=1)
    rag_allowed: bool
    risk_flags: list[str] = Field(default_factory=list)
    intent_tags: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class EvidenceRecord(StrictModel):
    evidence_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceSet(StrictModel):
    """Normalized B output used by D.

    ``approved_evidence_ids`` is deliberately separate from ``evidence``:
    retrieved context is not automatically approved context.
    """

    decision: str = Field(min_length=1)
    approved_evidence_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)


class SupportedClaim(StrictModel):
    claim_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class UnsupportedRequest(StrictModel):
    request: str = Field(min_length=1)
    reason: str = ""


class CandidateResponse(StrictModel):
    """Canonical C v0.1 response, matching C's v2 interface."""

    decision: Literal["ANSWER", "PARTIAL", "INSUFFICIENT"]
    answer: str = Field(min_length=1)
    supported_claims: list[SupportedClaim] = Field(default_factory=list)
    unsupported_requests: list[UnsupportedRequest] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ClaimFailure(StrictModel):
    claim_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    status: str = Field(min_length=1)
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class OutputGateRequest(StrictModel):
    """D canonical input contract.

    The adapter accepts the repository's existing A/B/C shapes and produces
    this small boundary object. Raw values remain untrusted until the gate
    validates each nested contract.
    """

    request_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    policy: dict[str, Any]
    evidence_set: dict[str, Any]
    candidate_response: Any


class OutputGateResult(StrictModel):
    """The only D decision is PASS or FALLBACK."""

    request_id: str
    schema_version: str
    decision: Literal["PASS", "FALLBACK"]
    passed: bool
    failure_type: Literal["NONE", "SCHEMA", "EVIDENCE", "POLICY", "SEMANTIC", "DEPENDENCY"]
    reason_codes: list[str] = Field(default_factory=list)
    failed_claims: list[ClaimFailure] = Field(default_factory=list)
    invalid_evidence_ids: list[str] = Field(default_factory=list)
    final_response: str = Field(min_length=1)
    candidate_decision: str | None = None
    verifier: str | None = None
