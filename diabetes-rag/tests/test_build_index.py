"""第 6 步驗收測試。

`build_index.chunk_document` 不需要網路，直接測試即可。真正呼叫 Gemini
做 embedding 需要 GEMINI_API_KEY 與網路，本測試環境沒有——所以驗收標準
中「chunk 端到端可被檢索」的部分，改用合併一個小型*合成*衛教 chunk 檔案
（格式與 scripts/build_index.py 實際產出相同）進向量索引，確認
load_vector_chunks + VectorRetriever 能正確接住它來驗證。真的對線上語料
跑這支腳本，是持有實驗室 GEMINI_API_KEY 的人要做的一次性操作
（見 scripts/build_index.py 的 docstring）——本測試套件不會重新驗證這件事。
"""

import json

from scripts.build_index import MAX_CHUNK_CHARS, MIN_CHUNK_CHARS, chunk_document
from rag_retrieval.loaders import load_education_documents, load_vector_chunks
from rag_retrieval.retrievers.vector import VectorRetriever


def test_chunk_document_covers_real_corpus_with_reasonable_sizes():
    documents = load_education_documents()
    assert len(documents) == 21
    total_chunks = 0
    for doc in documents:
        pieces = chunk_document(doc["page_content"])
        assert pieces, "每篇文件至少要產生一個 chunk"
        for piece in pieces:
            assert piece.strip()
            assert len(piece) <= MAX_CHUNK_CHARS * 2  # 單一超長句子可能超過 MAX
        total_chunks += len(pieces)
    assert total_chunks > 21  # 平均每篇文件應不只一個 chunk


def test_merging_education_chunks_makes_them_retrievable(tmp_path):
    # 真實的 embedding 是 3072 維；合成的這筆必須維度一致——
    # VectorRetriever 會把所有 chunk 疊成同一個矩陣，維度不一致在建構時
    # 就會直接拋出例外，而不只是給出錯誤答案。
    dim = 3072
    vector = [0.0] * dim
    vector[0] = 1.0
    synthetic = [
        {
            "chunk_id": "hpa-dm-book_sec99_00",
            "source": "hpa-dm-book",
            "version": "3rd-2022",
            "date": "2022-01-21",
            "status": "active",
            "content": "糖尿病飲食應均衡攝取六大類食物，並定時定量控制醣類攝取。",
            "retriever": "vector",
            "embedding_dim": dim,
            "embedding": vector,
        }
    ]
    extra_path = tmp_path / "education_chunks_embedded.json"
    extra_path.write_text(json.dumps(synthetic, ensure_ascii=False), encoding="utf-8")

    base_count = len(load_vector_chunks())
    merged = load_vector_chunks(extra_paths=[str(extra_path)])
    assert len(merged) == base_count + 1

    retriever = VectorRetriever(merged)
    results = retriever.search_by_vector(vector, top_k=1)
    assert results[0].chunk_id == "hpa-dm-book_sec99_00"
