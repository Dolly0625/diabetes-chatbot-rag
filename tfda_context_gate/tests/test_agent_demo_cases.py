from __future__ import annotations

import pytest

from tfda_context_gate.a_router.router import route_request
from tfda_context_gate.a_router.guard import GuardCategory, GuardSafety, PromptInjectionGuardResult
from tfda_context_gate.agent.agent_demo_case_schema import load_agent_demo_cases
from tfda_context_gate.b_context_gate.gate import DeterministicContextGate
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult
from tfda_context_gate.rag import TFDADrugSafetyRetriever, load_tfda_rows
from tfda_context_gate.rag.schemas import rag_to_b_input
from tfda_context_gate.workflow import run_workflow


CASES = load_agent_demo_cases()
BY_ID = {case.case_id: case for case in CASES}


def make_request(case):
    return {
        "request_id": case.case_id,
        "schema_version": "a.v0.1",
        "user_raw_input": case.user_query,
        "declared_role": case.role,
        "language": "zh-TW",
    }


def test_agent_demo_case_schema_contains_three_core_cases_and_two_regressions():
    assert {case.case_id for case in CASES} == {
        "AG-ASK-001",
        "AG-REWRITE-001",
        "AG-FALLBACK-001",
        "PI-1",
        "PI-2",
    }
    assert BY_ID["AG-ASK-001"].expected_agent_action == "ASK_USER"
    assert BY_ID["AG-REWRITE-001"].expected_agent_action == "REWRITE_QUERY"
    assert BY_ID["AG-FALLBACK-001"].expected_agent_action == "FALLBACK"


def test_expected_evidence_ids_exist_in_real_tfda_corpus():
    ids = {str(row.get("id") or row["metadata"]["document_id"]) for row in load_tfda_rows()}
    assert BY_ID["AG-ASK-001"].expected_evidence_id in ids
    assert BY_ID["AG-REWRITE-001"].expected_evidence_id in ids
    assert BY_ID["AG-FALLBACK-001"].expected_evidence_id is None


def test_agent_ground_truth_does_not_assign_sglt2_to_ask_user_case():
    case = BY_ID["AG-ASK-001"]
    assert "SGLT2" not in case.user_query
    assert case.simulated_user_reply == "SGLT2 抑制劑"
    assert case.expected_agent_action == "ASK_USER"


def test_rewrite_ground_truth_is_rank_improving_and_meaning_preserving():
    case = BY_ID["AG-REWRITE-001"]
    assert case.rewritten_query is not None
    assert "疼痛" not in case.rewritten_query
    assert "紅腫" not in case.rewritten_query
    validation = case.model_extra["retrieval_validation"]
    assert validation["original"]["expected_evidence_rank"] == 2
    assert validation["rewritten"]["expected_evidence_rank"] == 1
    assert validation["rewritten"]["expected_evidence_score"] > validation["original"]["expected_evidence_score"]


def test_fallback_ground_truth_has_no_semaglutide_evidence():
    case = BY_ID["AG-FALLBACK-001"]
    assert case.expected_a_route == "G_GENERAL_EDUCATION"
    assert case.expected_evidence_id is None
    validation = case.model_extra["retrieval_validation"]
    assert validation["recovery"]["semaglutide_present_in_corpus"] is False


def test_prompt_injection_regression_never_reaches_agent_or_rag():
    class ExistingQwen3GuardBlockedResult:
        """Test double for the existing Qwen3Guard adapter boundary."""

        def check(self, _raw_input):
            return PromptInjectionGuardResult(
                blocked=True,
                safety=GuardSafety.UNSAFE,
                categories=(GuardCategory.JAILBREAK,),
            )

    for case_id in ("PI-1", "PI-2"):
        case = BY_ID[case_id]
        result = run_workflow(make_request(case), prompt_injection_guard=ExistingQwen3GuardBlockedResult())
        assert result.status == "BLOCKED"
        assert result.fallback_reason in ("A_BLOCKED", "R_GUARDRAIL_BLOCKED")
        assert result.rag_result is None
        assert result.a_result["router_status"] == "R_POLICY_BOUNDARY"
        assert "REASON_PROMPT_INJECTION_SUSPECTED" in result.a_result["reason_codes"]
        assert not any(event["component"] == "RAG" for event in result.trace["events"])
        assert all(event.get("agent_action") is None for event in result.trace["events"])
        assert all(event.get("actions_taken") == [] for event in result.trace["events"])


@pytest.fixture(scope="module")
def real_retriever() -> TFDADrugSafetyRetriever:
    pytest.importorskip("langchain_huggingface")
    pytest.importorskip("sentence_transformers")
    return TFDADrugSafetyRetriever(top_k=5)


def retrieve(retriever, case_id: str, query: str):
    return retriever.retrieve(
        QueryExpansionResult(
            request_id=case_id,
            original_query=query,
            retrieval_queries=[query],
            strategy="identity-deterministic",
        )
    )


def test_ask_user_real_retrieval_improves_after_sglt2_clarification(real_retriever):
    case = BY_ID["AG-ASK-001"]
    initial = retrieve(real_retriever, case.case_id, case.user_query)
    clarified_query = case.model_extra["clarified_query"]
    clarified = retrieve(real_retriever, case.case_id, clarified_query)
    assert case.expected_evidence_id in [item.evidence_id for item in initial.evidence]
    assert clarified.evidence[0].evidence_id == case.expected_evidence_id
    assert clarified.evidence[0].metadata["藥品成分"] == "SGLT2抑制劑類"
    assert DeterministicContextGate().evaluate(rag_to_b_input(initial)).decision == "INSUFFICIENT"


def test_rewrite_real_retrieval_improves_rank(real_retriever):
    case = BY_ID["AG-REWRITE-001"]
    initial = retrieve(real_retriever, case.case_id, case.user_query)
    rewritten = retrieve(real_retriever, case.case_id, case.rewritten_query)
    initial_ids = [item.evidence_id for item in initial.evidence]
    rewritten_ids = [item.evidence_id for item in rewritten.evidence]
    assert initial_ids.index(case.expected_evidence_id) == 1
    assert rewritten_ids.index(case.expected_evidence_id) == 0
    initial_score = initial.evidence[initial_ids.index(case.expected_evidence_id)].score
    rewritten_score = rewritten.evidence[rewritten_ids.index(case.expected_evidence_id)].score
    assert rewritten_score > initial_score


def test_fallback_real_retrieval_stays_without_expected_evidence(real_retriever):
    case = BY_ID["AG-FALLBACK-001"]
    initial = retrieve(real_retriever, case.case_id, case.user_query)
    recovery_query = case.recovery_attempts[0]["query"]
    recovery = retrieve(real_retriever, case.case_id, recovery_query)
    assert all("Semaglutide" not in item.metadata.get("藥品成分", "") for item in initial.evidence)
    assert all("Semaglutide" not in item.metadata.get("藥品成分", "") for item in recovery.evidence)
    assert DeterministicContextGate().evaluate(rag_to_b_input(initial)).decision == "INSUFFICIENT"
    assert DeterministicContextGate().evaluate(rag_to_b_input(recovery)).decision == "INSUFFICIENT"
