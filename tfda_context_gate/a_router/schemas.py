from __future__ import annotations

# 本模組定義 A 路由器的核心資料契約（Schemas）
# 管線總覽（7 步）：RequestContext → 正規化 → PromptInjectionGuard → RuleBased/LLM 訊號萃取 → merge_signals → policy_gate → AResult
# 關鍵邊界：AResult.rag_allowed 僅當 router_status == G_GENERAL_EDUCATION 時為 True，其餘一律 False
from pydantic import BaseModel, ConfigDict, Field

from .labels import (
    DeclaredRole,
    IntentTag,
    LanguageCode,
    PolicyReasonCode,
    Polarity,
    RiskFlag,
    RouterStatus,
    TargetSubject,
    TimeFrame,
)


class StrictModel(BaseModel):
    """嚴格模式基底模型：禁止額外欄位，確保契約穩定。"""

    model_config = ConfigDict(extra="forbid")  # 禁止未定義欄位，避免下游誤用 多的欄位直接報錯 ValidationError


class RequestContext(StrictModel):
    """A 路由器輸入契約：管線第 1 步，承載原始使用者輸入與身分宣告。"""

    request_id: str = Field(min_length=1)  # 請求唯一識別，用於追蹤與冪等
    schema_version: str = Field(default="a.v0.1", min_length=1)  # 契約版本號，預設 a.v0.1
    user_raw_input: str = Field(min_length=1, max_length=8_000)  # 使用者原始輸入，1~8000 字元
    declared_role: DeclaredRole  # 宣告身分（病患/照護者/醫事人員），僅作參考不作授權依據
    language: LanguageCode = LanguageCode.ZH_TW  # 輸入語系，預設繁中 zh-TW


class ContextModifiers(StrictModel):
    """語境修飾子：描述時間、對象、語氣與語系，供下游細化回應。"""

    time_frame: TimeFrame = TimeFrame.CURRENT  # 時間框架：當前/過去/假設
    target_subject: TargetSubject = TargetSubject.SELF  # 目標對象：本人/家人照護者/第三方
    polarity: Polarity = Polarity.AFFIRMATIVE  # 語氣極性：肯定/否定（影響風險判讀）
    language: LanguageCode = LanguageCode.ZH_TW  # 語系標記，預設繁中


class RouterSignals(StrictModel):
    """Layer 1 輸出：僅為觀測訊號，不含最終路由決策（管線第 4-5 步產物）。
    Layer 1 output: observations only, with no final route field."""

    # intent_tags：意圖標籤（教育/症狀/診斷請求等），由規則或 LLM 萃取
    # risk_flags：風險旗標（急症/用藥個人化/注入攻擊等），觸發政策閘門
    # context_modifiers：語境修飾子，補充時間/對象/語氣資訊

    intent_tags: list[IntentTag] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    context_modifiers: ContextModifiers


class AResult(StrictModel):
    """A 路由器最終輸出：穩定傳遞給下游的唯一契約（管線第 7 步）。
    Stable A-to-downstream payload; `router_status` is the sole route."""

    # router_status：唯一路由欄位，決定下游行為（8 種狀態含 F_ROUTER_DEPENDENCY）
    # reason_codes：政策原因碼，解釋為何路由至此狀態
    # rag_allowed：是否允許 RAG 檢索，僅 G_GENERAL_EDUCATION 為 True（嚴格邊界）

    request_id: str  # 回填原始請求 ID
    schema_version: str  # 回填契約版本
    user_raw_input: str  # 回填原始輸入（供稽核）
    declared_role: DeclaredRole  # 回填宣告身分
    language: LanguageCode  # 回填語系
    intent_tags: list[IntentTag]  # 最終意圖標籤集合
    risk_flags: list[RiskFlag]  # 最終風險旗標集合
    context_modifiers: ContextModifiers  # 最終語境修飾子
    router_status: RouterStatus  # 唯一路由結果（下游僅依此欄位分流）
    reason_codes: list[PolicyReasonCode]  # 路由原因碼（可多個，供解釋與日誌）
    # [工程新增] Explicit downstream guard so callers do not re-implement policy.
    rag_allowed: bool  # 下游 RAG 開關：僅 G_GENERAL_EDUCATION 為 True，其餘皆 False（不可自行重算）
    task_type: str | None = Field(default=None, description="產品任務類型：pre_visit_intake / patient_education / clinician_evidence，僅當 PRE_VISIT_INTAKE 意圖時為 pre_visit_intake")

    @classmethod
    def from_request_and_decision(
        cls,
        request: RequestContext,
        signals: RouterSignals,
        router_status: RouterStatus,
        reason_codes: list[PolicyReasonCode],
    ) -> "AResult":
        """工廠方法：組合 RequestContext + RouterSignals + 政策決策 → AResult。
        輸入：request（原始請求）、signals（觀測訊號）、router_status（路由結果）、reason_codes（原因碼）
        輸出：完整 AResult；rag_allowed 自動依 router_status 判定（僅 G_GENERAL_EDUCATION 為 True）
        """
        task_type = "pre_visit_intake" if IntentTag.PRE_VISIT_INTAKE in signals.intent_tags else None
        return cls(
            request_id=request.request_id,
            schema_version=request.schema_version,
            user_raw_input=request.user_raw_input,
            declared_role=request.declared_role,
            language=request.language,
            intent_tags=signals.intent_tags,
            risk_flags=signals.risk_flags,
            context_modifiers=signals.context_modifiers,
            router_status=router_status,
            reason_codes=reason_codes,
            rag_allowed=router_status is RouterStatus.G_GENERAL_EDUCATION,  # 嚴格邊界：只有一般衛教可 RAG
            task_type=task_type,
        )
