from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult


RAG_SCHEMA_VERSION = "rag.v0.1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RAGResult(StrictModel):
    request_id: str = Field(min_length=1)
    schema_version: str = Field(default=RAG_SCHEMA_VERSION, min_length=1)
    original_query: str = Field(min_length=1)
    retrieval_queries: list[str] = Field(min_length=1)
    evidence: list[CanonicalEvidence] = Field(default_factory=list)
    retrieval_latency_ms: float | None = Field(default=None, ge=0)


def rag_to_b_input(rag_result: RAGResult):
    """Adapter kept here so the RAG package exposes no B implementation details."""

    from tfda_context_gate.b_context_gate.schemas import CanonicalBInput

    return CanonicalBInput(
        request_id=rag_result.request_id,
        original_query=rag_result.original_query,
        retrieval_queries=rag_result.retrieval_queries,
        evidence=rag_result.evidence,
    )

