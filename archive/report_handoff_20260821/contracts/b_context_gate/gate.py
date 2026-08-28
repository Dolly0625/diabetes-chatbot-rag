from __future__ import annotations

from typing import Literal, Protocol

from .schemas import CanonicalBInput, CanonicalBResult


class ContextGate(Protocol):
    def evaluate(self, request: CanonicalBInput) -> CanonicalBResult:
        ...


class DeterministicContextGate:
    """Small offline B boundary for the deterministic baseline.

    This is a workflow adapter/demo gate, not a replacement for the existing
    B LLM context judge. Fixture evidence may explicitly set
    ``metadata['fixture_b_approved']`` to control an approved subset. Records
    without that explicit fixture approval are not automatically approved; the
    gate fails closed so retrieved context cannot silently become B evidence.
    """

    name = "deterministic-context-gate-fixture"

    def __init__(self, *, approval_mode: Literal["fixture", "all_retrieved"] = "fixture") -> None:
        if approval_mode not in {"fixture", "all_retrieved"}:
            raise ValueError("approval_mode must be 'fixture' or 'all_retrieved'")
        self.approval_mode = approval_mode
        if approval_mode == "all_retrieved":
            self.name = "deterministic-context-gate-demo-all-retrieved"

    def evaluate(self, request: CanonicalBInput) -> CanonicalBResult:
        if not request.evidence:
            return CanonicalBResult(
                request_id=request.request_id,
                decision="INSUFFICIENT",
                approved_evidence_ids=[],
                evidence=[],
                reason_codes=["CONTEXT_INSUFFICIENT", "NO_RETRIEVED_EVIDENCE"],
                retrieval_feedback={"retrieval_queries": request.retrieval_queries},
                relevance="NONE",
                sufficiency="INSUFFICIENT",
                safety="NOT_ASSESSED",
            )

        seen: set[str] = set()
        duplicate_ids: list[str] = []
        for evidence in request.evidence:
            if evidence.evidence_id in seen:
                duplicate_ids.append(evidence.evidence_id)
            seen.add(evidence.evidence_id)
        if duplicate_ids:
            return CanonicalBResult(
                request_id=request.request_id,
                decision="UNSAFE",
                approved_evidence_ids=[],
                evidence=request.evidence,
                reason_codes=["DUPLICATE_EVIDENCE_ID"],
                retrieval_feedback={"duplicate_ids": duplicate_ids},
                relevance="UNKNOWN",
                sufficiency="UNSAFE",
                safety="FAIL",
            )

        approved = [
            evidence.evidence_id
            for evidence in request.evidence
            if self.approval_mode == "all_retrieved"
            or evidence.metadata.get("fixture_b_approved") is True
        ]
        if not approved:
            return CanonicalBResult(
                request_id=request.request_id,
                decision="INSUFFICIENT",
                approved_evidence_ids=[],
                evidence=request.evidence,
                reason_codes=["CONTEXT_INSUFFICIENT", "NO_APPROVED_EVIDENCE"],
                retrieval_feedback={"retrieval_queries": request.retrieval_queries},
                relevance="UNKNOWN",
                sufficiency="INSUFFICIENT",
                safety="NOT_ASSESSED",
            )
        return CanonicalBResult(
            request_id=request.request_id,
            decision="PASS",
            approved_evidence_ids=approved,
            evidence=request.evidence,
            reason_codes=[
                "B_CONTEXT_CONTRACT_VALID",
                "DEMO_RETRIEVED_EVIDENCE_APPROVED"
                if self.approval_mode == "all_retrieved"
                else "EVIDENCE_APPROVED",
            ],
            retrieval_feedback={"retrieval_queries": request.retrieval_queries},
            relevance="RETRIEVED",
            sufficiency="SUFFICIENT",
            safety="DEMO_RETRIEVED_APPROVED" if self.approval_mode == "all_retrieved" else "FIXTURE_APPROVED",
        )
