from __future__ import annotations

from dataclasses import dataclass

# 本模組為政策閘門（Policy Gate）：管線第 6 步，將 RouterSignals 映射為唯一 RouterStatus
# 優先級管線（由高至低）：注入攻擊 → 急症 → 緊急轉真人 → 個人化用藥 → 一般藥物資訊 → 診斷請求 → 超範圍 → 需釐清 → 一般衛教
# 僅 G_GENERAL_EDUCATION 允許 RAG，其餘皆阻擋
# 維護注意：本檔的連續 if 是刻意設計（可稽核、順序即優先級），非重構遺漏；請勿在未經政策核准前改為表驅動或規則引擎

from .labels import IntentTag, PolicyReasonCode, RiskFlag, RouterStatus
from .schemas import RouterSignals


@dataclass(frozen=True)
class PolicyConfig:
    """政策配置：僅做路由映射，臨床觸發檢測刻意外部化。
    預設值遵循專案文件的八路由政策表；當有正式核准的硬規則時，擁有者可替換風險映射，無需改動路由器或下游契約。
    Route mapping only; clinical trigger detection is intentionally external.

    The defaults follow the eight-route policy tables in the project documents.
    An owner can replace the risk mapping when formally approved hard rules are
    available, without changing the router or downstream contract.
    """

    emergency_risks: tuple[RiskFlag, ...] = (RiskFlag.POSSIBLE_EMERGENCY,)  # 觸發 E_EMERGENCY 的風險集合
    urgent_risks: tuple[RiskFlag, ...] = (
        RiskFlag.MENTAL_HEALTH_CRISIS,  # 心理危機 → U_URGENT_HUMAN
        RiskFlag.HIGH_RISK_NOT_EXCLUDED,  # 高風險未排除 → U_URGENT_HUMAN
    )  # 觸發 U_URGENT_HUMAN 的風險集合


DEFAULT_POLICY = PolicyConfig()


@dataclass(frozen=True)
class PolicyDecision:
    """政策決策結果：單一路由狀態與對應原因碼。"""

    status: RouterStatus  # 路由狀態（8 選 1）
    reason_codes: tuple[PolicyReasonCode, ...]  # 原因碼（至少一個，供稽核）


def policy_gate(signals: RouterSignals, config: PolicyConfig = DEFAULT_POLICY) -> PolicyDecision:
    """政策閘門：從已驗證訊號確定性地回傳唯一路由（管線第 6 步）。
    優先級：注入否決 > 急症 > 緊急轉真人 > 用藥轉介 > 診斷/超範圍 > 釐清 > 一般衛教。
    提示注入為固定安全否決，導向既有 R_POLICY_BOUNDARY，不新增路由且不可被語意或身分覆蓋。
    Return exactly one deterministic route from validated signals.

    Prompt injection is a fixed security veto.  It routes to the existing
    policy-boundary status; it does not create a new route and cannot be
    overridden by semantic signals or a declared role.
    輸入：signals（觀測訊號）、config（政策配置，預設 DEFAULT_POLICY）
    輸出：PolicyDecision（唯一狀態 + 原因碼）

    設計說明（給維護者）：
    - 本函式刻意用「一堆 if + 優先級早回傳」而非表驅動／規則引擎：8 條規則內可讀性與可稽核性最佳，
      稽核時可逐行對照政策文件，無需追字典或引擎排序。
    - 順序即優先級，不可重排；重排需更新文件與測試並經政策擁有者核准。
    - 未來若規則 >20 條或需 PM 熱更新，再考慮改表驅動／OPA，MVP 階段不提前抽象。
    """

    reasons: list[PolicyReasonCode] = []  # 收集原因碼
    risks = set(signals.risk_flags)  # 轉集合加速交集判斷
    intents = set(signals.intent_tags)

    # 1. 最高優先：提示注入安全否決 → R_POLICY_BOUNDARY（不可被覆蓋）
    if RiskFlag.PROMPT_INJECTION_SUSPECTED in risks:
        reasons.append(PolicyReasonCode.REASON_PROMPT_INJECTION_SUSPECTED)
        return PolicyDecision(RouterStatus.R_POLICY_BOUNDARY, tuple(reasons))

    # 2. 急症風險 → E_EMERGENCY
    if risks.intersection(config.emergency_risks):
        reasons.append(PolicyReasonCode.REASON_POSSIBLE_EMERGENCY)
        return PolicyDecision(RouterStatus.E_EMERGENCY, tuple(reasons))

    # 3. 緊急轉真人：心理危機或高風險未排除 → U_URGENT_HUMAN
    if risks.intersection(config.urgent_risks):
        if RiskFlag.MENTAL_HEALTH_CRISIS in risks:
            reasons.append(PolicyReasonCode.REASON_MENTAL_HEALTH_CRISIS)
        else:
            reasons.append(PolicyReasonCode.REASON_HIGH_RISK_NOT_EXCLUDED)
        return PolicyDecision(RouterStatus.U_URGENT_HUMAN, tuple(reasons))

    # 4. 個人化用藥（風險或意圖任一命中）→ M_MEDICATION_REFERRAL
    if (
        RiskFlag.PERSONALIZED_MEDICATION in risks
        or IntentTag.MEDICATION_CHANGE_REQUEST in intents
    ):
        reasons.append(PolicyReasonCode.REASON_PERSONALIZED_MEDICATION_REQUEST)
        return PolicyDecision(RouterStatus.M_MEDICATION_REFERRAL, tuple(reasons))

    # 5. 一般藥物資訊 → 僅當伴隨個人化用藥風險時才轉介；純通識視為一般衛教（由後續步驟處理）
    if IntentTag.GENERAL_MEDICATION_INFORMATION in intents and RiskFlag.PERSONALIZED_MEDICATION in risks:
        reasons.append(PolicyReasonCode.INQUIRY_GENERAL_MEDICATION_INFORMATION)
        return PolicyDecision(RouterStatus.M_MEDICATION_REFERRAL, tuple(reasons))

    # 6. 診斷請求 → R_POLICY_BOUNDARY（政策邊界，不可直接診斷）
    if IntentTag.DIAGNOSIS_REQUEST in intents:
        reasons.append(PolicyReasonCode.REASON_DIAGNOSIS_OR_TREATMENT_REQUEST)
        return PolicyDecision(RouterStatus.R_POLICY_BOUNDARY, tuple(reasons))

    # 7. 超範圍：僅含 NON_MEDICAL 且無教育/症狀意圖 → O_OUT_OF_SCOPE
    if IntentTag.NON_MEDICAL in intents and not (
        intents & {IntentTag.GENERAL_EDUCATION, IntentTag.SYMPTOM_INFORMATION}
    ):
        reasons.append(PolicyReasonCode.REASON_OUT_OF_SCOPE)
        return PolicyDecision(RouterStatus.O_OUT_OF_SCOPE, tuple(reasons))

    # 8. 無任何意圖 → Q_CLARIFICATION（資訊不足）
    if not intents:
        reasons.append(PolicyReasonCode.REASON_INSUFFICIENT_INFORMATION)
        return PolicyDecision(RouterStatus.Q_CLARIFICATION, tuple(reasons))

    # 9. 有意圖但需判斷是否可進一般衛教（含看診前整理 proactive trigger）
    if IntentTag.PRE_VISIT_INTAKE in intents:
        reasons.append(PolicyReasonCode.INQUIRY_GENERAL_EDUCATION)
    elif IntentTag.SYMPTOM_INFORMATION in intents:
        reasons.append(PolicyReasonCode.INQUIRY_SYMPTOM_INFORMATION)
    elif IntentTag.GENERAL_MEDICATION_INFORMATION in intents:
        reasons.append(PolicyReasonCode.INQUIRY_GENERAL_MEDICATION_INFORMATION)
    elif IntentTag.GENERAL_EDUCATION in intents:
        reasons.append(PolicyReasonCode.INQUIRY_GENERAL_EDUCATION)
    else:
        reasons.append(PolicyReasonCode.REASON_INSUFFICIENT_INFORMATION)
        return PolicyDecision(RouterStatus.Q_CLARIFICATION, tuple(reasons))

    # 10. 通過安全檢查 → G_GENERAL_EDUCATION（唯一允許 RAG 的狀態）
    reasons.extend(
        [
            PolicyReasonCode.NO_CRITICAL_SYMPTOMS_DETECTED,  # 佐證：未檢出危急症狀
            PolicyReasonCode.MEETS_SAFE_SCOPE,  # 佐證：符合安全範圍
        ]
    )
    return PolicyDecision(RouterStatus.G_GENERAL_EDUCATION, tuple(reasons))
