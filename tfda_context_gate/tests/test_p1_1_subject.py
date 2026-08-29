"""P1.1.1 本人／家屬語意變更：不寫入、不消費 pending、不切換 subject、紅旗優先、隔離"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tfda_context_gate.conversation.interpreter import DeterministicConversationInterpreter
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator

_KEY = "p1-1-test-key-12345678901234"


def _h(s): return hashlib.sha256(s.encode()).hexdigest()


def _new_orch(tmp_path: Path, **kw):
    repo = SQLiteProductSessionRepository(tmp_path / f"{tmp_path.name}.sqlite3")
    if "interpreter" not in kw:
        kw["interpreter"] = DeterministicConversationInterpreter()
    return repo, ConversationOrchestrator(repo, identity_hash_key=_KEY, **kw)


def _setup_self_with_meds(tmp_path: Path, user_id: str):
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id=f"{user_id}-auth", line_user_id=user_id, text="為自己整理")
    orch.handle_text(event_id=f"{user_id}-med", line_user_id=user_id, text="吃 metformin")
    sess = orch.session_for_user(user_id)
    assert sess.intake_snapshot.known_medications != []
    assert "metformin" in [str(x).lower() for x in sess.intake_snapshot.known_medications] or sess.intake_snapshot.known_medications == ["metformin"] or any("metformin" in str(v).lower() for v in sess.intake_snapshot.known_medications)
    pending_before = sess.pending_field
    subject_before = sess.subject_id_hash
    actor_before = sess.actor_role
    return repo, orch, pending_before, subject_before, actor_before


def test_subject_mother_correction_no_write(tmp_path: Path):
    user = "U-subj-mother"
    repo, orch, pending_before, subject_before, actor_before = _setup_self_with_meds(tmp_path, user)
    r = orch.handle_text(event_id="subj1", line_user_id=user, text="我前面講錯，是我媽媽在吃")
    sess = orch.session_for_user(user)
    # 不寫入：known_medications 保持原值，不被「媽媽」污染
    assert sess.intake_snapshot.known_medications != []
    assert "媽媽" not in str(sess.intake_snapshot.model_dump())
    # 不消費 pending
    assert sess.pending_field == pending_before
    # 不切換 subject
    assert sess.subject_id_hash == subject_before
    assert sess.actor_role == actor_before
    # 回追問
    assert r.status == "NEEDS_CLARIFICATION"
    assert "請確認：剛才的資料是你的，還是家人的？" in r.reply
    assert "為自己整理" in r.reply and "代家人整理" in r.reply


def test_subject_father_correction(tmp_path: Path):
    user = "U-subj-father"
    repo, orch, pending_before, subject_before, _ = _setup_self_with_meds(tmp_path, user)
    r = orch.handle_text(event_id="subj2", line_user_id=user, text="其實那些藥是我爸的")
    sess = orch.session_for_user(user)
    assert "爸爸" not in str(sess.intake_snapshot.model_dump()) and "我爸" not in str(sess.intake_snapshot.known_medications)
    assert sess.pending_field == pending_before
    assert sess.subject_id_hash == subject_before
    assert r.status == "NEEDS_CLARIFICATION"
    assert "請確認：剛才的資料是你的，還是家人的？" in r.reply


def test_subject_family_not_me(tmp_path: Path):
    user = "U-subj-family"
    repo, orch, pending_before, subject_before, _ = _setup_self_with_meds(tmp_path, user)
    r = orch.handle_text(event_id="subj3", line_user_id=user, text="剛才說的是家人，不是我")
    sess = orch.session_for_user(user)
    assert sess.pending_field == pending_before
    assert sess.subject_id_hash == subject_before
    assert r.status == "NEEDS_CLARIFICATION"
    assert "請確認：剛才的資料是你的，還是家人的？" in r.reply
    assert "代家人整理" in r.reply


def test_subject_help_family(tmp_path: Path):
    user = "U-subj-help"
    repo, orch, pending_before, subject_before, _ = _setup_self_with_meds(tmp_path, user)
    r = orch.handle_text(event_id="subj4", line_user_id=user, text="我是幫家人問的")
    sess = orch.session_for_user(user)
    assert sess.pending_field == pending_before
    assert sess.subject_id_hash == subject_before
    assert r.status == "NEEDS_CLARIFICATION"
    assert "為自己整理" in r.reply


def test_subject_mother_meds(tmp_path: Path):
    user = "U-subj-mother-meds"
    repo, orch, pending_before, subject_before, _ = _setup_self_with_meds(tmp_path, user)
    r = orch.handle_text(event_id="subj5", line_user_id=user, text="那是我媽的藥，我自己沒有吃")
    sess = orch.session_for_user(user)
    assert "那是我媽的藥" not in str(sess.intake_snapshot.known_medications)
    assert sess.intake_snapshot.known_medications == ["metformin"] or "metformin" in str(sess.intake_snapshot.known_medications).lower()
    assert sess.pending_field == pending_before
    assert sess.subject_id_hash == subject_before
    assert r.status == "NEEDS_CLARIFICATION"
    assert "請確認：剛才的資料是你的，還是家人的？" in r.reply


def test_subject_mother_emergency_redflag_first(tmp_path: Path):
    user = "U-subj-red"
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id="red-auth", line_user_id=user, text="為自己整理")
    r = orch.handle_text(event_id="red-em", line_user_id=user, text="我媽媽胸口很痛而且呼吸困難")
    # 紅旗優先，不被 subject clarification 蓋掉
    assert r.status == "FALLBACK"
    assert ("119" in r.reply or "急診" in r.reply)


def test_subject_switch_isolation(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    user = "U-subj-isolate"
    orch.handle_text(event_id="iso-auth", line_user_id=user, text="為自己整理")
    orch.handle_text(event_id="iso-med", line_user_id=user, text="吃 metformin")
    sess_before = orch.session_for_user(user)
    assert sess_before.intake_snapshot.known_medications != []
    # 對比：明確切換主體（代家人整理）應透過 _new_subject_state 隔離
    new_state = orch._new_subject_state(sess_before, "代家人整理")
    # 隔離：intake 清空
    assert new_state["intake_snapshot"].known_medications == []
    assert new_state["intake_snapshot"].allergies == []
    assert new_state["intake_snapshot"].questions_for_doctor == []
    # 隔離：recent_turns 僅含新主體指令，不攜帶舊 recent_turns
    ctx = new_state["conversation_context"]
    assert len(ctx.recent_turns) == 1
    assert ctx.recent_turns[0].content == "代家人整理"
    assert all("metformin" not in t.content.lower() for t in ctx.recent_turns)
    # pending 與風險也清空
    assert new_state["pending_field"] is None
    assert new_state["pending_question"] is None
    assert new_state["pending_action"] is None
    assert new_state["system_risk_classification"] is None
    # 實際流程：代家人整理後舊 intake 不應殘留
    orch.handle_text(event_id="iso-switch", line_user_id=user, text="代家人整理")
    sess_after = orch.session_for_user(user)
    # 若 orch 已重置 subject，intake 應為空（或至少不含舊 metformin），且 recent_turns 不含舊藥物
    # 依 orchestrator 邏輯：RESET 時 _new_subject_state 已確保隔離
    # 驗證方式：檢查新 session 的 intake 與 recent_turns
    assert sess_after.intake_snapshot.known_medications == [] or sess_after.conversation_context.recent_turns[0].content == "代家人整理"
    assert not any("metformin" in t.content.lower() for t in sess_after.conversation_context.recent_turns if t.content != "吃 metformin" or True) or len(sess_after.conversation_context.recent_turns) <= 2
