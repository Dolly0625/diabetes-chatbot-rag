"""第 9 步驗收測試：低信心的圖譜三元組會被捨棄，並發出
LOW_CONFIDENCE_EVIDENCE_DROPPED；allow-list 與截斷都正常運作。
"""

from rag_retrieval.contract.enums import WarningCode
from rag_retrieval.gate_out import (
    DEFAULT_TOP_N,
    gate_out_per_track,
    truncate,
    truncate_balanced,
)
from rag_retrieval.retrievers.base import Candidate


def _graph_cand(chunk_id, confidence, relation_type="TREATS", entities=None):
    return Candidate(
        chunk_id=chunk_id,
        source="src",
        version="v1",
        date="2024-01-01",
        status="active",
        content="content",
        retriever="graph",
        score=0.8,
        score_type="graph_traversal",
        entities=entities or [{"id": "e1", "type": "Substance", "label": "x", "code": None}],
        relations=[{"relation": relation_type, "condition": None}],
        relation_type=relation_type,
        confidence=confidence,
    )


def test_low_confidence_triple_dropped_with_warning():
    candidates = [_graph_cand("low", 0.1), _graph_cand("high", 0.9)]
    kept, warnings = gate_out_per_track(candidates)
    assert {c.chunk_id for c in kept} == {"high"}
    assert WarningCode.LOW_CONFIDENCE_EVIDENCE_DROPPED in warnings


def test_all_dropped_emits_empty_after_threshold_filter():
    candidates = [_graph_cand("low1", 0.1), _graph_cand("low2", 0.2)]
    kept, warnings = gate_out_per_track(candidates)
    assert kept == []
    assert WarningCode.EMPTY_AFTER_THRESHOLD_FILTER in warnings


def test_no_candidates_emits_no_warnings():
    kept, warnings = gate_out_per_track([])
    assert kept == []
    assert warnings == []


def test_disallowed_entity_type_is_dropped():
    bad = _graph_cand(
        "bad",
        0.9,
        entities=[{"id": "e1", "type": "NotARealType", "label": "x", "code": None}],
    )
    kept, _ = gate_out_per_track([bad])
    assert kept == []


def test_truncation_respects_top_n():
    candidates = [_graph_cand(f"c{i}", 0.9) for i in range(DEFAULT_TOP_N + 3)]
    assert len(truncate(candidates)) == DEFAULT_TOP_N


def _vector_cand(chunk_id):
    return Candidate(
        chunk_id=chunk_id,
        source="src",
        version="v1",
        date="2024-01-01",
        status="active",
        content="content",
        retriever="vector",
        score=0.5,
        score_type="similarity",
    )


def test_truncate_balanced_guarantees_one_slot_per_track():
    # 純融合順序下，6 個 graph 候選會贏過 1 個 vector 候選——若沒有
    # balance 機制，top_n=DEFAULT_TOP_N 會把 vector 那個截掉。
    fused = [_graph_cand(f"g{i}", 0.9) for i in range(6)] + [_vector_cand("v0")]
    result = truncate_balanced(fused, top_n=DEFAULT_TOP_N)
    assert len(result) == DEFAULT_TOP_N
    assert "v0" in {c.chunk_id for c in result}


def test_truncate_balanced_matches_plain_truncate_for_single_track():
    fused = [_graph_cand(f"g{i}", 0.9) for i in range(DEFAULT_TOP_N + 3)]
    assert [c.chunk_id for c in truncate_balanced(fused)] == [
        c.chunk_id for c in truncate(fused)
    ]
