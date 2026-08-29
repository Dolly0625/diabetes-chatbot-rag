"""RAG adapter boundary for the deterministic baseline workflow."""

from .hpa_retriever import HPADietRetriever, MultiSourceRetriever, build_hpa_caches, load_hpa_rows
from .external_contract import (
    RetrievedChunk,
    RetrievalGuardrailResult,
    RetrievalRequest,
    RetrievalResponse,
    retrieval_request_from_results,
    retrieval_response_to_rag_result,
)
from .retriever import FixtureRetriever, Retriever, adapt_legacy_retrieval
from .schemas import RAG_SCHEMA_VERSION, RAGResult
from .tfda_retriever import TFDADrugSafetyRetriever, TFDADatasetError, load_tfda_rows

__all__ = [
    "FixtureRetriever",
    "HPADietRetriever",
    "MultiSourceRetriever",
    "TFDADrugSafetyRetriever",
    "TFDADatasetError",
    "build_hpa_caches",
    "load_hpa_rows",
    "load_tfda_rows",
    "RAGResult",
    "RAG_SCHEMA_VERSION",
    "RetrievedChunk",
    "RetrievalGuardrailResult",
    "RetrievalRequest",
    "RetrievalResponse",
    "Retriever",
    "adapt_legacy_retrieval",
    "retrieval_request_from_results",
    "retrieval_response_to_rag_result",
]
