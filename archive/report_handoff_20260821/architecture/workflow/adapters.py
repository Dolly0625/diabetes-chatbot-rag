from __future__ import annotations

from typing import Any

from tfda_context_gate.a_router.schemas import AResult
from tfda_context_gate.b_context_gate.schemas import CanonicalBResult
from tfda_context_gate.c_generator.schemas import EvidenceAwareV2Answer
from tfda_context_gate.c_generator.workflow_adapter import CWorkflowInput, c_input_from_b_result
from tfda_context_gate.d_output_gate.gate import DEFAULT_FALLBACK
from tfda_context_gate.query_expansion.adapters import from_a_result
from tfda_context_gate.query_expansion.schemas import QueryExpansionInput
from tfda_context_gate.rag.schemas import RAGResult, rag_to_b_input


def a_to_query_expansion(a_result: AResult) -> QueryExpansionInput:
    return from_a_result(a_result)


def rag_to_b(rag_result: RAGResult):
    return rag_to_b_input(rag_result)


def b_to_c(b_result: CanonicalBResult, *, original_query: str) -> CWorkflowInput:
    return c_input_from_b_result(b_result, original_query=original_query)


def c_to_d(
    *,
    request_id: str,
    a_result: AResult,
    b_result: CanonicalBResult,
    c_result: EvidenceAwareV2Answer,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "schema_version": "d.v0.1",
        "a_result": a_result.model_dump(mode="json"),
        "b_result": b_result.model_dump(mode="json"),
        "c_result": c_result.model_dump(mode="json"),
    }

