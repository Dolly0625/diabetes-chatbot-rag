"""在 29 筆可檢索三元組上運作的記憶體內圖譜檢索器。用一個 dict，不用
Neo4j——見 CLAUDE.md §10（「29 條三元組不需要圖資料庫」）。

實體串接是依正規化後的 label，而非 id：同一個概念（例如皮膚澱粉樣變性症）
在上游每一筆三元組裡都會有不同的 `id`（Preprocessing B 是逐三元組指派 id），
所以用 id 相等永遠連不起兩筆三元組。在這個規模下，用字串比對 label 是
CLAUDE.md §10 明確認可的做法（「不做實體解析／UMLS，字串比對足以應付
29–60 條三元組」）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..loaders import GraphTripleRecord
from .base import Candidate

# Boundary B §1.2：預設的遍歷跳數預算。IS_A 是結構性階層邊，不消耗跳數
# 預算（詳見 Boundary - B（校對版）.md §1.2 的對應討論）——目前 29 筆三元組
# 裡沒有任何 IS_A 實例，但保留這個「成本為 0」的處理方式，是為了讓未來
# 出現 IS_A 三元組時，不會悄悄破壞類別層級的安全查詢。
HOP_BUDGET = 2

_TRAILING_PUNCT_RE = re.compile(r"[。，,.\s]+$")


def _normalise_label(label: str) -> str:
    return _TRAILING_PUNCT_RE.sub("", label.strip()).casefold()


@dataclass
class GraphSearchResult:
    candidates: list[Candidate]
    graph_path_status: str  # COMPLETE | PARTIAL | NOT_APPLICABLE
    hop_limit_reached: bool = False


class GraphRetriever:
    def __init__(self, triples: list[GraphTripleRecord]):
        self._triples = triples
        self._labels: list[tuple[str, str]] = [
            (_normalise_label(t.subject.label), _normalise_label(t.object.label))
            for t in triples
        ]

    @staticmethod
    def _label_matches(label_cf: str, query_terms: list[str], query_text_cf: str) -> bool:
        """雙向子字串比對：正規化後的 label 出現在查詢文字裡，或查詢的
        某個詞出現在較長的 label 裡都算命中。不做實體解析／UMLS——
        CLAUDE.md §10。"""
        if not label_cf:
            return False
        if label_cf in query_text_cf:
            return True
        return any(len(term) >= 2 and term in label_cf for term in query_terms)

    def _match_count(self, idx: int, query_terms: list[str], query_text_cf: str) -> int:
        """回傳 0、1 或 2：查詢命中了 {subject, object} 中的幾個。若一筆
        三元組的 subject *和* object 都出現在查詢裡，代表這是比「只共用
        subject」更精確的命中（例如藥名同時出現在同一藥物、不相關的 TREATS
        三元組裡）——用這個數字避免信心相近或更高的一般性事實，蓋過
        真正精確命中的那一筆。"""
        subj_cf, obj_cf = self._labels[idx]
        return sum(
            1
            for label_cf in (subj_cf, obj_cf)
            if self._label_matches(label_cf, query_terms, query_text_cf)
        )

    def search(self, queries: list[str], top_k: int = 10) -> GraphSearchResult:
        query_text_cf = " ".join(queries).casefold()
        query_terms = query_text_cf.split()

        match_count_by_idx = {
            i: count
            for i in range(len(self._triples))
            if (count := self._match_count(i, query_terms, query_text_cf)) > 0
        }
        seed_idxs = list(match_count_by_idx)
        if not seed_idxs:
            return GraphSearchResult(candidates=[], graph_path_status="NOT_APPLICABLE")

        # dist：正規化後的 label -> 距離最近種子實體的跳數
        dist: dict[str, int] = {}
        for i in seed_idxs:
            subj_l, obj_l = self._labels[i]
            dist[subj_l] = 0
            dist[obj_l] = 0

        hop_of_triple: dict[int, int] = {i: 0 for i in seed_idxs}
        hop_limit_reached = False

        changed = True
        while changed:
            changed = False
            for i, triple in enumerate(self._triples):
                subj_l, obj_l = self._labels[i]
                cost = 0 if triple.relation == "IS_A" else 1
                for a, b in ((subj_l, obj_l), (obj_l, subj_l)):
                    if a not in dist:
                        continue
                    new_dist = dist[a] + cost
                    if new_dist > HOP_BUDGET:
                        hop_limit_reached = True
                        continue
                    if b not in dist or new_dist < dist[b]:
                        dist[b] = new_dist
                        changed = True
                    triple_hop = min(dist[subj_l], dist[obj_l])
                    if i not in hop_of_triple or triple_hop < hop_of_triple[i]:
                        hop_of_triple[i] = triple_hop

        candidates = [
            self._to_candidate(i, hop_of_triple[i], match_count_by_idx.get(i, 0))
            for i in sorted(hop_of_triple)
        ]
        candidates.sort(key=lambda c: -c.score)
        candidates = candidates[:top_k]

        graph_path_status = "PARTIAL" if hop_limit_reached else "COMPLETE"
        return GraphSearchResult(
            candidates=candidates,
            graph_path_status=graph_path_status,
            hop_limit_reached=hop_limit_reached,
        )

    def _to_candidate(self, idx: int, hop: int, match_count: int = 0) -> Candidate:
        t = self._triples[idx]
        confidence = t.confidence if t.confidence is not None else 0.5
        # 直接命中的種子（hop 0）會依「多命中一端」加成，讓兩端都命中
        # （subject *和* object 都符合查詢）的三元組，即使信心相近，也能
        # 蓋過只命中一端的——詳見 _match_count。經跳數展開而來（非種子）
        # 的三元組不加成。
        specificity = 2 ** max(0, match_count - 1) if hop == 0 else 1
        score = max(0.0, min(1.0, confidence * specificity * (0.9**hop)))
        entity = lambda e: {"id": e.id, "type": e.type, "label": e.label, "code": e.code}
        relation = {
            "subject": t.subject.id,
            "subject_type": t.subject_type,
            "relation": t.relation,
            "object": t.object.id,
            "object_type": t.object_type,
            "condition": t.condition,
            "effect": t.effect,
            "confidence": t.confidence,
            "negation_checked": t.negation_checked,
            "additional_sources": t.additional_sources,
        }
        return Candidate(
            chunk_id=t.chunk_id,
            source=t.source,
            version=t.version,
            date=t.date,
            status=t.status,
            content=t.content,
            retriever="graph",
            score=score,
            score_type="graph_traversal",
            entities=[entity(t.subject), entity(t.object)],
            relations=[relation],
            relation_type=t.relation,
            confidence=t.confidence,
            negation_checked=t.negation_checked,
        )
