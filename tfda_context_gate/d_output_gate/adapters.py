"""D 輸出閘門適配層（Adapters）— 繁體中文註解版

本檔案負責將倉庫既有的 A/B/C 異構形狀轉為 D 的正規邊界物件 OutputGateRequest，
邏輯零改動，僅補充中文說明。

【在 8 步流水線中的定位：步驟 1 適配】
  build_gate_request 是流水線入口，將多種歷史 payload 形狀（a_result/b_result/c_result、
  b_context/context_gate_result、output 等）統一為 OutputGateRequest。
  適配失敗 → gate 層直接 FALLBACK + SCHEMA（D_INPUT_SCHEMA_INVALID）。

【6 組欄位映射（異構 → 正規）】
  映射 1：evidence_id ← evidence_id / document_id / chunk_id / id
          為何 4 選 1：B 的證據識別在不同版本/模組中命名不一致，需兼容全部歷史命名
  映射 2：content     ← content / page_content / text
          為何 3 選 1：證據正文欄位同樣存在多種命名，需全兼容
  映射 3：approved_evidence_ids ← approved_evidence_ids / approved_document_ids
          為何 2 選 1：批准清單的命名亦有歷史差異
  映射 4：evidence    ← evidence / contexts / retrieved_contexts
          為何 3 選 1：證據列表的外層鍵名不統一
  映射 5：policy      ← policy / a_result / policy_result（皆視為 A 原始字典）
          為何 3 選 1：A 結果的外層包裝鍵名多樣
  映射 6：candidate_response ← candidate_response / c_result / output
          為何 3 選 1：C 結果的外層包裝鍵名多樣；此為必填，缺失則直接拋 ValueError

【重要不變式】
  - 不推導授權：若 B 未顯式提供 approved_* 欄位，預設為空列表，不從 evidence 自動推導
  - 不覆蓋 A 決策：policy 僅做欄位提取，不做值轉換或預設推斷
  - 缺失保持缺失：讓 gate 層以 SCHEMA 失敗明確報錯，而非適配層捏造預設值
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from .schemas import (
    CandidateResponse,
    EvidenceSet,
    OutputGateRequest,
    PolicySnapshot,
    SupportedClaim,
    UnsupportedRequest,
)


def _as_dict(value: Any) -> dict[str, Any]:
    # 將任意輸入轉為字典，供適配層統一處理
    # 為何支援 BaseModel：A/B/C 可能已是 Pydantic 模型，需先 model_dump
    # 為何支援 Mapping：多數歷史 payload 為普通 dict
    # 為何拋 TypeError：既非模型也非映射的值無法提取欄位，屬不可適配輸入
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"expected an object, got {type(value).__name__}")


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    # 按優先順序取第一個非 None 的鍵值
    # 為何跳過 None：None 表示該鍵雖存在但無有效值，應繼續嘗試下一個別名
    # 為何不跳過空字串/空列表：空值可能是有意義的（如空批准清單），需保留給上層判斷
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def build_gate_request(payload: Mapping[str, Any] | OutputGateRequest) -> OutputGateRequest:
    """Adapt existing A/B/C payloads without changing their implementations.

    Accepted shapes include the canonical D shape and the existing fixture
    shape: ``a_result``, ``b_result``/``b_context``, and ``c_result``/``output``.
    Missing fields are kept missing so the gate can fail closed with a useful
    deterministic reason instead of inventing an interface.
    """

    # ── 若已是正規物件，直接回傳 ──
    # 為何檢查：呼叫方可能已手動建構 OutputGateRequest，無需重複適配
    if isinstance(payload, OutputGateRequest):
        return payload
    raw = dict(payload)

    # ── 映射 5：提取 A 策略原始字典 ──
    # 為何 3 選 1（policy / a_result / policy_result）：兼容 D 正規形與歷史 fixture 形
    # 為何 fallback 到 raw：若外層本身就是 A 字典（無包裝），則整體視為 A
    a_raw = _first(raw, "policy", "a_result", "policy_result")
    if a_raw is None:
        a_raw = raw
    a = _as_dict(a_raw)

    # ── 映射 4 的外層：提取 B 證據集原始字典 ──
    # 為何 4 選 1（evidence_set / b_result / b_context / context_gate_result）：B 的外層鍵名歷史多樣
    # 為何 fallback 到 raw：同 A，若無包裝則整體視為 B
    b_raw = _first(raw, "evidence_set", "b_result", "b_context", "context_gate_result")
    if b_raw is None:
        b_raw = raw
    b = _as_dict(b_raw)

    # ── 映射 6：提取 C 候選回應原始值 ──
    # 為何 3 選 1（candidate_response / c_result / output）：C 的外層鍵名歷史多樣
    # 為何缺失即拋錯：C 是 D 驗證的主體，無候選回應則無法進行任何後續校驗
    c_raw = _first(raw, "candidate_response", "c_result", "output")
    if c_raw is None:
        raise ValueError("missing candidate_response/c_result/output")

    # ── 建構 policy 快照字典（提取 5 個欄位）──
    # 為何僅提取這 5 欄：對應 PolicySnapshot 的全部欄位，其餘 A 欄位與 D 無關
    # 為何用 .get 而非 _first：policy 內層鍵名固定，無別名問題
    policy = {
        "router_status": a.get("router_status"),
        "rag_allowed": a.get("rag_allowed"),
        "risk_flags": a.get("risk_flags", []),
        "intent_tags": a.get("intent_tags", []),
        "reason_codes": a.get("reason_codes", []),
    }

    # ── 映射 3：提取批准清單 ──
    # 為何 2 選 1（approved_evidence_ids / approved_document_ids）：歷史命名差異
    approved = _first(b, "approved_evidence_ids", "approved_document_ids")
    if approved is None:
        # Do not derive approval from retrieved context. B must explicitly
        # mark the evidence that it approved.
        # 為何預設空列表而非從 evidence 推導：檢索到的上下文 ≠ 被批准的上下文；
        # 若自動推導會讓未經 B 背書的證據被視為已授權，破壞 EVIDENCE 校驗的意義
        approved = []
    # ── 映射 4 內層：提取證據記錄列表 ──
    # 為何 3 選 1（evidence / contexts / retrieved_contexts）：證據列表鍵名歷史多樣
    raw_records = _first(b, "evidence", "contexts", "retrieved_contexts") or []
    evidence: list[dict[str, Any]] = []
    for record in raw_records:
        item = _as_dict(record)
        # ── 映射 1：evidence_id 的 4 選 1 ──
        # 為何 4 選 1（evidence_id / document_id / chunk_id / id）：B 的證據識別在不同版本中命名不同
        evidence_id = _first(item, "evidence_id", "document_id", "chunk_id", "id")
        # ── 映射 2：content 的 3 選 1 ──
        # 為何 3 選 1（content / page_content / text）：證據正文欄位同樣存在多種命名
        content = _first(item, "content", "page_content", "text")
        metadata = item.get("metadata", {})
        evidence.append(
            {
                "evidence_id": evidence_id,
                "content": content,
                "metadata": metadata if isinstance(metadata, dict) else {},
                # 為何檢查 metadata 型別：非字典的 metadata 無法結構化存儲，降級為空字典避免後續異常
            }
        )

    evidence_set = {
        "decision": b.get("decision", b.get("b_decision")),  # 為何 2 選 1：B 決策鍵名有 decision / b_decision 兩種
        "approved_evidence_ids": approved,
        "evidence": evidence,
    }
    return OutputGateRequest(
        request_id=raw.get("request_id"),  # 為何可為 None：缺失時由 Pydantic 校驗報 SCHEMA 錯誤，而非適配層捏造
        schema_version=raw.get("schema_version", "d.v0.1"),  # 為何預設 d.v0.1：歷史 payload 可能無版本號，給予預設以便追溯
        policy=policy,
        evidence_set=evidence_set,
        candidate_response=c_raw,  # 為何保持原始值：C 的解析延後至 gate 層 parse_candidate_response，適配層不做結構假設
    )


def parse_policy(value: Any) -> PolicySnapshot:
    # 解析並校驗 PolicySnapshot（8 步流水線步驟 2）
    # 為何用 model_validate：利用 Pydantic 嚴格校驗字串快照的每個欄位
    return PolicySnapshot.model_validate(value)


def parse_evidence_set(value: Any) -> EvidenceSet:
    # 解析並校驗 EvidenceSet（8 步流水線步驟 3）
    return EvidenceSet.model_validate(value)


def parse_candidate_response(value: Any) -> CandidateResponse:
    raw = _as_dict(value)
    if raw.get("decision") == "CLINICIAN_DRAFT" or "evidence_summary" in raw or "source_table" in raw:
        return CandidateResponse.model_validate(raw)
    if "supported_claims" in raw or "unsupported_requests" in raw:
        return CandidateResponse.model_validate(raw)
    if "claims" in raw:
        claims = []
        for claim in raw.get("claims", []):
            item = _as_dict(claim)
            claims.append(
                SupportedClaim(
                    claim_id=item.get("claim_id"),
                    claim=item.get("claim"),
                    evidence_ids=item.get("evidence_ids", []),
                )
            )
        return CandidateResponse(
            decision=raw.get("decision"),
            answer=raw.get("answer"),
            supported_claims=claims,
            unsupported_requests=[],
            limitations=raw.get("limitations", []),
        )
    raise ValueError("C response has neither v2 supported_claims nor v1 claims")


def validation_error_text(error: Exception) -> str:
    # 提取驗證錯誤的首行文本，用於 reason_codes
    # 為何僅取首行：完整 ValidationError 可能很長，首行已含關鍵資訊，避免 reason_codes 過長
    if isinstance(error, ValidationError):
        return str(error).splitlines()[0]
    return str(error)
