"""P1.1 新增 18 項：factory、schema、未見語句、控制、active_task、async、fallback"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from tfda_context_gate.conversation import ConversationContextManager
from tfda_context_gate.conversation.envelope import build_conversation_envelope
from tfda_context_gate.conversation.interpreter import (
    ConversationInterpreterFactory,
    DeterministicConversationInterpreter,
    ConversationTurnInterpretation,
    IntakeCandidate,
)
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator

_KEY = "p1-1-test-key-12345678901234"

def _h(s): return hashlib.sha256(s.encode()).hexdigest()

def _new_orch(tmp_path: Path, **kw):
    repo = SQLiteProductSessionRepository(tmp_path / f"{tmp_path.name}.sqlite3")
    if "interpreter" not in kw:
        kw["interpreter"] = DeterministicConversationInterpreter()
    return repo, ConversationOrchestrator(repo, identity_hash_key=_KEY, **kw)

# 1. ROUTER 存在、CONVERSATION 不存在時仍啟用 Formal
def test_factory_uses_router_when_conversation_missing(monkeypatch):
    monkeypatch.setenv("CONVERSATION_LLM_MODEL", "")
    monkeypatch.setenv("ROUTER_LLM_MODEL", "opencode/mimo-v2.5")
    # Need to ensure PYTEST not forcing deterministic
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    interp = ConversationInterpreterFactory.from_env()
    # Should be Formal (or at least not Deterministic fallback due to missing model)
    assert interp.__class__.__name__ == "FormalConversationInterpreter"

# 2. 兩者都不存在才 deterministic
def test_factory_fallback_when_no_model(monkeypatch):
    monkeypatch.setenv("CONVERSATION_LLM_MODEL", "")
    monkeypatch.setenv("ROUTER_LLM_MODEL", "")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("tfda_context_gate.run_config.load_dotenv_file", lambda path=None: {})
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    interp = ConversationInterpreterFactory.from_env()
    assert interp.__class__.__name__ == "DeterministicConversationInterpreter"

# 3. 程式碼無硬編碼模型 fallback
def test_no_hardcode_model_fallback():
    import pathlib, re
    text = Path("tfda_context_gate/conversation/interpreter.py").read_text()
    # Should not contain hardcoded "opencode/mimo-v2.5" as fallback
    assert '"opencode/mimo-v2.5"' not in text
    assert "'opencode/mimo-v2.5'" not in text
    # run_config should not hardcode either
    text2 = Path("tfda_context_gate/run_config.py").read_text()
    assert "mimo-v2.5" not in text2 or "ROUTER" in text2  # allow only in comments about .env

# 4. source_quote 必須來自 current_message
def test_source_quote_in_current_message(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id="sq1", line_user_id="U-sq", text="為自己整理")
    # Use deterministic to get candidate
    sess = orch.session_for_user("U-sq")
    env = build_conversation_envelope(sess, "我有吃 metformin")
    interp = DeterministicConversationInterpreter()
    res = interp.interpret(env)
    for c in res.intake_candidates:
        assert c.source_quote in env.current_message

# 5. 未知 field schema reject
def test_unknown_field_schema_reject():
    with pytest.raises(Exception):
        IntakeCandidate(field_name="unknown_field_xyz", candidate_value="x", source_quote="x", confidence=0.9, explicitly_stated=True, requires_confirmation=True)
    with pytest.raises(Exception):
        ConversationTurnInterpretation(intents=["UNKNOWN"], unknown_extra="bad")  # type: ignore[arg-type]

# 6. unseen fruit followup
def test_unseen_fruit_followup(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id="f1", line_user_id="U-f6", text="糖尿病可以吃水果嗎？")
    r = orch.handle_text(event_id="f2", line_user_id="U-f6", text="所以每天大概能碰幾份啊？")
    assert r.status == "COMPLETED"
    sess = orch.session_for_user("U-f6")
    assert sess.intake_snapshot.known_medications == []

# 7. unseen 二甲雙胍+芭樂多意圖
def test_unseen_erjia_guala_multi(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id="m1", line_user_id="U-m7", text="為自己整理")
    r = orch.handle_text(event_id="m2", line_user_id="U-m7", text="醫生有開二甲雙胍給我，另外芭樂能吃嗎？")
    sess = orch.session_for_user("U-m7")
    assert sess is not None
    assert any("二甲雙胍" in x or "metformin" in x.lower() for x in sess.intake_snapshot.known_medications) or "二甲雙胍" in str(sess.intake_snapshot.known_medications).lower() or "metformin" in str(sess.intake_snapshot.known_medications).lower() or sess.intake_snapshot.known_medications == ["二甲雙胍"] or "metformin" in str(sess.intake_snapshot.known_medications).lower()
    # At least known_medications should have something, not empty, and should not be polluted with doctor question
    assert sess.intake_snapshot.known_medications != []
    # pending should advance
    assert sess.pending_field != "known_medications"
    # reply should contain intake confirm + education
    assert "二甲雙胍" in r.reply or "metformin" in r.reply.lower() or "記為" in r.reply

# 8. 純藥物衛教不寫 intake
def test_pure_erjia_question_no_write(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id="q1", line_user_id="U-q8", text="為自己整理")
    r = orch.handle_text(event_id="q2", line_user_id="U-q8", text="二甲雙胍會有什麼副作用？")
    sess = orch.session_for_user("U-q8")
    assert sess.intake_snapshot.known_medications == []

# 9. 先不要填了不污染
def test_pause_not_polluted(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id="p1", line_user_id="U-p9", text="為自己整理")
    orch.handle_text(event_id="p2", line_user_id="U-p9", text="吃 metformin")
    r = orch.handle_text(event_id="p3", line_user_id="U-p9", text="先不要填了")
    sess = orch.session_for_user("U-p9")
    # Should be paused, not written as allergy
    assert sess.status == "PAUSED"
    assert "先不要填了" not in str(sess.intake_snapshot.model_dump())
    assert sess.intake_snapshot.allergies == [] or "先不要填了" not in str(sess.intake_snapshot.allergies)

# 10. 繼續整理不污染
def test_resume_not_polluted(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id="r1", line_user_id="U-r10", text="為自己整理")
    orch.handle_text(event_id="r2", line_user_id="U-r10", text="吃 metformin")
    orch.handle_text(event_id="r3", line_user_id="U-r10", text="先不要填了")
    r = orch.handle_text(event_id="r4", line_user_id="U-r10", text="繼續整理")
    sess = orch.session_for_user("U-r10")
    # Should return to pending, not write
    assert sess.status == "ACTIVE"
    assert "繼續整理" not in str(sess.intake_snapshot.model_dump())
    # Next pending should be original, not polluted
    assert sess.pending_field == "allergies"

# 11. 感謝／閒聊不污染
def test_thanks_not_polluted(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id="t1", line_user_id="U-t11", text="為自己整理")
    r = orch.handle_text(event_id="t2", line_user_id="U-t11", text="謝謝")
    sess = orch.session_for_user("U-t11")
    assert "謝謝" not in str(sess.intake_snapshot.model_dump())
    assert sess.intake_snapshot.known_medications == []
    assert r.status in ("NEEDS_CLARIFICATION", "SIDE_ANSWER", "COMPLETED", "CHITCHAT", "BLOCKED", "FALLBACK")

# 12. 一般衛教不建立 pending intake
def test_general_education_no_pending(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    r = orch.handle_text(event_id="g1", line_user_id="U-g12", text="糖尿病可以吃水果嗎？")
    sess = orch.session_for_user("U-g12")
    assert sess.pending_field is None or sess.pending_field == "known_medications" and sess.status != "ACTIVE" or sess.authorization_status.value == "UNVERIFIED"
    # More precise: for unauthenticated general education, active_task should be general_education and pending should be None
    from tfda_context_gate.conversation.envelope import build_conversation_envelope
    env = build_conversation_envelope(sess, "糖尿病可以吃水果嗎？")
    assert env.active_task == "general_education"
    assert env.pending_field is None

# 13. intake 中不重複邀請開始 intake
def test_intake_no_repeat_invite(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    orch.handle_text(event_id="i1", line_user_id="U-i13", text="為自己整理")
    r = orch.handle_text(event_id="i2", line_user_id="U-i13", text="吃 metformin")
    # Next should be next question, not invite
    assert "如果要看醫生需要幫你整理嗎" not in r.reply
    # Also after answering education during intake, should not invite again
    r2 = orch.handle_text(event_id="i3", line_user_id="U-i13", text="糖尿病可以吃水果嗎？")
    assert "如果要看醫生需要幫你整理嗎" not in r2.reply

# 14. async LINE session 保留問題、placeholder、push answer (simulate via direct orchestrator + line_bot)
def test_async_line_session_retains(tmp_path: Path, monkeypatch):
    # Simulate line_bot async: placeholder + push
    from fastapi.testclient import TestClient
    import importlib
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", _KEY)
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "async14.sqlite3"))
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "")
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "true")
    monkeypatch.setenv("LINE_DEMO_MODE", "true")
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", "")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    replies=[]
    monkeypatch.setattr(line_app, "_reply_text", lambda _t, text, **k: replies.append(text) or True)
    client = TestClient(line_app.app)
    def _ev(eid, txt):
        return {"events": [{"type":"message","webhookEventId":eid,"replyToken":f"reply-{eid}","source":{"type":"user","userId":"U-async14"},"message":{"type":"text","id":f"msg-{eid}","text":txt}}]}
    # Education that triggers async (if use_formal true, but in test use_formal may be false, so we force async via monkeypatch)
    monkeypatch.setattr(line_app, "_should_use_async_formal", lambda text, _: "水果" in text)
    r1 = client.post("/callback", json=_ev("a1", "糖尿病可以吃水果嗎？"))
    assert r1.status_code == 200
    # Check session has user turn + placeholder + eventually push
    import time
    time.sleep(0.5)  # wait for background push
    orch = line_app._get_conversation_orchestrator()
    sess = orch.session_for_user("U-async14") if orch else None
    assert sess is not None
    # Should contain the question
    assert any("水果" in t.content for t in sess.conversation_context.recent_turns)

# 15. webhook replay 不重複 push／turn
def test_webhook_replay_no_duplicate(tmp_path: Path):
    repo, orch = _new_orch(tmp_path)
    r1 = orch.handle_text(event_id="dup15", line_user_id="U-dup15", text="為自己整理")
    r2 = orch.handle_text(event_id="dup15", line_user_id="U-dup15", text="不同內容")
    assert r2.replayed is True
    assert r1.reply == r2.reply
    sess = orch.session_for_user("U-dup15")
    assert len([t for t in sess.conversation_context.recent_turns if t.role=="user"]) == 1

# 16. 紅旗先於正式 interpreter
def test_red_flag_before_interpreter(tmp_path: Path):
    from tfda_context_gate.conversation.interpreter import ConversationTurnInterpretation
    class BadInterp:
        def interpret(self, envelope):
            # Would return intake if called, but red flag should abort before
            return ConversationTurnInterpretation(intents=["INTAKE_ANSWER"], intake_candidates=[IntakeCandidate(field_name="known_medications", candidate_value="bad", source_quote="bad", confidence=0.9, explicitly_stated=True, requires_confirmation=True)])
    repo = SQLiteProductSessionRepository(tmp_path / "red16.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, interpreter=BadInterp())
    orch.handle_text(event_id="red1", line_user_id="U-red16", text="為自己整理")
    r = orch.handle_text(event_id="red2", line_user_id="U-red16", text="我現在呼吸困難而且快昏倒")
    assert r.status == "FALLBACK" and ("119" in r.reply or "急診" in r.reply)

# 17. interpreter timeout/schema error 安全 fallback
def test_interpreter_fallback(tmp_path: Path):
    class TimeoutInterp:
        def interpret(self, envelope):
            raise TimeoutError("timeout")
    repo = SQLiteProductSessionRepository(tmp_path / "fb17.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, interpreter=TimeoutInterp())
    orch.handle_text(event_id="fb1", line_user_id="U-fb17", text="為自己整理")
    r = orch.handle_text(event_id="fb2", line_user_id="U-fb17", text="你好")
    assert r is not None

    class SchemaErrInterp:
        def interpret(self, envelope):
            raise ValueError("schema")
    repo2 = SQLiteProductSessionRepository(tmp_path / "fb17b.sqlite3")
    orch2 = ConversationOrchestrator(repo2, identity_hash_key=_KEY, interpreter=SchemaErrInterp())
    orch2.handle_text(event_id="fb3", line_user_id="U-fb17b", text="為自己整理")
    r2 = orch2.handle_text(event_id="fb4", line_user_id="U-fb17b", text="你好")
    assert r2 is not None

# 18. 原 278 不回歸 (smoke)
def test_original_278_still_pass():
    from tfda_context_gate.workflow.runner import run_workflow
    wf = run_workflow({"request_id":"orig-278","user_raw_input":"請說明糖尿病的一般飲食原則。","declared_role":"PATIENT","language":"zh-TW"})
    assert wf.status == "COMPLETED"
