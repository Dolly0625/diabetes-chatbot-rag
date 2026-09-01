"""intent_tags -> retrieval_route。由 Multi-RAG A 負責（CLAUDE.md §7）；
這裡的預設值——恆為 HYBRID——是該模組沒有交付時出貨的版本，也是刻意選擇
的安全、低風險做法：兩條軌道都跑、交給 fusion 去排序，絕不會像猜錯單一
軌道那樣漏掉證據。
"""

from __future__ import annotations

from .contract.enums import IntentTag, RetrievalRoute


def decide_route(intent_tags: list[IntentTag]) -> RetrievalRoute:
    return RetrievalRoute.HYBRID
