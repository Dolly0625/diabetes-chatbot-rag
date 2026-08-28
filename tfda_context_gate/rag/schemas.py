from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult


# RAG 層版本號：與 B 層版本獨立追蹤
RAG_SCHEMA_VERSION = "rag.v0.1"


class StrictModel(BaseModel):
    """嚴格模型：禁止額外欄位，確保 RAG 契約不被意外擴張。"""

    model_config = ConfigDict(extra="forbid")


class RAGResult(StrictModel):
    """RAG 檢索的標準輸出，承載從 QueryExpansion 到 B 審查的橋接資料。

    欄位：
      request_id           — 請求追蹤 ID
      schema_version       — RAG 契約版本
      original_query       — 使用者原始提問（全程透傳不改寫）
      retrieval_queries    — 實際送檢索的查詢列表（來自 QueryExpansion）
      evidence             — 檢索到的標準化證據清單
      retrieval_latency_ms — 檢索耗時（毫秒），可為 None
    """

    request_id: str = Field(min_length=1)  # 請求追蹤 ID
    schema_version: str = Field(default=RAG_SCHEMA_VERSION, min_length=1)  # RAG 契約版本
    original_query: str = Field(min_length=1)  # 使用者原始提問（透傳）
    retrieval_queries: list[str] = Field(min_length=1)  # 檢索查詢列表（至少 1 筆）
    evidence: list[CanonicalEvidence] = Field(default_factory=list)  # 檢索證據清單
    retrieval_latency_ms: float | None = Field(default=None, ge=0)  # 檢索耗時（毫秒）


def rag_to_b_input(rag_result: RAGResult):
    """將 RAG 結果轉為 B 審查輸入。

    轉接器刻意放在 RAG 套件內，讓 RAG 套件不直接依賴 B 的實作細節，
    僅透過 CanonicalBInput 進行邊界傳遞。

    參數:
        rag_result: RAG 檢索結果
    回傳:
        對應的 CanonicalBInput，可直接送入 ContextGate.evaluate()
    """

    from tfda_context_gate.b_context_gate.schemas import CanonicalBInput

    return CanonicalBInput(
        request_id=rag_result.request_id,
        original_query=rag_result.original_query,
        retrieval_queries=rag_result.retrieval_queries,
        evidence=rag_result.evidence,
    )
