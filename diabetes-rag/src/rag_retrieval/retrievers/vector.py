"""記憶體內的餘弦相似度向量檢索器。85 筆 3072 維 chunk 只需要 numpy 內積，
不需要向量資料庫——見 CLAUDE.md §10。
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

import numpy as np

from ..loaders import VectorChunkRecord
from .base import Candidate


class VectorRetriever:
    def __init__(self, chunks: Sequence[VectorChunkRecord]):
        self._chunks = list(chunks)
        matrix = np.array([c.embedding for c in self._chunks], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        # 載入時就一次正規化；embedding 本來就近似單位向量（CLAUDE.md §4），
        # 但這樣做能防範誤差累積，且不增加每次查詢的成本。
        self._matrix = matrix / norms

    def search_by_vector(self, query_vector, top_k: int = 5) -> list[Candidate]:
        """對已計算好的查詢向量做純餘弦 top-k。不呼叫網路——這正是讓本
        retriever 可以做單元測試的原因。"""
        if not self._chunks:
            return []
        q = np.asarray(query_vector, dtype=np.float32)
        norm = np.linalg.norm(q)
        if norm > 0:
            q = q / norm
        scores = self._matrix @ q
        k = min(top_k, len(self._chunks))
        top_idx = np.argsort(-scores)[:k]
        return [
            Candidate(
                chunk_id=self._chunks[i].chunk_id,
                source=self._chunks[i].source,
                version=self._chunks[i].version,
                date=self._chunks[i].date,
                status=self._chunks[i].status,
                content=self._chunks[i].content,
                retriever="vector",
                score=float(scores[i]),
                score_type="similarity",
            )
            for i in top_idx
        ]

    def search(
        self,
        queries: list[str],
        top_k: int = 5,
        embed_fn: Optional[Callable[[str], list[float]]] = None,
    ) -> list[Candidate]:
        """對每個查詢字串做 embedding 並合併結果，同一個 chunk 在多個查詢
        變體間取最高分（CONTRACT_v1 §1：`retrieval_queries` 的查詢展開由
        RAG 端負責）。"""
        if embed_fn is None:
            # 延遲載入：讓只用 search_by_vector 的呼叫端（例如測試）
            # 不需要強制依賴 Gemini／網路。
            from ..embedding import embed_query

            embed_fn = embed_query

        best: dict[str, Candidate] = {}
        for query in queries:
            vector = embed_fn(query)
            for cand in self.search_by_vector(vector, top_k=top_k):
                existing = best.get(cand.chunk_id)
                if existing is None or cand.score > existing.score:
                    best[cand.chunk_id] = cand

        ranked = sorted(best.values(), key=lambda c: -c.score)
        return ranked[:top_k]
