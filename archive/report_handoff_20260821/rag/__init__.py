"""RAG adapter boundary for the deterministic baseline workflow."""

from .retriever import FixtureRetriever, Retriever, adapt_legacy_retrieval
from .tfda_retriever import TFDADrugSafetyRetriever, TFDADatasetError, load_tfda_rows
from .schemas import RAG_SCHEMA_VERSION, RAGResult

__all__ = [
    "FixtureRetriever",
    "TFDADrugSafetyRetriever",
    "TFDADatasetError",
    "load_tfda_rows",
    "RAGResult",
    "RAG_SCHEMA_VERSION",
    "Retriever",
    "adapt_legacy_retrieval",
]
