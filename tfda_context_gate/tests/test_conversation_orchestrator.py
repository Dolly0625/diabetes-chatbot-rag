from __future__ import annotations

from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.sharing import ShareGrantService


_KEY = "demo-identity-hash-key-at-least-16"


def test_self_selection_persists_authorized_patient_session(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)

    result = orchestrator.handle_text(
        event_id="event-self-1",
        line_user_id="U-sensitive-line-id",
        text="為自己整理",
    )
    session = repository.get(result.session_id)

    assert result.status == "NEEDS_CLARIFICATION"
    assert result.intake_stage == "stage1"
    assert session is not None
    assert session.authorization_status == "PATIENT_SELF"
    assert session.principal_id_hash != "U-sensitive-line-id"
    assert "U-sensitive-line-id" not in session.model_dump_json()


def test_prepare_visit_requires_self_or_family_selection_first(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)

    result = orchestrator.handle_text(
        event_id="role-first", line_user_id="U-new", text="我要準備看診"
    )

    assert result.status == "NEEDS_ROLE_SELECTION"
    assert "為自己整理" in result.reply and "代家人整理" in result.reply


def test_natural_return_visit_phrase_cannot_bypass_role_selection(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)

    result = orchestrator.handle_text(
        event_id="role-natural", line_user_id="U-new", text="我明天回診，現在有吃 metformin"
    )
    session = repository.get(result.session_id)

    assert result.status == "NEEDS_ROLE_SELECTION"
    assert session is not None and session.intake_snapshot.known_medications == []


def test_proxy_must_confirm_consent_before_receiving_permissions(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)

    first = orchestrator.handle_text(event_id="proxy-1", line_user_id="U-proxy", text="代家人整理")
    unverified = repository.get(first.session_id)
    second = orchestrator.handle_text(event_id="proxy-2", line_user_id="U-proxy", text="已取得同意")
    authorized = repository.get(second.session_id)

    assert first.status == "NEEDS_AUTHORIZATION"
    assert unverified is not None and unverified.permission_scopes == []
    assert authorized is not None
    assert authorized.authorization_status == "AUTHORIZED_CAREGIVER"
    assert {str(scope) for scope in authorized.permission_scopes} == {
        "CREATE_PROXY_INTAKE", "VIEW_PROXY_SUMMARY", "SHARE_PROXY_SUMMARY"
    }


def test_proxy_information_source_is_structured_not_only_conversation_text(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    orchestrator.handle_text(event_id="source-1", line_user_id="U-source", text="代家人整理")
    orchestrator.handle_text(event_id="source-2", line_user_id="U-source", text="已取得同意")
    result = orchestrator.handle_text(event_id="source-3", line_user_id="U-source", text="家人本人描述")
    session = repository.get(result.session_id)

    assert session is not None
    assert str(session.information_source) == "SUBJECT_REPORTED_VIA_PROXY"
    assert session.intake_stage == "stage1"


def test_authorized_family_can_complete_confirm_and_share_proxy_intake(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    messages = [
        "代家人整理",
        "已取得同意",
        "家人本人描述",
        "吃 metformin，無過敏，有高血壓，家族無糖尿病",
        "昨天開始，覺得頭暈，程度3/10",
        "想問醫師是否需要調整飲食",
        "確認完成",
    ]
    result = None
    for index, text in enumerate(messages):
        result = orchestrator.handle_text(
            event_id=f"proxy-complete-{index}", line_user_id="U-proxy-complete", text=text
        )
    assert result is not None
    session = repository.get(result.session_id)
    assert session is not None and session.status == "SUBMITTED"
    assert str(session.actor_role) == "RELATED_PERSON"
    assert str(session.information_source) == "SUBJECT_REPORTED_VIA_PROXY"
    assert ShareGrantService(repository).create(session).single_use is True


def test_intake_continues_across_messages_and_repository_restart(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    first_repository = SQLiteProductSessionRepository(path)
    first = ConversationOrchestrator(first_repository, identity_hash_key=_KEY)
    selected = first.handle_text(event_id="intake-1", line_user_id="U-patient", text="為自己整理")

    second = ConversationOrchestrator(SQLiteProductSessionRepository(path), identity_hash_key=_KEY)
    response = second.handle_text(
        event_id="intake-2",
        line_user_id="U-patient",
        text="吃 metformin，無過敏，有高血壓，家族無糖尿病",
    )
    session = second.repository.get(selected.session_id)

    assert response.intake_stage == "stage2"
    assert session is not None
    assert session.intake_snapshot.known_medications == ["metformin"]
    assert session.intake_snapshot.allergies == ["無"]
    assert session.intake_snapshot.chronic_conditions == ["高血壓"]
    assert session.conversation_context.clinical_state.known_medications == ["metformin"]
    assert session.conversation_context.clinical_state.current_stage == "stage2"
    assert session.conversation_context.clinical_state.completed_stages == ["stage1"]


def test_unknown_answer_is_valid_and_advances_to_next_single_question(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    orchestrator.handle_text(event_id="unknown-1", line_user_id="U-unknown", text="為自己整理")

    result = orchestrator.handle_text(
        event_id="unknown-2", line_user_id="U-unknown", text="我不太知道欸"
    )
    session = repository.get(result.session_id)

    assert result.status == "NEEDS_CLARIFICATION"
    assert "待看診確認" in result.reply
    assert "過敏" in result.reply and "第" not in result.reply  # INTAKE_FIELD_QUESTIONS["allergies"] 含「過敏」，防 drifts
    assert "目前無法處理此請求" not in result.reply
    assert session is not None
    assert session.intake_snapshot.known_medications == ["不清楚（待看診確認）"]
    assert session.pending_field == "allergies"


def test_general_education_digression_answers_then_returns_to_saved_intake(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    orchestrator.handle_text(event_id="side-1", line_user_id="U-side", text="為自己整理")
    orchestrator.handle_text(event_id="side-2", line_user_id="U-side", text="我不太知道欸")

    result = orchestrator.handle_text(
        event_id="side-3",
        line_user_id="U-side",
        text="請說明糖尿病的一般飲食原則",
    )
    session = repository.get(result.session_id)

    assert result.status == "SIDE_ANSWER"
    assert "一般糖尿病飲食原則" in result.reply
    assert "看診資料我先幫你留著" in result.reply
    assert "下一步" not in result.reply
    assert session is not None and session.pending_field == "allergies"
    assert session.status == "PAUSED"
    assert session.intake_snapshot.known_medications == ["不清楚（待看診確認）"]


def test_intake_can_pause_and_resume_without_losing_progress(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    orchestrator.handle_text(event_id="pause-1", line_user_id="U-pause", text="為自己整理")
    orchestrator.handle_text(event_id="pause-2", line_user_id="U-pause", text="目前沒有用藥")

    paused = orchestrator.handle_text(
        event_id="pause-3", line_user_id="U-pause", text="暫停整理"
    )
    resumed = orchestrator.handle_text(
        event_id="pause-4", line_user_id="U-pause", text="繼續整理"
    )
    session = repository.get(resumed.session_id)

    assert paused.status == "PAUSED"
    assert resumed.status == "NEEDS_CLARIFICATION"
    assert "過敏" in resumed.reply and "第" not in resumed.reply  # INTAKE_FIELD_QUESTIONS["allergies"] 防漂移
    assert session is not None and session.status == "ACTIVE"
    assert session.intake_snapshot.known_medications == ["無"]


def test_cancel_clears_unsubmitted_intake_and_can_restart(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    orchestrator.handle_text(event_id="cancel-1", line_user_id="U-cancel", text="為自己整理")
    orchestrator.handle_text(event_id="cancel-2", line_user_id="U-cancel", text="metformin")

    cancelled = orchestrator.handle_text(
        event_id="cancel-3", line_user_id="U-cancel", text="取消整理"
    )
    closed = repository.get(cancelled.session_id)
    restarted = orchestrator.handle_text(
        event_id="cancel-4", line_user_id="U-cancel", text="準備看診"
    )

    assert cancelled.status == "CANCELLED"
    assert closed is not None and closed.status == "CLOSED"
    assert closed.intake_snapshot.known_medications == []
    assert restarted.status == "NEEDS_ROLE_SELECTION"


def test_stage_checkpoint_summarizes_before_next_section(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    messages = ["為自己整理", "metformin", "沒有過敏", "高血壓", "沒有家族史"]
    result = None
    for index, text in enumerate(messages):
        result = orchestrator.handle_text(
            event_id=f"checkpoint-{index}", line_user_id="U-checkpoint", text=text
        )

    assert result is not None
    assert result.intake_stage == "stage2"
    assert "用藥與病史已記下" in result.reply
    assert "什麼時候開始" in result.reply and "第" not in result.reply  # INTAKE_FIELD_QUESTIONS["symptom_onset"] 防漂移


def test_duplicate_webhook_replays_same_result_without_duplicate_turn(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    first = orchestrator.handle_text(event_id="duplicate-1", line_user_id="U-dup", text="為自己整理")
    before = repository.get(first.session_id)

    replay = orchestrator.handle_text(event_id="duplicate-1", line_user_id="U-dup", text="不同內容")
    after = repository.get(first.session_id)

    assert replay.replayed is True
    assert replay.reply == first.reply
    assert before is not None and after is not None
    assert after.version == before.version
    assert len(after.conversation_context.recent_turns) == len(before.conversation_context.recent_turns)


def test_medication_bag_image_updates_intake_without_persisting_raw_bytes(tmp_path):
    class FakeOCR:
        def extract(self, _image: bytes):
            return {"meds": ["metformin"], "confidence": 0.99, "qr_used": False, "ocr_used": True}

    path = tmp_path / "sessions.sqlite3"
    repository = SQLiteProductSessionRepository(path)
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    selected = orchestrator.handle_text(event_id="image-1", line_user_id="U-image", text="為自己整理")
    raw_image = b"unique-raw-medication-image-contents"

    result = orchestrator.handle_image(
        event_id="image-2", line_user_id="U-image", image_bytes=raw_image, ocr_service=FakeOCR()
    )
    session = repository.get(selected.session_id)

    assert result.status == "NEEDS_CLARIFICATION"
    assert session is not None and session.intake_snapshot.known_medications == ["metformin"]
    assert raw_image not in path.read_bytes()
    assert all(turn.content != raw_image.decode() for turn in session.conversation_context.recent_turns)


def test_submitted_intake_can_reopen_only_selected_section(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    messages = [
        "為自己整理",
        "吃 metformin，無過敏，有高血壓，家族無糖尿病",
        "三天前開始，早晨血糖偏高，程度4/10",
        "我想問醫師飲食要注意什麼？",
        "確認完成",
    ]
    for index, value in enumerate(messages):
        orchestrator.handle_text(event_id=f"modify-{index}", line_user_id="U-modify", text=value)

    result = orchestrator.handle_text(
        event_id="modify-section", line_user_id="U-modify", text="修改症狀"
    )
    session = repository.get(result.session_id)

    assert result.intake_stage == "stage2"
    assert session is not None and session.status == "ACTIVE"
    assert session.intake_snapshot.known_medications == ["metformin"]
    assert session.intake_snapshot.symptom_description is None
    assert session.conversation_context.clinical_state.symptom_description is None


def test_switching_subject_cannot_carry_health_data_between_patient_and_family(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    orchestrator = ConversationOrchestrator(repository, identity_hash_key=_KEY)
    orchestrator.handle_text(event_id="switch-1", line_user_id="U-switch", text="為自己整理")
    first = orchestrator.handle_text(
        event_id="switch-2",
        line_user_id="U-switch",
        text="吃 metformin，無過敏，有高血壓，家族無糖尿病",
    )
    before = repository.get(first.session_id)
    switched = orchestrator.handle_text(
        event_id="switch-3", line_user_id="U-switch", text="代家人整理"
    )
    after = repository.get(switched.session_id)

    assert before is not None and before.intake_snapshot.known_medications == ["metformin"]
    assert after is not None and after.intake_snapshot.known_medications == []
    assert after.conversation_context.clinical_state.known_medications == []
    assert all("metformin" not in turn.content for turn in after.conversation_context.recent_turns)
    assert after.permission_scopes == []
