"""C workflow input — 正規 workflow 輸入契約與 B→C 轉接

本檔為 workflow_adapter 拆分後的核心契約層：
- CWorkflowInput 7 欄（extra="forbid", min_length=1 約束完整保留）
- C_V2_SCHEMA_VERSION
- c_input_from_b_result
- to_legacy_v2_case
- CGenerator Protocol（供 deterministic / langchain 共用）
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field

from tfda_context_gate.b_context_gate.schemas import CanonicalBResult, CanonicalEvidence


C_V2_SCHEMA_VERSION = "c.v2"  # C v2 正規輸入的 schema 版本標籤


class CWorkflowInput(BaseModel):
    """正規 workflow 輸入（進入 C v2 runner 的唯一形狀）。

    7 欄位說明（詳細版新增 intake）：
    1. request_id：請求唯一識別，對應 B 層 request_id
    2. schema_version：固定為 c.v2，用於契約版本控管
    3. original_query：使用者原始提問
    4. b_decision：B Context Gate 的決策結果
    5. approved_evidence_ids：B 核准的 evidence_id 清單（V2SupportedClaim.evidence_ids 必須來自此）
    6. evidence：候選證據內容清單（CanonicalEvidence 陣列）
    7. intake：可選的 PreVisitIntake 快照（用於詳細版 4 段結構：基本資料/時間軸/待確認），僅整理已提供事實
    """

    model_config = ConfigDict(extra="forbid")  # 禁止額外欄位，確保契約嚴格

    request_id: str = Field(min_length=1)  # 欄位1：請求 ID，不可為空
    schema_version: str = Field(default=C_V2_SCHEMA_VERSION, min_length=1)  # 欄位2：契約版本，預設 c.v2
    original_query: str = Field(min_length=1)  # 欄位3：原始提問
    b_decision: str = Field(min_length=1)  # 欄位4：B 層決策
    approved_evidence_ids: list[str] = Field(default_factory=list)  # 欄位5：B 核准的 evidence_id 清單
    evidence: list[CanonicalEvidence] = Field(default_factory=list)  # 欄位6：證據內容清單
    intake: Any | None = Field(default=None, description="可選 intake 快照，用於詳細版基本資料/時間軸/待確認")  # 欄位7：intake 快照


class CGenerator(Protocol):
    """C 生成器協定（兩種實作皆須遵守）。

    任何 C v2 生成器皆須實作 generate(CWorkflowInput) -> EvidenceAwareV2Answer。
    臨床草稿生成器回傳 ClinicianEvidenceDraft，亦符合此協定（透過 Union）。
    串流協定：若支援 stream，則以 Iterator[str] 逐塊輸出 answer 欄位，完整物件仍需經 D 驗證後才對外串流。
    """

    def generate(self, request: CWorkflowInput) -> Union[Any, Any]:
        """依正規輸入產生 v2 結構化回答或臨床草稿。"""
        ...

    def stream(
        self, request: CWorkflowInput, *, chunk_size: int = 20
    ) -> Iterator[str]:  # pragma: no cover - protocol only
        """串流輸出 answer 欄位（可選）；未實作時由呼叫方退回 generate 後切塊。"""
        ...


def c_input_from_b_result(
    b_result: CanonicalBResult, *, original_query: str, intake: Any | None = None
) -> CWorkflowInput:
    """從 B 層正規結果轉為 C 層正規輸入（B→C 轉接第一步）。

    流程：CanonicalBResult（含 request_id / decision / approved_evidence_ids / evidence）
    → CWorkflowInput（補上 original_query、schema_version 與可選 intake）。
    intake 用於詳細版 4 段結構（基本資料/時間軸/待確認），僅整理已提供事實，不推定。
    """
    return CWorkflowInput(
        request_id=b_result.request_id,  # 沿用 B 層 request_id
        original_query=original_query,  # 補上原始提問（B 結果本身不含 query）
        b_decision=b_result.decision,  # 沿用 B 層決策
        approved_evidence_ids=b_result.approved_evidence_ids,  # 沿用 B 核准清單（v2 引用唯一來源）
        evidence=b_result.evidence,  # 沿用證據內容
        intake=intake,  # 可選 intake 快照（詳細版用）
    )


def to_legacy_v2_case(request: CWorkflowInput) -> dict[str, Any]:
    """在 live-chain 邊界將正規輸入轉為舊實驗 prompt 形狀（僅此處做轉換）。

    目的：讓既有 C v2 實驗的 prompt 函式（evidence_aware_v2_user_prompt）可直接沿用，
    不需改動實驗既有邏輯。詳細版會額外帶入 intake 供 clinician_draft_user_prompt 使用。
    """

    base: dict[str, Any] = {
        "case_id": request.request_id,  # 轉為舊欄位名 case_id
        "case_type": "workflow_baseline",  # workflow 固定類型
        "query": request.original_query,  # 轉為舊欄位名 query
        "b_decision": request.b_decision,  # 直傳 B 決策
        "approved_document_ids": list(request.approved_evidence_ids),  # 轉為舊欄位名 approved_document_ids
        "contexts": [
            {
                "document_id": item.evidence_id,  # 轉為舊欄位名 document_id
                "page_content": item.content,  # 轉為舊欄位名 page_content
                "source": item.source,
                "metadata": item.metadata,
                "score": item.score,
                "發布日期": item.date or "",  # 舊實驗以「發布日期」為鍵
                "version": item.version,
            }
            for item in request.evidence  # 逐筆轉換 evidence → context
        ],
    }
    if request.intake is not None:
        base["intake"] = request.intake
        base["intake_data"] = request.intake
        base["previsit_intake"] = request.intake
    return base
