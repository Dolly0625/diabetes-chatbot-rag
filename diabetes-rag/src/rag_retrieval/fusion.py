"""HYBRID 路由的 Reciprocal Rank Fusion。

RRF 只依名次合併兩份排序清單，不看分數大小——這在這裡完全正確，因為
`score` 在不同 score_type 之間（similarity vs graph_traversal，
CONTRACT_v1 §2.3）本來就不可比較。融合只決定合併後候選清單的**順序**，
每筆候選回傳時仍保留自己原本的 score／score_type，不做更動。
"""

from __future__ import annotations

from .retrievers.base import Candidate

# RAG-Fusion 的標準預設值（Cormack et al. 2009／RRF 慣例）。
RRF_K = 60

# 結構化的圖譜事實（注意事項、誘因、引發之病況）在融合後的順序上必須
# 蓋過敘述性的 vector 文字：若一筆中／高風險的圖譜事實與一段看似相關的
# vector 段落同時命中查詢，事實應該排在前面。這是一項用常數編碼的安全
# 決策，不是可任意調整的相關性參數——見 CLAUDE.md 建置順序第 8 步。
W_GRAPH = 2.0
W_VECTOR = 1.0


def reciprocal_rank_fusion(
    vector_candidates: list[Candidate],
    graph_candidates: list[Candidate],
) -> list[Candidate]:
    """把兩份已排序的候選清單合併成一份融合排序。結果具確定性：分數相同
    時退回「先出現者優先」（vector 先於 graph，然後依各自清單內的名次）。"""
    rrf_scores: dict[str, float] = {}
    first_seen: dict[str, Candidate] = {}

    for weight, candidates in ((W_VECTOR, vector_candidates), (W_GRAPH, graph_candidates)):
        for rank, candidate in enumerate(candidates, start=1):
            rrf_scores[candidate.chunk_id] = rrf_scores.get(candidate.chunk_id, 0.0) + weight / (
                RRF_K + rank
            )
            first_seen.setdefault(candidate.chunk_id, candidate)

    ranked_ids = sorted(rrf_scores, key=lambda chunk_id: -rrf_scores[chunk_id])
    return [first_seen[chunk_id] for chunk_id in ranked_ids]
