"""第 5 步驗收測試：metformin/eGFR 的 CAUTION_FOR 三元組能被查詢檢索到，
且跳數展開與跳數上限的計算，在胰島素注射部位這條真實的 2 跳路徑上
（可檢索資料裡真的存在）行為合理。
"""

from rag_retrieval.loaders import load_graph_triples
from rag_retrieval.retrievers.graph import GraphRetriever


def _retriever():
    return GraphRetriever(load_graph_triples())


def test_metformin_egfr_caution_triple_retrievable_by_query():
    result = _retriever().search(["metformin 腎功能 注意事項", "metformin eGFR 減量"])
    relations = {c.relation_type for c in result.candidates}
    assert "CAUTION_FOR" in relations
    caution = [c for c in result.candidates if c.relation_type == "CAUTION_FOR"]
    assert any("eGFR" in (c.relations[0]["condition"] or "") for c in caution)


def test_insulin_injection_site_two_hop_chain():
    result = _retriever().search(["胰島素 注射部位"])
    chunk_ids = {c.chunk_id for c in result.candidates}
    assert any("insulin_amyloidosis" in cid for cid in chunk_ids)
    # 這條 2 跳鏈（TRIGGERS 接著 RISK_FACTOR_FOR）裡的兩筆三元組都應該會
    # 出現，因為整條鏈都在 2 跳預算之內
    relations = {c.relation_type for c in result.candidates}
    assert "TRIGGERS" in relations
    assert "RISK_FACTOR_FOR" in relations


def test_no_match_returns_not_applicable():
    result = _retriever().search(["完全無關的問題 高爾夫球 果嶺速度"])
    assert result.candidates == []
    assert result.graph_path_status == "NOT_APPLICABLE"


def test_graph_chunks_carry_required_graph_fields():
    result = _retriever().search(["metformin"])
    assert result.candidates
    for c in result.candidates:
        assert c.entities and len(c.entities) == 2
        assert c.relations and len(c.relations) == 1
        assert c.retriever == "graph"
        assert c.score_type == "graph_traversal"
