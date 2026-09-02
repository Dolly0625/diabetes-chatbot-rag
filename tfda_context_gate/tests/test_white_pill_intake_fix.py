"""最小可驗證修復：白色小藥丸不得寫入 known_medications，其他路徑不退步"""

from pathlib import Path
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.conversation.interpreter import DeterministicConversationInterpreter

_KEY = "white-pill-test-key-12345678901234"

def _new_orch(tmp_path: Path):
    repo = SQLiteProductSessionRepository(tmp_path / f"{tmp_path.name}_{id(tmp_path)}.sqlite3")
    return ConversationOrchestrator(repo, identity_hash_key=_KEY, interpreter=DeterministicConversationInterpreter(), use_formal=False)

def test_white_pill_not_written_and_asks_bag(tmp_path: Path):
    orch = _new_orch(tmp_path)
    orch.handle_text(event_id="w1", line_user_id="U-white", text="為自己整理")
    r = orch.handle_text(event_id="w2", line_user_id="U-white", text="吃白色小藥丸")
    sess = orch.session_for_user("U-white")
    assert sess.intake_snapshot.known_medications == [], f"白色小藥丸不應直接寫入，got {sess.intake_snapshot.known_medications}"
    assert "藥袋" in r.reply, f"應追問藥袋，got {r.reply}"
    assert sess.pending_field == "known_medications"

def test_white_pill_variants_also_brown_bag(tmp_path: Path):
    for txt in ["白色藥丸", "白色小藥丸", "不記得藥名", "忘了吃什麼藥"]:
        repo = SQLiteProductSessionRepository(tmp_path / f"var_{txt[:4]}.sqlite3")
        orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, interpreter=DeterministicConversationInterpreter(), use_formal=False)
        orch.handle_text(event_id="v1", line_user_id=f"U-{txt}", text="為自己整理")
        r = orch.handle_text(event_id="v2", line_user_id=f"U-{txt}", text=txt)
        sess = orch.session_for_user(f"U-{txt}")
        assert sess.intake_snapshot.known_medications == [], f"{txt!r} 不應寫入"
        assert "藥袋" in r.reply or "顏色、形狀" in r.reply

def test_confirmation_word_not_pollute_and_advance(tmp_path: Path):
    orch = _new_orch(tmp_path)
    orch.handle_text(event_id="c1", line_user_id="U-conf", text="為自己整理")
    orch.handle_text(event_id="c2", line_user_id="U-conf", text="metformin")
    sess = orch.session_for_user("U-conf")
    assert sess.intake_snapshot.known_medications == ["metformin"]
    assert sess.pending_field == "allergies"
    # 現在 pending 是 allergies，說「正確」不應寫入任何欄位
    r = orch.handle_text(event_id="c3", line_user_id="U-conf", text="正確")
    sess2 = orch.session_for_user("U-conf")
    assert "正確" not in sess2.intake_snapshot.known_medications
    assert "正確" not in (sess2.intake_snapshot.allergies or [])
    assert sess2.intake_snapshot.known_medications == ["metformin"], "確認詞不應覆蓋原值"
    # 若前一輪是白色小藥丸的追問，確認詞也不應寫入
    repo2 = SQLiteProductSessionRepository(tmp_path / "conf2.sqlite3")
    orch2 = ConversationOrchestrator(repo2, identity_hash_key=_KEY, interpreter=DeterministicConversationInterpreter(), use_formal=False)
    orch2.handle_text(event_id="c4", line_user_id="U-conf2", text="為自己整理")
    orch2.handle_text(event_id="c5", line_user_id="U-conf2", text="吃白色小藥丸")
    orch2.handle_text(event_id="c6", line_user_id="U-conf2", text="正確")
    sess3 = orch2.session_for_user("U-conf2")
    assert sess3.intake_snapshot.known_medications == [], "確認詞不應讓白色小藥丸入欄"

def test_metformin_normal_write_and_advance(tmp_path: Path):
    orch = _new_orch(tmp_path)
    orch.handle_text(event_id="m1", line_user_id="U-met", text="為自己整理")
    r = orch.handle_text(event_id="m2", line_user_id="U-met", text="metformin")
    sess = orch.session_for_user("U-met")
    assert sess.intake_snapshot.known_medications == ["metformin"]
    assert sess.pending_field == "allergies"
    assert "對嗎" in r.reply or "過敏" in r.reply

def test_cancel_not_pollute_meds(tmp_path: Path):
    orch = _new_orch(tmp_path)
    orch.handle_text(event_id="x1", line_user_id="U-cancel", text="為自己整理")
    orch.handle_text(event_id="x2", line_user_id="U-cancel", text="吃白色小藥丸")
    r = orch.handle_text(event_id="x3", line_user_id="U-cancel", text="不要記")
    sess = orch.session_for_user("U-cancel")
    assert "不要記" not in sess.intake_snapshot.known_medications
    assert sess.intake_snapshot.known_medications == []
    # 取消語意不應讓 pending 亂跳到已完成
    assert sess.pending_field == "known_medications"

def test_two_attempts_then_sentinel(tmp_path: Path):
    orch = _new_orch(tmp_path)
    orch.handle_text(event_id="t1", line_user_id="U-2try", text="為自己整理")
    orch.handle_text(event_id="t2", line_user_id="U-2try", text="吃白色小藥丸")
    orch.handle_text(event_id="t3", line_user_id="U-2try", text="白色藥丸")
    r = orch.handle_text(event_id="t4", line_user_id="U-2try", text="不記得")
    sess = orch.session_for_user("U-2try")
    assert sess.intake_snapshot.known_medications == ["不清楚（待看診確認）"]
    assert sess.pending_field == "allergies"
    assert "待看診確認" in r.reply or "過敏" in r.reply
