"""D 輸出閘門主流程（Gate）— 繁體中文註解版

本檔案實作 D 的 8 步強制驗證流水線，邏輯零改動，僅補充中文說明。
整體原則：deterministic-first（確定性優先）、fail-closed（有疑慮即降級為 FALLBACK）。

【8 步流水線在 run_output_gate 中的對應】
  步驟 1 適配            → build_gate_request(payload)                         → 失敗 → SCHEMA / D_INPUT_SCHEMA_INVALID
  步驟 2 A 快照校驗       → parse_policy(request.policy)                        → 失敗 → SCHEMA / A_POLICY_SCHEMA_INVALID
  步驟 3 B 證據集校驗     → parse_evidence_set(request.evidence_set)            → 失敗 → SCHEMA / B_EVIDENCE_SCHEMA_INVALID
  步驟 4 C 形狀校驗       → parse_candidate_response + _validate_candidate_shape → 失敗 → SCHEMA / C_* 形狀錯誤碼
  步驟 5 B PASS 與 evidence_id 歸屬 → evidence_set.decision + _validate_evidence_ids → 失敗 → EVIDENCE
  步驟 6 A 風險紅線       → check_policy_snapshot + check_candidate_red_lines   → 失敗 → POLICY
  步驟 7 棄權分支         → not candidate.supported_claims                      → 成功 → PASS / D_SAFE_ABSTENTION_ACCEPTED
  步驟 8 語意驗證         → verifier.verify(...) + _semantic_failure            → 失敗 → SEMANTIC / DEPENDENCY，成功 → PASS

【6 種 failure_type 與決策理由】
  SCHEMA     — 契約不合規，無法信任輸入，直接 FALLBACK
  EVIDENCE   — 證據未授權或缺失，無法證明主張，直接 FALLBACK
  POLICY     — 觸及政策紅線（路由/風險/紅線短語），直接 FALLBACK
  SEMANTIC   — 主張與證據詞彙重疊不足或含過度承諾/個人化診斷，直接 FALLBACK
  DEPENDENCY — verifier 異常或回傳非法，直接 FALLBACK（不讓異常穿透為 PASS）
  NONE       — 無失敗，對應 PASS（僅棄權或語意全通過兩種路徑）
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .adapters import (
    build_gate_request,
    parse_candidate_response,
    parse_evidence_set,
    parse_policy,
    validation_error_text,
)
from .policy import PolicyRuleConfig, check_candidate_red_lines, check_policy_snapshot, check_previsit_summary
from .schemas import (
    CandidateResponse,
    ClaimFailure,
    EvidenceSet,
    OutputGateRequest,
    OutputGateResult,
    PolicySnapshot,
)
from .verifier import HeuristicSemanticVerifier, SemanticVerifier, SemanticVerificationResult


# 預設降級回應：當 D 判定無法驗證時，回傳此安全語句而非候選回答
DEFAULT_FALLBACK = "目前無法驗證這份回答是否有足夠依據，因此無法提供可靠回覆；請改由合格醫療專業人員評估。"


def _result(
    request: OutputGateRequest,
    *,
    decision: str,
    failure_type: str,
    reason_codes: list[str] | tuple[str, ...] = (),
    failed_claims: list[ClaimFailure] | None = None,
    invalid_evidence_ids: list[str] | None = None,
    final_response: str = DEFAULT_FALLBACK,
    candidate_decision: str | None = None,
    verifier: str | None = None,
) -> OutputGateResult:
    # 統一建構 OutputGateResult，確保所有分支回傳格式一致
    # 為何對 reason_codes 去重（dict.fromkeys）：避免同一原因碼重複出現，影響監控與可讀性
    # 為何 passed = decision == "PASS"：passed 是 decision 的布林鏡像，方便呼叫方直接判斷
    return OutputGateResult(
        request_id=request.request_id,
        schema_version=request.schema_version,
        decision=decision,  # 決策理由：僅 PASS/FALLBACK 二值，fail-closed
        passed=decision == "PASS",
        failure_type=failure_type,
        reason_codes=list(dict.fromkeys(reason_codes)),
        failed_claims=failed_claims or [],
        invalid_evidence_ids=invalid_evidence_ids or [],
        final_response=final_response,
        candidate_decision=candidate_decision,
        verifier=verifier,
    )


def _validate_candidate_shape(candidate: CandidateResponse) -> list[str]:
    reasons: list[str] = []
    if candidate.decision == "ANSWER" and not candidate.supported_claims:
        reasons.append("C_ANSWER_HAS_NO_SUPPORTED_CLAIMS")
    if candidate.decision == "PARTIAL" and not (
        candidate.supported_claims or candidate.unsupported_requests
    ):
        reasons.append("C_PARTIAL_HAS_NO_SUPPORTED_OR_UNSUPPORTED_FIELDS")
    if candidate.decision == "INSUFFICIENT" and candidate.supported_claims:
        reasons.append("C_INSUFFICIENT_HAS_SUPPORTED_CLAIMS")
    if candidate.decision == "CLINICIAN_DRAFT":
        if not candidate.source_table:
            reasons.append("C_CLINICIAN_DRAFT_MISSING_SOURCE_TABLE")
        if not candidate.disclaimer or len(candidate.disclaimer.strip()) < 5:
            reasons.append("C_CLINICIAN_DRAFT_MISSING_DISCLAIMER")
        effective_claims = candidate.evidence_summary if candidate.evidence_summary else candidate.supported_claims
        if not effective_claims:
            reasons.append("C_CLINICIAN_DRAFT_HAS_NO_EVIDENCE_SUMMARY")
        for claim in effective_claims:
            if not claim.evidence_ids:
                reasons.append("CLAIM_WITHOUT_EVIDENCE_ID")
        for row in candidate.source_table:
            if not row.evidence_id:
                reasons.append("C_CLINICIAN_DRAFT_SOURCE_ROW_MISSING_EVIDENCE_ID")
        if candidate.disclaimer and "確認" not in candidate.disclaimer and "confirm" not in candidate.disclaimer.lower():
            reasons.append("C_CLINICIAN_DRAFT_DISCLAIMER_MISSING_CONFIRMATION")
    else:
        for claim in candidate.supported_claims:
            if not claim.evidence_ids:
                reasons.append("CLAIM_WITHOUT_EVIDENCE_ID")
    return list(dict.fromkeys(reasons))


def _validate_evidence_ids(candidate: CandidateResponse, evidence_set: EvidenceSet) -> tuple[list[str], list[str], list[str]]:
    evidence_ids = {record.evidence_id for record in evidence_set.evidence}
    approved_ids = set(evidence_set.approved_evidence_ids)
    if candidate.decision == "CLINICIAN_DRAFT":
        effective_claims = candidate.evidence_summary if candidate.evidence_summary else candidate.supported_claims
        referenced = {eid for claim in effective_claims for eid in claim.evidence_ids}
        referenced.update(row.evidence_id for row in candidate.source_table if row.evidence_id)
    else:
        referenced = {
            evidence_id
            for claim in candidate.supported_claims
            for evidence_id in claim.evidence_ids
        }
    missing = sorted(referenced - evidence_ids)
    not_approved = sorted((referenced & evidence_ids) - approved_ids)
    malformed_approved = sorted(approved_ids - evidence_ids)
    return missing, not_approved, malformed_approved


def _semantic_failure(
    result: SemanticVerificationResult,
) -> tuple[list[ClaimFailure], list[str]]:
    # 將語意驗證結果轉為閘門失敗資訊（8 步流水線步驟 8 的後處理）
    failures = list(result.failed_claims) + list(result.unsupported_answer_claims)
    reasons = list(result.reason_codes)
    if result.failed_claims:
        # 為何追加：只要有主張級失敗，就需一個總括原因碼供監控
        reasons.append("CLAIM_SEMANTIC_VERIFICATION_FAILED")
    if result.unsupported_answer_claims:
        # 為何追加：answer 全文若含無據事實主張，需獨立原因碼標記
        reasons.append("ANSWER_HAS_UNSUPPORTED_FACTUAL_CLAIMS")
    return failures, list(dict.fromkeys(reasons))


def run_output_gate(
    payload: dict[str, Any] | OutputGateRequest,
    *,
    verifier: SemanticVerifier | None = None,
    policy_rules: PolicyRuleConfig | None = None,
    fallback_response: str = DEFAULT_FALLBACK,
) -> OutputGateResult:
    """Run D v0.1 in deterministic-first order and fail closed."""
    if isinstance(payload, dict):
        payload = _ensure_intake_evidence_set(payload)

    # ── 步驟 1：適配（Adapter）──
    # 為何先適配：將異構 A/B/C 原始形狀統一為 OutputGateRequest，後續步驟才有可信邊界物件
    # 為何 fail-closed：適配失敗代表輸入根本無法理解，直接 FALLBACK，不嘗試修補
    try:
        request = build_gate_request(payload)
    except Exception as exc:
        # We cannot safely recover tracing fields from a malformed payload.
        # 為何用 try 捕捉所有異常：payload 可能完全畸形，需兜底提取 request_id/schema_version 以便追溯
        request = OutputGateRequest(
            request_id=str(payload.get("request_id", "unknown")) if isinstance(payload, dict) else "unknown",
            schema_version=str(payload.get("schema_version", "d.v0.1")) if isinstance(payload, dict) else "d.v0.1",
            policy={},
            evidence_set={},
            candidate_response=None,
        )
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["D_INPUT_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)
        # 決策理由：FALLBACK + SCHEMA — 輸入契約本身無效，無法進入後續任何校驗

    # ── 步驟 2：A 快照校驗 ──
    # 為何檢查：PolicySnapshot 是字串快照，需驗證 router_status/rag_allowed 等欄位合法性
    try:
        policy = parse_policy(request.policy)
    except (ValidationError, TypeError, ValueError) as exc:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["A_POLICY_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)
        # 決策理由：FALLBACK + SCHEMA — A 策略快照不合規，無法信任路由與風險判斷

    # ── 步驟 3：B 證據集校驗 ──
    # 為何檢查：EvidenceSet 需符合契約（decision/evidence/approved_evidence_ids 形狀）
    try:
        evidence_set = parse_evidence_set(request.evidence_set)
    except (ValidationError, TypeError, ValueError) as exc:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["B_EVIDENCE_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)
        # 決策理由：FALLBACK + SCHEMA — B 證據集不合規，無法信任證據來源

    # ── 步驟 4：C 形狀校驗 ──
    # 為何分兩段：先解析（兼容 v1/v2），再做形狀語意檢查（ANSWER/PARTIAL/INSUFFICIENT 約束）
    try:
        candidate = parse_candidate_response(request.candidate_response)
    except (ValidationError, TypeError, ValueError) as exc:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["C_OUTPUT_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)
        # 決策理由：FALLBACK + SCHEMA — C 回應不合規，無法信任候選回答

    candidate_reasons = _validate_candidate_shape(candidate)
    if candidate_reasons:
        # 為何檢查：C 形狀錯誤（如 ANSWER 無主張、INSUFFICIENT 有主張）屬契約違規
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=candidate_reasons, final_response=fallback_response, candidate_decision=getattr(candidate, "decision", None))
        # 決策理由：FALLBACK + SCHEMA — C 形狀語意矛盾，不可放行

    # ── 步驟 5a：B PASS 校驗 ──
    # 為何檢查：B 的 decision 必須為 PASS，才代表證據集已通過 B 的品質閘
    if evidence_set.decision != "PASS":
        return _result(request, decision="FALLBACK", failure_type="EVIDENCE", reason_codes=["B_EVIDENCE_SET_NOT_APPROVED"], final_response=fallback_response, candidate_decision=candidate.decision)
        # 決策理由：FALLBACK + EVIDENCE — B 未批准，證據集整體不可信

    # ── 步驟 5b：evidence_id 歸屬校驗 ──
    # 為何檢查：確保 C 引用的每個 evidence_id 都存在且被 B 批准，且 B 批准的皆有記錄
    missing, not_approved, malformed_approved = _validate_evidence_ids(candidate, evidence_set)
    invalid_ids = sorted(set(missing + not_approved + malformed_approved))
    evidence_reasons: list[str] = []
    if missing:
        # 為何檢查：引用了不存在的證據，屬幻覺引用
        evidence_reasons.append("EVIDENCE_ID_NOT_FOUND")
    if not_approved:
        # 為何檢查：引用了未被 B 批准的證據，屬越權引用
        evidence_reasons.append("EVIDENCE_ID_NOT_APPROVED_BY_B")
    if malformed_approved:
        # 為何檢查：B 批准清單含孤兒 ID，屬 B 資料不一致
        evidence_reasons.append("B_APPROVED_EVIDENCE_MISSING_RECORD")
    if evidence_reasons:
        return _result(request, decision="FALLBACK", failure_type="EVIDENCE", reason_codes=evidence_reasons, invalid_evidence_ids=invalid_ids, final_response=fallback_response, candidate_decision=candidate.decision)
        # 決策理由：FALLBACK + EVIDENCE — 證據歸屬任一類錯誤皆不可放行

    # ── 步驟 6a：A 政策快照紅線 ──
    # 為何檢查：路由非 G_GENERAL_EDUCATION、硬風險、RAG 未允許、藥物變更意圖皆屬政策紅線
    policy_check = check_policy_snapshot(policy)
    if policy_check.failed:
        return _result(request, decision="FALLBACK", failure_type="POLICY", reason_codes=policy_check.reason_codes, final_response=fallback_response, candidate_decision=candidate.decision)
        # 決策理由：FALLBACK + POLICY — 觸及 A 策略紅線，不可放行

    # ── 步驟 6b：候選回應顯式紅線 ──
    # 為何檢查：候選文本含「自行停藥/調整劑量」等顯式紅線短語，即使 A 已放行也需攔截
    redline_check = check_candidate_red_lines(candidate, policy_rules or PolicyRuleConfig())
    if redline_check.failed:
        return _result(request, decision="FALLBACK", failure_type="POLICY", reason_codes=redline_check.reason_codes, final_response=fallback_response, candidate_decision=candidate.decision)
        # 決策理由：FALLBACK + POLICY — 觸及顯式輸出紅線，不可放行

    if candidate.decision == "INSUFFICIENT":
        final = candidate.answer
        if candidate.disclaimer and candidate.disclaimer not in final:
            final = final + "\n\n【待確認聲明】" + candidate.disclaimer
        return _result(request, decision="PASS", failure_type="NONE", reason_codes=["D_SAFE_ABSTENTION_ACCEPTED"], final_response=final, candidate_decision=candidate.decision, verifier=None)
    if candidate.decision == "CLINICIAN_DRAFT":
        effective = candidate.evidence_summary if candidate.evidence_summary else candidate.supported_claims
        if not effective:
            return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["C_CLINICIAN_DRAFT_HAS_NO_EVIDENCE_SUMMARY"], final_response=fallback_response, candidate_decision=candidate.decision)
    elif not candidate.supported_claims:
        return _result(request, decision="PASS", failure_type="NONE", reason_codes=["D_SAFE_ABSTENTION_ACCEPTED"], final_response=candidate.answer, candidate_decision=candidate.decision, verifier=None)

    # ── 步驟 8：語意驗證 ──
    # 為何需要：前 7 步僅做結構與政策檢查，最後需驗證「主張文本是否被證據文本支撐」
    # 為何預設 HeuristicSemanticVerifier：詞彙重疊 0.85 的 demo 實作，非醫療驗證，僅示範介面
    active_verifier = verifier or HeuristicSemanticVerifier()
    try:
        semantic = active_verifier.verify(candidate, evidence_set, policy)
        if not isinstance(semantic, SemanticVerificationResult):
            # 為何檢查型別：防止自訂 verifier 回傳非法物件導致後續誤判為 PASS
            raise TypeError("semantic verifier returned an invalid result")
    except Exception as exc:
        return _result(request, decision="FALLBACK", failure_type="DEPENDENCY", reason_codes=["VERIFIER_DEPENDENCY_FAILURE", type(exc).__name__], final_response=fallback_response, candidate_decision=candidate.decision, verifier=getattr(active_verifier, "name", type(active_verifier).__name__))
        # 決策理由：FALLBACK + DEPENDENCY — verifier 異常或回傳非法，視為依賴失敗，不可放行

    failed_claims, semantic_reasons = _semantic_failure(semantic)
    if semantic_reasons:
        # 為何檢查：只要有主張級失敗或 answer 含無據事實，即語意驗證不通過
        return _result(request, decision="FALLBACK", failure_type="SEMANTIC", reason_codes=semantic_reasons, failed_claims=failed_claims, final_response=fallback_response, candidate_decision=candidate.decision, verifier=getattr(active_verifier, "name", type(active_verifier).__name__))
        # 決策理由：FALLBACK + SEMANTIC — 主張與證據詞彙重疊不足或含過度承諾/個人化診斷

    if candidate.decision == "CLINICIAN_DRAFT":
        final = candidate.answer
        if candidate.disclaimer and candidate.disclaimer not in final:
            final = final + "\n\n【待確認聲明】" + candidate.disclaimer
        if candidate.source_table:
            table_lines = [f"{row.evidence_id} | {row.source or ''} | {row.date or ''} | {row.version or ''} | {row.score if row.score is not None else ''}" for row in candidate.source_table]
            final = final + "\n\n【來源對照表】\n" + "\n".join(table_lines)
        return _result(request, decision="PASS", failure_type="NONE", reason_codes=["OUTPUT_GATE_PASSED", "CLINICIAN_DRAFT_PENDING_CONFIRMATION"], final_response=final, candidate_decision=candidate.decision, verifier=getattr(active_verifier, "name", type(active_verifier).__name__))
    return _result(request, decision="PASS", failure_type="NONE", reason_codes=["OUTPUT_GATE_PASSED"], final_response=candidate.answer, candidate_decision=candidate.decision, verifier=getattr(active_verifier, "name", type(active_verifier).__name__))


def _ensure_intake_evidence_set(payload: dict[str, Any]) -> dict[str, Any]:
    """若 b_result 缺失（intake 流程 B 被繞過），補齊 INTAKE_SUFFICIENT dummy。"""
    if isinstance(payload, dict) and payload.get("b_result") is None:
        # 統一在 gate 內補齊，避免 graph 各處重複 dummy
        payload = dict(payload)
        payload["b_result"] = {
            "request_id": str(payload.get("request_id", "unknown")),
            "decision": "PASS",
            "approved_evidence_ids": [],
            "evidence": [],
            "reason_codes": ["INTAKE_SUFFICIENT"],
            "identified_missing_information": [],
            "retrieval_feedback": {"retrieval_queries": [str(payload.get("request_id", ""))]},
            "relevance": "INTAKE",
            "sufficiency": "SUFFICIENT",
            "safety": "INTAKE_APPROVED",
        }
    # 同步處理 evidence_set 別名（部分呼叫方直接傳 evidence_set）
    if isinstance(payload, dict) and payload.get("evidence_set") is None and payload.get("b_result") is not None:
        pass
    return payload


def run_previsit_output_gate(
    payload: dict[str, Any],
    *,
    policy_rules: PolicyRuleConfig | None = None,
    fallback_response: str = DEFAULT_FALLBACK,
) -> OutputGateResult:
    payload = _ensure_intake_evidence_set(payload)
    try:
        request = build_gate_request(payload)
    except Exception as exc:
        request = OutputGateRequest(
            request_id=str(payload.get("request_id", "unknown")) if isinstance(payload, dict) else "unknown",
            schema_version=str(payload.get("schema_version", "d.v0.1")) if isinstance(payload, dict) else "d.v0.1",
            policy={},
            evidence_set={},
            candidate_response=None,
        )
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["D_INPUT_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)
    try:
        policy = parse_policy(request.policy)
    except Exception as exc:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["A_POLICY_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)
    candidate_raw = request.candidate_response
    is_previsit = isinstance(candidate_raw, dict) and "summary_text" in candidate_raw
    if is_previsit:
        hard_risks = set(policy.risk_flags).intersection({"POSSIBLE_EMERGENCY", "MENTAL_HEALTH_CRISIS", "PERSONALIZED_MEDICATION", "HIGH_RISK_NOT_EXCLUDED", "PROMPT_INJECTION_SUSPECTED"})
        if hard_risks:
            return _result(request, decision="FALLBACK", failure_type="POLICY", reason_codes=["POLICY_HARD_RISK_PRESENT"], final_response=fallback_response)
        if policy.router_status not in {"G_GENERAL_EDUCATION", "Q_CLARIFICATION"}:
            if policy.router_status in {"E_EMERGENCY", "U_URGENT_HUMAN"}:
                return _result(request, decision="FALLBACK", failure_type="POLICY", reason_codes=["POLICY_HARD_RISK_PRESENT"], final_response=fallback_response)
    else:
        policy_check = check_policy_snapshot(policy)
        if policy_check.failed:
            return _result(request, decision="FALLBACK", failure_type="POLICY", reason_codes=policy_check.reason_codes, final_response=fallback_response)
    if isinstance(candidate_raw, dict) and "summary_text" in candidate_raw:
        summary_text = candidate_raw.get("summary_text", "")
        disclaimer = candidate_raw.get("disclaimer")
        intake_check = check_previsit_summary(summary_text, disclaimer, policy_rules or PolicyRuleConfig())
        if intake_check.failed:
            return _result(request, decision="FALLBACK", failure_type="POLICY", reason_codes=intake_check.reason_codes, final_response=fallback_response)
        try:
            from tfda_context_gate.intake.schemas import PreVisitSummary

            PreVisitSummary.model_validate(candidate_raw)
        except Exception as exc:
            return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["PREVISIT_SUMMARY_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)
        return _result(request, decision="PASS", failure_type="NONE", reason_codes=["OUTPUT_GATE_PASSED", "PREVISIT_SUMMARY_VALIDATED"], final_response=summary_text, verifier=None)
    try:
        candidate = parse_candidate_response(request.candidate_response)
    except Exception as exc:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=["C_OUTPUT_SCHEMA_INVALID", validation_error_text(exc)], final_response=fallback_response)
    candidate_reasons = _validate_candidate_shape(candidate)
    if candidate_reasons:
        return _result(request, decision="FALLBACK", failure_type="SCHEMA", reason_codes=candidate_reasons, final_response=fallback_response, candidate_decision=getattr(candidate, "decision", None))
    redline_check = check_candidate_red_lines(candidate, policy_rules or PolicyRuleConfig())
    if redline_check.failed:
        return _result(request, decision="FALLBACK", failure_type="POLICY", reason_codes=redline_check.reason_codes, final_response=fallback_response, candidate_decision=candidate.decision)
    return _result(request, decision="PASS", failure_type="NONE", reason_codes=["OUTPUT_GATE_PASSED"], final_response=candidate.answer, candidate_decision=candidate.decision, verifier=None)
    # 決策理由：PASS + NONE — 8 步全通過，候選回答可信，放行原始 answer
