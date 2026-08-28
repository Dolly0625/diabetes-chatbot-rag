from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EvidenceClaim(BaseModel):
    claim_id: str = Field(description="Short stable identifier such as claim_1")
    claim: str = Field(description="One factual claim made in the answer")
    evidence_ids: list[str] = Field(default_factory=list)


class EvidenceAwareAnswer(BaseModel):
    decision: Literal["ANSWER", "INSUFFICIENT"]
    answer: str
    claims: list[EvidenceClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class V2SupportedClaim(BaseModel):
    claim_id: str = Field(description="Short stable identifier only, such as c1 or claim_1; never put an evidence ID here")
    claim: str = Field(description="A factual statement supported by the supplied context")
    evidence_ids: list[str] = Field(description="One or more approved evidence IDs that support this claim")


class V2UnsupportedRequest(BaseModel):
    request: str = Field(description="A requested part that the supplied context cannot answer")
    reason: str = Field(default="", description="Why the supplied context cannot answer this request")


class EvidenceAwareV2Answer(BaseModel):
    decision: Literal["ANSWER", "PARTIAL", "INSUFFICIENT"]
    answer: str
    supported_claims: list[V2SupportedClaim] = Field(default_factory=list)
    unsupported_requests: list[V2UnsupportedRequest] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AuxiliaryEvaluation(BaseModel):
    decision: Literal["ANSWER", "INSUFFICIENT"]
    supported_claim_count: int = Field(ge=0)
    partially_supported_claim_count: int = Field(ge=0)
    unsupported_claim_count: int = Field(ge=0)
    important_claim_count: int = Field(ge=0)
    insufficient_handling_correct: bool
    reason_codes: list[str] = Field(default_factory=list)


class V2AuxiliaryEvaluation(BaseModel):
    decision: Literal["ANSWER", "PARTIAL", "INSUFFICIENT"]
    supported_claim_count: int = Field(ge=0)
    partially_supported_claim_count: int = Field(ge=0)
    unsupported_claim_count: int = Field(ge=0)
    important_claim_count: int = Field(ge=0)
    partial_answer_correct: bool
    over_refusal: bool
    insufficient_handling_correct: bool
    reason_codes: list[str] = Field(default_factory=list)
