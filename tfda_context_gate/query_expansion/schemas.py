from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# 查詢擴展層版本號
QUERY_EXPANSION_SCHEMA_VERSION = "query_expansion.v0.1"


class StrictModel(BaseModel):
    """嚴格模型：禁止額外欄位，確保查詢擴展契約穩定。"""

    model_config = ConfigDict(extra="forbid")


class QueryExpansionInput(StrictModel):
    """查詢擴展的輸入，承接 A 層路由結果。

    欄位：
      request_id    — 請求追蹤 ID
      original_query — 使用者原始提問（全程透傳）
      router_status — A 層路由狀態（如 PASS / CLARIFY 等）
      intent_tags   — 意圖標籤列表
      declared_role — 宣告角色（病患／醫護／照護者等，可選）
      language      — 語言標記（可選）
    """

    request_id: str = Field(min_length=1)  # 請求追蹤 ID
    original_query: str = Field(min_length=1)  # 使用者原始提問
    router_status: str = Field(min_length=1)  # A 層路由狀態
    intent_tags: list[str] = Field(default_factory=list)  # 意圖標籤
    declared_role: str | None = None  # 宣告角色
    language: str | None = None  # 語言


class QueryExpansionResult(StrictModel):
    """查詢擴展的輸出，傳遞給 RAG 檢索層。

    欄位：
      request_id        — 請求追蹤 ID
      schema_version    — 契約版本
      original_query    — 使用者原始提問（Identity 策略下與 retrieval_queries 相同）
      retrieval_queries — 實際送檢索的查詢列表（至少 1 筆）
      strategy          — 擴展策略名稱（預設 identity，即不改寫）
    """

    request_id: str = Field(min_length=1)  # 請求追蹤 ID
    schema_version: str = Field(default=QUERY_EXPANSION_SCHEMA_VERSION, min_length=1)  # 契約版本
    original_query: str = Field(min_length=1)  # 使用者原始提問（全程保留）
    retrieval_queries: list[str] = Field(min_length=1)  # 送檢索的查詢列表
    strategy: str = Field(default="identity", min_length=1)  # 擴展策略（identity = 不改寫）
