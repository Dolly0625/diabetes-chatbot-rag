from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .adapters import (
    build_gate_request,
    parse_candidate_response,
    parse_evidence_set,
    parse_policy,
    validation_error_text,
)
from .policy import PolicyRuleConfig, check_candidate_red_lines, check_policy_snapshot
from .schemas import (
    CandidateResponse,
    ClaimFailure,
    EvidenceSet,
    OutputGateRequest,
    OutputGateResult,
    PolicySnapshot,
)
from .verifier import HeuristicSemanticVerifier, SemanticVerifier, SemanticVerificationResult


DEFAULT_FALLBACK = "目前無法驗證這份回答是否有足夠依據，因此無法提供可靠回覆；請改由合格醫療專業人員評估。"


def _result(
    request: OutputGateRequest,
    *,
    decision: str,
    failure_type: str,
    reason_codes: list[str] | tuple[str, ...] = (),
    failed_claims: list[ClaimFailure] | None = None,
    invalid_evidence_ids: list[str] | None = None,
    final_response: str = DEFAULT_FALLBACK,
    candidate_decision: str | None = None,
    verifier: str | None = None,
) -> OutputGateResult:
    return OutputGateResult(
        request_id=request.request_id,
        schema_version=request.schema_version,
        decision=decision,
        passed=decision == "PASS",
        failure_type=failure_type,
        reason_codes=list(dict.fromkeys(reason_codes)),
        failed_claims=failed_claims or [],
        invalid_evidence_ids=invalid_evidence_ids or [],
        final_response=final_response,
        candidate_decision=candidate_decision,
        verifier=verifier,
    )


def _validate_candidate_shape(candidate: CandidateResponse) -> list[str]:
    reasons: list[str] = []
    if candidate.decision == "ANSWER" and not candidate.supported_claims:
        reasons.append("C_ANSWER_HAS_NO_SUPPORTED_CLAIMS")
    if candidate.decision == "PARTIAL" and not (
        candidate.supported_claims or candidate.unsupported_requests
    ):
        reasons.append("C_PARTIAL_HAS_NO_SUPPORTED_OR_UNSUPPORTED_FIELDS")
    if candidate.decision == "INSUFFICIENT" and candidate.supported_claims:
        reasons.append("C_INSUFFICIENT_HAS_SUPPORTED_CLAIMS")
    for claim in candidate.supported_claims:
        if not claim.evidence_ids:
            reasons.append("CLAIM_WITHOUT_EVIDENCE_ID")
    return list(dict.fromkeys(reasons))


def _validate_evidence_ids(candidate: CandidateResponse, evidence_set: EvidenceSet) -> tuple[list[str], list[str], list[str]]:
    evidence_ids = {record.evidence_id for record in evidence_set.evidence}
    approved_ids = set(evidence_set.approved_evidence_ids)
    referenced = {
        evidence_id
        for claim in candidate.supported_claims
        for evidence_id in claim.evidence_ids
    }
    missing = sorted(referenced - evidence_ids)
    not_approved = sorted((referenced & evidence_ids) - approved_ids)
    malformed_approved = sorted(approved_ids - evidence_ids)
    return missing, not_approved, malformed_approved


def _semantic_failure(
    result: SemanticVerificationResult,
) -> tuple[list[ClaimFailure], list[str]]:
    failures = list(result.failed_claims) + list(result.unsupported_answer_claims)
    reasons = list(result.reason_codes)
    if result.failed_claims:
        reasons.append("CLAIM_SEMANTIC_VERIFICATION_FAILED")
    if result.unsupported_answer_claims:
        reasons.append("ANSWER_HAS_UNSUPPORTED_FACTUAL_CLAIMS")
    return failures, list(dict.fromkeys(reasons))


def run_output_gate(
    payload: dict[str, Any] | OutputGateRequest,
    *,
    verifier: SemanticVerifier | None = None,
    policy_rules: PolicyRuleConfig | None = None,
    fallback_response: str = DEFAULT_FALLBACK,
) -> OutputGateResult:
    """Run D v0.1 in deterministic-first order and fail closed."""

    try:
        request = build_gate_request(payload)
    except Exception as exc:
        # We cannot safely recover tracing fields from a malformed payload.
        request = OutputGateRequest(
            request_id=str(payload.get("request_id", "unknown")) if isinstance(payload, dict) else "unknown",
            schema_version=str(payload.get("schema_version", "d.v0.1")) if isinstance(payload, dict) else "d.v0.1",
            policy={},
            evidence_set={},
            candidate_response=None,
        )
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["D_INPUT_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)

    try:
        policy = parse_policy(request.policy)
    except (ValidationError, TypeError, ValueError) as exc:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["A_POLICY_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)

    try:
        evidence_set = parse_evidence_set(request.evidence_set)
    except (ValidationError, TypeError, ValueError) as exc:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["B_EVIDENCE_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)

    try:
        candidate = parse_candidate_response(request.candidate_response)
    except (ValidationError, TypeError, ValueError) as exc:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["C_OUTPUT_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)

    candidate_reasons = _validate_candidate_shape(candidate)
    if candidate_reasons:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=candidate_reasons, final_response=fallback_response, candidate_decision=getattr(candidate, "decision", None))

    if evidence_set.decision != "PASS":
        return _result(request, decision="FALLBACK", failure_type="EVIDENCE", reason_codes=["B_EVIDENCE_SET_NOT_APPROVED"], final_response=fallback_response, candidate_decision=candidate.decision)

    missing, not_approved, malformed_approved = _validate_evidence_ids(candidate, evidence_set)
    invalid_ids = sorted(set(missing + not_approved + malformed_approved))
    evidence_reasons: list[str] = []
    if missing:
        evidence_reasons.append("EVIDENCE_ID_NOT_FOUND")
    if not_approved:
        evidence_reasons.append("EVIDENCE_ID_NOT_APPROVED_BY_B")
    if malformed_approved:
        evidence_reasons.append("B_APPROVED_EVIDENCE_MISSING_RECORD")
    if evidence_reasons:
        return _result(request, decision="FALLBACK", failure_type="EVIDENCE", reason_codes=evidence_reasons, invalid_evidence_ids=invalid_ids, final_response=fallback_response, candidate_decision=candidate.decision)

    policy_check = check_policy_snapshot(policy)
    if policy_check.failed:
        return _result(request, decision="FALLBACK", failure_type="POLICY", reason_codes=policy_check.reason_codes, final_response=fallback_response, candidate_decision=candidate.decision)

    redline_check = check_candidate_red_lines(candidate, policy_rules or PolicyRuleConfig())
    if redline_check.failed:
        return _result(request, decision="FALLBACK", failure_type="POLICY", reason_codes=redline_check.reason_codes, final_response=fallback_response, candidate_decision=candidate.decision)

    if not candidate.supported_claims:
        return _result(request, decision="PASS", failure_type="NONE", reason_codes=["D_SAFE_ABSTENTION_ACCEPTED"], final_response=candidate.answer, candidate_decision=candidate.decision, verifier=None)

    active_verifier = verifier or HeuristicSemanticVerifier()
    try:
        semantic = active_verifier.verify(candidate, evidence_set, policy)
        if not isinstance(semantic, SemanticVerificationResult):
            raise TypeError("semantic verifier returned an invalid result")
    except Exception as exc:
        return _result(request, decision="FALLBACK", failure_type="DEPENDENCY", reason_codes=["VERIFIER_DEPENDENCY_FAILURE", type(exc).__name__], final_response=fallback_response, candidate_decision=candidate.decision, verifier=getattr(active_verifier, "name", type(active_verifier).__name__))

    failed_claims, semantic_reasons = _semantic_failure(semantic)
    if semantic_reasons:
        return _result(request, decision="FALLBACK", failure_type="SEMANTIC", reason_codes=semantic_reasons, failed_claims=failed_claims, final_response=fallback_response, candidate_decision=candidate.decision, verifier=getattr(active_verifier, "name", type(active_verifier).__name__))

    return _result(request, decision="PASS", failure_type="NONE", reason_codes=["OUTPUT_GATE_PASSED"], final_response=candidate.answer, candidate_decision=candidate.decision, verifier=getattr(active_verifier, "name", type(active_verifier).__name__))
