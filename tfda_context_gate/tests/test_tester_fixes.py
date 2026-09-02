from __future__ import annotations

import pytest

from tfda_context_gate.access_control import AuthorizationStatus, InformationSource, PermissionScope
from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.line_orchestration.orchestrator import (
    _standardize_severity,
    _SEVERITY_EXPLICIT_RE,
    ConversationOrchestrator,
)
from tfda_context_gate.intake.tool import PreVisitIntakeTool
from tfda_context_gate.product_session.schemas import ProductSession


def test_severity_standardization_unit():
    """Verify 1-10 scores, fractional scores, and natural terms map correctly."""
    # 1-3 -> 輕度
    assert _standardize_severity("1") == "輕度"
    assert _standardize_severity("2") == "輕度"
    assert _standardize_severity("3") == "輕度"
    assert _standardize_severity("2分") == "輕度"
    assert _standardize_severity("大概2分") == "輕度"
    assert _standardize_severity("2/10") == "輕度"
    assert _standardize_severity("輕微") == "輕度"
    assert _standardize_severity("還好") == "輕度"

    # 4-6 -> 中度
    assert _standardize_severity("4") == "中度"
    assert _standardize_severity("5") == "中度"
    assert _standardize_severity("6") == "中度"
    assert _standardize_severity("4分") == "中度"
    assert _standardize_severity("差不多5分") == "中度"
    assert _standardize_severity("5/10") == "中度"
    assert _standardize_severity("普通") == "中度"
    assert _standardize_severity("中等") == "中度"

    # 7-10 -> 重度
    assert _standardize_severity("7") == "重度"
    assert _standardize_severity("8") == "重度"
    assert _standardize_severity("9") == "重度"
    assert _standardize_severity("10") == "重度"
    assert _standardize_severity("7分") == "重度"
    assert _standardize_severity("大概7分") == "重度"
    assert _standardize_severity("7/10") == "重度"
    assert _standardize_severity("嚴重") == "重度"
    assert _standardize_severity("非常嚴重") == "重度"


def test_previsit_intake_tool_extract_severity():
    tool = PreVisitIntakeTool()
    assert tool._extract_severity("7") == "重度"
    assert tool._extract_severity("7分") == "重度"
    assert tool._extract_severity("大概7分") == "重度"
    assert tool._extract_severity("大概7") == "重度"
    assert tool._extract_severity("4/10") == "中度"
    assert tool._extract_severity("2分") == "輕度"
    assert tool._extract_severity("普通") == "中度"
    assert tool._extract_severity("很嚴重") == "重度"


def test_orchestrator_severity_progression_bare_digit_7(tmp_path):
    """When symptom_severity is pending, sending '7' records '重度' and advances to questions_for_doctor."""
    from tfda_context_gate.product_session.repository import SQLiteProductSessionRepository
    repo = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orch = ConversationOrchestrator(
        repository=repo,
        identity_hash_key="test-key-16-bytes-long",
        use_formal=False,
    )
    session = orch._load_or_create("patient-test-7")
    session = session.model_copy(update={
        "authorization_status": AuthorizationStatus.PATIENT_SELF,
        "status": "ACTIVE",
        "intake_stage": "stage2",
        "pending_field": "symptom_severity",
        "pending_question": "請問症狀的嚴重程度大概如何？可以輸入 1～10 分，或是輕度、中度、重度。",
        "intake_snapshot": PreVisitIntake(
            known_medications=["metformin"],
            allergies=["無"],
            chronic_conditions=["糖尿病"],
            family_history=["無"],
            symptom_onset="三天前",
            symptom_description="早晨血糖偏高",
        ),
    }, deep=True)
    session = repo.save(session, expected_version=session.version)

    # User enters bare digit "7"
    orch._process_text(session, "7")
    saved = repo.get(session.session_id)
    assert saved.intake_snapshot.symptom_severity == "重度"
    assert saved.pending_field == "questions_for_doctor"


def test_orchestrator_severity_invalid_input_prompts_clearly(tmp_path):
    """When symptom_severity is pending, sending invalid input prompts user clearly without advancing."""
    from tfda_context_gate.product_session.repository import SQLiteProductSessionRepository
    repo = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orch = ConversationOrchestrator(
        repository=repo,
        identity_hash_key="test-key-16-bytes-long",
        use_formal=False,
    )
    session = orch._load_or_create("patient-test-invalid-sev")
    session = session.model_copy(update={
        "authorization_status": AuthorizationStatus.PATIENT_SELF,
        "status": "ACTIVE",
        "intake_stage": "stage2",
        "pending_field": "symptom_severity",
        "pending_question": "請問症狀的嚴重程度大概如何？可以輸入 1～10 分，或是輕度、中度、重度。",
        "intake_snapshot": PreVisitIntake(
            known_medications=["metformin"],
            allergies=["無"],
            chronic_conditions=["糖尿病"],
            family_history=["無"],
            symptom_onset="三天前",
            symptom_description="早晨血糖偏高",
        ),
    }, deep=True)
    session = repo.save(session, expected_version=session.version)

    # User enters invalid characters / out of range
    result = orch._process_text(session, "???")
    assert "請輸入 1～10 的數字" in getattr(result, "reply", "")
    saved = repo.get(session.session_id)
    assert saved.intake_snapshot.symptom_severity is None


def test_orchestrator_completion_keywords(tmp_path):
    """User typing '完成對話' or '完成' in review stage marks session as SUBMITTED."""
    from tfda_context_gate.product_session.repository import SQLiteProductSessionRepository
    repo = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orch = ConversationOrchestrator(
        repository=repo,
        identity_hash_key="test-key-16-bytes-long",
        use_formal=False,
    )
    for cmd in ["完成對話", "完成", "結束對話", "確認完成"]:
        session = orch._load_or_create(f"patient-confirm-{cmd}")
        session = session.model_copy(update={
            "authorization_status": AuthorizationStatus.PATIENT_SELF,
            "status": "AWAITING_CONFIRMATION",
            "intake_stage": "review",
            "permission_scopes": [
                PermissionScope.CREATE_OWN_INTAKE,
                PermissionScope.VIEW_OWN_SUMMARY,
                PermissionScope.SHARE_OWN_SUMMARY,
            ],
            "intake_snapshot": PreVisitIntake(
                known_medications=["metformin"],
                allergies=["無"],
                chronic_conditions=["糖尿病"],
                family_history=["無"],
                symptom_onset="三天前",
                symptom_description="早晨血糖偏高",
                symptom_severity="重度",
                questions_for_doctor=["飲食要注意什麼？"],
            ),
        }, deep=True)
        session = repo.save(session, expected_version=session.version)

        orch._process_text(session, cmd)
        saved = repo.get(session.session_id)
        assert saved.status == "SUBMITTED"
        assert saved.intake_stage == "submitted"


def test_review_stage_does_not_trigger_education_rag(tmp_path):
    """In review stage, editing questions or information does not get routed to education RAG."""
    from tfda_context_gate.product_session.repository import SQLiteProductSessionRepository
    repo = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orch = ConversationOrchestrator(
        repository=repo,
        identity_hash_key="test-key-16-bytes-long",
        use_formal=True,
    )
    session = orch._load_or_create("patient-review-edit")
    session = session.model_copy(update={
        "authorization_status": AuthorizationStatus.PATIENT_SELF,
        "status": "AWAITING_CONFIRMATION",
        "intake_stage": "review",
        "permission_scopes": [
            PermissionScope.CREATE_OWN_INTAKE,
            PermissionScope.VIEW_OWN_SUMMARY,
            PermissionScope.SHARE_OWN_SUMMARY,
        ],
        "intake_snapshot": PreVisitIntake(
            known_medications=["metformin"],
            allergies=["無"],
            chronic_conditions=["糖尿病"],
            family_history=["無"],
            symptom_onset="三天前",
            symptom_description="早晨血糖偏高",
            symptom_severity="重度",
            questions_for_doctor=["飲食要注意什麼？"],
        ),
    }, deep=True)
    session = repo.save(session, expected_version=session.version)

    # In review stage, _is_async_narrow_eligible and _looks_like_side_question must return False
    assert orch._is_async_narrow_eligible(session, "想問醫師的問題要改成藥物有什麼副作用") is False
    assert orch._looks_like_side_question(session, "想問醫師的問題要改成藥物有什麼副作用") is False
