"""第 8 步驗收測試：RRF 排序穩定、具確定性，且 w_graph > w_vector，
使得名次相同時圖譜事實會勝過 vector chunk。
"""

from rag_retrieval.fusion import RRF_K, W_GRAPH, W_VECTOR, reciprocal_rank_fusion
from rag_retrieval.retrievers.base import Candidate


def _cand(chunk_id, retriever, score, score_type):
    return Candidate(
        chunk_id=chunk_id,
        source="src",
        version="v1",
        date="2024-01-01",
        status="active",
        content="content",
        retriever=retriever,
        score=score,
        score_type=score_type,
    )


def test_weights_favour_graph_track():
    assert W_GRAPH > W_VECTOR


def test_rank_one_in_both_tracks_graph_wins_tie():
    vector = [_cand("v1", "vector", 0.99, "similarity")]
    graph = [_cand("g1", "graph", 0.5, "graph_traversal")]
    fused = reciprocal_rank_fusion(vector, graph)
    assert fused[0].chunk_id == "g1"


def test_fusion_is_deterministic_across_runs():
    vector = [_cand(f"v{i}", "vector", 1.0 - i * 0.1, "similarity") for i in range(3)]
    graph = [_cand(f"g{i}", "graph", 1.0 - i * 0.1, "graph_traversal") for i in range(3)]
    first = [c.chunk_id for c in reciprocal_rank_fusion(vector, graph)]
    second = [c.chunk_id for c in reciprocal_rank_fusion(vector, graph)]
    assert first == second


def test_all_candidates_present_no_duplicates():
    vector = [_cand("v1", "vector", 0.9, "similarity"), _cand("v2", "vector", 0.8, "similarity")]
    graph = [_cand("g1", "graph", 0.7, "graph_traversal")]
    fused = reciprocal_rank_fusion(vector, graph)
    assert {c.chunk_id for c in fused} == {"v1", "v2", "g1"}
    assert len(fused) == 3


def test_empty_track_does_not_break_fusion():
    vector = [_cand("v1", "vector", 0.9, "similarity")]
    fused = reciprocal_rank_fusion(vector, [])
    assert [c.chunk_id for c in fused] == ["v1"]


def test_rrf_k_is_positive_constant():
    assert RRF_K > 0
