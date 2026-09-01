"""第 7 步驗收測試：CONTRACT_v1 §2.5 裡的每一種 relation 都對應正確，
vector chunk 永遠是 UNKNOWN（絕不是 LOW），且 max_evidence_risk_level
是算出來的摘要值，不是直接斷言出來的。
"""

import pytest

from rag_retrieval.contract.enums import EvidenceRiskLevel as R
from rag_retrieval.risk import compute_risk, max_evidence_risk_level


@pytest.mark.parametrize(
    "relation,expected_level,expected_signal",
    [
        ("CONTRAINDICATED_FOR", R.HIGH, "CONTRAINDICATION"),
        ("INDUCES", R.HIGH, "SERIOUS_ADVERSE_EVENT"),
        ("RISK_FACTOR_FOR", R.MEDIUM, "RISK_FACTOR"),
        ("TRIGGERS", R.HIGH, "TRIGGER"),
        ("CAUTION_FOR", R.MEDIUM, "CAUTION"),
        ("INTERACTS_WITH", R.MEDIUM, "INTERACTION"),
        ("REQUIRES_MONITORING", R.MEDIUM, "MONITORING"),
        ("CAUSES_SIDE_EFFECT", R.LOW, "SIDE_EFFECT"),
        ("TREATS", R.LOW, "GENERAL"),
        ("IS_A", R.LOW, "GENERAL"),
    ],
)
def test_relation_lookup_table_matches_contract(relation, expected_level, expected_signal):
    level, signals, basis = compute_risk(relation, "src", "cond", "content")
    assert level == expected_level
    assert signals == [expected_signal]
    assert relation in basis


def test_vector_chunk_is_always_unknown_never_low():
    level, signals, basis = compute_risk(None, "src", None, "content")
    assert level == R.UNKNOWN
    assert signals == []
    assert basis == "vector chunk，無結構化關係，風險等級不可推導"


def test_caution_for_never_upgraded_to_high():
    level, _, _ = compute_risk("CAUTION_FOR", "src", "eGFR 30-45", "text")
    assert level == R.MEDIUM


def test_max_evidence_risk_level_excludes_unknown_unless_all_unknown():
    assert max_evidence_risk_level([R.UNKNOWN, R.MEDIUM, R.LOW]) == R.MEDIUM
    assert max_evidence_risk_level([R.LOW, R.HIGH]) == R.HIGH
    assert max_evidence_risk_level([R.UNKNOWN, R.UNKNOWN]) == R.UNKNOWN
    assert max_evidence_risk_level([]) == R.UNKNOWN
