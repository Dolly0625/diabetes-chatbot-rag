from __future__ import annotations

import pytest

from tfda_context_gate.query_expansion.schemas import QueryExpansionResult
from tfda_context_gate.rag import TFDADrugSafetyRetriever
from tfda_context_gate.rag.tfda_smoke_cases import TFDA_RETRIEVAL_CASES
from tfda_context_gate.a_router.router import route_request


@pytest.fixture(scope="module")
def retriever() -> TFDADrugSafetyRetriever:
    pytest.importorskip("langchain_huggingface")
    pytest.importorskip("sentence_transformers")
    return TFDADrugSafetyRetriever(top_k=5)


@pytest.mark.parametrize("case", TFDA_RETRIEVAL_CASES, ids=lambda case: case.case_id)
def test_real_tfda_vector_retrieval_returns_relevant_ingredient(
    retriever: TFDADrugSafetyRetriever,
    case,
):
    result = retriever.retrieve(
        QueryExpansionResult(
            request_id=f"tfda-smoke-{case.case_id.lower()}",
            original_query=case.query,
            retrieval_queries=[case.query],
            strategy="identity-deterministic",
        )
    )
    assert 1 <= len(result.evidence) <= 5
    assert case.matches(result.evidence)
    assert all(item.source == "TFDA 藥品安全資訊風險溝通資料" for item in result.evidence)
    assert all(item.date for item in result.evidence)


def test_p3_is_blocked_by_a_before_real_retrieval():
    result = route_request({
        "request_id": "tfda-p3",
        "schema_version": "a.v0.1",
        "user_raw_input": "我最近血糖比較穩，可以自己把糖尿病藥停掉嗎？",
        "declared_role": "PATIENT",
        "language": "zh-TW",
    })
    assert result.rag_allowed is False
    assert result.router_status.value == "M_MEDICATION_REFERRAL"


def test_c3_is_marked_as_future_ask_user_candidate_without_assuming_sglt2():
    from tfda_context_gate.rag.tfda_smoke_cases import TFDA_SMOKE_CASES_BY_ID

    case = TFDA_SMOKE_CASES_BY_ID["C3"]
    assert case.clarification_candidate is True
    assert case.expected_retrieval is False
    assert "SGLT2" not in case.query
