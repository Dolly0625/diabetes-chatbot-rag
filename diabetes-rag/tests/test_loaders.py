"""第 2 步驗收測試：85/29 筆數、日期全為 ISO 格式、壞 chunk 已被捨棄。"""

import re

from rag_retrieval.loaders import load_graph_triples, load_vector_chunks

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_vector_chunk_count_and_dates():
    # 85 筆原始 TFDA chunk（CLAUDE.md §4）減去被捨棄的那 1 筆 4.htm chunk
    # = 84。一旦 scripts/build_index.py 產生了
    # data/education_chunks_embedded.json（第 6 步的自動合併），總數可能
    # 更多，所以這裡只鎖定 TFDA 來源的子集合，不鎖定總數。
    chunks = load_vector_chunks()
    tfda_chunks = [c for c in chunks if c.source.startswith("tfda-risk-")]
    assert len(tfda_chunks) == 84
    assert len(chunks) >= 84
    assert all(_ISO_DATE.match(c.date) for c in chunks)


def test_bad_chunk_dropped():
    chunks = load_vector_chunks()
    assert "tfda-risk-019_sec3_04" not in {c.chunk_id for c in chunks}


def test_graph_triple_count_and_dates():
    triples = load_graph_triples()
    assert len(triples) == 29
    assert all(_ISO_DATE.match(t.date) for t in triples)


def test_graph_chunk_ids_follow_contract_format():
    triples = load_graph_triples()
    for t in triples:
        assert t.chunk_id.startswith(f"{t.source}_tri_")
    # 每個 source 內部的序號都是唯一的
    assert len({t.chunk_id for t in triples}) == len(triples)


def test_metformin_egfr_caution_triple_is_retrievable():
    triples = load_graph_triples()
    caution = [t for t in triples if t.relation == "CAUTION_FOR"]
    assert any("eGFR" in (t.condition or "") for t in caution)
