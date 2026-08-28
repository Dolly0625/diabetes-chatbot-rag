from __future__ import annotations

# ── 有界 Agent 契約（繁中註解）──────────────────────────────────────────────
# 核心不變式：
# - Planner 僅三選一：ASK_USER / REWRITE_QUERY / FALLBACK，不可覆蓋 A/B/C/D、不可批證據、不可選節點
# - AgentDecision 為 discriminated union（以 action 為判別子），由圖驗證後才進入狀態
# - AgentDecisionContext 為窄化輸入，非 WorkflowState 全量，僅含 B 可見的最小資訊

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel


AgentAction = Literal["ASK_USER", "REWRITE_QUERY", "FALLBACK"]  # 三選一動作
AgentReasonCode = Literal[
    "MISSING_REQUIRED_CONTEXT",  # 缺必要上下文（ASK_USER）
    "QUERY_FORMULATION_NEEDS_REWRITE",  # 需重寫查詢（REWRITE_QUERY）
    "RECOVERY_EXHAUSTED",  # 復原已耗盡（FALLBACK）
    "LIMIT_EXCEEDED",  # 超限（圖強制 FALLBACK）
    "PLANNER_FAILURE",  # Planner 失效
    "REWRITER_FAILURE",  # 重寫器失效
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AskUserDecision(StrictModel):
    """ASK_USER：需使用者補充資訊，missing_information 為必要欄位名。"""

    action: Literal["ASK_USER"]
    reason_code: AgentReasonCode
    missing_information: list[str] = Field(min_length=1, max_length=4)  # 對應 B 的 identified_missing_information


class RewriteQueryDecision(StrictModel):
    """REWRITE_QUERY：核心事實已具，僅需重寫查詢以改善檢索。"""

    action: Literal["REWRITE_QUERY"]
    reason_code: AgentReasonCode


class FallbackDecision(StrictModel):
    """FALLBACK：無合理復原或已超限，由圖導向封閉式降級。"""

    action: Literal["FALLBACK"]
    reason_code: AgentReasonCode


AgentDecisionUnion = Union[AskUserDecision, RewriteQueryDecision, FallbackDecision]

AgentDecision = Annotated[
    AgentDecisionUnion,
    Field(discriminator="action"),  # 以 action 判別三選一
]


class AgentDecisionStructuredOutput(RootModel[AgentDecision]):
    """Pydantic root wrapper used by LangChain provider JSON-schema APIs."""

    pass


class EvidenceSummary(StrictModel):
    """Small, deterministic projection of B-visible retrieval metadata.

    【繁中註解】僅含 B 可見的檢索元數據投影（id/rank/score/ingredient/title/source/date/snippet），
    不含原文全文，避免 Planner 將檢索候選誤作使用者事實。
    """

    evidence_id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    score: float | None = None
    ingredient: str | None = None
    title: str | None = None
    source: str | None = None
    date: str | None = None
    snippet: str | None = Field(default=None, max_length=240)  # 截斷至 240 字


class AgentAttempt(StrictModel):
    """System-written history item; the Planner can only read it.

    【繁中註解】系統寫入的歷史嘗試，Planner 唯讀；記錄 query/已完成動作/B 判定/檢索摘要。
    """

    query: str = Field(min_length=1, max_length=8_000)
    completed_agent_action: AgentAction
    b_decision: str = Field(min_length=1)
    b_reason_codes: list[str] = Field(default_factory=list, max_length=8)
    retrieval_outcome: dict[str, object] = Field(default_factory=dict)


class AgentDecisionContext(StrictModel):
    """Narrow Planner input. It is deliberately not WorkflowState.

    【繁中註解｜窄化上下文】僅含 Planner 所需最小資訊，非 WorkflowState 全量：
    original_query/current_query 雙軌、B 判定與原因碼、中性 identified_missing_information、
    受限 retrieval_feedback、至多 5 筆 evidence_summaries、至多 2 筆 previous_attempts。
    """

    original_query: str = Field(min_length=1, max_length=8_000)  # 溯源基準
    current_query: str = Field(min_length=1, max_length=8_000)  # 當前查詢
    b_decision: str = Field(min_length=1)  # B 判定（PASS/INSUFFICIENT/UNSAFE）
    b_reason_codes: list[str] = Field(default_factory=list, max_length=8)
    # Neutral B observation. The Planner interprets it; B never selects an
    # Agent action or emits a control instruction here.
    # 【繁中註解】中性觀察：B 僅描述缺什麼，是否 ASK_USER 由 Planner 判斷，B 不發控制指令
    identified_missing_information: list[str] = Field(default_factory=list, max_length=8)
    retrieval_feedback: dict[str, object] = Field(default_factory=dict)  # 僅保留小欄位（見 context._limited_feedback）
    evidence_summaries: list[EvidenceSummary] = Field(default_factory=list, max_length=5)
    previous_attempts: list[AgentAttempt] = Field(default_factory=list, max_length=2)
