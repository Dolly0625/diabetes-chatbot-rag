"""D 輸出閘門契約定義（Schemas）— 繁體中文註解版

本檔案定義 D（Output Gate）邊界的全部資料契約，邏輯零改動，僅補充中文說明。

【D 的 8 步強制驗證流水線總覽】
  步驟 1 適配（Adapter）：將 A/B/C 異構原始 payload 轉為 OutputGateRequest（見 adapters.build_gate_request）
  步驟 2 A 快照校驗：解析 PolicySnapshot，字串快照比對（見 gate.parse_policy + policy.check_policy_snapshot）
  步驟 3 B 證據集校驗：解析 EvidenceSet，檢查 B 是否 PASS（見 gate.parse_evidence_set）
  步驟 4 C 形狀校驗：解析 CandidateResponse，檢查 ANSWER/PARTIAL/INSUFFICIENT 形狀（見 gate.parse_candidate_response + _validate_candidate_shape）
  步驟 5 B PASS 與 evidence_id 歸屬校驗：missing / not_approved / malformed_approved（見 gate._validate_evidence_ids）
  步驟 6 A 風險紅線：路由非 G_GENERAL_EDUCATION、硬風險、意圖標籤、顯式紅線短語（見 policy.check_policy_snapshot / check_candidate_red_lines）
  步驟 7 棄權（Abstention）分支：supported_claims 為空時直接 PASS，視為安全棄權（見 gate 棄權判斷）
  步驟 8 語意驗證：HeuristicSemanticVerifier 詞彙重疊 0.85 非醫療驗證（見 verifier.HeuristicSemanticVerifier）
  終態：僅輸出 PASS 或 FALLBACK，fail-closed（失敗即降級）

【6 種 failure_type 枚舉】
  NONE       — 無失敗，對應 PASS
  SCHEMA     — 契約/形狀錯誤（A/B/C 任一解析或形狀校驗失敗）
  EVIDENCE   — 證據歸屬錯誤（B 未 PASS、evidence_id 缺失/未授權/孤兒授權）
  POLICY     — 政策紅線（路由、風險、意圖、顯式紅線短語）
  SEMANTIC   — 語意驗證失敗（claim 與證據詞彙重疊不足、過度承諾、個人化診斷）
  DEPENDENCY — 外部依賴失敗（verifier 拋異常或回傳非法型別）

【PolicySnapshot 設計要點：字串快照 vs A enum】
  A 內部使用 Enum 強型別路由狀態；D 刻意用 str 快照（router_status: str），
  將 A 的最終決策當作「事實」拷貝，不推斷、不覆蓋，避免 D 與 A 的 Enum 定義耦合，
  並在 policy.check_policy_snapshot 中以字串集合 KNOWN_ROUTER_STATUSES 做白名單校驗。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    # 嚴格模型：禁止額外欄位，確保契約精確匹配，避免未知欄位悄悄通過
    model_config = ConfigDict(extra="forbid")


class PolicySnapshot(StrictModel):
    """The final A decision copied into the D boundary.

    D treats these fields as facts from A. It does not infer or replace A's
    policy decision.
    """

    # ── PolicySnapshot：A 決策的字串快照（非 Enum）──
    # 為何用 str 而非 Enum：D 不應與 A 的 Enum 定義強耦合；D 只做字串白名單校驗，
    # 若 A 新增路由狀態，D 能以 POLICY_UNKNOWN_ROUTER_STATUS 明確失敗，而非解析期崩潰。
    # 對應 8 步流水線步驟 2（A 快照校驗）與步驟 6（A 風險紅線）。

    router_status: str = Field(min_length=1)  # 路由狀態字串快照；僅 G_GENERAL_EDUCATION 允許放行，其餘一律 POLICY 失敗
    rag_allowed: bool  # RAG 是否被 A 允許；為 False 時直接 POLICY_RAG_NOT_ALLOWED
    risk_flags: list[str] = Field(default_factory=list)  # 風險旗標列表；命中 HARD_POLICY_RISKS 即 POLICY_HARD_RISK_PRESENT
    intent_tags: list[str] = Field(default_factory=list)  # 意圖標籤；含 MEDICATION_CHANGE_REQUEST 即 POLICY_MEDICATION_CHANGE_REQUEST
    reason_codes: list[str] = Field(default_factory=list)  # A 的原因碼透傳，僅作追溯，不參與放行判斷


class EvidenceRecord(StrictModel):
    # 單筆證據記錄，對應 B 檢索到的原始上下文
    evidence_id: str = Field(min_length=1)  # 證據唯一識別；為何檢查 min_length=1：空字串無法追溯與授權比對
    content: str = Field(min_length=1)  # 證據正文；為何檢查 min_length=1：空內容無法支撐語意驗證
    metadata: dict[str, Any] = Field(default_factory=dict)  # 來源詮釋資料（頁碼、來源等），不參與驗證，僅追溯


class EvidenceSet(StrictModel):
    """Normalized B output used by D.

    ``approved_evidence_ids`` is deliberately separate from ``evidence``:
    retrieved context is not automatically approved context.
    """

    # ── EvidenceSet：B 輸出的正規化形態 ──
    # 為何 approved_evidence_ids 與 evidence 分離：檢索到的上下文 ≠ 被 B 批准的上下文；
    # D 必須校驗「引用是否落在已批准集合內」，防止 C 引用未經 B 背書的證據。
    # 對應 8 步流水線步驟 3 與步驟 5。

    decision: str = Field(min_length=1)  # B 的決策字串；僅 "PASS" 允許繼續，否則 EVIDENCE 失敗（B_EVIDENCE_SET_NOT_APPROVED）
    approved_evidence_ids: list[str] = Field(default_factory=list)  # B 明確批准的 evidence_id 白名單；為何獨立欄位：避免從 evidence 自動推導授權
    evidence: list[EvidenceRecord] = Field(default_factory=list)  # 完整證據記錄列表；用於語意驗證時的文本比對


class SupportedClaim(StrictModel):
    # 被證據支撐的主張（C 聲稱有據可查的論點）
    claim_id: str = Field(min_length=1)  # 主張唯一識別；為何檢查：用於 failed_claims 精確定位
    claim: str = Field(min_length=1)  # 主張文本；為何檢查：空主張無意義且無法做詞彙重疊
    evidence_ids: list[str] = Field(default_factory=list)  # 支撐此主張的 evidence_id 列表；為何可空：由 gate 層檢查 CLAIM_WITHOUT_EVIDENCE_ID


class UnsupportedRequest(StrictModel):
    # C 明確標記為「無法支撐/無法回答」的請求
    request: str = Field(min_length=1)  # 未被滿足的請求原文
    reason: str = ""  # 未滿足原因；為何可空：僅作說明，不影響閘門決策


class ClinicianSourceRowStrict(StrictModel):
    evidence_id: str = Field(min_length=1)
    source: str | None = None
    date: str | None = None
    version: str | None = None
    score: float | None = None


class CandidateResponse(StrictModel):
    """Canonical C v0.1 response, matching C's v2 interface + clinician draft."""

    decision: Literal["ANSWER", "PARTIAL", "INSUFFICIENT", "CLINICIAN_DRAFT"]  # 新增 CLINICIAN_DRAFT 供醫護草稿
    answer: str = Field(min_length=1)
    supported_claims: list[SupportedClaim] = Field(default_factory=list)
    unsupported_requests: list[UnsupportedRequest] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence_summary: list[SupportedClaim] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    source_table: list[ClinicianSourceRowStrict] = Field(default_factory=list)
    disclaimer: str | None = None
    request_id: str | None = None


class ClaimFailure(StrictModel):
    # 單筆主張失敗詳情，用於 OutputGateResult.failed_claims
    claim_id: str = Field(min_length=1)  # 失敗主張的識別
    claim: str = Field(min_length=1)  # 失敗主張的原文
    status: str = Field(min_length=1)  # 失敗狀態（如 UNSUPPORTED）；為何用 str：兼容多種 verifier 回傳
    reason: str = ""  # 失敗原因說明
    evidence_ids: list[str] = Field(default_factory=list)  # 關聯的 evidence_id，便於追溯是哪組證據不足


class OutputGateRequest(StrictModel):
    """D canonical input contract.

    The adapter accepts the repository's existing A/B/C shapes and produces
    this small boundary object. Raw values remain untrusted until the gate
    validates each nested contract.
    """

    # ── OutputGateRequest：D 的正規輸入契約（邊界物件）──
    # 為何需要此層：A/B/C 原始形狀異構（a_result/b_result/c_result 等），adapter 先轉為此小而精的邊界物件，
    # 原始值在 gate 逐層 parse 之前皆視為「不可信」，必須通過各子契約校驗才可使用。
    # 對應 8 步流水線步驟 1（適配）的產物，是後續 7 步的唯一可信輸入。

    request_id: str = Field(min_length=1)  # 請求追蹤識別；為何必填：用於日誌與 OutputGateResult 回填
    schema_version: str = Field(min_length=1)  # 契約版本（如 d.v0.1）；為何必填：用於相容性與追溯
    policy: dict[str, Any]  # A 策略快照原始字典；為何用 dict 而非 PolicySnapshot：保持未驗證狀態，由 gate 層 parse_policy 再校驗
    evidence_set: dict[str, Any]  # B 證據集原始字典；為何用 dict：同上，延後至 parse_evidence_set 才視為可信
    candidate_response: Any  # C 候選回應原始值；為何用 Any：需兼容 v1/v2/多種外層包裝，由 parse_candidate_response 適配


class OutputGateResult(StrictModel):
    """The only D decision is PASS or FALLBACK."""

    # ── OutputGateResult：D 的唯一決策輸出 ──
    # 為何只有 PASS/FALLBACK 二值：D 是 fail-closed 閘門，任何疑慮皆降級為 FALLBACK，不存在「部分通過」。
    # 對應 8 步流水線終態。

    request_id: str  # 回填請求識別，與輸入 request_id 一致
    schema_version: str  # 回填契約版本
    decision: Literal["PASS", "FALLBACK"]  # 最終決策；為何僅二值：D 不做分級放行，僅判斷是否可信
    passed: bool  # 是否通過；為何冗餘：decision == "PASS" 的布林鏡像，方便呼叫方直接判斷
    failure_type: Literal["NONE", "SCHEMA", "EVIDENCE", "POLICY", "SEMANTIC", "DEPENDENCY"]  # 6 種失敗類型（見檔案頂部說明）；為何 6 種：精確區分失敗根因以利監控與除錯
    reason_codes: list[str] = Field(default_factory=list)  # 失敗/通過原因碼列表；去重後回傳
    failed_claims: list[ClaimFailure] = Field(default_factory=list)  # 語意驗證失敗的主張詳情；僅 SEMANTIC 失敗時非空
    invalid_evidence_ids: list[str] = Field(default_factory=list)  # 非法 evidence_id 列表（缺失/未授權/孤兒授權）；僅 EVIDENCE 失敗時非空
    final_response: str = Field(min_length=1)  # 最終回應文本；PASS 時為 candidate.answer，FALLBACK 時為安全降級語句
    candidate_decision: str | None = None  # C 原始決策（ANSWER/PARTIAL/INSUFFICIENT）回填；為何可空：SCHEMA 早期失敗時可能無法解析
    verifier: str | None = None  # 實際執行的 verifier 名稱；為何可空：棄權分支不執行 verifier
