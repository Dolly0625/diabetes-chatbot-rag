"""第 4 步驗收測試：餘弦 top-k 正確且夠快。用每個 chunk 自己的 embedding
當作查詢向量（自我檢索），所以測試不需要網路連線／Gemini API key——這裡
驗證的是檢索的數學邏輯，embedding 呼叫本身另外在 embedding.py 驗證。
"""

import time

from rag_retrieval.loaders import load_vector_chunks
from rag_retrieval.retrievers.vector import VectorRetriever


def _retriever():
    return VectorRetriever(load_vector_chunks())


def test_self_retrieval_ranks_the_same_chunk_first():
    retriever = _retriever()
    chunks = load_vector_chunks()
    target = next(c for c in chunks if "飲食" in c.content)

    results = retriever.search_by_vector(target.embedding, top_k=5)

    assert results[0].chunk_id == target.chunk_id
    assert results[0].score > 0.99
    assert results[0].score_type == "similarity"
    assert results[0].retriever == "vector"


def test_top_k_respected():
    retriever = _retriever()
    chunks = load_vector_chunks()
    results = retriever.search_by_vector(chunks[0].embedding, top_k=3)
    assert len(results) == 3


def test_search_by_vector_latency_under_50ms():
    retriever = _retriever()
    chunks = load_vector_chunks()
    start = time.perf_counter()
    retriever.search_by_vector(chunks[0].embedding, top_k=5)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50
