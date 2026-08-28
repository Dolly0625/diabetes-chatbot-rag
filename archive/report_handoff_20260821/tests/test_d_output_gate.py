from __future__ import annotations

from tfda_context_gate.d_output_gate.gate import run_output_gate
from tfda_context_gate.d_output_gate.verifier import MappingSemanticVerifier


def evidence_payload(*, approved=None, records=None):
    return {
        "b_decision": "PASS",
        "approved_document_ids": approved or ["e1"],
        "contexts": records
        or [{"document_id": "e1", "page_content": "SGLT2 抑制劑可能導致酮酸中毒。"}],
    }


def candidate(*, decision="ANSWER", evidence_ids=None, answer="SGLT2 抑制劑可能導致酮酸中毒。"):
    claim_evidence_ids = ["e1"] if evidence_ids is None else evidence_ids
    return {
        "decision": decision,
        "answer": answer,
        "supported_claims": (
            [{"claim_id": "c1", "claim": "SGLT2 抑制劑可能導致酮酸中毒。", "evidence_ids": claim_evidence_ids}]
            if decision != "INSUFFICIENT"
            else []
        ),
        "unsupported_requests": [],
        "limitations": [],
    }


def payload(c=None, *, b=None, policy=None):
    return {
        "request_id": "d-test-001",
        "schema_version": "d.v0.1",
        "a_result": policy
        or {"router_status": "G_GENERAL_EDUCATION", "rag_allowed": True, "risk_flags": [], "intent_tags": []},
        "b_result": b or evidence_payload(),
        "c_result": c or candidate(),
    }


def test_evidence_aware_answer_passes():
    result = run_output_gate(payload())
    assert result.decision == "PASS"
    assert result.passed is True
    assert result.final_response == "SGLT2 抑制劑可能導致酮酸中毒。"


def test_missing_evidence_id_fails():
    result = run_output_gate(payload(candidate(evidence_ids=["missing"])))
    assert result.decision == "FALLBACK"
    assert "EVIDENCE_ID_NOT_FOUND" in result.reason_codes
    assert result.invalid_evidence_ids == ["missing"]


def test_evidence_id_outside_b_approved_set_fails():
    b = evidence_payload(
        approved=["e1"],
        records=[
            {"document_id": "e1", "page_content": "approved context"},
            {"document_id": "e2", "page_content": "unapproved context"},
        ],
    )
    result = run_output_gate(payload(candidate(evidence_ids=["e2"]), b=b))
    assert result.decision == "FALLBACK"
    assert "EVIDENCE_ID_NOT_APPROVED_BY_B" in result.reason_codes
    assert result.invalid_evidence_ids == ["e2"]


def test_claim_without_evidence_cannot_pass():
    result = run_output_gate(payload(candidate(evidence_ids=[])))
    assert result.decision == "FALLBACK"
    assert "CLAIM_WITHOUT_EVIDENCE_ID" in result.reason_codes


def test_malformed_c_schema_fails():
    malformed = {"decision": "NOT_A_DECISION", "answer": "bad", "supported_claims": []}
    result = run_output_gate(payload(malformed))
    assert result.decision == "FALLBACK"
    assert result.failure_type == "SCHEMA"
    assert "C_OUTPUT_SCHEMA_INVALID" in result.reason_codes


def test_a_policy_veto_cannot_be_bypassed_by_c():
    policy = {
        "router_status": "M_MEDICATION_REFERRAL",
        "rag_allowed": False,
        "risk_flags": ["PERSONALIZED_MEDICATION"],
        "intent_tags": ["MEDICATION_CHANGE_REQUEST"],
    }
    result = run_output_gate(
        payload(candidate(answer="你可以自行停藥。"), policy=policy)
    )
    assert result.decision == "FALLBACK"
    assert result.failure_type == "POLICY"
    assert "POLICY_ROUTE_NOT_GENERAL_EDUCATION" in result.reason_codes


def test_verifier_dependency_failure_uses_safe_fallback():
    result = run_output_gate(payload(), verifier=MappingSemanticVerifier(fail_reason="judge unavailable"))
    assert result.decision == "FALLBACK"
    assert result.failure_type == "DEPENDENCY"
    assert "VERIFIER_DEPENDENCY_FAILURE" in result.reason_codes
    assert result.final_response.startswith("目前無法驗證")


def test_partial_supported_and_unsupported_fields_are_handled():
    partial = {
        "decision": "PARTIAL",
        "answer": "文件支持酮酸中毒風險；未提供發生率。",
        "supported_claims": [
            {"claim_id": "c1", "claim": "SGLT2 抑制劑可能導致酮酸中毒。", "evidence_ids": ["e1"]}
        ],
        "unsupported_requests": [{"request": "精確發生率", "reason": "文件沒有提供"}],
        "limitations": [],
    }
    result = run_output_gate(payload(partial))
    assert result.decision == "PASS"
    assert result.candidate_decision == "PARTIAL"


def test_semantically_unsupported_claim_cannot_pass():
    result = run_output_gate(
        payload(),
        verifier=MappingSemanticVerifier(
            {"c1": "UNSUPPORTED"}, reason_codes={"c1": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE"}
        ),
    )
    assert result.decision == "FALLBACK"
    assert result.failure_type == "SEMANTIC"
    assert result.failed_claims[0].claim_id == "c1"


def test_semantic_overconfidence_cannot_pass():
    result = run_output_gate(
        payload(candidate(answer="SGLT2 抑制劑一定不會造成任何風險。"))
    )
    assert result.decision == "FALLBACK"
    assert result.failure_type == "SEMANTIC"
    assert "SEMANTIC_OVERCONFIDENCE" in result.reason_codes


def test_legacy_c_v1_claims_are_adapted_without_changing_c():
    legacy = {
        "decision": "ANSWER",
        "answer": "SGLT2 抑制劑可能導致酮酸中毒。",
        "claims": [{"claim_id": "c1", "claim": "SGLT2 抑制劑可能導致酮酸中毒。", "evidence_ids": ["e1"]}],
        "limitations": [],
    }
    result = run_output_gate(payload(legacy))
    assert result.decision == "PASS"
