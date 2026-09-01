"""風險是查表得出的，不是判斷出來的（CLAUDE.md 不可退讓事項 #7）。
relation -> evidence_risk_level 對照表逐字取自 CONTRACT_v1 §2.5。絕不讓
LLM 推論風險等級；絕不把 CAUTION_FOR 升級成禁忌。
"""

from __future__ import annotations

from typing import Optional, Sequence

from .contract.enums import EvidenceRiskLevel, SafetySignalType
from .retrievers.base import Candidate

# relation -> (evidence_risk_level, safety_signal_type)。CONTRACT_v1 §2.5。
#
# RISK_FACTOR_FOR 的文件寫的是「HIGH（禁忌路徑上）／MEDIUM」：只有當該
# risk factor 落在通往 CONTRAINDICATED_FOR 三元組的路徑上時才是 HIGH。
# 目前沒有任何可檢索的 CONTRAINDICATED_FOR 三元組（CLAUDE.md §4），
# 所以這條升級路徑在現有資料上根本走不到；預設為 MEDIUM 是文件明訂的
# 後備值，不是隨便猜的。
_RISK_TABLE: dict[str, tuple[EvidenceRiskLevel, SafetySignalType]] = {
    "CONTRAINDICATED_FOR": (EvidenceRiskLevel.HIGH, SafetySignalType.CONTRAINDICATION),
    "INDUCES": (EvidenceRiskLevel.HIGH, SafetySignalType.SERIOUS_ADVERSE_EVENT),
    "RISK_FACTOR_FOR": (EvidenceRiskLevel.MEDIUM, SafetySignalType.RISK_FACTOR),
    "TRIGGERS": (EvidenceRiskLevel.HIGH, SafetySignalType.TRIGGER),
    "CAUTION_FOR": (EvidenceRiskLevel.MEDIUM, SafetySignalType.CAUTION),
    "INTERACTS_WITH": (EvidenceRiskLevel.MEDIUM, SafetySignalType.INTERACTION),
    "REQUIRES_MONITORING": (EvidenceRiskLevel.MEDIUM, SafetySignalType.MONITORING),
    "CAUSES_SIDE_EFFECT": (EvidenceRiskLevel.LOW, SafetySignalType.SIDE_EFFECT),
    "TREATS": (EvidenceRiskLevel.LOW, SafetySignalType.GENERAL),
    "IS_A": (EvidenceRiskLevel.LOW, SafetySignalType.GENERAL),
}

_RISK_ORDER = {EvidenceRiskLevel.LOW: 0, EvidenceRiskLevel.MEDIUM: 1, EvidenceRiskLevel.HIGH: 2}

_VECTOR_RISK_BASIS = "vector chunk，無結構化關係，風險等級不可推導"


def compute_risk(
    relation_type: Optional[str],
    source: str,
    condition: Optional[str],
    content: str,
) -> tuple[EvidenceRiskLevel, list[SafetySignalType], str]:
    """Vector chunk（relation_type 為 None）恆為 UNKNOWN——沒有 relation
    不代表風險低（CLAUDE.md 不可退讓事項 #9）。
    """
    if relation_type is None:
        return EvidenceRiskLevel.UNKNOWN, [], _VECTOR_RISK_BASIS

    if relation_type not in _RISK_TABLE:
        # schema v3 裡從未出現過——寧可安全失敗，也不要用猜的。
        return EvidenceRiskLevel.UNKNOWN, [], (
            f"unrecognised relation type {relation_type!r}; risk level withheld"
        )

    level, signal = _RISK_TABLE[relation_type]
    basis = f"{relation_type} | {source} | {condition or '無條件限制'} | 原文：{content}"
    return level, [signal], basis


def annotate_candidate(candidate: Candidate) -> tuple[EvidenceRiskLevel, list[SafetySignalType], str]:
    return compute_risk(candidate.relation_type, candidate.source, _condition_of(candidate), candidate.content)


def _condition_of(candidate: Candidate) -> Optional[str]:
    if candidate.relations:
        return candidate.relations[0].get("condition")
    return None


def max_evidence_risk_level(levels: Sequence[EvidenceRiskLevel]) -> EvidenceRiskLevel:
    """response 層級的摘要值：取所有 chunk 等級的最大值，UNKNOWN 不參與
    取大，除非全部都是 UNKNOWN（CONTRACT_v1 §2）。"""
    known = [level for level in levels if level != EvidenceRiskLevel.UNKNOWN]
    if not known:
        return EvidenceRiskLevel.UNKNOWN
    return max(known, key=lambda level: _RISK_ORDER[level])
