from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ── B 層版本號：用於追蹤契約變更，確保上下游解析一致 ──
B_SCHEMA_VERSION = "b.v0.1"
# B 審查的 5 種決策含義：
#   PASS         — 上下文充足且安全，可放行
#   INSUFFICIENT — 證據不足，無法支撐回答
#   UNSAFE       — 偵測到風險（如重複 ID），必須阻擋
#   REVIEW       — 需人工複審
#   FALLBACK     — 降級處理
BDecision = Literal["PASS", "INSUFFICIENT", "UNSAFE", "REVIEW", "FALLBACK"]


class StrictModel(BaseModel):
    """嚴格模型：禁止額外欄位，避免契約外資料悄悄流入。"""

    model_config = ConfigDict(extra="forbid")


class CanonicalEvidence(StrictModel):
    """B 工作流程邊界上的單筆檢索證據（7 基礎欄 + RAG 對齊擴充）。

    基礎 7 欄（必填/選填）：
      1. evidence_id — 證據唯一識別（必填）
      2. content     — 證據正文（必填）
      3. source      — 來源標註（如 fixture / TFDA 資料集），上游未提供則為 None
      4. metadata    — 額外中繼資料字典，預設空 dict
      5. score       — 檢索相似度分數，未提供則為 None
      6. date        — 發布日期字串，未提供則為 None
      7. version     — 版本號，未提供則為 None

    RAG Kickoff 對齊擴充（4 欄，Optional）：
      8. score_type — 分數類型（如 cosine / reranker / graph_traversal），未提供則 None
      9. status     — 資料狀態（VALID / REVOKED / SUPERSEDED），未提供則 None
      10. retriever — 檢索器類型（Vector / Graph / Hybrid），未提供則 None
      11. chunk_id_alias 僅透過 adapter 多鍵回退寫入 evidence_id，不另存欄位

    風險等級擴充（4 欄，文件「想問 RAG 組 1」）：
      12. evidence_risk_level  — RAG 標註的風險等級 HIGH/MEDIUM/LOW/UNKNOWN
      13. safety_signal_types — 安全訊號類型列表（如 CONTRAINDICATION/CAUTION）
      14. risk_basis          — 風險判定依據（relation/來源段落/路徑描述）
      15. entities/relations  — Graph 專屬：實體與關係列表

    Optional 欄位在上游未提供時保持 ``None`` 或空列表，
    轉接器（adapter）絕不會憑空捏造來源或風險資訊。
    """

    evidence_id: str = Field(min_length=1)  # ① 證據唯一 ID，不可為空字串
    content: str = Field(min_length=1)  # ② 證據正文，不可為空
    source: str | None = None  # ③ 來源標註，未提供則 None（不捏造）
    metadata: dict[str, Any] = Field(default_factory=dict)  # ④ 額外中繼資料
    score: float | None = None  # ⑤ 相似度分數，未提供則 None
    date: str | None = None  # ⑥ 發布日期，未提供則 None
    version: str | None = None  # ⑦ 版本號，未提供則 None
    # ── RAG Kickoff 對齊（全部 Optional，保持向下相容） ──
    score_type: str | None = None  # ⑧ 分數類型，未提供則 None
    status: str | None = None  # ⑨ 資料狀態，未提供則 None
    retriever: str | None = None  # ⑩ 檢索器類型，未提供則 None
    # ── 風險等級（文件 §二-3 想問 RAG 組 1） ──
    evidence_risk_level: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"] | None = None  # ⑫ 風險等級
    safety_signal_types: list[str] = Field(default_factory=list)  # ⑬ 安全訊號類型列表
    risk_basis: str | None = None  # ⑭ 風險判定依據
    # ── Graph 專屬 ──
    entities: list[Any] = Field(default_factory=list)  # ⑮ Graph 實體列表
    relations: list[Any] = Field(default_factory=list)  # ⑮ Graph 關係列表


class CanonicalBInput(StrictModel):
    """送入 B 審查閘門的標準輸入。

    包含請求 ID、原始使用者提問、檢索查詢列表與待審證據清單。
    v0.1 預留 task_type / tool_context 供 v0.2 三流程與工具化使用，
    gate 現階段僅透傳不判讀，確保未來相容而不影響 v0.1 驗收。
    """

    request_id: str = Field(min_length=1)  # 請求追蹤 ID
    schema_version: str = Field(default=B_SCHEMA_VERSION, min_length=1)  # 契約版本
    original_query: str = Field(min_length=1)  # 使用者原始提問（未經改寫）
    retrieval_queries: list[str] = Field(min_length=1)  # 實際送檢索的查詢列表（至少 1 筆）
    evidence: list[CanonicalEvidence] = Field(default_factory=list)  # 待審證據清單
    task_type: str | None = None  # 預留：產品任務類型（patient_education / pre_visit_intake / clinician_evidence），v0.1 不判讀
    tool_context: dict[str, Any] = Field(default_factory=dict)  # 預留：工具上下文（source_id / filters / tool_name / status），v0.1 僅透傳


class CanonicalBResult(StrictModel):
    """B 審查的唯一輸出形狀，正式工作流程僅可見此結構。

    5 種 decision 含義見上方 BDecision 註解。
    審查結果包含：核准的證據 ID、完整證據、原因碼、缺失資訊提示等。
    """

    request_id: str = Field(min_length=1)  # 對應的請求 ID
    schema_version: str = Field(default=B_SCHEMA_VERSION, min_length=1)  # 契約版本
    decision: BDecision  # 5 擇 1 的審查決策（PASS/INSUFFICIENT/UNSAFE/REVIEW/FALLBACK）
    approved_evidence_ids: list[str] = Field(default_factory=list)  # 被核准放行的證據 ID 列表
    evidence: list[CanonicalEvidence] = Field(default_factory=list)  # 完整證據清單（無論是否核准皆保留）
    reason_codes: list[str] = Field(default_factory=list)  # 決策原因碼，供下游追溯
    # 中性觀察欄位：B 僅回報哪些使用者資訊仍未被識別，不主動建議 Agent 下一步動作
    identified_missing_information: list[str] = Field(default_factory=list, max_length=8)  # 缺失資訊提示（最多 8 項）
    retrieval_feedback: dict[str, Any] = Field(default_factory=dict)  # 回饋給檢索層的資訊（如重複 ID、查詢列表）
    relevance: str | None = None  # 相關性評估（RETRIEVED / NONE / UNKNOWN 等）
    sufficiency: str | None = None  # 充足性評估（SUFFICIENT / INSUFFICIENT / UNSAFE 等）
    conflict: str | None = None  # 衝突檢測結果
    safety: str | None = None  # 安全性評估（FIXTURE_APPROVED / DEMO_RETRIEVED_APPROVED / FAIL 等）
