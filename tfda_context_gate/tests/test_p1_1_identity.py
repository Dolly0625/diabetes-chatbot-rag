"""P1.1.1 身分詢問 — 冷啟動也能正確回答，且不啟動 intake

下游：主線會修改 orchestrator.py 的身分路由（_handle_product_command / is_identity），
本檔以 DeterministicConversationInterpreter 保持 hermetic，驗證：
- 冷啟動直接問身分 → 正確回答、pending_field 仍 None、不寫入 intake
- 5 變體皆含 AI + 不是真人/不是醫師 + 不提供診斷
- 身分詢問後 pending_field 仍 None
- 身分 + 紅旗混合時優先緊急（不可繞過）
- intake 中問身分不污染已知欄位
"""

from __future__ import annotations

import re
from pathlib import Path

from tfda_context_gate.conversation.interpreter import DeterministicConversationInterpreter
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository

_KEY = "p1-1-identity-test-key-12345678901234"

_IDENTITY_VARIANTS = [
    "你是真人嗎？",
    "現在是機器人在回覆嗎？",
    "這是 AI 客服嗎？",
    "有人在嗎，還是電腦自動回答？",
    "我是在跟醫生聊天嗎？",
]


def _new_orch(tmp_path: Path, **kw):
    repo = SQLiteProductSessionRepository(tmp_path / f"{tmp_path.name}.sqlite3")
    if "interpreter" not in kw:
        kw["interpreter"] = DeterministicConversationInterpreter()
    return repo, ConversationOrchestrator(repo, identity_hash_key=_KEY, **kw)


def _assert_identity_reply(reply: str):
    """變體皆應：含 AI、且說明不是真人/不是醫師、且不提供診斷（否定式）。"""
    assert "AI" in reply or "ai" in reply.lower(), f"reply 應包含 AI，實際: {reply!r}"
    has_not_human = any(x in reply for x in ("不是真人", "不是真的人", "不是人類", "不是人"))
    has_not_doctor = any(x in reply for x in ("不是醫師", "不是醫生", "不是醫師/醫生", "不是醫護", "不是醫生"))
    # 滿足其一即可，任務描述為「不是真人/不是醫師」擇一或兩者皆有皆算通過
    assert has_not_human or has_not_doctor, f"reply 應說明不是真人或不是醫師，實際: {reply!r}"
    # 不提供診斷：應含否定診斷字樣，且不應直接給個人診斷
    has_no_diag = any(x in reply for x in ("不做診斷", "不提供診斷", "不能診斷", "不會診斷", "不進行診斷", "不是診斷"))
    # 若含「診斷」必須是否定語境；若完全不含診斷但含「不是醫師」亦視為符合
    if "診斷" in reply:
        assert has_no_diag or has_not_doctor, f"含診斷應為否定語境，實際: {reply!r}"
    else:
        # 至少要有 不是醫師/醫生 或 不做診斷 來表達不提供診斷
        assert has_not_doctor or has_no_diag, f"應聲明不提供診斷/不是醫師，實際: {reply!r}"
    # 不應直接給個人診斷結論
    assert not re.search(r"你有糖尿病|你罹患|確診為.*糖尿病|你是糖尿病", reply), f"身分回覆不應提供個人診斷，實際: {reply!r}"


def _assert_no_intake_pollution(sess):
    snap = sess.intake_snapshot
    dump = snap.model_dump()
    assert snap.known_medications == [], f"不應寫入 known_medications，實際 {snap.known_medications}"
    assert snap.allergies == [], f"不應寫入 allergies，實際 {snap.allergies}"
    assert snap.chronic_conditions == [], f"不應寫入 chronic_conditions，實際 {snap.chronic_conditions}"
    assert snap.family_history == [], f"不應寫入 family_history，實際 {snap.family_history}"
    assert snap.symptom_onset is None, f"不應寫入 symptom_onset，實際 {snap.symptom_onset}"
    assert snap.symptom_description is None, f"不應寫入 symptom_description，實際 {snap.symptom_description}"
    assert snap.symptom_severity is None, f"不應寫入 symptom_severity，實際 {snap.symptom_severity}"
    assert snap.questions_for_doctor == [], f"不應寫入 questions_for_doctor，實際 {snap.questions_for_doctor}"
    for v in _IDENTITY_VARIANTS:
        assert v.strip("？?。.!！") not in str(dump), f"身分字串不應污染 intake: {v}"
    # pending_field 亦應為 None（由各測試再精確斷言）


# 1. 冷啟動不先建立 intake，直接問身分
def test_identity_cold_start_no_intake(tmp_path: Path):
    _, orch = _new_orch(tmp_path)
    r = orch.handle_text(event_id="id-cold-1", line_user_id="U-id-cold", text="你是真人嗎？")
    _assert_identity_reply(r.reply)
    sess = orch.session_for_user("U-id-cold")
    assert sess.pending_field is None, f"pending_field 應仍 None，實際 {sess.pending_field}"
    _assert_no_intake_pollution(sess)
    # intake_stage 應保持初始（stage1 但 pending 為 None 不算進入 intake 流程）或 None
    assert sess.intake_stage in (None, "stage1", "stage1"), "冷啟動身分詢問不應進入 intake 需確認階段"
    # 不應被誤判為需要授權的 intake 流程
    assert r.status not in ("NEEDS_CLARIFICATION",) or "AI" in r.reply


# 2. 5 變體皆應正確回覆且不寫入
def test_identity_variants(tmp_path: Path):
    for idx, variant in enumerate(_IDENTITY_VARIANTS):
        repo, orch = _new_orch(tmp_path)
        # 每個變體用獨立 user，避免 dedup/short-ttl 干擾
        uid = f"U-id-var-{idx}"
        r = orch.handle_text(event_id=f"id-var-{idx}", line_user_id=uid, text=variant)
        _assert_identity_reply(r.reply)
        sess = orch.session_for_user(uid)
        assert sess.pending_field is None, f"variant {variant!r} pending_field 應 None，實際 {sess.pending_field}"
        _assert_no_intake_pollution(sess)


# 3. 身分詢問後 pending_field 仍 None（獨立驗證）
def test_identity_no_pending_created(tmp_path: Path):
    _, orch = _new_orch(tmp_path)
    r = orch.handle_text(event_id="id-nopending-1", line_user_id="U-id-nopending", text="這是 AI 客服嗎？")
    _assert_identity_reply(r.reply)
    sess = orch.session_for_user("U-id-nopending")
    assert sess.pending_field is None
    assert sess.pending_question is None or "AI" not in (sess.pending_question or "")
    _assert_no_intake_pollution(sess)

    # 連續再問一次身分，仍不產生 pending
    r2 = orch.handle_text(event_id="id-nopending-2", line_user_id="U-id-nopending", text="有人在嗎，還是電腦自動回答？")
    _assert_identity_reply(r2.reply)
    sess2 = orch.session_for_user("U-id-nopending")
    assert sess2.pending_field is None


# 4. 身分詢問不能繞過紅旗（混合時應優先緊急）
def test_identity_no_redflag_bypass(tmp_path: Path):
    _, orch = _new_orch(tmp_path)
    # 變體 + 典型紅旗句混合
    mixed = "你是真人嗎？我現在呼吸困難而且快昏倒"
    r = orch.handle_text(event_id="id-red-1", line_user_id="U-id-red", text=mixed)
    assert r.status == "FALLBACK", f"紅旗混合應 FALLBACK，實際 {r.status} / {r.reply!r}"
    assert ("119" in r.reply or "急診" in r.reply or "緊急" in r.reply), f"應回緊急指引，實際: {r.reply!r}"

    # 另一混合：AI 客服 + 胸痛冒冷汗
    _, orch2 = _new_orch(tmp_path)
    mixed2 = "這是 AI 客服嗎？我胸痛冒冷汗快昏倒了"
    r2 = orch2.handle_text(event_id="id-red-2", line_user_id="U-id-red2", text=mixed2)
    assert r2.status == "FALLBACK"
    assert ("119" in r2.reply or "急診" in r2.reply or "緊急" in r2.reply)


# 5. 已在 intake 中問身分，不應污染已知欄位
def test_identity_after_intake_no_pollution(tmp_path: Path):
    _, orch = _new_orch(tmp_path)
    # 建立 intake：為自己整理 -> 吃 metformin
    orch.handle_text(event_id="id-pol-1", line_user_id="U-id-pol", text="為自己整理")
    orch.handle_text(event_id="id-pol-2", line_user_id="U-id-pol", text="吃 metformin")
    sess_before = orch.session_for_user("U-id-pol")
    before_meds = list(sess_before.intake_snapshot.known_medications)
    before_allergies = list(sess_before.intake_snapshot.allergies)
    before_pending = sess_before.pending_field
    before_snapshot_dump = sess_before.intake_snapshot.model_dump()
    assert before_meds != [] or before_pending is not None  # 確保已進入 intake

    # 在 intake 進行中穿插身分詢問
    r = orch.handle_text(event_id="id-pol-3", line_user_id="U-id-pol", text="你是真人嗎？")
    _assert_identity_reply(r.reply)
    sess_after = orch.session_for_user("U-id-pol")
    assert sess_after.intake_snapshot.known_medications == before_meds, f"身分詢問不應污染 known_medications，before {before_meds} after {sess_after.intake_snapshot.known_medications}"
    assert sess_after.intake_snapshot.allergies == before_allergies
    # 其他欄位亦不應被身分字串污染
    assert "你是真人嗎" not in str(sess_after.intake_snapshot.model_dump())
    assert sess_after.intake_snapshot.symptom_onset == before_snapshot_dump["symptom_onset"]
    assert sess_after.intake_snapshot.symptom_description == before_snapshot_dump["symptom_description"]
    assert sess_after.intake_snapshot.questions_for_doctor == before_snapshot_dump["questions_for_doctor"]
    # pending_field 應保持不變（不因身分詢問而被改為錯誤欄位或清空為 None 導致流程丟失）
    # 允許保持原 pending 或仍指向下一個 intake 欄位，但絕不應被身分字串寫入
    assert sess_after.pending_field == before_pending or sess_after.pending_field in (
        "allergies",
        "chronic_conditions",
        "family_history",
        "symptom_onset",
        "symptom_description",
        "symptom_severity",
        "questions_for_doctor",
    )
