from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol

from tfda_context_gate.b_context_gate.adapters import normalize_evidence_list
from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult

from .schemas import RAGResult


class Retriever(Protocol):
    """檢索器協定：任何實作只需提供 retrieve 方法即可替換（如 Fixture / TFDA 真實檢索）。"""

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        """輸入 QueryExpansionResult，回傳 RAGResult。"""
        ...


def adapt_legacy_retrieval(
    records: list[Any],
    *,
    request_id: str,
    original_query: str,
    retrieval_queries: list[str],
    retrieval_latency_ms: float | None = None,
) -> RAGResult:
    """將 phase 2/3/5 的舊版檢索列標準化為 RAGResult，不改動那些實驗腳本本身。

    參數:
        records: 舊版檢索記錄列表
        request_id: 請求追蹤 ID
        original_query: 使用者原始提問
        retrieval_queries: 檢索查詢列表
        retrieval_latency_ms: 檢索耗時（可選）
    回傳:
        標準化的 RAGResult
    """

    return RAGResult(
        request_id=request_id,
        original_query=original_query,
        retrieval_queries=retrieval_queries,
        evidence=normalize_evidence_list(records),  # 透過多鍵回退標準化每筆記錄
        retrieval_latency_ms=retrieval_latency_ms,
    )


def default_fixture_evidence() -> list[CanonicalEvidence]:
    """回傳離線 E2E demo 用的固定證據（非臨床語料）。

    共 3 筆：
      E1 — fixture_b_approved=True，核准的一般飲食原則
      E2 — fixture_b_approved=True，核准的個人化建議
      E3 — fixture_b_approved=False，未核准的候選資料（用於測試邊界過濾）
    """

    return [
        CanonicalEvidence(
            evidence_id="E1",
            content="一般糖尿病飲食原則包括均衡飲食與控制總熱量。",
            source="fixture",  # 標示為測試固件，非真實 TFDA 資料
            metadata={"fixture_case": "normal", "fixture_b_approved": True},  # B 閘門會核准
        ),
        CanonicalEvidence(
            evidence_id="E2",
            content="飲食安排應依個人狀況與醫療專業人員建議調整。",
            source="fixture",
            metadata={"fixture_case": "normal", "fixture_b_approved": True},  # B 閘門會核准
        ),
        CanonicalEvidence(
            evidence_id="E3",
            content="本筆是檢查 evidence boundary 的未核准候選資料。",
            source="fixture",
            metadata={"fixture_case": "normal", "fixture_b_approved": False},  # B 閘門不會核准，用於測試過濾
        ),
    ]


class FixtureRetriever:
    """離線 RAG 固件檢索器，供確定性工作流程 demo／測試使用。

    特性：
      - 確定性：每次 retrieve 回傳相同的證據列表，不依賴外部服務
      - 保留原始查詢：original_query 與 retrieval_queries 原樣透傳，不做改寫
      - 不宣稱來自真實 phase 腳本的檢索結果
    """

    name = "fixture-retriever"  # 檢索器識別名稱

    def __init__(self, evidence: list[CanonicalEvidence] | None = None) -> None:
        """初始化固件檢索器。

        參數:
            evidence: 自訂證據列表；若為 None 則使用 default_fixture_evidence() 的 3 筆
        """
        self.evidence = list(evidence) if evidence is not None else default_fixture_evidence()

    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        """執行確定性檢索：直接回傳固件證據並計算耗時。

        參數:
            request: 查詢擴展結果（含 request_id、original_query、retrieval_queries）
        回傳:
            RAGResult，evidence 為固件資料的淺拷貝，耗時為實際執行時間
        """
        started = time.perf_counter()
        # 固件保持檢索確定性，並完整保留原始查詢；不宣稱來自線上 phase 腳本的結果
        return RAGResult(
            request_id=request.request_id,
            original_query=request.original_query,  # 原始提問原樣透傳
            retrieval_queries=request.retrieval_queries,  # 檢索查詢原樣透傳
            evidence=list(self.evidence),  # 淺拷貝，避免外部竄改內部狀態
            retrieval_latency_ms=(time.perf_counter() - started) * 1000,
        )
