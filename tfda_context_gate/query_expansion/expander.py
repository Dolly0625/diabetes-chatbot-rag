from __future__ import annotations

from typing import Protocol

from .schemas import QueryExpansionInput, QueryExpansionResult


class QueryExpander(Protocol):
    """查詢擴展器協定：任何實作只需提供 expand 方法即可替換。"""

    def expand(self, request: QueryExpansionInput) -> QueryExpansionResult:
        """輸入 QueryExpansionInput，回傳 QueryExpansionResult。"""
        ...


class IdentityQueryExpander:
    """安全的 v0.1 查詢擴展：完整保留使用者原始查詢，不做任何改寫。

    設計理念：
      - original_query 原樣透傳至 retrieval_queries，不增刪、不改寫
      - 避免 LLM 改寫引入語意漂移，確保檢索結果可追溯
      - 策略名稱固定為 identity-deterministic，便於日誌與除錯識別
    """

    name = "identity-deterministic"  # 策略識別名稱

    def expand(self, request: QueryExpansionInput) -> QueryExpansionResult:
        """執行 Identity 擴展：將原始查詢直接作為唯一的檢索查詢。

        參數:
            request: 查詢擴展輸入（含 request_id、original_query 等）
        回傳:
            QueryExpansionResult，其中 retrieval_queries 僅含原始查詢一筆，
            original_query 完整保留，strategy 標為 identity-deterministic
        """
        return QueryExpansionResult(
            request_id=request.request_id,
            original_query=request.original_query,  # 完整保留原始提問
            retrieval_queries=[request.original_query],  # 不改寫，直接沿用原始查詢
            strategy=self.name,
        )
