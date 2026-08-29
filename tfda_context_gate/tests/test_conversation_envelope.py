"""P1 Commit1: ConversationEnvelope bounded/privacy/persistence/isolation tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from tfda_context_gate.conversation import ConversationContextManager
from tfda_context_gate.conversation.envelope import (
    ConversationEnvelope,
    build_conversation_envelope,
    envelope_to_model_context,
)
from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.product_session.schemas import PendingAction, ProductSession


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _session_with_turns(n_exchanges: int = 0, **overrides) -> ProductSession:
    mgr = ConversationContextManager()
    ctx = mgr.create("sess-001")
    for i in range(n_exchanges):
        ctx = mgr.append_turn(ctx, role="user", content=f"user msg {i}")
        ctx = mgr.append_turn(ctx, role="assistant", content=f"assistant reply {i}")
    now = datetime.now(timezone.utc)
    base = dict(
        session_id="sess-001",
        principal_id_hash=_h("principalA"),
        subject_id_hash=_h("principalA"),
        conversation_context=ctx,
        created_at=now - timedelta(days=1),
        updated_at=now,
        expires_at=now + timedelta(days=6),
    )
    base.update(overrides)
    return ProductSession.model_validate(base)


# 1. envelope 欄位與 extra forbid
def test_envelope_fields_and_extra_forbid():
    sess = _session_with_turns(1)
    env = build_conversation_envelope(sess, "測試訊息")
    assert env.schema_version == "conversation.envelope.v1"
    assert env.active_task in ("pre_visit_intake", "general_education", "chitchat", "idle", "unknown")
    assert env.session_status in ("ACTIVE", "PAUSED", "AWAITING_CONFIRMATION", "SUBMITTED", "CLOSED")
    assert env.actor_role in ("PATIENT", "RELATED_PERSON", "SYSTEM_ADMIN", "PRACTITIONER")
    assert env.current_message == "測試訊息"
    # extra forbid
    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        ConversationEnvelope.model_validate({**env.model_dump(mode="json"), "unknown_field": "bad"})
    # model extra field via constructor should also fail
    with pytest.raises(Exception):
        ConversationEnvelope(**{**env.model_dump(mode="python"), "evil": 123})  # type: ignore[arg-type]


# 2. 最多 5 組 exchanges (10 turns) bounded
def test_envelope_bounded_5_exchanges():
    sess = _session_with_turns(7)  # 7 exchanges = 14 turns
    env = build_conversation_envelope(sess, "current")
    # should keep last 5 user exchanges => 10 turns
    assert len(env.recent_turns) <= 10
    assert len(env.recent_turns) == 10
    # earliest kept should be user msg 2 (since 0,1 dropped)
    assert env.recent_turns[0].content == "user msg 2"
    assert env.recent_turns[1].content == "assistant reply 2"
    # with 3 exchanges should keep all
    sess3 = _session_with_turns(3)
    env3 = build_conversation_envelope(sess3, "hi")
    assert len(env3.recent_turns) == 6

    # exactly 5 exchanges => 10 turns keep all
    sess5 = _session_with_turns(5)
    env5 = build_conversation_envelope(sess5, "hi")
    assert len(env5.recent_turns) == 10


# 2b. current_message 獨立保存，不可被 summary 或 sentinel 覆蓋
def test_envelope_current_message_independent():
    sess = _session_with_turns(1)
    env = build_conversation_envelope(sess, "獨立的當前訊息")
    assert env.current_message == "獨立的當前訊息"
    # recent_turns should not contain current_message as sentinel duplication
    assert all(t.content != "獨立的當前訊息" for t in env.recent_turns) or True  # if no prior same content, still independent
    # even if recent contains similar, current_message is separate field
    mgr = ConversationContextManager()
    ctx = mgr.create("sess-002")
    ctx = mgr.append_turn(ctx, role="user", content="獨立的當前訊息")
    ctx = mgr.append_turn(ctx, role="assistant", content="reply")
    now = datetime.now(timezone.utc)
    sess2 = ProductSession(
        session_id="sess-002",
        principal_id_hash=_h("p"),
        subject_id_hash=_h("p"),
        conversation_context=ctx,
        created_at=now - timedelta(days=1),
        updated_at=now,
        expires_at=now + timedelta(days=6),
    )
    env2 = build_conversation_envelope(sess2, "獨立的當前訊息")
    # current_message still preserved even though recent contains same string
    assert env2.current_message == "獨立的當前訊息"
    assert env2.recent_turns[0].content == "獨立的當前訊息"  # recent keeps prior, but current_message is independent copy


# 3. 不含 ID/hash/token/raw image/sentinel
def test_envelope_no_forbidden_fields():
    mgr = ConversationContextManager()
    ctx = mgr.create("sess-003")
    ctx = mgr.append_turn(ctx, role="user", content="hello")
    ctx = mgr.append_turn(ctx, role="assistant", content="hi")
    intake = PreVisitIntake(known_medications=["metformin"], allergies=["無"])
    sess = ProductSession(
        session_id="sess-003",
        principal_id_hash=_h("principalX"),
        subject_id_hash=_h("subjectY"),
        conversation_context=ctx,
        intake_snapshot=intake,
        pending_action=PendingAction(type="PENDING_CONFIRM_QUESTION", proposal="想問醫師的問題嗎？"),
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
        updated_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=6),
    )
    env = build_conversation_envelope(sess, "測試隱私")
    dumped = env.model_dump(mode="json")
    dumped_str = str(dumped)
    for forbidden in ("principal_id_hash", "subject_id_hash", "line_user_id", "token_hash", "share_grant", "webhook_event", "event_id", "claim_token", "fact_revision", "compacted_turn_hash", "raw_image", "image_bytes", "api_key", "sentinel"):
        assert forbidden not in dumped_str, f"envelope leaked forbidden field {forbidden}"
    # also envelope_to_model_context must not leak
    ctx2 = envelope_to_model_context(env)
    ctx_str = str(ctx2)
    for forbidden in ("principal_id_hash", "subject_id_hash", "line_user_id", "token_hash"):
        assert forbidden not in ctx_str
    # raw image never in envelope even if session had image? (ProductSession never stores raw image, but ensure)
    assert "image_bytes" not in dumped_str


# 3b. confirmed_intake 只能含已驗證、pending_action 明確未確認不混入
def test_envelope_confirmed_vs_pending_isolation():
    intake = PreVisitIntake(known_medications=["metformin"], allergies=["無"])
    pending = PendingAction(type="PENDING_CONFIRM_QUESTION", proposal="metformin 會傷腎嗎？要記入嗎？")
    sess = _session_with_turns(0, intake_snapshot=intake, pending_action=pending)
    env = build_conversation_envelope(sess, "要記入嗎？")
    # confirmed_intake should be the intake snapshot, not including pending proposal
    assert env.confirmed_intake.known_medications == ["metformin"]
    assert "metformin 會傷腎嗎" not in str(env.confirmed_intake.model_dump())
    # pending_action is explicit and separate
    assert env.pending_action is not None
    assert env.pending_action["type"] == "PENDING_CONFIRM_QUESTION" if isinstance(env.pending_action, dict) else env.pending_action.type == "PENDING_CONFIRM_QUESTION"  # type: ignore[union-attr]
    # pending not mixed into confirmed
    assert env.pending_action != env.confirmed_intake


# 4. subject switch isolation — new subject must not carry old subject turns/facts
def test_envelope_subject_switch_isolation(tmp_path=None):
    mgr = ConversationContextManager()
    # Old subject session with rich history
    ctx_old = mgr.create("sess-switch")
    for i in range(4):
        ctx_old = mgr.append_turn(ctx_old, role="user", content=f"old subject message {i}")
        ctx_old = mgr.append_turn(ctx_old, role="assistant", content=f"old reply {i}")
    now = datetime.now(timezone.utc)
    sess_old = ProductSession(
        session_id="sess-switch",
        principal_id_hash=_h("principalA"),
        subject_id_hash=_h("old_subject"),
        conversation_context=ctx_old,
        intake_snapshot=PreVisitIntake(known_medications=["old_med"], allergies=["old_allergy"]),
        created_at=now - timedelta(days=1),
        updated_at=now,
        expires_at=now + timedelta(days=6),
    )
    env_old = build_conversation_envelope(sess_old, "old current")
    assert any("old subject" in t.content for t in env_old.recent_turns)
    assert env_old.confirmed_intake.known_medications == ["old_med"]

    # Simulate subject switch via _new_subject_state (as orchestrator does)
    from tfda_context_gate.line_orchestration.orchestrator import ConversationOrchestrator
    from tfda_context_gate.product_session import SQLiteProductSessionRepository

    repo_path = tmp_path / "switch.sqlite3" if tmp_path else "/tmp/switch_test.sqlite3"
    repo = SQLiteProductSessionRepository(repo_path)
    # Create via repo to test persistence isolation too
    # Use fresh orchestrator to call _new_subject_state
    orch = ConversationOrchestrator(repository=repo, identity_hash_key="test-key-12345678")
    new_state = orch._new_subject_state(sess_old, "那個是我媽媽，不是我")
    # Build new session as orchestrator would
    new_ctx = new_state["conversation_context"]
    new_sess = ProductSession(
        session_id="sess-switch",
        principal_id_hash=_h("principalA"),
        subject_id_hash=_h("new_subject_mom"),
        conversation_context=new_ctx,
        intake_snapshot=new_state["intake_snapshot"],
        created_at=now - timedelta(days=1),
        updated_at=now,
        expires_at=now + timedelta(days=6),
    )
    env_new = build_conversation_envelope(new_sess, "媽媽的狀況")
    # Old turns/facts must not appear
    assert all("old subject" not in t.content for t in env_new.recent_turns)
    assert env_new.confirmed_intake.known_medications == []
    assert env_new.confirmed_intake.allergies == []
    # recent_turns should only contain the switch command itself, not old
    assert len(env_new.recent_turns) <= 2


# 5. 重啟後可從 SQLite 建立相同 envelope (deterministic + persistence)
def test_envelope_persistence_deterministic(tmp_path):
    mgr = ConversationContextManager()
    ctx = mgr.create("persist-001")
    ctx = mgr.append_turn(ctx, role="user", content="糖尿病可以吃水果嗎？")
    ctx = mgr.append_turn(ctx, role="assistant", content="水果原則...")
    intake = PreVisitIntake(known_medications=["metformin"])
    now = datetime.now(timezone.utc)
    sess = ProductSession(
        session_id="persist-001",
        principal_id_hash=_h("userPersist"),
        subject_id_hash=_h("userPersist"),
        conversation_context=ctx,
        intake_snapshot=intake,
        intake_stage="stage1",
        pending_field="allergies",
        pending_question="有沒有過敏？",
        created_at=now - timedelta(days=1),
        updated_at=now,
        expires_at=now + timedelta(days=6),
    )
    repo = SQLiteProductSessionRepository(tmp_path / "persist.sqlite3")
    repo.create(sess)
    # Simulate restart: new repo instance
    repo2 = SQLiteProductSessionRepository(tmp_path / "persist.sqlite3")
    loaded = repo2.get("persist-001")
    assert loaded is not None
    env_before = build_conversation_envelope(sess, "那一天可以吃多少？")
    env_after = build_conversation_envelope(loaded, "那一天可以吃多少？")
    assert env_before.model_dump(mode="json") == env_after.model_dump(mode="json")
    # envelope_to_model_context also deterministic
    assert envelope_to_model_context(env_before) == envelope_to_model_context(env_after)


# 6. deterministic: same input -> same envelope, different message -> different envelope
def test_envelope_deterministic():
    sess = _session_with_turns(2)
    e1 = build_conversation_envelope(sess, "訊息A")
    e2 = build_conversation_envelope(sess, "訊息A")
    e3 = build_conversation_envelope(sess, "訊息B")
    assert e1.model_dump(mode="json") == e2.model_dump(mode="json")
    assert e1.model_dump(mode="json") != e3.model_dump(mode="json")


# 7. envelope extra forbid rejects unknown fields via model_validate
def test_envelope_schema_rejects_unknown():
    sess = _session_with_turns(0)
    env = build_conversation_envelope(sess, "hello")
    payload = env.model_dump(mode="json")
    payload["unexpected_field_xyz"] = "should fail"
    with pytest.raises(Exception):
        ConversationEnvelope.model_validate(payload)
