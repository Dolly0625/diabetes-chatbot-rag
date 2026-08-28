"""D 輸出閘門政策檢查（Policy）— 繁體中文註解版

本檔案定義政策快照的校驗規則與顯式輸出紅線，邏輯零改動，僅補充中文說明。

【在 8 步流水線中的定位：步驟 6 A 風險紅線】
  check_policy_snapshot 檢查 A 快照的路由/風險/意圖是否觸及紅線
  check_candidate_red_lines 檢查候選回答文本是否含顯式紅線短語（如自行停藥）
  任一失敗 → FALLBACK + POLICY

【PolicySnapshot 字串快照 vs A enum 的設計】
  A 內部以 Enum 強型別管理路由狀態（如 G_GENERAL_EDUCATION）；
  D 刻意用 str 快照（見 schemas.PolicySnapshot.router_status: str），
  將 A 的最終決策當作「事實」拷貝，不推斷、不覆蓋，避免 D 與 A 的 Enum 定義耦合。
  D 僅以字串白名單（KNOWN_ROUTER_STATUSES）與字串比對做校驗，
  若 A 新增狀態，D 能以 POLICY_UNKNOWN_ROUTER_STATUS 明確失敗，而非解析期崩潰。

【風險紅線分類】
  硬風險（HARD_POLICY_RISKS）：POSSIBLE_EMERGENCY、MENTAL_HEALTH_CRISIS 等 5 類，命中即 POLICY_HARD_RISK_PRESENT
  路由紅線：非 G_GENERAL_EDUCATION 一律 POLICY_ROUTE_NOT_GENERAL_EDUCATION
  意圖紅線：MEDICATION_CHANGE_REQUEST 即 POLICY_MEDICATION_CHANGE_REQUEST
  顯式輸出紅線：候選文本含「自行停藥/調整劑量」等正則即 POLICY_EXPLICIT_OUTPUT_REDLINE
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .schemas import CandidateResponse, PolicySnapshot


# 已知路由狀態白名單（字串集合，非 Enum）
# 為何用字串集合：對應 PolicySnapshot 的字串快照設計，避免與 A 的 Enum 耦合
# 為何僅 G_GENERAL_EDUCATION 可放行：D 僅允許一般衛教類路由，其餘皆需降級
KNOWN_ROUTER_STATUSES = {
    "E_EMERGENCY",
    "U_URGENT_HUMAN",
    "M_MEDICATION_REFERRAL",
    "R_POLICY_BOUNDARY",
    "Q_CLARIFICATION",
    "G_GENERAL_EDUCATION",
    "O_OUT_OF_SCOPE",
    "F_ROUTER_DEPENDENCY",
}

# 硬風險集合：命中任一即視為高風險，不可放行
# 為何這 5 類為硬風險：涉及急症、心理危機、個人化用藥、高風險未排除、提示注入，均需人工介入
HARD_POLICY_RISKS = {
    "POSSIBLE_EMERGENCY",
    "MENTAL_HEALTH_CRISIS",
    "PERSONALIZED_MEDICATION",
    "HIGH_RISK_NOT_EXCLUDED",
    "PROMPT_INJECTION_SUSPECTED",
}


@dataclass(frozen=True)
class PolicyRuleConfig:
    """Configurable output red-lines.

    A's route/risk fields are authoritative. The phrase rules below are only
    explicit, candidate red-lines documented by D (e.g. direct stop/change
    medication instructions); they are not clinical thresholds and must be
    reviewed before production use.
    """

    # ── 可配置的顯式輸出紅線 ──
    # 為何可配置：紅線短語需隨政策演進調整，以 dataclass 參數化而非硬編碼
    # 為何 frozen：配置不可變，避免運行時被意外修改
    # 警告：以下僅為顯式候選紅線（如直接指示停藥/調劑量），非臨床閾值，生產前需審查

    prohibited_patterns: tuple[str, ...] = field(
        default_factory=lambda: (
            r"(?:你|您|病人|患者)?\s*(?:可以|應該|請)\s*(?:自行)?(?:停藥|換藥)",
            r"(?:自行|直接)\s*(?:增加|減少|調整|加倍|減半)\s*(?:用藥|藥物|藥量|劑量)",
            r"(?:把|將).{0,12}(?:劑量|藥量).{0,12}(?:調整|改成|增加|減少)",
        )
    )
    clinician_prohibited_patterns: tuple[str, ...] = field(
        default_factory=lambda: (
            r"建議(?:您|你|病人|患者)?.{0,10}(?:劑量|服用|使用).{0,10}\d+\s*(?:mg|毫克|單位|g)",
            r"處方.{0,8}(?:為|是).{0,8}\d+\s*(?:mg|毫克)",
            r"確診為.{0,12}糖尿病",
            r"你就是糖尿病",
            r"請(?:直接|立即)服用.{0,8}\d+",
            r"每日服用.{0,8}\d+\s*(?:mg|毫克)",
            r"劑量.{0,8}(?:為|是|調整為).{0,8}\d+",
        )
    )
    intake_prohibited_patterns: tuple[str, ...] = field(
        default_factory=lambda: (
            r"確診為.{0,12}糖尿病",
            r"你就是糖尿病",
            r"診斷為.{0,12}(?:糖尿病|高血糖)",
            r"建議(?:您|你|病人|患者)?.{0,10}(?:劑量|服用|使用|治療)",
            r"處方(?:為|是).{0,8}\d+",
            r"請(?:直接|立即)?(?:服用|使用|調整).{0,8}(?:藥物|劑量|胰島素)",
            r"治療方案.{0,8}(?:為|是)",
            r"調整(?:劑量|用藥).{0,8}(?:為|至|到)\s*\d+",
            r"每日服用.{0,8}\d+",
        )
    )

    def compiled(self) -> tuple[re.Pattern[str], ...]:
        # 編譯正則，供 check_candidate_red_lines 使用
        # 為何用 re.IGNORECASE：不區分大小寫，覆蓋中英文混合場景
        return tuple(re.compile(pattern, re.IGNORECASE) for pattern in self.prohibited_patterns)


@dataclass(frozen=True)
class PolicyCheck:
    # 政策檢查結果
    failed: bool  # 是否觸及紅線
    reason_codes: tuple[str, ...] = ()  # 觸及的原因碼集合


def check_policy_snapshot(policy: PolicySnapshot) -> PolicyCheck:
    # 檢查 A 策略快照是否觸及政策紅線（8 步流水線步驟 6a）
    # 為何逐項檢查：每項對應獨立的 POLICY 原因碼，需精確區分失敗根因
    reasons: list[str] = []
    if policy.router_status not in KNOWN_ROUTER_STATUSES:
        # 為何檢查：未知路由狀態代表 A 的決策超出 D 已知白名單，不可信任
        reasons.append("POLICY_UNKNOWN_ROUTER_STATUS")
    if policy.router_status != "G_GENERAL_EDUCATION":
        # 為何檢查：D 僅允許一般衛教路由；其他路由（急症、轉介、澄清等）皆需降級為 FALLBACK
        reasons.append("POLICY_ROUTE_NOT_GENERAL_EDUCATION")
    if policy.rag_allowed is not True:
        # 為何檢查：RAG 未被 A 允許時，證據集可信度不足，不可放行
        # 為何用 is not True 而非 not：嚴格要求布林 True，避免 truthy 值誤判
        reasons.append("POLICY_RAG_NOT_ALLOWED")
    hard_risks = set(policy.risk_flags).intersection(HARD_POLICY_RISKS)
    if hard_risks:
        # 為何檢查：命中任一硬風險（急症/心理危機/個人化用藥等）即需人工介入，不可自動放行
        reasons.append("POLICY_HARD_RISK_PRESENT")
    if "MEDICATION_CHANGE_REQUEST" in set(policy.intent_tags):
        # 為何檢查：使用者意圖為藥物變更請求時，需轉介而非直接回答
        reasons.append("POLICY_MEDICATION_CHANGE_REQUEST")
    return PolicyCheck(bool(reasons), tuple(dict.fromkeys(reasons)))
    # 為何去重：多項風險可能映射到同一原因碼，需去重


def check_candidate_red_lines(
    candidate: CandidateResponse,
    config: PolicyRuleConfig,
) -> PolicyCheck:
    claims = candidate.evidence_summary if candidate.decision == "CLINICIAN_DRAFT" and candidate.evidence_summary else candidate.supported_claims
    text = "\n".join(
        [candidate.answer, *(claim.claim for claim in claims), *candidate.conflicts, candidate.disclaimer or ""]
    )
    reasons: list[str] = []
    for pattern in config.compiled():
        if pattern.search(text):
            reasons.append("POLICY_EXPLICIT_OUTPUT_REDLINE")
    if candidate.decision == "CLINICIAN_DRAFT":
        for pat in config.clinician_prohibited_patterns:
            if re.compile(pat, re.IGNORECASE).search(text):
                reasons.append("CLINICIAN_PERSONALIZED_DOSAGE_OR_DIAGNOSIS")
                break
    return PolicyCheck(bool(reasons), tuple(dict.fromkeys(reasons)))


def check_previsit_summary(
    summary_text: str,
    disclaimer: str | None = None,
    config: PolicyRuleConfig | None = None,
) -> PolicyCheck:
    cfg = config or PolicyRuleConfig()
    text = summary_text + ("\n" + disclaimer if disclaimer else "")
    reasons: list[str] = []
    for pat in cfg.intake_prohibited_patterns:
        if re.compile(pat, re.IGNORECASE).search(text):
            reasons.append("PREVISIT_SUMMARY_CONTAINS_DIAGNOSIS_OR_TREATMENT")
            break
    for pat in cfg.prohibited_patterns:
        if re.compile(pat, re.IGNORECASE).search(text):
            reasons.append("POLICY_EXPLICIT_OUTPUT_REDLINE")
            break
    return PolicyCheck(bool(reasons), tuple(dict.fromkeys(reasons)))


def iter_candidate_text(candidate: CandidateResponse) -> Iterable[str]:
    yield candidate.answer
    claims = candidate.evidence_summary if candidate.decision == "CLINICIAN_DRAFT" and candidate.evidence_summary else candidate.supported_claims
    yield from (claim.claim for claim in claims)
    yield from candidate.conflicts
    if candidate.disclaimer:
        yield candidate.disclaimer
