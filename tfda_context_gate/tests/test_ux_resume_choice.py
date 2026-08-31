from __future__ import annotations

from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository

_KEY = "demo-identity-hash-key-at-least-16"


def _setup_active_draft(repository, orchestrator, user_id="U-draft"):
    orchestrator.handle_text(event_id="setup-role-1", line_user_id=user_id, text="為自己整理")
    r = orchestrator.handle_text(event_id="setup-intake-1", line_user_id=user_id, text="吃 metformin")
    return r


def _assert_has_resume_metadata(result):
    assert result.metadata is not None
    assert result.metadata.get("requires_resume_decision") is True
    assert result.metadata.get("has_existing_draft") is True


def _assert_no_resume_metadata(result):
    if result.metadata is None:
        return
    assert not (result.metadata.get("requires_resume_decision") is True and result.metadata.get("has_existing_draft") is True)


def test_no_old_draft_normal_start(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "s.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    result = orch.handle_text(event_id="evt-no-draft", line_user_id="U-new", text="我要準備看診")
    assert result.status == "NEEDS_ROLE_SELECTION"
    assert "為自己整理" in result.reply
    _assert_no_resume_metadata(result)


def test_old_draft_not_auto_resume(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "s.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    _setup_active_draft(repo, orch, user_id="U-A")
    session_before = orch.session_for_user("U-A")
    assert session_before is not None
    before_meds = list(session_before.intake_snapshot.known_medications)
    before_stage = session_before.intake_stage
    before_pending = session_before.pending_field

    result = orch.handle_text(event_id="evt-trigger", line_user_id="U-A", text="我要準備看診")
    assert result.status == "NEEDS_RESUME_CHOICE"
    assert "繼續上次整理" in result.reply
    assert "開始新的整理" in result.reply
    assert "取消整理" in result.reply
    _assert_has_resume_metadata(result)

    after = orch.session_for_user("U-A")
    assert after is not None
    assert after.pending_action is not None
    assert after.pending_action.type == "PENDING_RESUME_CHOICE"
    assert list(after.intake_snapshot.known_medications) == before_meds
    assert after.intake_stage == before_stage
    assert after.pending_field == before_pending


def test_choose_continue_preserves_data(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "s.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    _setup_active_draft(repo, orch, user_id="U-B")
    orch.handle_text(event_id="evt-choice-1", line_user_id="U-B", text="我要準備看診")
    before = orch.session_for_user("U-B")
    assert before is not None
    expected_pending = before.pending_field
    expected_stage = before.intake_stage
    expected_meds = list(before.intake_snapshot.known_medications)

    result = orch.handle_text(event_id="evt-continue", line_user_id="U-B", text="繼續上次整理")
    assert result.status == "NEEDS_CLARIFICATION"
    _assert_no_resume_metadata(result)
    after = orch.session_for_user("U-B")
    assert after is not None
    assert after.pending_action is None
    assert list(after.intake_snapshot.known_medications) == expected_meds
    assert after.intake_stage == expected_stage
    assert after.pending_field == expected_pending
    assert "過敏" in result.reply


def test_choose_new_clears_unsubmitted_only(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "s.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    _setup_active_draft(repo, orch, user_id="U-C")
    choice = orch.handle_text(event_id="evt-choice-2", line_user_id="U-C", text="我要準備看診")
    _assert_has_resume_metadata(choice)
    result = orch.handle_text(event_id="evt-new", line_user_id="U-C", text="開始新的整理")
    assert result.status == "NEEDS_ROLE_SELECTION"
    _assert_no_resume_metadata(result)
    after = orch.session_for_user("U-C")
    assert after is not None
    assert after.intake_snapshot.known_medications == []
    assert after.status == "CLOSED"
    assert after.pending_action is None


def test_submitted_not_cleared(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "s.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    orch.handle_text(event_id="sub-1", line_user_id="U-sub", text="為自己整理")
    orch.handle_text(event_id="sub-2", line_user_id="U-sub", text="吃 metformin，無過敏，有高血壓，家族無糖尿病")
    orch.handle_text(event_id="sub-3", line_user_id="U-sub", text="三天前開始，早晨血糖偏高，程度4/10")
    orch.handle_text(event_id="sub-4", line_user_id="U-sub", text="我想問醫師飲食要注意什麼？")
    confirm = orch.handle_text(event_id="sub-5", line_user_id="U-sub", text="確認完成")
    assert confirm.status == "SUBMITTED"
    submitted_session = orch.session_for_user("U-sub")
    assert submitted_session is not None
    assert submitted_session.status == "SUBMITTED"
    meds_before = list(submitted_session.intake_snapshot.known_medications)

    result = orch.handle_text(event_id="sub-start", line_user_id="U-sub", text="我要準備看診")
    assert result.status != "NEEDS_RESUME_CHOICE"
    _assert_no_resume_metadata(result)

    from datetime import datetime, timezone
    from tfda_context_gate.product_session.schemas import PendingAction

    sess = orch.session_for_user("U-sub")
    assert sess is not None
    pending = PendingAction(type="PENDING_RESUME_CHOICE", created_at=datetime.now(timezone.utc))
    orch.repository.save(sess.model_copy(update={"pending_action": pending}, deep=True), expected_version=sess.version)
    result2 = orch.handle_text(event_id="sub-new", line_user_id="U-sub", text="開始新的整理")
    _assert_no_resume_metadata(result2)
    after = orch.session_for_user("U-sub")
    assert after is not None
    assert after.status == "SUBMITTED"
    assert list(after.intake_snapshot.known_medications) == meds_before
    assert after.intake_stage == "submitted"


def test_cross_user_isolation(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "s.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    _setup_active_draft(repo, orch, user_id="U-X")
    orch.handle_text(event_id="iso-B1", line_user_id="U-Y", text="為自己整理")
    orch.handle_text(event_id="iso-B2", line_user_id="U-Y", text="吃 insulin")
    choice = orch.handle_text(event_id="iso-A-choice", line_user_id="U-X", text="我要準備看診")
    _assert_has_resume_metadata(choice)
    result_new = orch.handle_text(event_id="iso-A-new", line_user_id="U-X", text="開始新的整理")
    _assert_no_resume_metadata(result_new)
    session_x = orch.session_for_user("U-X")
    session_y = orch.session_for_user("U-Y")
    assert session_x is not None
    assert session_x.intake_snapshot.known_medications == []
    assert session_y is not None
    assert session_y.intake_snapshot.known_medications == ["insulin"]
    assert session_y.pending_action is None
    non_choice_y = orch.handle_text(event_id="iso-Y-check", line_user_id="U-Y", text="我要準備看診")
    _assert_has_resume_metadata(non_choice_y)


def test_optimistic_lock_and_red_flag_still_enforced(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "s.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    _setup_active_draft(repo, orch, user_id="U-lock")
    choice = orch.handle_text(event_id="lock-choice", line_user_id="U-lock", text="我要準備看診")
    _assert_has_resume_metadata(choice)
    result = orch.handle_text(event_id="lock-red", line_user_id="U-lock", text="我胸痛呼吸困難快昏倒")
    assert result.status == "FALLBACK"
    assert result.fallback_reason == "A_EMERGENCY"
    _assert_no_resume_metadata(result)
    sess = orch.session_for_user("U-lock")
    assert sess is not None
    assert sess.pending_action is not None
    assert sess.pending_action.type == "PENDING_RESUME_CHOICE"


def test_resume_choice_reprompt_still_has_metadata_and_cancel_clears_without_metadata(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "s.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    _setup_active_draft(repo, orch, user_id="U-reprompt")
    first = orch.handle_text(event_id="reprompt-1", line_user_id="U-reprompt", text="我要準備看診")
    _assert_has_resume_metadata(first)
    assert "取消整理" in first.reply
    second = orch.handle_text(event_id="reprompt-2", line_user_id="U-reprompt", text="隨便打字不是指令")
    assert second.status == "NEEDS_RESUME_CHOICE"
    _assert_has_resume_metadata(second)
    assert "取消整理" in second.reply
    after = orch.session_for_user("U-reprompt")
    assert after is not None
    assert after.intake_snapshot.known_medications == ["metformin"]
    cancelled = orch.handle_text(event_id="reprompt-3", line_user_id="U-reprompt", text="不填了")
    assert cancelled.status == "CANCELLED"
    _assert_no_resume_metadata(cancelled)
    cleared = orch.session_for_user("U-reprompt")
    assert cleared is not None
    assert cleared.intake_snapshot.known_medications == []


def test_cancel_alias_explicit_text_also_cancels(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "s.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    _setup_active_draft(repo, orch, user_id="U-alias")
    choice = orch.handle_text(event_id="alias-1", line_user_id="U-alias", text="我要準備看診")
    assert "取消整理" in choice.reply
    _assert_has_resume_metadata(choice)
    cancelled = orch.handle_text(event_id="alias-2", line_user_id="U-alias", text="取消整理")
    assert cancelled.status == "CANCELLED"
    _assert_no_resume_metadata(cancelled)
    cleared = orch.session_for_user("U-alias")
    assert cleared is not None
    assert cleared.intake_snapshot.known_medications == []
