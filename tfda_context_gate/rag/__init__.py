"""RAG adapter boundary for the deterministic baseline workflow."""

from .hpa_retriever import HPADietRetriever, MultiSourceRetriever, build_hpa_caches, load_hpa_rows
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
    "Retriever",
    "adapt_legacy_retrieval",
]
