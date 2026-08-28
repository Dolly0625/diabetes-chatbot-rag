from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult

from .schemas import RAGResult


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCUMENTS_PATH = PACKAGE_ROOT / "data" / "processed" / "langchain_documents.json"
USER_DOCUMENTS_PATH = Path("/mnt/data/langchain_documents.json")
DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"


class TFDADatasetError(ValueError):
    """Raised when the processed TFDA corpus cannot satisfy the RAG contract."""


def resolve_documents_path(path: str | Path | None = None) -> Path:
    """Resolve an explicit path, the user-provided /mnt/data path, or repo data."""

    candidates = []
    if path is not None:
        candidates.append(Path(path).expanduser())
    env_path = os.getenv("TFDA_DOCUMENTS_PATH")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend([USER_DOCUMENTS_PATH, DEFAULT_DOCUMENTS_PATH])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    searched = ", ".join(str(item) for item in candidates)
    raise FileNotFoundError(f"TFDA processed corpus not found; searched: {searched}")


def load_tfda_rows(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load and validate one processed JSON row per TFDA risk communication record."""

    resolved = resolve_documents_path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TFDADatasetError(f"invalid JSON corpus: {resolved}") from exc
    if not isinstance(payload, list):
        raise TFDADatasetError("TFDA corpus must be a top-level JSON list")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise TFDADatasetError(f"corpus row {index} is not an object")
        metadata = item.get("metadata")
        evidence_id = item.get("id") or (
            metadata.get("document_id") if isinstance(metadata, dict) else None
        )
        content = item.get("page_content")
        if not evidence_id:
            raise TFDADatasetError(f"corpus row {index} has no id/document_id")
        if not isinstance(content, str) or not content.strip():
            raise TFDADatasetError(f"corpus row {index} has empty page_content")
        if not isinstance(metadata, dict):
            raise TFDADatasetError(f"corpus row {index} has invalid metadata")
        evidence_id = str(evidence_id)
        if evidence_id in seen_ids:
            raise TFDADatasetError(f"duplicate corpus evidence id: {evidence_id}")
        seen_ids.add(evidence_id)
        rows.append(item)
    return rows


class TFDADrugSafetyRetriever:
    """Minimal real vector RAG retriever over the processed TFDA corpus.

    The index is built lazily on the first retrieval call. Each processed row is
    kept as one LangChain ``Document``; no chunking or synthetic evidence is
    introduced. The retriever is deliberately injectable so unit tests can use
    ``FixtureRetriever`` without rebuilding this index.
    """

    name = "tfda-huggingface-inmemory-vector-retriever"

    def __init__(
        self,
        *,
        documents_path: str | Path | None = None,
        top_k: int = 5,
        embedding_model: str | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        self.documents_path = resolve_documents_path(documents_path)
        self.top_k = top_k
        self.embedding_model = embedding_model or os.getenv("EMBED_MODEL", DEFAULT_EMBEDDING_MODEL)
        self._store: Any | None = None
        self._rows_by_id: dict[str, dict[str, Any]] = {}

    def _ensure_store(self) -> Any:
        if self._store is not None:
            return self._store
        try:
            from langchain_core.documents import Document
            from langchain_core.vectorstores import InMemoryVectorStore
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError as exc:
            raise RuntimeError(
                "real TFDA retrieval requires langchain-huggingface and "
                "sentence-transformers; install tfda_context_gate/requirements.txt"
            ) from exc

        rows = load_tfda_rows(self.documents_path)
        documents = []
        for row in rows:
            metadata = dict(row["metadata"])
            evidence_id = str(row.get("id") or metadata["document_id"])
            metadata.setdefault("document_id", evidence_id)
            documents.append(
                Document(
                    id=evidence_id,
                    page_content=row["page_content"],
                    metadata=metadata,
                )
            )
            self._rows_by_id[evidence_id] = row

        embedding_kwargs = {
            "encode_kwargs": {"normalize_embeddings": True, "prompt": "passage: "},
            "query_encode_kwargs": {"normalize_embeddings": True, "prompt": "query: "},
        }
        try:
            embeddings = HuggingFaceEmbeddings(model=self.embedding_model, **embedding_kwargs)
        except TypeError:
            # Compatibility with older langchain-huggingface releases.
            embeddings = HuggingFaceEmbeddings(model_name=self.embedding_model, **embedding_kwargs)
        store = InMemoryVectorStore(embedding=embeddings)
        store.add_documents(documents)
        self._store = store
        return store

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        started = time.perf_counter()
        store = self._ensure_store()
        ranked: dict[str, tuple[Any, float]] = {}
        for query in request.retrieval_queries:
            for document, score in store.similarity_search_with_score(query, k=self.top_k):
                evidence_id = str(document.metadata.get("document_id") or document.id)
                numeric_score = float(score)
                previous = ranked.get(evidence_id)
                if previous is None or numeric_score > previous[1]:
                    ranked[evidence_id] = (document, numeric_score)

        results = sorted(ranked.values(), key=lambda item: item[1], reverse=True)[: self.top_k]
        evidence = []
        for document, score in results:
            metadata = dict(document.metadata)
            evidence.append(
                CanonicalEvidence(
                    evidence_id=str(document.metadata.get("document_id") or document.id),
                    content=document.page_content,
                    source=str(metadata.get("source_dataset")) if metadata.get("source_dataset") else None,
                    metadata=metadata,
                    score=score,
                    date=str(metadata.get("發布日期")) if metadata.get("發布日期") else None,
                    version=str(metadata.get("version")) if metadata.get("version") else None,
                )
            )
        return RAGResult(
            request_id=request.request_id,
            original_query=request.original_query,
            retrieval_queries=request.retrieval_queries,
            evidence=evidence,
            retrieval_latency_ms=(time.perf_counter() - started) * 1000,
        )
