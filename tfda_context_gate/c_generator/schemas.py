"""
tfda_context_gate.c_generator.schemas — C 層結構化輸出契約（v1 / v2）

【v1 vs v2 差異對照表】
┌──────────────┬──────────────────────────────┬──────────────────────────────────┐
│ 維度         │ v1（EvidenceAwareAnswer）    │ v2（EvidenceAwareV2Answer）      │
├──────────────┼──────────────────────────────┼──────────────────────────────────┤
│ decision 三態│ 2 態：ANSWER / INSUFFICIENT  │ 3 態：ANSWER / PARTIAL / INSUFFICIENT │
│              │ 無 PARTIAL                  │ 新增 PARTIAL（部分可答、部分缺口）│
│ 主張欄位     │ claims: list[EvidenceClaim]  │ supported_claims: list[V2SupportedClaim] │
│ 缺口欄位     │ 無 unsupported_requests      │ unsupported_requests: list[V2UnsupportedRequest] │
│ 限制說明     │ limitations                  │ limitations（沿用）              │
│ 評估模型     │ AuxiliaryEvaluation（2 態）  │ V2AuxiliaryEvaluation（3 態，新增 partial_answer_correct / over_refusal）│
└──────────────┴──────────────────────────────┴──────────────────────────────────┘

【證據引用核心規則】
- V2SupportedClaim.evidence_ids 必須來自 B-approved 清單（B Context Gate 核准的 evidence_id），
  不可自創 ID、不可把 evidence_id 塞進 claim_id。
- claim_id 僅為短標籤（如 c1、claim_1），與 evidence_id 嚴格分離。
- decision 三態語意：
  ANSWER＝主要要求皆有足夠文件支持；
  PARTIAL＝至少一部分有支持、另一部分無支持；
  INSUFFICIENT＝核心要求完全無直接文件支持。

【輔助評估差異】
- v1 AuxiliaryEvaluation：decision 2 態，無 partial_answer_correct / over_refusal。
- v2 V2AuxiliaryEvaluation：decision 3 態，新增 partial_answer_correct（部分回答是否正確）
  與 over_refusal（是否過度拒答）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceClaim(BaseModel):
    """v1 單一事實主張（對應 EvidenceAwareAnswer.claims）。

    v1 專用：evidence_ids 可為空；v2 請改用 V2SupportedClaim（必填 evidence_ids 且須為 B-approved）。
    """

    claim_id: str = Field(description="Short stable identifier such as claim_1")
    claim: str = Field(description="One factual claim made in the answer")
    evidence_ids: list[str] = Field(default_factory=list)  # v1 允許空；v2 則必填且須為 B-approved


class EvidenceAwareAnswer(BaseModel):
    """v1 證據感知回答契約（僅 2 態決策）。

    decision 僅 ANSWER / INSUFFICIENT，無 PARTIAL；
    重要事實拆於 claims，缺口僅能以 limitations 文字描述，無結構化 unsupported_requests。
    """

    decision: Literal["ANSWER", "INSUFFICIENT"]  # v1 僅 2 態，無 PARTIAL
    answer: str  # 繁中精簡回答本體
    claims: list[EvidenceClaim] = Field(default_factory=list)  # v1 主張清單
    limitations: list[str] = Field(default_factory=list)  # 質性限制／缺口文字


class V2SupportedClaim(BaseModel):
    """v2 已支持主張（evidence_ids 必填且必須來自 B-approved）。

    關鍵規則：evidence_ids 只能填 B Context Gate 核准的 evidence_id，不可自創；
    claim_id 僅為 c1/c2 等短標籤，不可填入 evidence_id。
    """

    claim_id: str = Field(description="Short stable identifier only, such as c1 or claim_1; never put an evidence ID here")
    claim: str = Field(description="A factual statement supported by the supplied context")
    evidence_ids: list[str] = Field(description="One or more approved evidence IDs that support this claim")  # 必須來自 B-approved，不可自創


class V2UnsupportedRequest(BaseModel):
    """v2 未支持要求（文件無法回答的部分）。

    與 supported_claims 互補：有證據的放 supported_claims，無證據的放此處並說明 reason。
    """

    request: str = Field(description="A requested part that the supplied context cannot answer")
    reason: str = Field(default="", description="Why the supplied context cannot answer this request")  # 缺口原因，一句短句


class EvidenceAwareV2Answer(BaseModel):
    """v2 證據感知回答契約（3 態決策：ANSWER / PARTIAL / INSUFFICIENT）。

    相較 v1 的核心差異：
    - decision 新增 PARTIAL（部分可答、部分缺口，不可因一半缺資料就整題 INSUFFICIENT）；
    - claims 更名為 supported_claims，型別改為 V2SupportedClaim（evidence_ids 必填且須為 B-approved）；
    - 新增 unsupported_requests 結構化缺口清單；
    - limitations 保留，用於日期／衝突／範圍等補充限制。
    """

    decision: Literal["ANSWER", "PARTIAL", "INSUFFICIENT"]  # v2 三態：ANSWER=皆支持 / PARTIAL=部分支持 / INSUFFICIENT=核心皆無支持
    answer: str  # 繁中精簡回答，需呼應 decision 與兩類清單
    supported_claims: list[V2SupportedClaim] = Field(default_factory=list)  # 有 B-approved 證據支持的主張
    unsupported_requests: list[V2UnsupportedRequest] = Field(default_factory=list)  # 無證據支持的要求與原因
    limitations: list[str] = Field(default_factory=list)  # 補充限制（日期、衝突、範圍等）

    @field_validator("limitations", mode="before")
    @classmethod
    def _coerce_limitations(cls, v: Any) -> list[str]:
        """自動矯正 limitations 型別：str→[str], None→[]（修 P4 C ValidationError 主因）。"""
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v else []
        return v  # type: ignore[return-value]


class AuxiliaryEvaluation(BaseModel):
    """v1 輔助評估結果（僅 2 態 decision，無 PARTIAL 相關欄位）。

    用於評估 v1 Generator 輸出；v2 請改用 V2AuxiliaryEvaluation。
    """

    decision: Literal["ANSWER", "INSUFFICIENT"]  # v1 僅 2 態
    supported_claim_count: int = Field(ge=0)  # 被 context 明確支持的重要主張數
    partially_supported_claim_count: int = Field(ge=0)  # 部分支持的主張數
    unsupported_claim_count: int = Field(ge=0)  # 不被支持的主張數
    important_claim_count: int = Field(ge=0)  # 重要主張總數
    insufficient_handling_correct: bool  # 對 insufficient 情境是否正確拒答
    reason_codes: list[str] = Field(default_factory=list)  # 評估原因代碼


class V2AuxiliaryEvaluation(BaseModel):
    """v2 輔助評估結果（3 態 decision，新增 PARTIAL 專屬欄位）。

    相較 v1 新增：
    - decision 支援 PARTIAL；
    - partial_answer_correct：是否正確做到「答有證據部分＋指出缺口」；
    - over_refusal：是否過度拒答（明明有部分支持卻判 INSUFFICIENT）。
    """

    decision: Literal["ANSWER", "PARTIAL", "INSUFFICIENT"]  # v2 三態
    supported_claim_count: int = Field(ge=0)  # 明確支持的主張數
    partially_supported_claim_count: int = Field(ge=0)  # 部分支持的主張數
    unsupported_claim_count: int = Field(ge=0)  # 不被支持的主張數
    important_claim_count: int = Field(ge=0)  # 重要主張總數
    partial_answer_correct: bool  # 是否正確處理 PARTIAL（答有證據部分＋標缺口）
    over_refusal: bool  # 是否過度拒答（有支持卻判 INSUFFICIENT）
    insufficient_handling_correct: bool  # insufficient 情境處理是否正確
    reason_codes: list[str] = Field(default_factory=list)  # 評估原因代碼


# ── Clinician Evidence Draft (p6.3 醫護證據工作流程) ──────────────────────────
# 與 EvidenceAwareV2Answer 共用同一 A-E 骨幹，但呈現層為專業草稿：
# - 保留來源/日期/版本/分數於 source_table（5 列對照，含 evidence_id/source/date/version/score）
# - 衝突保留於 conflicts，不自行仲裁
# - 需人工確認，disclaimer 明示非自動處方/診斷
# - 詳細版 4 段結構（answer 格式化文本，非 JSON only）：
#   一、基本資料：已知用藥/過敏史/慢性病史/家族史（僅整理已提供事實，不推定）
#   二、時間軸：症狀起始/描述/程度，按時間排序，含 onset/description/severity
#   三、安全訊號與限制：僅陳述已定義文字訊號，未命中不等於排除急症
#   四、待確認：藥袋提醒（攜帶藥袋核對）、待確認藥品標記、建議攜帶項目
# - answer 長度 300-400 字（中文字符），專業但易懂，詳細但不超過 800 字，含免責聲明
# - 禁止幻覺診斷：僅整理事實與證據，不作「確診為」等個人化診斷


class StrictModel(BaseModel):
    """嚴格模式基底：禁止額外欄位，確保契約穩定。"""

    model_config = ConfigDict(extra="forbid")


class ClinicianSourceRow(StrictModel):
    """source_table 單列：對應一筆 B-approved evidence 的溯源資訊。"""

    evidence_id: str = Field(min_length=1, description="B-approved evidence_id")
    source: str | None = Field(default=None, description="來源標註，如 TFDA / 指引名稱")
    date: str | None = Field(default=None, description="發布日期，保留原始字串")
    version: str | None = Field(default=None, description="版本號")
    score: float | None = Field(default=None, description="檢索分數")


class ClinicianEvidenceDraft(StrictModel):
    """醫護證據草稿契約（p6.3 詳細版）。

    與 EvidenceAwareV2Answer 的差異：
    - decision 僅 CLINICIAN_DRAFT / INSUFFICIENT（專業草稿，不含 ANSWER/PARTIAL 衛教語意）
    - evidence_summary 取代 supported_claims，語意同為 V2SupportedClaim 但呈現為專業摘要
    - conflicts 獨立欄位，保留證據間衝突，不自行選邊
    - source_table 必填，逐筆列出 evidence_id / source / date / version / score（詳細版 5 列對照）
    - disclaimer 必填，標示待人工確認、非自動處方/診斷
    - request_id 用於追溯與 E 觀測關聯
    - 詳細版 answer 為格式化文本（非 JSON only），含 4 段結構：
      一、基本資料：已知用藥/過敏史/慢性病史/家族史（僅整理已提供事實）
      二、時間軸：症狀起始/描述/程度，按時間排序
      三、安全訊號與限制：不得宣稱未命中有限規則即已排除急症
      四、待確認：藥袋提醒與待確認項目
      另附來源對照表（5 列）與免責聲明；全文 300-400 字，專業但易懂，不超過 800 字，禁止幻覺診斷
    """

    request_id: str = Field(min_length=1, description="請求唯一識別，對應 B/C 的 request_id")
    decision: Literal["CLINICIAN_DRAFT", "INSUFFICIENT"] = Field(description="CLINICIAN_DRAFT=有證據草稿 / INSUFFICIENT=核心無支持")
    answer: str = Field(min_length=1, description="專業草稿本文，保留來源日期與衝突說明")
    evidence_summary: list[V2SupportedClaim] = Field(default_factory=list, description="已支持的專業摘要主張，evidence_ids 必須來自 B-approved")
    conflicts: list[str] = Field(default_factory=list, description="證據間衝突描述，保留不仲裁")
    limitations: list[str] = Field(default_factory=list, description="限制說明（日期、範圍、未解問題等）")
    source_table: list[ClinicianSourceRow] = Field(default_factory=list, description="來源對照表，每列對應一筆 B-approved evidence")
    disclaimer: str = Field(min_length=1, description="待人工確認聲明，禁止自動處方/診斷")
