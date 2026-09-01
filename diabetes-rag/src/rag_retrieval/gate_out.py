"""各軌道各自的信心門檻、節點／關係 allow-list，以及 top-N 截斷。
依 CLAUDE.md §7 的分工：真正的信心門檻校準值由 Boundary B 負責；本模組
出貨的是「即使 Boundary B 一直沒交付，也依然正確」的寬鬆預設值。

執行順序：門檻與 allow-list（逐 chunk）在 RRF 融合**之前**跑，截斷在融合
**之後**跑——詳見 tool.py 記載的固定管線順序。
"""

from __future__ import annotations

from typing import Optional

from .contract.enums import EntityType, RelationType, WarningCode
from .retrievers.base import Candidate

# Boundary B（校對版）.md §1.1 把真正的信心門檻留成
# 「[TBD-需 8/24 M1 會議確認]」——一直沒定案。依模組分工表（CLAUDE.md §7），
# 這裡是 Boundary B 校準值一直沒送到時出貨的寬鬆預設值。目前 29 筆三元組
# 的信心值都落在 0.9–0.95，所以在真實資料上很少會被這條門檻擋下；存在的
# 意義是讓這個機制變成真正的程式碼，而不是只停留在文件上（CLAUDE.md
# §2.2 的 T6）。
DEFAULT_GRAPH_CONFIDENCE_THRESHOLD = 0.5

# CONTRACT_v1 自己的範例（02_empty.json）用 0.70 作為 similarity 軌道的
# 門檻（「3 筆候選皆低於 similarity 門檻 0.70」）——這是團隊已經拿給 LLM
# 組看過的唯一具體數字，所以這裡沿用它，而不是自己發明一個不相干的預設值。
# 真正的校準仍是 Boundary B 的工作（§1.1 的第一輪寬鬆過濾）。
DEFAULT_VECTOR_SIMILARITY_THRESHOLD = 0.70

DEFAULT_TOP_N = 5

_ALLOWED_ENTITY_TYPES = {member.value for member in EntityType}
_ALLOWED_RELATION_TYPES = {member.value for member in RelationType}

# Boundary B §1.1 也規定，CONTRAINDICATED_FOR／CAUTION_FOR／INDUCES 若
# 未通過 `negation_checked` 檢查就不該放行。但目前可檢索集合裡的每一筆
# 三元組 negation_checked 都是 False（上游的否定詞檢查其實還沒真的跑），
# 若在這裡強制執行，會悄悄把 9/3 demo 依賴的 CAUTION_FOR／INDUCES 證據全
# 清空。CLAUDE.md 的建置順序與不可退讓事項都沒有要求做這個檢查，因此
# 刻意留成 TODO，等真正跑過否定詞檢查的人來處理，而不是預設「全部擋下」。
# `negation_checked` 仍會透過 chunk 的 `relations[]` 往下傳，讓 LLM 組的
# Context Gate 看得到。


def filter_by_confidence(
    candidates: list[Candidate],
    threshold: float = DEFAULT_GRAPH_CONFIDENCE_THRESHOLD,
) -> tuple[list[Candidate], bool]:
    """捨棄抽取信心低於門檻的 graph 候選。vector 候選沒有抽取信心，
    原樣放行。回傳 (kept, any_dropped)。"""
    kept: list[Candidate] = []
    dropped = False
    for candidate in candidates:
        if (
            candidate.retriever == "graph"
            and candidate.confidence is not None
            and candidate.confidence < threshold
        ):
            dropped = True
            continue
        kept.append(candidate)
    return kept, dropped


def filter_by_similarity(
    candidates: list[Candidate],
    threshold: float = DEFAULT_VECTOR_SIMILARITY_THRESHOLD,
) -> tuple[list[Candidate], bool]:
    """捨棄相似度低於門檻的 vector 候選——這是 Boundary B §1.1 所說「依
    score_type 分別設定門檻」的結構性第一輪過濾。非 similarity 候選
    （graph）原樣放行。回傳 (kept, any_dropped)。"""
    kept: list[Candidate] = []
    dropped = False
    for candidate in candidates:
        if candidate.score_type == "similarity" and candidate.score < threshold:
            dropped = True
            continue
        kept.append(candidate)
    return kept, dropped


def filter_by_allow_list(candidates: list[Candidate]) -> tuple[list[Candidate], bool]:
    """Boundary B §1.3／§1.4：只有 schema v3 的節點與關係型別可以傳到
    呼叫端。目前 6 種實體型別與 10 種關係型別全部都在 allow-list 上，
    所以這是防範未來出現壞資料／未預期型別的防護底線，不是對現有資料
    的主動過濾。"""
    kept: list[Candidate] = []
    dropped = False
    for candidate in candidates:
        if candidate.retriever != "graph":
            kept.append(candidate)
            continue
        if candidate.relation_type is not None and candidate.relation_type not in _ALLOWED_RELATION_TYPES:
            dropped = True
            continue
        if candidate.entities and any(
            e.get("type") not in _ALLOWED_ENTITY_TYPES for e in candidate.entities
        ):
            dropped = True
            continue
        kept.append(candidate)
    return kept, dropped


def gate_out_per_track(
    candidates: list[Candidate],
    confidence_threshold: float = DEFAULT_GRAPH_CONFIDENCE_THRESHOLD,
    similarity_threshold: float = DEFAULT_VECTOR_SIMILARITY_THRESHOLD,
) -> tuple[list[Candidate], list[WarningCode]]:
    """對單一檢索軌道執行結構性過濾（在融合之前）。"""
    had_candidates = bool(candidates)
    kept, confidence_dropped = filter_by_confidence(candidates, confidence_threshold)
    kept, similarity_dropped = filter_by_similarity(kept, similarity_threshold)
    kept, allow_list_dropped = filter_by_allow_list(kept)

    warnings: list[WarningCode] = []
    if confidence_dropped:
        warnings.append(WarningCode.LOW_CONFIDENCE_EVIDENCE_DROPPED)
    if had_candidates and not kept:
        warnings.append(WarningCode.EMPTY_AFTER_THRESHOLD_FILTER)
    return kept, warnings


def truncate(candidates: list[Candidate], top_n: int = DEFAULT_TOP_N) -> list[Candidate]:
    """RRF 融合後執行一次的 top-N 截斷。"""
    return candidates[:top_n]


def truncate_balanced(fused: list[Candidate], top_n: int = DEFAULT_TOP_N) -> list[Candidate]:
    """Top-N 截斷，但保證 `fused` 裡出現過的每個軌道至少保留一個名額，
    其餘名額才依純 RRF 順序分配。

    若沒有這個保證，一個廣泛命中許多低價值圖譜事實的查詢（例如純飲食
    問題也命中了好幾筆關於「糖尿病」的一般性 TREATS 三元組），可能單純
    因為 w_graph > w_vector 就把截斷後的名額全部填成 graph 候選——RRF
    是依名次融合、不看分數大小，所以決定結果的會是軌道**規模**的失衡，
    而非真正的相關性。這會悄悄餓死 vector 軌道，而那正是 CLAUDE.md §1
    要求必須「直接回答」的衛教類問題所仰賴的軌道。每軌道一名額的保證
    之外的名額仍依融合順序分配，所以像 metformin/eGFR 這種真正危險的
    圖譜事實，在 hybrid 查詢裡依然會佔優勢。
    """
    if not fused or top_n <= 0:
        return []

    tracks_present = {c.retriever for c in fused}
    if len(tracks_present) <= 1 or top_n <= len(tracks_present):
        return fused[:top_n]

    result: list[Candidate] = []
    seen_ids: set[str] = set()
    for track in tracks_present:
        best = next((c for c in fused if c.retriever == track), None)
        if best is not None:
            result.append(best)
            seen_ids.add(best.chunk_id)

    for candidate in fused:
        if len(result) >= top_n:
            break
        if candidate.chunk_id in seen_ids:
            continue
        result.append(candidate)
        seen_ids.add(candidate.chunk_id)

    fused_rank = {c.chunk_id: i for i, c in enumerate(fused)}
    result.sort(key=lambda c: fused_rank[c.chunk_id])
    return result[:top_n]
