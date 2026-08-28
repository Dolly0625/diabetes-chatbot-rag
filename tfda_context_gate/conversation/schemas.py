from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tfda_context_gate.access_control import AuthorizationStatus
from tfda_context_gate.clinical_safety import SystemRiskClassification


ConversationRole = Literal["user", "assistant"]
ConversationStage = Literal["stage1", "stage2", "stage3", "review", "submitted"]
CompactionReason = Literal[
    "STAGE_COMPLETED",
    "TOKEN_THRESHOLD_REACHED",
    "RECENT_WINDOW_EXCEEDED",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationTurn(StrictModel):
    """仍可送入模型的單則近期對話；舊對話壓縮後不保留明文。"""

    turn_id: str = Field(min_length=1, max_length=128)
    role: ConversationRole
    content: str = Field(min_length=1, max_length=8_000)


class FactRevision(StrictModel):
    """結構化欄位修訂軌跡，只保留前後值 hash，不複製健康資料。"""

    field_name: str = Field(min_length=1, max_length=80)
    previous_value_hash: str | None = None
    new_value_hash: str = Field(min_length=64, max_length=64)
    source_turn_id: str | None = Field(default=None, max_length=128)


class ClinicalConversationState(StrictModel):
    """不可由對話壓縮改寫的臨床事實來源。

    本模型不自行從自由文字推斷醫療事實。呼叫端只能把已由 A、intake、
    OCR 或使用者確認流程驗證過的欄位更新進來。
    """

    original_query: str | None = Field(default=None, max_length=8_000)
    known_medications: list[str] = Field(default_factory=list, max_length=20)
    allergies: list[str] = Field(default_factory=list, max_length=20)
    chronic_conditions: list[str] = Field(default_factory=list, max_length=20)
    family_history: list[str] = Field(default_factory=list, max_length=20)
    symptom_onset: str | None = Field(default=None, max_length=500)
    symptom_description: str | None = Field(default=None, max_length=2_000)
    reported_severity: str | None = Field(default=None, max_length=500)
    risk_flags: list[str] = Field(default_factory=list, max_length=32)
    system_risk_classification: SystemRiskClassification | None = None
    negated_red_flags: list[str] = Field(default_factory=list, max_length=32)
    questions_for_doctor: list[str] = Field(default_factory=list, max_length=10)
    pending_question: str | None = Field(default=None, max_length=2_000)
    current_stage: ConversationStage | None = None
    authorization_status: AuthorizationStatus = AuthorizationStatus.UNVERIFIED
    completed_stages: list[ConversationStage] = Field(default_factory=list, max_length=5)
    fact_revisions: list[FactRevision] = Field(default_factory=list, max_length=200)


class CompactionPolicy(StrictModel):
    """上下文壓縮的系統持有設定；不得由使用者或 Planner 修改。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_context_tokens: int = Field(default=8_192, ge=256)
    compact_at_ratio: float = Field(default=0.60, gt=0.0, le=1.0)
    recent_exchanges: int = Field(default=4, ge=1, le=20)

    @property
    def token_threshold(self) -> int:
        return max(1, int(self.max_context_tokens * self.compact_at_ratio))


class CompactionDecision(StrictModel):
    should_compact: bool
    reasons: list[CompactionReason] = Field(default_factory=list)
    estimated_tokens: int = Field(ge=0)
    token_threshold: int = Field(ge=1)
    exchange_count: int = Field(ge=0)


class ConversationContext(StrictModel):
    """可持久化的產品對話上下文，不等同單次 LangGraph WorkflowState。"""

    session_id: str = Field(min_length=1, max_length=128)
    clinical_state: ClinicalConversationState = Field(default_factory=ClinicalConversationState)
    recent_turns: list[ConversationTurn] = Field(default_factory=list, max_length=200)
    compacted_turn_hashes: list[str] = Field(default_factory=list, max_length=5_000)
    compacted_turn_count: int = Field(default=0, ge=0)
    compaction_count: int = Field(default=0, ge=0)
    last_compaction_reasons: list[CompactionReason] = Field(default_factory=list)
