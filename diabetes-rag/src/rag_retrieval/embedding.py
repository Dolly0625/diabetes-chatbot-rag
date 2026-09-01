"""透過 Gemini API 做查詢時的 embedding。必須使用與語料庫相同的模型
（`models/gemini-embedding-2`，見 ../pipelines/vector_pipeline/embed_gemini.py），
否則餘弦相似度等於是在比較兩個不同空間裡的向量。

API key 絕不寫死在程式碼裡——CLAUDE.md §2 不可退讓事項 #8。一律從環境
變數 `GEMINI_API_KEY` 讀取，這把 key 由實驗室保管。
"""

from __future__ import annotations

import os

MODEL_NAME = "models/gemini-embedding-2"


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
    """以 task_type=RETRIEVAL_QUERY 對單一查詢字串做 embedding（語料庫是用
    RETRIEVAL_DOCUMENT 做的——Gemini 的 embedding 空間依 task_type 不對稱，
    所以這個區分會影響召回率）。"""
    return _embed(text, "RETRIEVAL_QUERY")


def embed_document(text: str) -> list[float]:
    """為建立索引對一段原始文字做 embedding（scripts/build_index.py 使用），
    task_type 與原本 85 筆語料建立時一致。"""
    return _embed(text, "RETRIEVAL_DOCUMENT")
