from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .schemas import CandidateResponse, PolicySnapshot


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

    prohibited_patterns: tuple[str, ...] = field(
        default_factory=lambda: (
            r"(?:你|您|病人|患者)?\s*(?:可以|應該|請)\s*(?:自行)?(?:停藥|換藥)",
            r"(?:自行|直接)\s*(?:增加|減少|調整|加倍|減半)\s*(?:用藥|藥物|藥量|劑量)",
            r"(?:把|將).{0,12}(?:劑量|藥量).{0,12}(?:調整|改成|增加|減少)",
        )
    )

    def compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(pattern, re.IGNORECASE) for pattern in self.prohibited_patterns)


@dataclass(frozen=True)
class PolicyCheck:
    failed: bool
    reason_codes: tuple[str, ...] = ()


def check_policy_snapshot(policy: PolicySnapshot) -> PolicyCheck:
    reasons: list[str] = []
    if policy.router_status not in KNOWN_ROUTER_STATUSES:
        reasons.append("POLICY_UNKNOWN_ROUTER_STATUS")
    if policy.router_status != "G_GENERAL_EDUCATION":
        reasons.append("POLICY_ROUTE_NOT_GENERAL_EDUCATION")
    if policy.rag_allowed is not True:
        reasons.append("POLICY_RAG_NOT_ALLOWED")
    hard_risks = set(policy.risk_flags).intersection(HARD_POLICY_RISKS)
    if hard_risks:
        reasons.append("POLICY_HARD_RISK_PRESENT")
    if "MEDICATION_CHANGE_REQUEST" in set(policy.intent_tags):
        reasons.append("POLICY_MEDICATION_CHANGE_REQUEST")
    return PolicyCheck(bool(reasons), tuple(dict.fromkeys(reasons)))


def check_candidate_red_lines(
    candidate: CandidateResponse,
    config: PolicyRuleConfig,
) -> PolicyCheck:
    text = "\n".join(
        [candidate.answer, *(claim.claim for claim in candidate.supported_claims)]
    )
    reasons = [
        "POLICY_EXPLICIT_OUTPUT_REDLINE"
        for pattern in config.compiled()
        if pattern.search(text)
    ]
    return PolicyCheck(bool(reasons), tuple(dict.fromkeys(reasons)))


def iter_candidate_text(candidate: CandidateResponse) -> Iterable[str]:
    yield candidate.answer
    yield from (claim.claim for claim in candidate.supported_claims)
