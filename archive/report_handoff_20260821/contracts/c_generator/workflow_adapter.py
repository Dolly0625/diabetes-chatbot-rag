"""Workflow adapter for the frozen C v2 generator contract.

The existing C v2 experiment continues to own its live runner and legacy case
fixture shape. This module defines the smaller canonical workflow input and
adapts it to the existing v2 prompt when a live chain is injected.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from tfda_context_gate.b_context_gate.schemas import CanonicalBResult, CanonicalEvidence

from .prompts import EVIDENCE_AWARE_V2_SYSTEM, evidence_aware_v2_user_prompt
from .schemas import EvidenceAwareV2Answer, V2SupportedClaim, V2UnsupportedRequest


C_V2_SCHEMA_VERSION = "c.v2"


class CWorkflowInput(BaseModel):
    """Canonical workflow input; only this shape enters C v2 in the runner."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    schema_version: str = Field(default=C_V2_SCHEMA_VERSION, min_length=1)
    original_query: str = Field(min_length=1)
    b_decision: str = Field(min_length=1)
    approved_evidence_ids: list[str] = Field(default_factory=list)
    evidence: list[CanonicalEvidence] = Field(default_factory=list)


class CGenerator(Protocol):
    def generate(self, request: CWorkflowInput) -> EvidenceAwareV2Answer:
        ...


def c_input_from_b_result(b_result: CanonicalBResult, *, original_query: str) -> CWorkflowInput:
    return CWorkflowInput(
        request_id=b_result.request_id,
        original_query=original_query,
        b_decision=b_result.decision,
        approved_evidence_ids=b_result.approved_evidence_ids,
        evidence=b_result.evidence,
    )


def to_legacy_v2_case(request: CWorkflowInput) -> dict[str, Any]:
    """Build the old experiment prompt shape at the live-chain boundary only."""

    return {
        "case_id": request.request_id,
        "case_type": "workflow_baseline",
        "query": request.original_query,
        "b_decision": request.b_decision,
        "approved_document_ids": list(request.approved_evidence_ids),
        "contexts": [
            {
                "document_id": item.evidence_id,
                "page_content": item.content,
                "source": item.source,
                "metadata": item.metadata,
                "score": item.score,
                "發布日期": item.date or "",
                "version": item.version,
            }
            for item in request.evidence
        ],
    }


class DeterministicFixtureCGenerator:
    """Offline C v2 implementation used only for E2E contract validation."""

    name = "deterministic-c-v2-fixture"

    def __init__(self, *, max_evidence: int | None = None) -> None:
        if max_evidence is not None and max_evidence < 1:
            raise ValueError("max_evidence must be >= 1 when provided")
        self.max_evidence = max_evidence

    def generate(self, request: CWorkflowInput) -> EvidenceAwareV2Answer:
        approved = set(request.approved_evidence_ids)
        usable = [item for item in request.evidence if item.evidence_id in approved]
        if self.max_evidence is not None:
            usable = usable[: self.max_evidence]
        if not usable:
            return EvidenceAwareV2Answer(
                decision="INSUFFICIENT",
                answer="目前提供的資料不足以可靠回答這個問題。",
                supported_claims=[],
                unsupported_requests=[
                    V2UnsupportedRequest(
                        request=request.original_query,
                        reason="沒有可用的 B-approved evidence",
                    )
                ],
                limitations=["本次 workflow 使用的 evidence 不足。"],
            )

        claims = [
            V2SupportedClaim(
                claim_id=f"c{index}",
                claim=item.content,
                evidence_ids=[item.evidence_id],
            )
            for index, item in enumerate(usable, 1)
        ]
        return EvidenceAwareV2Answer(
            decision="ANSWER",
            answer="根據提供的資料：" + "".join(item.claim for item in claims),
            supported_claims=claims,
            unsupported_requests=[],
            limitations=[],
        )


class LangChainCV2Generator:
    """Adapter for an already-configured C v2 structured-output chain.

    The workflow never constructs a model implicitly. Callers inject the
    existing chain, preserving C v2's prompt and structured output contract.
    """

    name = "langchain-c-v2-adapter"

    def __init__(self, chain: Any) -> None:
        self.chain = chain

    def generate(self, request: CWorkflowInput) -> EvidenceAwareV2Answer:
        from langchain_core.messages import HumanMessage, SystemMessage

        response = self.chain.invoke(
            [
                SystemMessage(content=EVIDENCE_AWARE_V2_SYSTEM),
                HumanMessage(content=evidence_aware_v2_user_prompt(to_legacy_v2_case(request))),
            ]
        )
        parsed = response.get("parsed") if isinstance(response, dict) else response
        if parsed is None:
            raise ValueError("C v2 structured output did not contain parsed data")
        return EvidenceAwareV2Answer.model_validate(parsed)
