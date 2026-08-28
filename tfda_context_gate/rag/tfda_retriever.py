from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult

from .schemas import RAGResult


# 套件根目錄，用於定位預設資料路徑
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCUMENTS_PATH = PACKAGE_ROOT / "data" / "processed" / "langchain_documents.json"  # 預設 129 筆 TFDA 語料
USER_DOCUMENTS_PATH = Path("/mnt/data/langchain_documents.json")  # 使用者自訂路徑（優先）
DEFAULT_EMBEDDING_MODEL = "ollama/bge-m3:latest"  # 預設多語嵌入模型（固化 bge-m3，消除分叉）
CACHE_DIR = PACKAGE_ROOT / "data" / "processed" / ".vector_cache"  # 向量快取目錄（持久化）

# G4 retrieval precision: similarity threshold α (tunable) and topic consistency
RETRIEVAL_THRESHOLD = float(os.getenv("RETRIEVAL_THRESHOLD", os.getenv("RETRIEVAL_ALPHA", "0.55")))
CACHE_VERSION = "g5-faq-v1"

_FAQ_CAUSE_KEYWORDS = ["為什麼", "成因", "為什麼會", "怎麼來的", "怎麼會", "為何"]
_FAQ_GENETIC_KEYWORDS = ["遺傳", "家族", "會不會遺傳"]
_FAQ_SLEEP_KEYWORDS = ["睡覺", "睡不好", "睡前", "疲倦", "想睡", "睡眠"]
_FAQ_CAPABILITY_KEYWORDS = ["可以做什麼", "能幫", "怎麼用", "功能", "你可以", "你能做什麼", "能做什麼", "幫什麼", "可以跟我說什麼"]
_FAQ_KEYWORD_MAP: dict[str, list[str]] = {
    "cause_overview": _FAQ_CAUSE_KEYWORDS,
    "genetic": _FAQ_GENETIC_KEYWORDS,
    "sleep": _FAQ_SLEEP_KEYWORDS,
    "capability": _FAQ_CAPABILITY_KEYWORDS,
}
_FAQ_TOPIC_NORMALIZE: dict[str, str] = {
    "成因總覽": "cause_overview",
    "遺傳": "genetic",
    "睡眠與血糖": "sleep",
    "本助手能做什麼": "capability",
    "cause_overview": "cause_overview",
    "genetic": "genetic",
    "sleep": "sleep",
    "capability": "capability",
}


def _faq_bonus(metadata: dict[str, Any], query_text: str) -> float:
    if not metadata.get("is_faq"):
        return 0.0
    raw_topic = str(metadata.get("faq_topic") or metadata.get("topic") or "")
    norm = _FAQ_TOPIC_NORMALIZE.get(raw_topic, raw_topic)
    keywords = _FAQ_KEYWORD_MAP.get(norm)
    if not keywords:
        return 0.0
    q = query_text or ""
    for kw in keywords:
        if kw in q:
            return 0.05
    return 0.0

# Topic keywords for inference (cause vs diet vs others)
_CAUSE_KEYWORDS = ["為什麼", "為何", "成因", "怎麼來的", "原因", "病因", "遺傳", "家族", "胰島素阻抗", "胰島素", "第一型", "第二型", "發病", "危險因子"]
_DIET_KEYWORDS = ["飲食", "吃什麼", "食物", "營養", "碳水", "熱量", "代換", "怎麼吃", "可以吃", "膳食纖維", "低升糖"]
_SLEEP_KEYWORDS = ["睡眠", "睡覺", "失眠", "睡不好", "睡前", "疲倦"]
_EXERCISE_KEYWORDS = ["運動", "快走", "游泳", "騎車"]
_MEDICATION_KEYWORDS = ["藥品", "藥物", "用藥", "劑量", "metformin", "胰島素治療"]

_TOPIC_ALLOWLIST: dict[str, set[str]] = {
    "cause": {"cause", "general", "etiology", "genetics"},
    "diet": {"diet", "general", "nutrition"},
    "sleep": {"sleep", "general"},
    "exercise": {"exercise", "general"},
    "medication": {"medication", "general", "drug"},
    "general": {"general", "cause", "diet", "sleep", "exercise", "medication", "drug"},
}


def _infer_topic(text: str, metadata: dict[str, Any] | None = None) -> str:
    """Infer chunk topic from text + source_dataset."""
    t = text or ""
    if any(k in t for k in _CAUSE_KEYWORDS[:8]):  # at least cause triggers
        # need more specific: if diet keywords also present, prefer cause when cause keywords exist
        if any(k in t for k in ["成因", "為什麼", "為何", "怎麼來的", "遺傳", "家族史", "胰島素阻抗"]):
            return "cause"
    if any(k in t for k in _SLEEP_KEYWORDS):
        # disambiguate: diet docs may mention sleep briefly, but cause not about sleep
        if "睡眠不足影響血糖" in t or "充足睡眠" in t:
            return "sleep"
        if any(k in t for k in ["睡覺", "失眠", "睡眠"]):
            # if primarily sleep FAQ
            return "sleep"
    if any(k in t for k in _DIET_KEYWORDS):
        # diet dominates if no cause signal
        if not any(k in t for k in ["成因", "為什麼會有", "遺傳"]):
            return "diet"
    if any(k in t for k in _EXERCISE_KEYWORDS):
        return "exercise"
    if any(k in t for k in _MEDICATION_KEYWORDS):
        return "medication"
    # fallback via source_dataset
    src = ""
    if metadata:
        src = str(metadata.get("source_dataset") or metadata.get("source_id") or metadata.get("source") or "")
    if "食品營養" in src:
        return "diet"
    if "國民飲食" in src:
        return "diet"
    if "糖尿病與我" in src:
        # diabetes book contains both cause and general
        if any(k in t for k in ["第一型", "第二型", "認識糖尿病"]):
            return "cause"
        return "general"
    if "TFDA" in src or "風險溝通" in src:
        return "medication"
    return "general"


def _infer_query_topic(query: str) -> str:
    """Infer expected topic from query intent."""
    q = query or ""
    if any(k in q for k in ["為什麼", "為何", "成因", "怎麼來的", "原因", "病因", "遺傳", "家族"]):
        return "cause"
    if any(k in q for k in ["吃什麼", "怎麼吃", "飲食", "食物", "營養", "可以吃", "菜單"]):
        return "diet"
    if any(k in q for k in ["睡覺", "睡眠", "睡不好", "失眠", "睡前"]):
        return "sleep"
    if any(k in q for k in ["運動"]):
        return "exercise"
    if any(k in q for k in ["藥", "劑量"]):
        return "medication"
    return "general"


class TFDADatasetError(ValueError):
    """當處理後的 TFDA 語料無法滿足 RAG 契約時拋出。"""

    pass


def resolve_documents_path(path: str | Path | None = None) -> Path:
    """解析語料路徑，優先順序：顯式傳入 > 環境變數 > /mnt/data > 套件預設。

    參數:
        path: 顯式指定的路徑（可選）
    回傳:
        第一個存在的語料檔案路徑
    拋錯:
        皆不存在時拋 FileNotFoundError
    """

    candidates = []
    if path is not None:
        candidates.append(Path(path).expanduser())  # ① 顯式傳入的路徑
    env_path = os.getenv("TFDA_DOCUMENTS_PATH")
    if env_path:
        candidates.append(Path(env_path).expanduser())  # ② 環境變數指定的路徑
    candidates.extend([USER_DOCUMENTS_PATH, DEFAULT_DOCUMENTS_PATH])  # ③④ 固定候選
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    searched = ", ".join(str(item) for item in candidates)
    raise FileNotFoundError(f"TFDA processed corpus not found; searched: {searched}")


def load_tfda_rows(path: str | Path | None = None) -> list[dict[str, Any]]:
    """載入並驗證 TFDA 風險溝通語料，每列對應一筆處理後的 JSON 記錄。

    驗證規則：
      - 頂層必須是 JSON list
      - 每列必須是 dict，且含 id/document_id、非空 page_content、metadata dict
      - evidence_id 不可重複

    參數:
        path: 語料路徑（可選，自動解析）
    回傳:
        驗證後的原始列列表
    """

    resolved = resolve_documents_path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TFDADatasetError(f"invalid JSON corpus: {resolved}") from exc
    if not isinstance(payload, list):
        raise TFDADatasetError("TFDA corpus must be a top-level JSON list")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()  # 用於檢測重複 ID
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise TFDADatasetError(f"corpus row {index} is not an object")
        metadata = item.get("metadata")
        # evidence_id 優先取 id，否則取 metadata.document_id
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
    """基於處理後 TFDA 語料的最小真實向量檢索器。

    核心機制：
      - 129 筆語料 lazy 載入：索引在首次 retrieve 呼叫時才建置（_ensure_store），
        避免初始化時就耗費嵌入計算資源
      - 每列語料保持為一個 LangChain Document，不做額外切塊或合成證據
      - 可注入替換：單元測試可用 FixtureRetriever 免建索引
    """

    name = "tfda-huggingface-inmemory-vector-retriever"

    def __init__(
        self,
        *,
        documents_path: str | Path | None = None,
        top_k: int = 5,
        embedding_model: str | None = None,
    ) -> None:
        """初始化 TFDA 檢索器（此時不建索引，延遲到首次檢索）。

        參數:
            documents_path: 語料路徑（可選，自動解析）
            top_k: 每次查詢回傳的 top-k 筆
            embedding_model: 嵌入模型名稱（可選，預設 multilingual-e5-small）
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        self.documents_path = resolve_documents_path(documents_path)
        self.top_k = top_k
        raw_model = embedding_model or os.getenv("EMBED_MODEL", DEFAULT_EMBEDDING_MODEL)
        if raw_model not in ("ollama/bge-m3:latest", "ollama/bge-m3", "bge-m3:latest", "bge-m3"):
            if raw_model == "intfloat/multilingual-e5-small":
                raw_model = "ollama/bge-m3:latest"
        if raw_model in ("bge-m3", "bge-m3:latest"):
            raw_model = "ollama/bge-m3:latest"
        self.embedding_model = raw_model
        self._store: Any | None = None  # 向量庫實例，lazy 初始化前為 None
        self._rows_by_id: dict[str, dict[str, Any]] = {}  # evidence_id → 原始列的對照表

    def _cache_key(self) -> str:
        import hashlib

        stat = self.documents_path.stat() if self.documents_path.exists() else None
        mtime_sec = stat.st_mtime_ns // 1_000_000_000 if stat else 0
        raw = f"{self.documents_path}:{mtime_sec}:{stat.st_size if stat else 0}:{self.embedding_model}:{CACHE_VERSION}:{RETRIEVAL_THRESHOLD}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _cache_path(self) -> Path:
        return CACHE_DIR / f"{self._cache_key()}.pkl"

    def _ensure_store(self) -> Any:
        """確保向量庫已建置；優先讀持久快取，否則 lazy 建置並落檔。

        建置流程：
          1. 記憶體已有 → 直接回傳
          2. 磁碟快取命中 → pickle 載入
          3. 未命中 → 載入 129 筆 TFDA 列，建 InMemoryVectorStore 並 pickle 存檔

        回傳:
            InMemoryVectorStore 實例
        """
        if self._store is not None:
            return self._store
        cache_path = self._cache_path()
        if cache_path.exists():
            try:
                import pickle

                with cache_path.open("rb") as f:
                    payload = pickle.load(f)
                    if "store_dict" in payload:
                        from langchain_core.vectorstores import InMemoryVectorStore

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
                        new_store = InMemoryVectorStore(embedding=embeddings)
                        new_store.store = payload["store_dict"]
                        self._store = new_store
                        self._rows_by_id = payload.get("rows_by_id", {})
                        return self._store
                    self._store = payload["store"]
                    self._rows_by_id = payload.get("rows_by_id", {})
                    return self._store
            except Exception:
                try:
                    cache_path.unlink(missing_ok=True)
                except Exception:
                    pass
        rows = load_tfda_rows(self.documents_path)
        try:
            from langchain_core.documents import Document
        except ImportError as exc:
            raise RuntimeError("langchain-core required") from exc

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
            original = row["page_content"]
            if "topic" not in metadata:
                metadata["topic"] = _infer_topic(original, metadata)
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
                    "real TFDA retrieval requires langchain-huggingface and "
                    "sentence-transformers; install tfda_context_gate/requirements.txt"
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
            import pickle
            import tempfile

            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            payload_dict = {"store_dict": store.store, "rows_by_id": self._rows_by_id, "embedding_model": self.embedding_model}
            fd, tmp_path = tempfile.mkstemp(dir=str(CACHE_DIR), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as tmp:
                    pickle.dump(payload_dict, tmp)
                os.replace(tmp_path, cache_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise
        except Exception:
            try:
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

    def _is_hpa_enabled(self) -> bool:
        """Check if HPA diet retrieval should be enabled (ollama/bge-m3)."""
        return self.embedding_model.startswith("ollama/") or "bge-m3" in self.embedding_model

    def _load_hpa_stores(self) -> list[Any]:
        """Load HPA vector stores from separate cache keys per source_id (hpa_*.pkl)."""
        stores: list[Any] = []
        if not self._is_hpa_enabled():
            return stores
        try:
            for cache_file in CACHE_DIR.glob("hpa_*.pkl"):
                try:
                    import pickle

                    with cache_file.open("rb") as f:
                        payload = pickle.load(f)
                        store = payload.get("store")
                        if store is not None:
                            stores.append(store)
                        elif "store_dict" in payload:
                            from langchain_core.vectorstores import InMemoryVectorStore

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
                            new_store = InMemoryVectorStore(embedding=embeddings)
                            new_store.store = payload["store_dict"]
                            stores.append(new_store)
                except Exception:
                    continue
        except Exception:
            pass
        return stores

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        """執行向量檢索：對每個 retrieval_query 做相似度搜尋，去重後取 top-k。

        流程：
          1. 觸發 _ensure_store() lazy 建置索引（首次呼叫才執行）
          2. 對每個 retrieval_query 執行 similarity_search_with_score
          3. 依 evidence_id 去重，保留最高分
          4. 依分數排序取 top-k，轉為 CanonicalEvidence
          5. 若為 bge-m3 則同時查詢 HPA diet 快取（hpa_*.pkl，separate keys）

        參數:
            request: 查詢擴展結果（含多個 retrieval_queries）
        回傳:
            RAGResult，含排序後的證據與檢索耗時
        """
        started = time.perf_counter()
        store = self._ensure_store()  # lazy 建置或取快取
        ranked: dict[str, tuple[Any, float]] = {}  # evidence_id → (document, score)
        for query in request.retrieval_queries:
            for document, score in store.similarity_search_with_score(query, k=self.top_k):
                evidence_id = str(document.metadata.get("document_id") or document.id)
                numeric_score = float(score)
                previous = ranked.get(evidence_id)
                # 去重：同一 evidence_id 僅保留最高分
                if previous is None or numeric_score > previous[1]:
                    ranked[evidence_id] = (document, numeric_score)

        # HPA diet retrieval for bge-m3 (separate cache keys per source_id)
        hpa_stores = self._load_hpa_stores()
        for hpa_store in hpa_stores:
            for query in request.retrieval_queries:
                try:
                    for document, score in hpa_store.similarity_search_with_score(query, k=self.top_k):
                        evidence_id = str(document.metadata.get("document_id") or document.id)
                        numeric_score = float(score)
                        previous = ranked.get(evidence_id)
                        if previous is None or numeric_score > previous[1]:
                            ranked[evidence_id] = (document, numeric_score)
                except Exception:
                    continue

        if ranked:
            boosted: dict[str, tuple[Any, float]] = {}
            for eid, (doc, sc) in ranked.items():
                bonus = _faq_bonus(dict(doc.metadata), request.original_query)
                if bonus:
                    for rq in request.retrieval_queries:
                        if rq != request.original_query:
                            b2 = _faq_bonus(dict(doc.metadata), rq)
                            if b2 > bonus:
                                bonus = b2
                boosted[eid] = (doc, sc + bonus if bonus else sc)
            ranked = boosted

        # G4: threshold + topic consistency check -> knowledge gap if fails
        if ranked:
            max_score = max(score for _, score in ranked.values())
            if max_score < RETRIEVAL_THRESHOLD:
                return RAGResult(
                    request_id=request.request_id,
                    original_query=request.original_query,
                    retrieval_queries=request.retrieval_queries,
                    evidence=[],
                    retrieval_latency_ms=(time.perf_counter() - started) * 1000,
                )
            expected_topic = _infer_query_topic(request.original_query)
            if expected_topic != "general":
                allowed = _TOPIC_ALLOWLIST.get(expected_topic, set())
                # ensure metadata topic is present (infer if missing)
                has_allowed = False
                for doc, sc in ranked.values():
                    if sc < RETRIEVAL_THRESHOLD:
                        continue
                    t = doc.metadata.get("topic") or _infer_topic(str(doc.metadata.get("original_content") or doc.page_content), doc.metadata)
                    if t in allowed:
                        has_allowed = True
                        break
                if not has_allowed:
                    return RAGResult(
                        request_id=request.request_id,
                        original_query=request.original_query,
                        retrieval_queries=request.retrieval_queries,
                        evidence=[],
                        retrieval_latency_ms=(time.perf_counter() - started) * 1000,
                    )
                # filter ranked to only allowed topics above threshold
                filtered = {eid: (doc, sc) for eid, (doc, sc) in ranked.items() if sc >= RETRIEVAL_THRESHOLD and ((doc.metadata.get("topic") or _infer_topic(str(doc.metadata.get("original_content") or doc.page_content), doc.metadata)) in allowed)}
                if filtered:
                    ranked = filtered
                else:
                    return RAGResult(
                        request_id=request.request_id,
                        original_query=request.original_query,
                        retrieval_queries=request.retrieval_queries,
                        evidence=[],
                        retrieval_latency_ms=(time.perf_counter() - started) * 1000,
                    )
            else:
                # general query: still enforce threshold
                filtered = {eid: (doc, sc) for eid, (doc, sc) in ranked.items() if sc >= RETRIEVAL_THRESHOLD}
                if not filtered:
                    return RAGResult(
                        request_id=request.request_id,
                        original_query=request.original_query,
                        retrieval_queries=request.retrieval_queries,
                        evidence=[],
                        retrieval_latency_ms=(time.perf_counter() - started) * 1000,
                    )
                ranked = filtered

        # 依分數降冪排序，取前 top_k
        results = sorted(ranked.values(), key=lambda item: item[1], reverse=True)[: self.top_k]
        evidence = []
        for document, score in results:
            metadata = dict(document.metadata)
            if "topic" not in metadata:
                metadata["topic"] = _infer_topic(str(metadata.get("original_content") or document.page_content), metadata)
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
