from __future__ import annotations

from dataclasses import dataclass

from .labels import IntentTag, PolicyReasonCode, RiskFlag, RouterStatus
from .schemas import RouterSignals


@dataclass(frozen=True)
class PolicyConfig:
    """Route mapping only; clinical trigger detection is intentionally external.

    The defaults follow the eight-route policy tables in the project documents.
    An owner can replace the risk mapping when formally approved hard rules are
    available, without changing the router or downstream contract.
    """

    emergency_risks: tuple[RiskFlag, ...] = (RiskFlag.POSSIBLE_EMERGENCY,)
    urgent_risks: tuple[RiskFlag, ...] = (
        RiskFlag.MENTAL_HEALTH_CRISIS,
        RiskFlag.HIGH_RISK_NOT_EXCLUDED,
    )


DEFAULT_POLICY = PolicyConfig()


@dataclass(frozen=True)
class PolicyDecision:
    status: RouterStatus
    reason_codes: tuple[PolicyReasonCode, ...]


def policy_gate(signals: RouterSignals, config: PolicyConfig = DEFAULT_POLICY) -> PolicyDecision:
    """Return exactly one deterministic route from validated signals.

    Prompt injection is a fixed security veto.  It routes to the existing
    policy-boundary status; it does not create a new route and cannot be
    overridden by semantic signals or a declared role.
    """

    reasons: list[PolicyReasonCode] = []
    risks = set(signals.risk_flags)
    intents = set(signals.intent_tags)

    if RiskFlag.PROMPT_INJECTION_SUSPECTED in risks:
        reasons.append(PolicyReasonCode.REASON_PROMPT_INJECTION_SUSPECTED)
        return PolicyDecision(RouterStatus.R_POLICY_BOUNDARY, tuple(reasons))

    if risks.intersection(config.emergency_risks):
        reasons.append(PolicyReasonCode.REASON_POSSIBLE_EMERGENCY)
        return PolicyDecision(RouterStatus.E_EMERGENCY, tuple(reasons))

    if risks.intersection(config.urgent_risks):
        if RiskFlag.MENTAL_HEALTH_CRISIS in risks:
            reasons.append(PolicyReasonCode.REASON_MENTAL_HEALTH_CRISIS)
        else:
            reasons.append(PolicyReasonCode.REASON_HIGH_RISK_NOT_EXCLUDED)
        return PolicyDecision(RouterStatus.U_URGENT_HUMAN, tuple(reasons))

    if (
        RiskFlag.PERSONALIZED_MEDICATION in risks
        or IntentTag.MEDICATION_CHANGE_REQUEST in intents
    ):
        reasons.append(PolicyReasonCode.REASON_PERSONALIZED_MEDICATION_REQUEST)
        return PolicyDecision(RouterStatus.M_MEDICATION_REFERRAL, tuple(reasons))

    if IntentTag.GENERAL_MEDICATION_INFORMATION in intents:
        reasons.append(PolicyReasonCode.INQUIRY_GENERAL_MEDICATION_INFORMATION)
        return PolicyDecision(RouterStatus.M_MEDICATION_REFERRAL, tuple(reasons))

    if IntentTag.DIAGNOSIS_REQUEST in intents:
        reasons.append(PolicyReasonCode.REASON_DIAGNOSIS_OR_TREATMENT_REQUEST)
        return PolicyDecision(RouterStatus.R_POLICY_BOUNDARY, tuple(reasons))

    if IntentTag.NON_MEDICAL in intents and not (
        intents & {IntentTag.GENERAL_EDUCATION, IntentTag.SYMPTOM_INFORMATION}
    ):
        reasons.append(PolicyReasonCode.REASON_OUT_OF_SCOPE)
        return PolicyDecision(RouterStatus.O_OUT_OF_SCOPE, tuple(reasons))

    if not intents:
        reasons.append(PolicyReasonCode.REASON_INSUFFICIENT_INFORMATION)
        return PolicyDecision(RouterStatus.Q_CLARIFICATION, tuple(reasons))

    if IntentTag.SYMPTOM_INFORMATION in intents:
        reasons.append(PolicyReasonCode.INQUIRY_SYMPTOM_INFORMATION)
    elif IntentTag.GENERAL_MEDICATION_INFORMATION in intents:
        reasons.append(PolicyReasonCode.INQUIRY_GENERAL_MEDICATION_INFORMATION)
    elif IntentTag.GENERAL_EDUCATION in intents:
        reasons.append(PolicyReasonCode.INQUIRY_GENERAL_EDUCATION)
    else:
        reasons.append(PolicyReasonCode.REASON_INSUFFICIENT_INFORMATION)
        return PolicyDecision(RouterStatus.Q_CLARIFICATION, tuple(reasons))

    reasons.extend(
        [
            PolicyReasonCode.NO_CRITICAL_SYMPTOMS_DETECTED,
            PolicyReasonCode.MEETS_SAFE_SCOPE,
        ]
    )
    return PolicyDecision(RouterStatus.G_GENERAL_EDUCATION, tuple(reasons))
