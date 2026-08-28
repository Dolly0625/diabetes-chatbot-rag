"""D 輸出閘門語意驗證器（Verifier）— 繁體中文註解版

本檔案定義語意驗證的介面與兩種實作，邏輯零改動，僅補充中文說明。

【在 8 步流水線中的定位：步驟 8 語意驗證】
  前 7 步僅做結構、政策與證據歸屬檢查；步驟 8 驗證「主張文本是否被證據文本語意支撐」。
  失敗 → FALLBACK + SEMANTIC；依賴異常 → FALLBACK + DEPENDENCY；全通過 → PASS。

【HeuristicSemanticVerifier 核心說明：詞彙重疊 0.85 非醫療驗證】
  - 性質：demo 級啟發式，非正式醫療安全機制，僅示範 D 的 verifier 介面
  - 方法：主張與證據的詞彙重疊率（token overlap）≥ 0.85 視為支撐，或主張原文被證據原文包含
  - 分詞：拉丁/數字連續串為一 token，中文按單字切分（見 _token_pattern）
  - 額外檢查：answer 全文掃描過度承諾（保證/一定/絕對…）與個人化診斷（你就是糖尿病…）
  - 生產建議：需替換為獨立評估的 claim/NLI 或 LLM verifier，並搭配版本化評估集

【MappingSemanticVerifier】
  測試替身（test double），供整合測試以確定性方式指定每條 claim 的支撐狀態。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from .schemas import CandidateResponse, ClaimFailure, EvidenceRecord, EvidenceSet, PolicySnapshot


class SemanticVerifier(Protocol):
    """Pluggable semantic verifier boundary for a future independent judge."""

    # ── 語意驗證器協定（可插拔邊界）──
    # 為何用 Protocol：未來可替換為獨立的 NLI/LLM 判斷器，D 僅依賴此介面
    # 為何需 name 屬性：用於 OutputGateResult.verifier 追溯實際執行的驗證器

    name: str

    def verify(
        self,
        candidate: CandidateResponse,
        evidence_set: EvidenceSet,
        policy: PolicySnapshot,
    ) -> "SemanticVerificationResult":
        ...


class SemanticVerificationResult:
    # 語意驗證結果容器
    # 為何分 failed_claims 與 unsupported_answer_claims：前者是主張級失敗，後者是 answer 全文的無據事實
    # 為何有 reason_codes：供 gate 層轉為 FALLBACK 的原因碼
    def __init__(
        self,
        *,
        failed_claims: list[ClaimFailure] | None = None,
        unsupported_answer_claims: list[ClaimFailure] | None = None,
        reason_codes: list[str] | None = None,
    ) -> None:
        self.failed_claims = failed_claims or []
        self.unsupported_answer_claims = unsupported_answer_claims or []
        self.reason_codes = reason_codes or []


class HeuristicSemanticVerifier:
    """Demo verifier, not a formal medical safety mechanism.

    It only uses lexical overlap to demonstrate the D interface. Production
    use needs an independently evaluated claim/NLI or LLM verifier and a
    versioned evaluation set.
    """

    # ── HeuristicSemanticVerifier：詞彙重疊 demo 實作 ──
    # 警告：此為非醫療驗證，僅示範介面，不具臨床有效性聲明

    name = "heuristic-demo-not-medical-safety"

    # Keep Latin/number runs together but compare Chinese at character level;
    # this lets the demo tolerate the short paraphrases present in C output.
    # 為何此正則：拉丁/數字連續串視為一 token（如 HbA1c、100mg），中文按單字切分，
    # 以容忍 C 輸出中的短改寫（如「血糖偏高」vs「血糖較高」仍有高重疊）
    _token_pattern = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", re.UNICODE)
    # 為何檢查過度承諾：answer 含「保證/一定/絕對/百分之百/guarantee/always/never」屬過度承諾，需標記 SEMANTIC_OVERCONFIDENCE
    # HPA 一般衛教含「胰島素絕對缺乏」等醫學描述，非過度承諾，需排除
    _overclaim_pattern = re.compile(r"保證|一定|絕對(?!缺乏)|百分之百|guarantee|always|never", re.IGNORECASE)
    # 為何檢查個人化診斷：answer 含「你就是糖尿病/確診為」等屬個人化診斷，需標記 SEMANTIC_PERSONALIZED_DIAGNOSIS
    _diagnosis_pattern = re.compile(r"你(?:就是|是)\S{0,12}(?:糖尿病|確診)|(?:確診|診斷)為", re.IGNORECASE)

    def verify(
        self,
        candidate: CandidateResponse,
        evidence_set: EvidenceSet,
        policy: PolicySnapshot,
    ) -> SemanticVerificationResult:
        by_id = {record.evidence_id: record for record in evidence_set.evidence}
        failed: list[ClaimFailure] = []
        reasons: list[str] = []
        claims = candidate.evidence_summary if candidate.decision == "CLINICIAN_DRAFT" and candidate.evidence_summary else candidate.supported_claims
        for claim in claims:
            # 為何遍歷每條主張：逐條驗證主張與其綁定證據的詞彙重疊
            texts = [by_id[evidence_id].content for evidence_id in claim.evidence_ids if evidence_id in by_id]
            # 為何過濾 evidence_id in by_id：前置步驟已校驗歸屬，此處防禦性過濾避免 KeyError
            if not texts:
                # 為何跳過：若無對應證據文本（理論上前置已攔截），則不判定失敗，避免重複報錯
                continue
            if self._is_supported(claim.claim, texts):
                # 為何檢查：詞彙重疊 ≥ 0.85 或原文包含即視為支撐，通過此條主張
                continue
            # 未通過 → 記錄為語意失敗
            failed.append(
                ClaimFailure(
                    claim_id=claim.claim_id,
                    claim=claim.claim,
                    status="UNSUPPORTED",
                    reason="demo lexical verifier found insufficient overlap",
                    evidence_ids=claim.evidence_ids,
                )
            )
            reasons.append("CLAIM_NOT_SUPPORTED_BY_EVIDENCE")

        answer_text = candidate.answer
        overclaim_match = self._overclaim_pattern.search(answer_text)
        if overclaim_match:
            matched_text = overclaim_match.group(0)
            evidence_texts_all = " ".join(record.content for record in evidence_set.evidence)
            if matched_text not in evidence_texts_all:
                reasons.append("SEMANTIC_OVERCONFIDENCE")
        if self._diagnosis_pattern.search(answer_text):
            # 為何檢查：answer 全文含個人化診斷表述，屬語意風險
            reasons.append("SEMANTIC_PERSONALIZED_DIAGNOSIS")
        return SemanticVerificationResult(failed_claims=failed, reason_codes=list(dict.fromkeys(reasons)))
        # 為何去重：多條主張同錯時避免重複原因碼

    def _is_supported(self, claim: str, evidence_texts: list[str]) -> bool:
        # 判斷單條主張是否被證據文本支撐（詞彙重疊啟發式）
        # 為何用詞彙重疊而非語意模型：demo 級簡易實作，非醫療驗證，僅示範介面
        claim_tokens = set(self._token_pattern.findall(claim.lower()))
        if not claim_tokens:
            # 為何回 False：主張無有效 token（如僅標點），無法計算重疊，視為不支撐
            return False
        evidence_tokens = set()
        for text in evidence_texts:
            evidence_tokens.update(self._token_pattern.findall(text.lower()))
        overlap = len(claim_tokens & evidence_tokens) / len(claim_tokens)
        # This deliberately simple heuristic is a demo only. It is not a
        # claim entailment score and has no clinical validity claim.
        # 為何閾值 0.85：經驗性 demo 閾值，平衡容忍改寫與攔截幻覺；非臨床有效性聲明
        # 為何額外檢查原文包含：若主張原文被任一證據原文包含，直接視為支撐，處理完全複製的情況
        return overlap >= 0.85 or any(claim.strip() in text for text in evidence_texts)


class MappingSemanticVerifier:
    """Small deterministic test double for semantic verifier integration tests."""

    # ── MappingSemanticVerifier：測試替身 ──
    # 為何需要：整合測試需以確定性方式指定每條 claim 的支撐狀態，避免依賴啟發式波動

    name = "mapping-test-double"

    def __init__(
        self,
        statuses: Mapping[str, str] | None = None,
        *,
        reason_codes: Mapping[str, str] | None = None,
        fail_reason: str | None = None,
    ) -> None:
        self.statuses = dict(statuses or {})  # claim_id → 狀態（如 SUPPORTED / UNSUPPORTED）
        self.reason_codes = dict(reason_codes or {})  # claim_id → 自訂原因碼
        self.fail_reason = fail_reason  # 若非 None，verify 時直接拋異常，用於測試 DEPENDENCY 路徑

    def verify(
        self,
        candidate: CandidateResponse,
        evidence_set: EvidenceSet,
        policy: PolicySnapshot,
    ) -> SemanticVerificationResult:
        if self.fail_reason:
            raise RuntimeError(self.fail_reason)
        failures = []
        reasons = []
        claims = candidate.evidence_summary if candidate.decision == "CLINICIAN_DRAFT" and candidate.evidence_summary else candidate.supported_claims
        for claim in claims:
            status = self.statuses.get(claim.claim_id, "SUPPORTED")  # 為何預設 SUPPORTED：未指定的主張視為通過
            if status == "SUPPORTED":
                continue
            failures.append(
                ClaimFailure(
                    claim_id=claim.claim_id,
                    claim=claim.claim,
                    status=status,
                    reason=self.reason_codes.get(claim.claim_id, "test double result"),
                    evidence_ids=claim.evidence_ids,
                )
            )
            reasons.append(self.reason_codes.get(claim.claim_id, f"CLAIM_{status}"))
        return SemanticVerificationResult(failed_claims=failures, reason_codes=reasons)
