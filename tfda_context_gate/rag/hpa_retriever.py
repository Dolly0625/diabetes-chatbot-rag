"""HPA diet retriever with bge-m3 Ollama embedding and CACHE_DIR persistence.

Reuses tfda_retriever.py bge-m3 Ollama embedding logic with truncation and CACHE_DIR persistence.
Keeps separate cache keys per source_id: HPA_DIET_GUIDE / HPA_DIABETES_BOOK / FOOD_NUTRITION
Populates CanonicalEvidence 15 fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import time
from pathlib import Path
from typing import Any

from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult
from tfda_context_gate.rag.schemas import RAGResult

try:
    from tfda_context_gate.rag.tfda_retriever import CACHE_DIR, PACKAGE_ROOT
except ImportError:
    PACKAGE_ROOT = Path(__file__).resolve().parents[1]
    CACHE_DIR = PACKAGE_ROOT / "data" / "processed" / ".vector_cache"

# HPA documents paths
HPA_DOCUMENTS_PATH = PACKAGE_ROOT / "data" / "processed" / "hpa_documents.json"
HPA_RAW_DIR = PACKAGE_ROOT / "data" / "processed" / "hpa_raw"

# Source mapping for cache files
HPA_SOURCE_IDS = ["FOOD_NUTRITION", "HPA_DIET_GUIDE", "HPA_DIABETES_BOOK"]


def _hpa_cache_key(source_id: str, embedding_model: str, documents_path: Path) -> str:
    """Generate cache key per source_id, keeping separate keys."""
    stat = documents_path.stat() if documents_path.exists() else None
    raw = f"hpa:{source_id}:{documents_path}:{stat.st_mtime if stat else 0}:{stat.st_size if stat else 0}:{embedding_model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _hpa_cache_path(source_id: str, embedding_model: str, documents_path: Path) -> Path:
    """Cache path for HPA source: hpa_<source_id>_<hash>.pkl"""
    key = _hpa_cache_key(source_id, embedding_model, documents_path)
    # Use hpa_ prefix as required: hpa_*.pkl
    return CACHE_DIR / f"hpa_{source_id.lower()}_{key}.pkl"


def load_hpa_rows(path: Path | None = None, source_id: str | None = None) -> list[dict[str, Any]]:
    """Load HPA rows, optionally filtered by source_id."""
    if path is None:
        path = HPA_DOCUMENTS_PATH
    if not path.exists():
        # Try individual source files
        if source_id:
            alt_path = HPA_RAW_DIR / f"{source_id.lower()}_documents.json"
            if alt_path.exists():
                path = alt_path
            else:
                raise FileNotFoundError(f"HPA documents not found: {path} or {alt_path}")
        else:
            raise FileNotFoundError(f"HPA documents not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("HPA corpus must be a top-level JSON list")

    rows = []
    for item in payload:
        if source_id and item.get("metadata", {}).get("source_id") != source_id:
            continue
        rows.append(item)
    return rows


class HPADietRetriever:
    """HPA diet retriever with bge-m3 Ollama embedding, separate cache per source_id.

    Reuses tfda_retriever.py embedding logic:
    - Ollama bge-m3 with truncation (1200 chars)
    - CACHE_DIR persistence
    - Separate cache keys per source_id (hpa_*.pkl)
    - CanonicalEvidence 15 fields populated
    """

    name = "hpa-diet-bge-m3-retriever"

    def __init__(
        self,
        *,
        documents_path: str | Path | None = None,
        source_id: str | None = None,
        top_k: int = 5,
        embedding_model: str | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        self.documents_path = Path(documents_path) if documents_path else HPA_DOCUMENTS_PATH
        self.source_id = source_id  # If None, load all HPA sources
        self.top_k = top_k
        self.embedding_model = embedding_model or os.getenv("EMBED_MODEL", "ollama/bge-m3:latest")
        # Normalize embedding_model to ollama/bge-m3 if needed
        if self.embedding_model in ("bge-m3", "bge-m3:latest"):
            self.embedding_model = "ollama/bge-m3:latest"
        self._store: Any | None = None
        self._rows_by_id: dict[str, dict[str, Any]] = {}

    def _cache_path(self) -> Path:
        # Use source_id in cache path to keep separate keys
        sid = self.source_id or "all"
        return _hpa_cache_path(sid, self.embedding_model, self.documents_path)

    def _ensure_store(self) -> Any:
        """Ensure vector store is built, with CACHE_DIR persistence."""
        if self._store is not None:
            return self._store

        cache_path = self._cache_path()
        if cache_path.exists():
            try:
                with cache_path.open("rb") as f:
                    payload = pickle.load(f)
                    # Try new format (store_dict) first, fallback to old (store)
                    if "store_dict" in payload:
                        from langchain_core.vectorstores import InMemoryVectorStore

                        # Recreate embedding
                        ollama_model = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3:latest")
                        ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
                        try:
                            from langchain_ollama import OllamaEmbeddings

                            model_name = self.embedding_model.split("/", 1)[-1] if "/" in self.embedding_model else ollama_model
                            embeddings = OllamaEmbeddings(model=model_name, base_url=ollama_base)
                        except Exception:
                            embeddings = None
                        if embeddings is None:
                            from langchain_huggingface import HuggingFaceEmbeddings

                            embeddings = HuggingFaceEmbeddings(model_name=self.embedding_model)
                        store = InMemoryVectorStore(embedding=embeddings)
                        store.store = payload["store_dict"]
                        self._store = store
                        self._rows_by_id = payload.get("rows_by_id", {})
                        return self._store
                    else:
                        self._store = payload["store"]
                        self._rows_by_id = payload.get("rows_by_id", {})
                        return self._store
            except Exception:
                pass

        # Load rows
        try:
            rows = load_hpa_rows(self.documents_path, self.source_id)
        except FileNotFoundError:
            # Try to ingest if not exists
            from tfda_context_gate.rag.hpa_ingest import ingest_all_sources, save_combined_documents

            all_docs = ingest_all_sources()
            save_combined_documents(all_docs, self.documents_path)
            rows = load_hpa_rows(self.documents_path, self.source_id)

        if not rows:
            raise ValueError(f"No HPA documents found for source_id={self.source_id}")

        try:
            from langchain_core.documents import Document
        except ImportError as exc:
            raise RuntimeError("langchain-core required") from exc

        # Reuse tfda_retriever embedding logic with truncation
        embeddings = None
        ollama_model = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3:latest")
        ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        use_ollama = self.embedding_model.startswith("ollama/") or self.embedding_model.startswith("bge-") or ollama_model in ("bge-m3:latest", "bge-m3")
        if use_ollama or self.embedding_model == ollama_model:
            try:
                from langchain_ollama import OllamaEmbeddings

                model_name = self.embedding_model.split("/", 1)[-1] if "/" in self.embedding_model else ollama_model
                embeddings = OllamaEmbeddings(model=model_name, base_url=ollama_base)
                embeddings.embed_query("test")
            except Exception:
                embeddings = None

        max_embed_chars = 1200 if embeddings is not None and "bge" in str(getattr(embeddings, "model", "")).lower() else None

        documents = []
        for row in rows:
            metadata = dict(row["metadata"])
            evidence_id = str(row.get("id") or metadata["document_id"])
            metadata.setdefault("document_id", evidence_id)
            # Ensure required metadata for CanonicalEvidence
            metadata.setdefault("source_dataset", metadata.get("source_dataset", "HPA"))
            metadata.setdefault("version", metadata.get("version", "1.0"))
            original = row["page_content"]
            truncated = original[:max_embed_chars] if max_embed_chars and len(original) > max_embed_chars else original
            if max_embed_chars and truncated != original:
                metadata["original_content"] = original
            documents.append(
                Document(
                    id=evidence_id,
                    page_content=truncated,
                    metadata=metadata,
                )
            )
            self._rows_by_id[evidence_id] = row

        if embeddings is None:
            try:
                from langchain_core.vectorstores import InMemoryVectorStore
                from langchain_huggingface import HuggingFaceEmbeddings
            except ImportError as exc:
                raise RuntimeError(
                    "real HPA retrieval requires langchain-huggingface and sentence-transformers"
                ) from exc
            embedding_kwargs = {
                "encode_kwargs": {"normalize_embeddings": True, "prompt": "passage: "},
                "query_encode_kwargs": {"normalize_embeddings": True, "prompt": "query: "},
            }
            try:
                embeddings = HuggingFaceEmbeddings(model=self.embedding_model, **embedding_kwargs)
            except TypeError:
                embeddings = HuggingFaceEmbeddings(model_name=self.embedding_model, **embedding_kwargs)
        else:
            from langchain_core.vectorstores import InMemoryVectorStore

        store = InMemoryVectorStore(embedding=embeddings)
        store.add_documents(documents)
        self._store = store

        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with cache_path.open("wb") as f:
                try:
                    pickle.dump({"store": store, "rows_by_id": self._rows_by_id}, f)
                except Exception:
                    f.seek(0)
                    f.truncate()
                    pickle.dump({"store_dict": store.store, "rows_by_id": self._rows_by_id, "embedding_model": self.embedding_model}, f)
        except Exception:
            pass

        return store

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        """Retrieve with CanonicalEvidence 15 fields populated."""
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
            content = str(metadata.get("original_content") or document.page_content)
            # Populate all 15 CanonicalEvidence fields
            evidence.append(
                CanonicalEvidence(
                    evidence_id=str(document.metadata.get("document_id") or document.id),
                    content=content,
                    source=str(metadata.get("source_dataset")) if metadata.get("source_dataset") else None,
                    metadata=metadata,
                    score=score,
                    date=str(metadata.get("發布日期") or metadata.get("date")) if (metadata.get("發布日期") or metadata.get("date")) else None,
                    version=str(metadata.get("version")) if metadata.get("version") else None,
                    score_type="cosine",
                    status="VALID",
                    retriever=self.name,
                    evidence_risk_level="UNKNOWN",
                    safety_signal_types=[],
                    risk_basis=None,
                    entities=[],
                    relations=[],
                )
            )
        return RAGResult(
            request_id=request.request_id,
            original_query=request.original_query,
            retrieval_queries=request.retrieval_queries,
            evidence=evidence,
            retrieval_latency_ms=(time.perf_counter() - started) * 1000,
        )


class MultiSourceRetriever:
    """Retriever that merges TFDA and HPA sources, keeping separate caches."""

    name = "multi-source-bge-m3-retriever"

    def __init__(
        self,
        *,
        top_k: int = 5,
        embedding_model: str | None = None,
        include_tfda: bool = True,
        include_hpa: bool = True,
        hpa_source_ids: list[str] | None = None,
    ) -> None:
        self.top_k = top_k
        self.embedding_model = embedding_model or os.getenv("EMBED_MODEL", "ollama/bge-m3:latest")
        if self.embedding_model in ("bge-m3", "bge-m3:latest"):
            self.embedding_model = "ollama/bge-m3:latest"
        self.include_tfda = include_tfda
        self.include_hpa = include_hpa
        self.hpa_source_ids = hpa_source_ids or HPA_SOURCE_IDS
        self._tfda_retriever: Any | None = None
        self._hpa_retrievers: dict[str, HPADietRetriever] = {}

    def _get_tfda_retriever(self) -> Any:
        if self._tfda_retriever is None and self.include_tfda:
            from tfda_context_gate.rag.tfda_retriever import TFDADrugSafetyRetriever

            self._tfda_retriever = TFDADrugSafetyRetriever(
                top_k=self.top_k, embedding_model=self.embedding_model
            )
        return self._tfda_retriever

    def _get_hpa_retriever(self, source_id: str) -> HPADietRetriever:
        if source_id not in self._hpa_retrievers:
            self._hpa_retrievers[source_id] = HPADietRetriever(
                source_id=source_id, top_k=self.top_k, embedding_model=self.embedding_model
            )
        return self._hpa_retrievers[source_id]

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        """Merge results from TFDA and HPA, deduplicate, top_k."""
        started = time.perf_counter()
        all_evidence: dict[str, CanonicalEvidence] = {}
        all_scores: dict[str, float] = {}

        # TFDA
        if self.include_tfda:
            try:
                tfda = self._get_tfda_retriever()
                if tfda:
                    result = tfda.retrieve(request)
                    for ev in result.evidence:
                        if ev.evidence_id not in all_evidence or (ev.score or 0) > (all_scores.get(ev.evidence_id, -1)):
                            all_evidence[ev.evidence_id] = ev
                            all_scores[ev.evidence_id] = ev.score or 0
            except Exception:
                pass

        # HPA per source_id (separate caches)
        if self.include_hpa:
            for sid in self.hpa_source_ids:
                try:
                    hpa = self._get_hpa_retriever(sid)
                    result = hpa.retrieve(request)
                    for ev in result.evidence:
                        if ev.evidence_id not in all_evidence or (ev.score or 0) > (all_scores.get(ev.evidence_id, -1)):
                            all_evidence[ev.evidence_id] = ev
                            all_scores[ev.evidence_id] = ev.score or 0
                except Exception:
                    continue

        # Sort by score and take top_k
        sorted_evidence = sorted(
            all_evidence.values(), key=lambda x: all_scores.get(x.evidence_id, 0), reverse=True
        )[: self.top_k]

        return RAGResult(
            request_id=request.request_id,
            original_query=request.original_query,
            retrieval_queries=request.retrieval_queries,
            evidence=sorted_evidence,
            retrieval_latency_ms=(time.perf_counter() - started) * 1000,
        )


def build_hpa_caches(embedding_model: str = "ollama/bge-m3:latest") -> list[Path]:
    """Build all HPA caches, return cache paths."""
    from tfda_context_gate.rag.hpa_ingest import ingest_all_sources, save_combined_documents

    # Ensure documents exist
    if not HPA_DOCUMENTS_PATH.exists():
        all_docs = ingest_all_sources()
        save_combined_documents(all_docs, HPA_DOCUMENTS_PATH)

    cache_paths = []
    for source_id in HPA_SOURCE_IDS:
        retriever = HPADietRetriever(source_id=source_id, embedding_model=embedding_model, top_k=5)
        retriever._ensure_store()
        cache_paths.append(retriever._cache_path())
        print(f"[HPA Cache] Built {source_id}: {retriever._cache_path()}")

    # Also build combined cache
    combined = HPADietRetriever(source_id=None, embedding_model=embedding_model, top_k=5)
    combined._ensure_store()
    cache_paths.append(combined._cache_path())
    print(f"[HPA Cache] Built combined: {combined._cache_path()}")

    return cache_paths
