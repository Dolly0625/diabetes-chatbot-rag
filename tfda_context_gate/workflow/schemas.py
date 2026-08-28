from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ── 工作流程對外契約（繁中註解）────────────────────────────────────────────
# WORKFLOW_SCHEMA_VERSION：對外結果的版本標籤，供追蹤與相容性判斷。
# WorkflowStatus 四態：COMPLETED（完成）、BLOCKED（A 政策阻擋）、
# FALLBACK（B/C/D/系統降級）、NEEDS_CLARIFICATION（Agent 要求補充資訊）。
WORKFLOW_SCHEMA_VERSION = "workflow.v0.1"
WorkflowStatus = Literal[
    "COMPLETED",
    "BLOCKED",
    "FALLBACK",
    "NEEDS_CLARIFICATION",
    "NEEDS_CONFIRMATION",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowResult(StrictModel):
    """工作流程最終對外結果；由 runner._finish 組裝，彙整 A/B/C/D 與 Agent 軌跡。"""

    request_id: str = Field(min_length=1)
    schema_version: str = Field(default=WORKFLOW_SCHEMA_VERSION, min_length=1)
    status: WorkflowStatus  # 對應 WorkflowStatus 四態
    final_response: str = Field(min_length=1)  # 最終回覆（完成或降級文案）
    fallback_reason: str | None = None  # 降級原因碼（如 B_INSUFFICIENT / D_FALLBACK）
    a_result: dict[str, Any] | None = None  # A 路由結果快照
    query_expansion: dict[str, Any] | None = None  # 查詢擴寫結果
    rag_result: dict[str, Any] | None = None  # RAG 檢索結果
    b_result: dict[str, Any] | None = None  # B 閘門判定結果
    c_result: dict[str, Any] | None = None  # C 生成結果
    d_result: dict[str, Any] | None = None  # D 輸出閘門結果
    agent_action: str | None = None  # 最後一次 Agent 動作（ASK_USER/REWRITE_QUERY/FALLBACK）
    agent_reason_code: str | None = None  # Agent 原因碼（僅供追蹤，非執行信號）
    question: str | None = None  # ASK_USER 時的追問句（build_agent_question 產生）
    current_query: str | None = None  # 當前查詢（可能已被重寫，與 original_query 區分）
    execution_history: list[dict[str, Any]] = Field(default_factory=list)  # previous_attempts 序列化
    agent_steps: int = Field(default=0, ge=0)  # 已消耗 Agent 步數（上限 max_agent_steps=2）
    rewrite_count: int = Field(default=0, ge=0)  # 已重寫次數（上限 max_rewrites=1）
    clarification_count: int = Field(default=0, ge=0)  # 已追問次數（上限 max_clarifications=1）
    termination_reason: str | None = None  # 終止原因（MAX_*_EXCEEDED / NEEDS_CLARIFICATION 等）
    intake_snapshot: dict[str, Any] | None = None  # 唯讀 intake 快照，供 ProductSession 延續
    intake_stage: str | None = None  # stage1/stage2/stage3/review/submitted
    previsit_summary: dict[str, Any] | None = None  # Review & Confirm 摘要快照
    system_risk_classification: dict[str, Any] | None = None  # 明確文字訊號安全分流
    trace: dict[str, Any]  # 完整軌跡快照（e_observability）
