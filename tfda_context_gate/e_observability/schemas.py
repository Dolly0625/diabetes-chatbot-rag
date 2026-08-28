"""E 觀測層結構化綱要（Schemas）— 唯讀觀測，不改答案。

本模組定義 E 層所有持久化紀錄的資料結構，負責「包住 A-E」的可觀測性：
- A（Input Router / Policy Gate）、RAG（檢索）、B（Context Gate）、C（Generator）、D（Output Gate）
  以及可選的 Agent 相關節點，全部以統一的 TraceEvent / EvaluationRecord / MetricsSnapshot 形式落盤。
- E 為純觀測層：只記錄、不介入路由或改寫答案（fail-open 設計見 tracer.py / sinks.py）。

設計要點：
- TraceStatus 8 種狀態完整覆蓋生命週期與分支結果（見下方註解）。
- RequestMetadata 為單次請求的共享中繼資料，所有事件皆攜帶以便關聯。
- StrictModel 禁止額外欄位，確保綱要穩定、可審計。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


E_SCHEMA_VERSION = "e.v0.1"
# 【觀測層狀態機】TraceStatus 8 種取值，對應 A-E 全流程：
# - STARTED: span 進入時先寫一筆起始事件（tracer.TraceSpan.__enter__），用於計算延遲與生命週期完整性
# - COMPLETED: 正常完成（A/B/C/D 成功、RAG 檢索完成、Agent 動作完成）
# - BLOCKED: A 層政策阻擋（prompt_guard / router 判定不可檢索）
# - INSUFFICIENT: B 層證據不足（sufficiency/relevance 不通過，需 fallback 或進 Agent）
# - FALLBACK: 觸發兜底回覆（B 非可恢復、D 校驗失敗、Agent 達上限等）
# - ERROR: 拋例外或依賴失敗（span __exit__ 捕獲例外時寫入）
# - SKIPPED: 節點被跳過（例如條件分支未執行）
# - NEEDS_CLARIFICATION: 需要使用者補充資訊（ASK_USER 節點）
TraceStatus = Literal[
    "STARTED",
    "COMPLETED",
    "BLOCKED",
    "INSUFFICIENT",
    "FALLBACK",
    "ERROR",
    "SKIPPED",
    "NEEDS_CLARIFICATION",
]


def utc_now() -> datetime:
    """回傳當前 UTC 時間（E 層所有 timestamp / started_at / completed_at 的統一時鐘）。"""
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    """嚴格模式基類：禁止額外欄位，確保 E 綱要不被意外污染。"""

    model_config = ConfigDict(extra="forbid")


class RequestMetadata(StrictModel):
    """單次請求的共享中繼資料（Request-level metadata）。

    每個 TraceEvent / EvaluationRecord 都會複製這些欄位，確保：
    - 依 request_id / trace_id / thread_id 可關聯全鏈路
    - original_query 為脫敏後文本（見 privacy.redact_text），原始明文不落盤
    - query_hash 為可逆不可逆的關聯雜湊（見 privacy.hash_text），用於不暴露原文的關聯查詢
    """

    request_id: str = Field(min_length=1)  # 請求唯一 ID，亦作為 trace_id 預設值
    trace_id: str | None = None  # 全鏈路追蹤 ID（通常等於 request_id）
    thread_id: str | None = None  # 會話/對話線程 ID（可選）
    schema_version: str = Field(default=E_SCHEMA_VERSION, min_length=1)  # 綱要版本，利於向前相容
    timestamp: datetime = Field(default_factory=utc_now)  # 請求建立時間（TraceRecorder 初始化時寫入）
    declared_role: str | None = None  # 宣告角色（A 層輸入）
    # This is redacted text when supplied. The original raw text is never
    # required for trace persistence; query_hash supports correlation without
    # storing the raw query in a production sink.
    original_query: str | None = None  # 脫敏後的使用者原始查詢（已過 redact_text）
    query_hash: str | None = None  # 原始查詢的 SHA256 雜湊（可關聯、不可逆）


class TraceEvent(StrictModel):
    """單次元件執行的結構化事件（One structured component execution event）。

    覆蓋 A/RAG/B/C/D 與可選 Agent 欄位，同一綱要包住全流程，利於：
    - workflow/graph.py 中每個節點以 trace.span(component, node_name) 包裝
    - trajectory.py 純展示渲染時按 component 分支顯示

    The optional fields intentionally cover A/RAG/B/C/D and reserve Agent
    fields without requiring an Agent implementation.
    """

    record_type: Literal["trace_event"] = "trace_event"
    request_id: str = Field(min_length=1)
    trace_id: str | None = None
    thread_id: str | None = None
    schema_version: str = Field(default=E_SCHEMA_VERSION, min_length=1)
    timestamp: datetime = Field(default_factory=utc_now)  # 事件寫入時間
    declared_role: str | None = None
    original_query: str | None = None  # 脫敏後文本（同 RequestMetadata）
    query_hash: str | None = None

    component: str = Field(min_length=1)  # 元件名：A / RAG / B / C / D / AGENT / SYSTEM 等
    node_name: str = Field(min_length=1)  # 節點名：input_router / retrieval / context_gate / generator / output_gate 等
    status: TraceStatus  # 8 種狀態之一，見 TraceStatus 註解
    started_at: datetime | None = None  # span 起始時間（TraceSpan 進入時記錄）
    completed_at: datetime | None = None  # span 完成時間（TraceSpan 離開時記錄）
    latency_ms: float | None = Field(default=None, ge=0)  # 延遲毫秒（completed_at - started_at）

    # A: Input Router + Policy Gate
    router_status: str | None = None  # A 路由狀態（例如 PASS / BLOCKED / F_ROUTER_DEPENDENCY）
    intent_tags: list[str] = Field(default_factory=list)  # 意圖標籤
    risk_flags: list[str] = Field(default_factory=list)  # 風險旗標
    reason_codes: list[str] = Field(default_factory=list)  # 原因碼（A/B/D 共用）
    rag_allowed: bool | None = None  # 是否允許進入 RAG
    prompt_guard_result: Any | None = None  # prompt 守衛結果（ALLOWED / BLOCKED）

    # RAG
    retrieval_query: str | None = None  # 實際檢索查詢（可能為重寫後）
    retrieved_count: int | None = Field(default=None, ge=0)  # 檢索命中數
    retrieved_evidence_ids: list[str] = Field(default_factory=list)  # 命中證據 ID 列表
    retrieval_latency_ms: float | None = Field(default=None, ge=0)  # 檢索延遲
    retrieval_attempt: int | None = Field(default=None, ge=1)  # 檢索嘗試次數（Agent 重寫後會遞增）
    retrieved_evidence: list[dict[str, Any]] = Field(default_factory=list)  # 精簡證據摘要（不含原文，僅 id/rank/score/source/date）

    # B: Contract Gate + Context Gate
    decision: str | None = None  # B 決策：PASS / INSUFFICIENT / UNSAFE 等
    outcome: str | None = None  # 通用 outcome（SYSTEM 等亦用）
    approved_evidence_ids: list[str] = Field(default_factory=list)  # B 核准的證據 ID
    approved_evidence_count: int | None = Field(default=None, ge=0)  # 核准數量
    b_attempt: int | None = Field(default=None, ge=1)  # B 評估次數
    relevance: str | None = None  # 相關性評估
    sufficiency: str | None = None  # 充分性評估
    conflict: str | None = None  # 衝突評估
    safety: str | None = None  # 安全性評估

    # C: Evidence-aware Generator
    candidate_decision: str | None = None  # C 候選決策
    claim_count: int | None = Field(default=None, ge=0)  # 聲明數量
    evidence_ids: list[str] = Field(default_factory=list)  # 引用的證據 ID
    presentation_mode: str | None = None  # PATIENT_EDUCATION vs CLINICIAN_DRAFT
    draft_type: str | None = None  # patient_education / clinician_evidence_draft
    source_table_count: int | None = Field(default=None, ge=0)
    conflicts_count: int | None = Field(default=None, ge=0)

    # D: Mandatory Output Gate
    failure_type: str | None = None  # 失敗類型（D 校驗失敗時）
    failed_claims: list[Any] = Field(default_factory=list)  # 未通過的聲明
    invalid_evidence_ids: list[str] = Field(default_factory=list)  # 無效證據 ID
    fallback_reason: str | None = None  # 兜底原因（B_INSUFFICIENT / D_FALLBACK 等）

    # Query rewrite / clarification display fields. These are structured
    # observations only; hidden model reasoning is never recorded.
    current_query: str | None = None  # 重寫前的當前查詢
    rewritten_query: str | None = None  # 重寫後的新查詢
    rewrite_attempt: int | None = Field(default=None, ge=1)  # 重寫嘗試次數
    missing_information: list[str] = Field(default_factory=list)  # 缺失資訊（ASK_USER 用）
    identified_missing_information: list[str] = Field(default_factory=list)  # B 識別的缺失資訊
    planner_context: dict[str, Any] | None = None  # 傳給 Planner 的上下文快照（已脫敏）
    question: str | None = None  # 向使用者提問的文本

    # System/dependency metadata
    model_name: str | None = None  # 模型名稱（Planner / Rewriter）
    token_usage: dict[str, int] | None = None  # token 使用量
    error_type: str | None = None  # 例外類型
    error_message: str | None = None  # 脫敏後的錯誤訊息

    # Agent v0.1 fields: optional so the same E contract covers the baseline.
    agent_action: str | None = None  # 實際執行的 Agent 動作
    requested_action: str | None = None  # Planner 原始請求的動作
    requested_reason_code: str | None = None  # 原始原因碼
    reason_code: str | None = None  # 最終原因碼
    actions_taken: list[str] = Field(default_factory=list)  # 已執行動作序列
    agent_step: int | None = Field(default=None, ge=0)  # Agent 步數
    step_count: int | None = Field(default=None, ge=0)  # 通用步數（同 agent_step）
    retry_count: int | None = Field(default=None, ge=0)  # 重試次數
    rewrite_count: int | None = Field(default=None, ge=0)  # 重寫次數
    clarification_count: int | None = Field(default=None, ge=0)  # 澄清次數
    tool_name: str | None = None  # 工具名稱（若有）
    termination_reason: str | None = None  # 終止原因（MAX_AGENT_STEPS_EXCEEDED 等）

    # Streaming observability (E trace for streaming)
    streaming: bool | None = None
    first_token_latency_ms: float | None = Field(default=None, ge=0)
    first_chunk_latency_ms: float | None = Field(default=None, ge=0)
    total_stream_latency_ms: float | None = Field(default=None, ge=0)
    stream_chunk_count: int | None = Field(default=None, ge=0)
    stream_char_count: int | None = Field(default=None, ge=0)
    stream_token_count: int | None = Field(default=None, ge=0)


class EvaluationRecord(StrictModel):
    """離線/人工評估用的對比紀錄（Evaluation data for later human/offline analysis）。

    與 TraceEvent 分離，專門記錄「預期 vs 實際」決策，用於事後分析與評測，
    不參與線上路由或答案生成。
    """

    record_type: Literal["evaluation"] = "evaluation"
    request_id: str = Field(min_length=1)
    thread_id: str | None = None
    schema_version: str = Field(default=E_SCHEMA_VERSION, min_length=1)
    timestamp: datetime = Field(default_factory=utc_now)
    original_query: str | None = None  # 脫敏後查詢
    query_hash: str | None = None  # 查詢雜湊
    expected_decision: str | None = None  # 預期決策（標註）
    actual_decision: str | None = None  # 實際決策（系統輸出）
    outcome: str | None = None  # 評估結果
    failure_type: str | None = None  # 失敗類型
    reason_codes: list[str] = Field(default_factory=list)  # 原因碼
    metadata: dict[str, Any] = Field(default_factory=dict)  # 額外中繼資料（已脫敏）


class LatencySummary(StrictModel):
    """單一元件的延遲彙總（計數 / 總和 / 平均）。"""

    count: int = Field(default=0, ge=0)  # 有延遲紀錄的事件數
    total_ms: float = Field(default=0, ge=0)  # 總延遲毫秒
    average_ms: float | None = Field(default=None, ge=0)  # 平均延遲


class MetricsSnapshot(StrictModel):
    """行程內輕量指標快照（Small in-process metrics snapshot）。

    由 MetricsCollector 彙整，供外部匯出系統替換或直接寫入 sink。
    """

    record_type: Literal["metrics"] = "metrics"
    request_count: int = Field(default=0, ge=0)  # 請求數
    event_count: int = Field(default=0, ge=0)  # 事件總數
    error_count: int = Field(default=0, ge=0)  # ERROR 狀態數
    fallback_count: int = Field(default=0, ge=0)  # FALLBACK/INSUFFICIENT 數
    blocked_count: int = Field(default=0, ge=0)  # BLOCKED 數
    by_component: dict[str, int] = Field(default_factory=dict)  # 按元件計數
    latency_by_component: dict[str, LatencySummary] = Field(default_factory=dict)  # 按元件延遲彙總
