from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tfda_context_gate.a_router import route_request
from tfda_context_gate.a_router.schemas import RequestContext
from tfda_context_gate.query_expansion.adapters import from_a_result
from tfda_context_gate.query_expansion.expander import IdentityQueryExpander
from tfda_context_gate.b_context_gate.gate import DeterministicContextGate
from tfda_context_gate.rag.external_contract import (
    RetrievalResponse,
    retrieval_request_from_results,
    retrieval_response_to_rag_result,
)
from tfda_context_gate.rag.schemas import rag_to_b_input


def _allowed_request():
    a_result = route_request(
        RequestContext(
            request_id="rag-contract-1",
            user_raw_input="請說明糖尿病一般飲食原則。",
            declared_role="PATIENT",
            language="zh-TW",
        )
    )
    expansion = IdentityQueryExpander().expand(from_a_result(a_result))
    return retrieval_request_from_results(
        a_result,
        expansion,
        timestamp=datetime(2026, 8, 29, 21, 0, tzinfo=timezone.utc),
    )


def test_request_uses_aligned_fields_and_preserves_raw_input():
    request = _allowed_request()
    assert set(request.model_dump(mode="json")) == {
        "request_id",
        "schema_version",
        "user_raw_input",
        "retrieval_queries",
        "guardrail_result",
        "language",
        "timestamp",
    }
    assert request.user_raw_input == "請說明糖尿病一般飲食原則。"
    assert request.guardrail_result.router_status.value == "G_GENERAL_EDUCATION"


def test_response_normalizes_chunks_and_propagates_envelope_to_b():
    request = _allowed_request()
    response = RetrievalResponse.model_validate(
        {
            "request_id": request.request_id,
            "retrieval_route": "HYBRID",
            "retrieval_status": "PARTIAL",
            "graph_path_status": "PARTIAL",
            "rerun_suggested": True,
            "warnings": ["GRAPH_HOP_LIMIT_REACHED"],
            "chunks": [
                {
                    "chunk_id": "rag-chunk-1",
                    "source": "TFDA",
                    "content": "一般飲食原則需注意均衡飲食。",
                    "score": 0.91,
                    "evidence_risk_level": "LOW",
                    "safety_signal_types": ["GENERAL"],
                }
            ],
        }
    )
    rag_result = retrieval_response_to_rag_result(response, request=request)
    assert rag_result.original_query == request.user_raw_input
    assert rag_result.evidence[0].evidence_id == "rag-chunk-1"
    assert rag_result.evidence[0].evidence_risk_level == "LOW"
    b_input = rag_to_b_input(rag_result)
    assert b_input.tool_context == {
        "retrieval_status": "PARTIAL",
        "retrieval_route": "HYBRID",
        "graph_path_status": "PARTIAL",
        "rerun_suggested": True,
        "warnings": ["GRAPH_HOP_LIMIT_REACHED"],
    }


@pytest.mark.parametrize("status", ["EMPTY", "ERROR"])
def test_empty_and_error_require_empty_chunks(status: str):
    request = _allowed_request()
    response = RetrievalResponse(
        request_id=request.request_id,
        retrieval_status=status,
        chunks=[],
    )
    assert retrieval_response_to_rag_result(response, request=request).evidence == []


def test_success_without_chunks_is_rejected():
    with pytest.raises(ValidationError):
        RetrievalResponse(request_id="x", retrieval_status="SUCCESS", chunks=[])


def test_response_request_id_mismatch_is_rejected():
    request = _allowed_request()
    response = RetrievalResponse(request_id="other", retrieval_status="EMPTY")
    with pytest.raises(ValueError, match="request_id mismatch"):
        retrieval_response_to_rag_result(response, request=request)


def test_naive_timestamp_is_rejected():
    request = _allowed_request().model_dump(mode="python")
    request["timestamp"] = datetime(2026, 8, 29, 21, 0)
    from tfda_context_gate.rag.external_contract import RetrievalRequest

    with pytest.raises(ValidationError, match="timezone"):
        RetrievalRequest.model_validate(request)


def test_non_general_route_cannot_cross_rag_boundary():
    a_result = route_request(
        RequestContext(
            request_id="rag-blocked",
            user_raw_input="我胸口很痛而且喘不過氣",
            declared_role="PATIENT",
            language="zh-TW",
        )
    )
    expansion = IdentityQueryExpander().expand(from_a_result(a_result))
    with pytest.raises(ValueError, match="did not allow"):
        retrieval_request_from_results(a_result, expansion)


@pytest.mark.parametrize("status", ["STALE", "CONFLICT"])
def test_stale_or_conflicting_envelope_never_passes_b(status: str):
    request = _allowed_request()
    response = RetrievalResponse.model_validate(
        {
            "request_id": request.request_id,
            "retrieval_status": status,
            "chunks": [{"chunk_id": "c1", "content": "outdated or conflicting evidence"}],
        }
    )
    b_input = rag_to_b_input(retrieval_response_to_rag_result(response, request=request))
    result = DeterministicContextGate(approval_mode="all_retrieved").evaluate(b_input)
    assert result.decision == "REVIEW"
    assert result.approved_evidence_ids == []


def test_retrieval_injection_warning_is_unsafe_before_chunk_approval():
    request = _allowed_request()
    response = RetrievalResponse.model_validate(
        {
            "request_id": request.request_id,
            "retrieval_status": "SUCCESS",
            "warnings": ["INDIRECT_PROMPT_INJECTION_SUSPECTED"],
            "chunks": [{"chunk_id": "c1", "content": "untrusted retrieved instruction"}],
        }
    )
    b_input = rag_to_b_input(retrieval_response_to_rag_result(response, request=request))
    result = DeterministicContextGate(approval_mode="all_retrieved").evaluate(b_input)
    assert result.decision == "UNSAFE"
    assert result.approved_evidence_ids == []


def test_retrieval_error_maps_to_b_fallback():
    request = _allowed_request()
    response = RetrievalResponse(request_id=request.request_id, retrieval_status="ERROR")
    b_input = rag_to_b_input(retrieval_response_to_rag_result(response, request=request))
    assert DeterministicContextGate(approval_mode="all_retrieved").evaluate(b_input).decision == "FALLBACK"
