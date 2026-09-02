from __future__ import annotations

import pytest

from tfda_context_gate.access_control import AuthorizationStatus, PermissionScope
from tfda_context_gate.intake.lean_agent import LeanIntakeAgent
from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.line_orchestration.orchestrator import ConversationOrchestrator
from tfda_context_gate.product_session.repository import SQLiteProductSessionRepository


def _make_session(tmp_path, name="session_test", status="ACTIVE", intake_stage="stage1", intake=None):
    repo = SQLiteProductSessionRepository(tmp_path / f"{name}.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key="test-key-at-least-16-bytes!")
    sess = orch._load_or_create(f"user-{name}")
    sess = sess.model_copy(update={
        "status": status,
        "intake_stage": intake_stage,
        "intake_snapshot": intake or PreVisitIntake(),
        "authorization_status": AuthorizationStatus.PATIENT_SELF,
        "permission_scopes": [
            PermissionScope.CREATE_OWN_INTAKE,
            PermissionScope.VIEW_OWN_SUMMARY,
            PermissionScope.SHARE_OWN_SUMMARY,
        ],
    }, deep=True)
    return sess


def test_lean_agent_severity_standardization():
    agent = LeanIntakeAgent()
    # 1-3 -> 輕度
    assert agent.standardize_severity("1") == "輕度"
    assert agent.standardize_severity("2分") == "輕度"
    assert agent.standardize_severity("大概3分") == "輕度"
    assert agent.standardize_severity("2/10") == "輕度"
    assert agent.standardize_severity("輕微") == "輕度"
    assert agent.standardize_severity("還好") == "輕度"

    # 4-6 -> 中度
    assert agent.standardize_severity("4") == "中度"
    assert agent.standardize_severity("5分") == "中度"
    assert agent.standardize_severity("差不多6分") == "中度"
    assert agent.standardize_severity("5/10") == "中度"
    assert agent.standardize_severity("普通") == "中度"
    assert agent.standardize_severity("中等") == "中度"

    # 7-10 -> 重度
    assert agent.standardize_severity("7") == "重度"
    assert agent.standardize_severity("8分") == "重度"
    assert agent.standardize_severity("大概7分") == "重度"
    assert agent.standardize_severity("7/10") == "重度"
    assert agent.standardize_severity("9") == "重度"
    assert agent.standardize_severity("10") == "重度"
    assert agent.standardize_severity("嚴重") == "重度"
    assert agent.standardize_severity("非常嚴重") == "重度"


def test_lean_agent_red_flag_interception(tmp_path):
    agent = LeanIntakeAgent()
    session = _make_session(tmp_path, "rf", status="ACTIVE", intake_stage="stage2")
    new_sess, resp = agent.process_turn(session, "我現在胸口劇痛，快要呼吸困難了")
    assert resp["status"] == "FALLBACK"
    assert "119" in resp["reply"]
    assert new_sess.system_risk_classification.get("level") == "RED_FLAG"


def test_lean_agent_full_intake_progression(tmp_path):
    agent = LeanIntakeAgent()
    session = _make_session(tmp_path, "flow", status="ACTIVE", intake_stage="stage1")

    # 1. 用藥
    session, r1 = agent.process_turn(session, "metformin")
    assert session.intake_snapshot.known_medications == ["metformin"]
    assert session.pending_field == "allergies"

    # 2. 過敏
    session, r2 = agent.process_turn(session, "無過敏")
    assert session.intake_snapshot.allergies == ["無"]
    assert session.pending_field == "chronic_conditions"

    # 3. 慢性病
    session, r3 = agent.process_turn(session, "高血壓")
    assert session.intake_snapshot.chronic_conditions == ["高血壓"]
    assert session.pending_field == "family_history"

    # 4. 家族史
    session, r4 = agent.process_turn(session, "無")
    assert session.intake_snapshot.family_history == ["無"]
    assert session.intake_stage == "stage2"
    assert session.pending_field == "symptom_onset"

    # 5. 發作時間
    session, r5 = agent.process_turn(session, "三天前開始")
    assert session.intake_snapshot.symptom_onset == "三天前開始"
    assert session.pending_field == "symptom_description"

    # 6. 症狀描述
    session, r6 = agent.process_turn(session, "常常口渴，晚上一直頻尿")
    assert "口渴" in session.intake_snapshot.symptom_description
    assert session.pending_field == "symptom_severity"

    # 7. 嚴重程度（測試單純數字 7）
    session, r7 = agent.process_turn(session, "7")
    assert session.intake_snapshot.symptom_severity == "重度"
    assert session.intake_stage == "stage3"
    assert session.pending_field == "questions_for_doctor"

    # 8. 想問醫師的問題
    session, r8 = agent.process_turn(session, "想問平時飲食要注意什麼？")
    assert session.intake_snapshot.questions_for_doctor == ["想問平時飲食要注意什麼？"]
    assert session.status == "AWAITING_CONFIRMATION"
    assert session.intake_stage == "review"
    assert "看診前資料整理摘要" in r8["reply"]
    assert len(r8["quick_replies"]) == 2

    # 9. 確認完成
    session, r9 = agent.process_turn(session, "完成對話")
    assert session.status == "SUBMITTED"
    assert session.intake_stage == "submitted"


def test_lean_agent_multi_clause_extraction(tmp_path):
    agent = LeanIntakeAgent()
    session = _make_session(tmp_path, "multi", status="ACTIVE", intake_stage="stage1")
    # 一次講多項：用藥 + 過敏 + 慢性病
    session, resp = agent.process_turn(session, "我有吃 metformin，沒有過敏，有高血壓")
    assert session.intake_snapshot.known_medications == ["metformin"]
    assert session.intake_snapshot.allergies == ["無"]
    assert session.intake_snapshot.chronic_conditions == ["高血壓"]
    assert session.pending_field == "family_history"


def test_lean_agent_review_modification(tmp_path):
    agent = LeanIntakeAgent()
    init_intake = PreVisitIntake(
        known_medications=["metformin"],
        allergies=["無"],
        chronic_conditions=["高血壓"],
        family_history=["無"],
        symptom_onset="三天前",
        symptom_description="早晨口渴",
        symptom_severity="中度",
        questions_for_doctor=["飲食控制"],
    )
    session = _make_session(tmp_path, "mod", status="AWAITING_CONFIRMATION", intake_stage="review", intake=init_intake)
    session, resp = agent.process_turn(session, "過敏要改成對盤尼西林過敏")
    assert session.intake_snapshot.allergies == ["盤尼西林"]
    assert session.intake_stage == "review"
    assert "盤尼西林" in resp["reply"]
