from __future__ import annotations

import re
from typing import Iterable, List

from .corpus import EXPECTED_SOURCE
from .schemas import (
    CandidateEvidence,
    EvidenceDecision,
    OutputDecision,
    PolicyDecision,
)


class InputPolicyGate:
    """Small experimental policy gate, not the production A router."""

    injection_patterns = [
        r"ignore (all|any|the) previous",
        r"忽略(所有|先前|以上).{0,8}(指令|規則)",
        r"system prompt",
        r"developer message",
    ]
    personalized_patterns = [
        r"我(現在|今天|剛剛).{0,16}(應該|能不能|要不要).{0,12}(吃|停|換|加|減)",
        r"幫我(診斷|決定|開藥)",
        r"一天(吃|用)幾",
        r"劑量.{0,6}(多少|幾)",
    ]
    emergency_patterns = [r"昏迷", r"無法呼吸", r"嚴重低血糖", r"意識不清"]

    def evaluate(self, query: str) -> PolicyDecision:
        for pattern in self.injection_patterns:
            if re.search(pattern, query, flags=re.IGNORECASE):
                return PolicyDecision(allowed=False, reason_code="PROMPT_INJECTION_BLOCKED")
        for pattern in self.emergency_patterns:
            if re.search(pattern, query, flags=re.IGNORECASE):
                return PolicyDecision(allowed=False, reason_code="EMERGENCY_ESCALATION_REQUIRED")
        for pattern in self.personalized_patterns:
            if re.search(pattern, query, flags=re.IGNORECASE):
                return PolicyDecision(allowed=False, reason_code="PERSONALIZED_MEDICATION_ADVICE_BLOCKED")
        return PolicyDecision(allowed=True, reason_code="GENERAL_INFORMATION_ALLOWED")


class EvidenceGate:
    """Approves provenance/schema only; it is not a clinical semantic judge."""

    def evaluate(self, evidence: Iterable[CandidateEvidence]) -> EvidenceDecision:
        items = list(evidence)
        if not items:
            return EvidenceDecision(
                decision="INSUFFICIENT",
                reason_codes=["NO_CANDIDATE_EVIDENCE"],
            )
        seen = set()
        approved = []
        reasons: List[str] = []
        for item in items:
            if item.evidence_id in seen:
                return EvidenceDecision(
                    decision="UNSAFE",
                    reason_codes=["DUPLICATE_EVIDENCE_ID"],
                )
            seen.add(item.evidence_id)
            if item.source != EXPECTED_SOURCE:
                reasons.append("UNAPPROVED_SOURCE:%s" % item.evidence_id)
                continue
            if not item.content.strip() or item.score <= 0:
                reasons.append("INVALID_EVIDENCE:%s" % item.evidence_id)
                continue
            approved.append(item.evidence_id)
        if not approved:
            return EvidenceDecision(
                decision="INSUFFICIENT",
                reason_codes=reasons or ["NO_APPROVED_EVIDENCE"],
            )
        return EvidenceDecision(
            decision="PASS",
            approved_evidence_ids=approved,
            reason_codes=["PROVENANCE_AND_SCHEMA_APPROVED"] + reasons,
        )


class OutputGate:
    citation_pattern = re.compile(r"\[(tfda-risk-\d{4})\]")
    unsafe_directives = [
        r"你應該(立即)?(停藥|換藥|加藥|減藥)",
        r"建議你(立即)?(停藥|換藥|加藥|減藥)",
        r"你每天(吃|服用|注射)",
        r"我診斷你",
    ]

    def evaluate(self, draft: str, approved_evidence_ids: Iterable[str]) -> OutputDecision:
        approved = set(approved_evidence_ids)
        citations = set(self.citation_pattern.findall(draft))
        reasons = []
        if not draft.strip():
            reasons.append("EMPTY_DRAFT")
        if not citations:
            reasons.append("NO_TFDA_CITATION")
        unapproved = sorted(citations - approved)
        if unapproved:
            reasons.append("UNAPPROVED_CITATIONS:%s" % ",".join(unapproved))
        if approved and not (citations & approved):
            reasons.append("NO_APPROVED_EVIDENCE_CITED")
        for pattern in self.unsafe_directives:
            if re.search(pattern, draft, flags=re.IGNORECASE):
                reasons.append("PERSONALIZED_DIRECTIVE")
                break
        if "不是個別診斷、處方或停換藥建議" not in draft:
            reasons.append("MISSING_SCOPE_NOTICE")
        return OutputDecision(
            decision="BLOCK" if reasons else "PASS",
            reason_codes=reasons or ["OUTPUT_CONTRACT_VALID"],
        )


def fallback_message(reason: str) -> str:
    messages = {
        "PROMPT_INJECTION_BLOCKED": "此請求無法通過安全輸入檢查。",
        "EMERGENCY_ESCALATION_REQUIRED": "這可能涉及緊急醫療情況，請立即聯絡當地緊急醫療服務或由醫療專業人員評估。",
        "PERSONALIZED_MEDICATION_ADVICE_BLOCKED": "無法提供個人化的停藥、換藥或劑量決定，請洽醫師或藥師。",
        "MAX_AGENT_STEPS_EXCEEDED": "系統已達安全步數上限，無法完成可靠回答。",
        "MAX_TOOL_CALLS_EXCEEDED": "系統已達工具呼叫上限，無法完成可靠回答。",
        "DEADLINE_EXCEEDED": "系統已達處理時間上限，無法完成可靠回答。",
        "EVIDENCE_INSUFFICIENT": "目前 TFDA 候選證據不足以支持可靠回答，請洽醫師或藥師。",
        "OUTPUT_BLOCKED": "候選回答未通過輸出檢查，因此不予提供。",
    }
    return messages.get(reason, "系統無法完成安全處理。")

