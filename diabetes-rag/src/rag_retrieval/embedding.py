"""透過 Gemini API 做查詢時的 embedding。必須使用與語料庫相同的模型
（`models/gemini-embedding-2`，見 ../pipelines/vector_pipeline/embed_gemini.py），
否則餘弦相似度等於是在比較兩個不同空間裡的向量。

API key 絕不寫死在程式碼裡——CLAUDE.md §2 不可退讓事項 #8。一律從環境
變數 `GEMINI_API_KEY` 讀取，這把 key 由實驗室保管。
"""

from __future__ import annotations

import json
import os
import re
from importlib import resources

MODEL_NAME = "models/gemini-embedding-2"
_DATA_PKG = "rag_retrieval.data"
_CACHE_FILENAME = "query_vector_cache.json"

_QUERY_CACHE: dict[str, list[float]] = {}
_NORM_QUERY_CACHE: dict[str, list[float]] = {}


def _normalize_key(text: str) -> str:
    # 移除標點與空白以提升快取命中率
    return re.sub(r"[\s\?？!！,，。；;、]+", "", text).lower()


def _init_query_cache() -> None:
    global _QUERY_CACHE, _NORM_QUERY_CACHE
    if _QUERY_CACHE:
        return
    try:
        ref = resources.files(_DATA_PKG).joinpath(_CACHE_FILENAME)
        if ref.is_file():
            with ref.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    _QUERY_CACHE.update(data)
                    for k, v in data.items():
                        _NORM_QUERY_CACHE[_normalize_key(k)] = v
    except Exception:
        pass


def _embed(text: str, task_type: str) -> list[float]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. It must come from the environment — "
            "never hardcode it (CLAUDE.md non-negotiable #8)."
        )

    import google.generativeai as genai

    genai.configure(api_key=api_key)
    result = genai.embed_content(model=MODEL_NAME, content=text, task_type=task_type)
    return result["embedding"]


def embed_query(text: str) -> list[float]:
    """以 task_type=RETRIEVAL_QUERY 對單一查詢字串做 embedding。
    優先命中本機 FAQ Query Vector Cache（0 毫秒），未命中時回退至雲端 API。"""
    _init_query_cache()
    raw_key = text.strip()
    if raw_key in _QUERY_CACHE:
        return _QUERY_CACHE[raw_key]

    norm_key = _normalize_key(raw_key)
    if norm_key in _NORM_QUERY_CACHE:
        return _NORM_QUERY_CACHE[norm_key]

    # 未命中則線上呼叫並寫入記憶體快取
    vec = _embed(text, "RETRIEVAL_QUERY")
    _QUERY_CACHE[raw_key] = vec
    _NORM_QUERY_CACHE[norm_key] = vec
    return vec


def embed_document(text: str) -> list[float]:
    """為建立索引對一段原始文字做 embedding（scripts/build_index.py 使用），
    task_type 與原本 85 筆語料建立時一致。"""
    return _embed(text, "RETRIEVAL_DOCUMENT")

