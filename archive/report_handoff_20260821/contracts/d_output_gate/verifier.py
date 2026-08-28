from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from .schemas import CandidateResponse, ClaimFailure, EvidenceRecord, EvidenceSet, PolicySnapshot


class SemanticVerifier(Protocol):
    """Pluggable semantic verifier boundary for a future independent judge."""

    name: str

    def verify(
        self,
        candidate: CandidateResponse,
        evidence_set: EvidenceSet,
        policy: PolicySnapshot,
    ) -> "SemanticVerificationResult":
        ...


class SemanticVerificationResult:
    def __init__(
        self,
        *,
        failed_claims: list[ClaimFailure] | None = None,
        unsupported_answer_claims: list[ClaimFailure] | None = None,
        reason_codes: list[str] | None = None,
    ) -> None:
        self.failed_claims = failed_claims or []
        self.unsupported_answer_claims = unsupported_answer_claims or []
        self.reason_codes = reason_codes or []


class HeuristicSemanticVerifier:
    """Demo verifier, not a formal medical safety mechanism.

    It only uses lexical overlap to demonstrate the D interface. Production
    use needs an independently evaluated claim/NLI or LLM verifier and a
    versioned evaluation set.
    """

    name = "heuristic-demo-not-medical-safety"

    # Keep Latin/number runs together but compare Chinese at character level;
    # this lets the demo tolerate the short paraphrases present in C output.
    _token_pattern = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", re.UNICODE)
    _overclaim_pattern = re.compile(r"保證|一定|絕對(?!缺乏)|百分之百|guarantee|always|never", re.IGNORECASE)
    _diagnosis_pattern = re.compile(r"你(?:就是|是)\S{0,12}(?:糖尿病|確診)|(?:確診|診斷)為", re.IGNORECASE)

    def verify(
        self,
        candidate: CandidateResponse,
        evidence_set: EvidenceSet,
        policy: PolicySnapshot,
    ) -> SemanticVerificationResult:
        by_id = {record.evidence_id: record for record in evidence_set.evidence}
        failed: list[ClaimFailure] = []
        reasons: list[str] = []
        for claim in candidate.supported_claims:
            texts = [by_id[evidence_id].content for evidence_id in claim.evidence_ids if evidence_id in by_id]
            if not texts:
                continue
            if self._is_supported(claim.claim, texts):
                continue
            failed.append(
                ClaimFailure(
                    claim_id=claim.claim_id,
                    claim=claim.claim,
                    status="UNSUPPORTED",
                    reason="demo lexical verifier found insufficient overlap",
                    evidence_ids=claim.evidence_ids,
                )
            )
            reasons.append("CLAIM_NOT_SUPPORTED_BY_EVIDENCE")

        answer_text = candidate.answer
        overclaim_match = self._overclaim_pattern.search(answer_text)
        if overclaim_match:
            matched_text = overclaim_match.group(0)
            evidence_texts_all = " ".join(record.content for record in evidence_set.evidence)
            if matched_text not in evidence_texts_all:
                reasons.append("SEMANTIC_OVERCONFIDENCE")
        if self._diagnosis_pattern.search(answer_text):
            reasons.append("SEMANTIC_PERSONALIZED_DIAGNOSIS")
        return SemanticVerificationResult(failed_claims=failed, reason_codes=list(dict.fromkeys(reasons)))

    def _is_supported(self, claim: str, evidence_texts: list[str]) -> bool:
        claim_tokens = set(self._token_pattern.findall(claim.lower()))
        if not claim_tokens:
            return False
        evidence_tokens = set()
        for text in evidence_texts:
            evidence_tokens.update(self._token_pattern.findall(text.lower()))
        overlap = len(claim_tokens & evidence_tokens) / len(claim_tokens)
        # This deliberately simple heuristic is a demo only. It is not a
        # claim entailment score and has no clinical validity claim.
        return overlap >= 0.85 or any(claim.strip() in text for text in evidence_texts)


class MappingSemanticVerifier:
    """Small deterministic test double for semantic verifier integration tests."""

    name = "mapping-test-double"

    def __init__(
        self,
        statuses: Mapping[str, str] | None = None,
        *,
        reason_codes: Mapping[str, str] | None = None,
        fail_reason: str | None = None,
    ) -> None:
        self.statuses = dict(statuses or {})
        self.reason_codes = dict(reason_codes or {})
        self.fail_reason = fail_reason

    def verify(
        self,
        candidate: CandidateResponse,
        evidence_set: EvidenceSet,
        policy: PolicySnapshot,
    ) -> SemanticVerificationResult:
        if self.fail_reason:
            raise RuntimeError(self.fail_reason)
        failures = []
        reasons = []
        for claim in candidate.supported_claims:
            status = self.statuses.get(claim.claim_id, "SUPPORTED")
            if status == "SUPPORTED":
                continue
            failures.append(
                ClaimFailure(
                    claim_id=claim.claim_id,
                    claim=claim.claim,
                    status=status,
                    reason=self.reason_codes.get(claim.claim_id, "test double result"),
                    evidence_ids=claim.evidence_ids,
                )
            )
            reasons.append(self.reason_codes.get(claim.claim_id, f"CLAIM_{status}"))
        return SemanticVerificationResult(failed_claims=failures, reason_codes=reasons)
