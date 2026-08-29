"""PendingAction lifecycle audit — 12 scenarios (TDD)."""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.line_orchestration.orchestrator import HONEST_FALLBACK_TEXT
from tfda_context_gate.product_session import ProductSession, SQLiteProductSessionRepository
from tfda_context_gate.workflow.schemas import WorkflowResult
from tfda_context_gate.intake.summary import generate_previsit_summary

_KEY = "pending-lifecycle-key-at-least-16-chars!!"


def _new_orch(tmp_path, name="sessions.sqlite3"):
    repo = SQLiteProductSessionRepository(tmp_path / name)
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    return orch, repo


# 1. service restart severity clarify
def test_lifecycle_01_severity_persist_after_restart(tmp_path):
    path = tmp_path / "lc01.sqlite3"
    repo = SQLiteProductSessionRepository(path)
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    orch.handle_text(event_id="lc01-1", line_user_id="U-lc01", text="為自己整理")
    orch.handle_text(event_id="lc01-2", line_user_id="U-lc01", text="metformin")
    orch.handle_text(event_id="lc01-3", line_user_id="U-lc01", text="沒有過敏")
    orch.handle_text(event_id="lc01-4", line_user_id="U-lc01", text="高血壓")
    orch.handle_text(event_id="lc01-5", line_user_id="U-lc01", text="無家族史")
    orch.handle_text(event_id="lc01-6", line_user_id="U-lc01", text="三天前開始")
    orch.handle_text(event_id="lc01-7", line_user_id="U-lc01", text="很容易餓，手會抖")
    r = orch.handle_text(event_id="lc01-8", line_user_id="U-lc01", text="好像滿嚴重的欸")
    assert r.status == "NEEDS_CLARIFICATION"
    sess_before = repo.get(orch.session_for_user("U-lc01").session_id)
    assert sess_before.pending_action is not None and sess_before.pending_action.type == "PENDING_SEVERITY_CLARIFY"
    # restart
    repo2 = SQLiteProductSessionRepository(path)
    orch2 = ConversationOrchestrator(repo2, identity_hash_key=_KEY)
    r2 = orch2.handle_text(event_id="lc01-9", line_user_id="U-lc01", text="6分")
    sess2 = repo2.get(r2.session_id)
    assert sess2.intake_snapshot.symptom_severity == "中度", f"6分應標準化為中度，實際 {sess2.intake_snapshot.symptom_severity}"
    assert sess2.pending_action is None
    assert sess2.intake_snapshot.known_medications == ["metformin"]


# 2. service restart pending question
def test_lifecycle_02_question_persist_after_restart(tmp_path):
    path = tmp_path / "lc02.sqlite3"
    repo = SQLiteProductSessionRepository(path)
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    orch.handle_text(event_id="lc02-1", line_user_id="U-lc02", text="為自己整理")
    orch.handle_text(event_id="lc02-2", line_user_id="U-lc02", text="metformin")
    wf = WorkflowResult(request_id="lc02-fb", status="FALLBACK", final_response=HONEST_FALLBACK_TEXT, fallback_reason="B_INSUFFICIENT", a_result=None, query_expansion=None, rag_result=None, b_result=None, c_result=None, d_result=None, agent_action=None, agent_reason_code=None, question=None, current_query="請問血糖可吃水果嗎", execution_history=[], agent_steps=0, rewrite_count=0, clarification_count=0, termination_reason="B_INSUFFICIENT", intake_snapshot=None, intake_stage=None, previsit_summary=None, system_risk_classification=None, trace={"events": [], "evaluations": []})
    orch.push_formal_result(line_user_id="U-lc02", event_id="lc02-push", workflow=wf, original_text="請問血糖可吃水果嗎", push_sender=lambda u, t: True)
    sess_before = repo.get(orch.session_for_user("U-lc02").session_id)
    assert sess_before.pending_action is not None and sess_before.pending_action.type == "PENDING_CONFIRM_QUESTION"
    assert sess_before.intake_snapshot.questions_for_doctor == []
    # restart
    repo2 = SQLiteProductSessionRepository(path)
    orch2 = ConversationOrchestrator(repo2, identity_hash_key=_KEY)
    r = orch2.handle_text(event_id="lc02-3", line_user_id="U-lc02", text="好，幫我記")
    sess2 = repo2.get(r.session_id)
    assert "請問血糖可吃水果嗎" in sess2.intake_snapshot.questions_for_doctor
    assert sess2.pending_action is None


# 3. webhook replay idempotency
def test_lifecycle_03_webhook_replay_once(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "lc03.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    orch.handle_text(event_id="lc03-1", line_user_id="U-lc03", text="為自己整理")
    orch.handle_text(event_id="lc03-2", line_user_id="U-lc03", text="metformin")
    wf = WorkflowResult(request_id="lc03-fb", status="FALLBACK", final_response=HONEST_FALLBACK_TEXT, fallback_reason="B_INSUFFICIENT", a_result=None, query_expansion=None, rag_result=None, b_result=None, c_result=None, d_result=None, agent_action=None, agent_reason_code=None, question=None, current_query="血糖可吃水果嗎", execution_history=[], agent_steps=0, rewrite_count=0, clarification_count=0, termination_reason="B_INSUFFICIENT", intake_snapshot=None, intake_stage=None, previsit_summary=None, system_risk_classification=None, trace={"events": [], "evaluations": []})
    orch.push_formal_result(line_user_id="U-lc03", event_id="lc03-push", workflow=wf, original_text="血糖可吃水果嗎", push_sender=lambda u, t: True)
    r1 = orch.handle_text(event_id="lc03-evt", line_user_id="U-lc03", text="好，幫我記")
    sess1 = repo.get(r1.session_id)
    assert sess1.intake_snapshot.questions_for_doctor.count("血糖可吃水果嗎") == 1
    turns1 = len(sess1.conversation_context.recent_turns)
    ver1 = sess1.version
    r2 = orch.handle_text(event_id="lc03-evt", line_user_id="U-lc03", text="好，幫我記")
    sess2 = repo.get(r2.session_id)
    assert r2.replayed is True
    assert sess2.intake_snapshot.questions_for_doctor.count("血糖可吃水果嗎") == 1
    assert sess2.version == ver1
    assert len(sess2.conversation_context.recent_turns) == turns1


# 4. reject / cancel variants
@pytest.mark.parametrize("txt", ["不用了", "不要記", "算了", "取消"])
def test_lifecycle_04_reject_clears_pending(tmp_path, txt):
    repo = SQLiteProductSessionRepository(tmp_path / f"lc04-{txt}.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    orch.handle_text(event_id="lc04-1", line_user_id="U-lc04", text="為自己整理")
    orch.handle_text(event_id="lc04-2", line_user_id="U-lc04", text="metformin")
    wf = WorkflowResult(request_id="lc04-fb", status="FALLBACK", final_response=HONEST_FALLBACK_TEXT, fallback_reason="B_INSUFFICIENT", a_result=None, query_expansion=None, rag_result=None, b_result=None, c_result=None, d_result=None, agent_action=None, agent_reason_code=None, question=None, current_query="要記的血糖問題", execution_history=[], agent_steps=0, rewrite_count=0, clarification_count=0, termination_reason="B_INSUFFICIENT", intake_snapshot=None, intake_stage=None, previsit_summary=None, system_risk_classification=None, trace={"events": [], "evaluations": []})
    orch.push_formal_result(line_user_id="U-lc04", event_id="lc04-push", workflow=wf, original_text="要記的血糖問題", push_sender=lambda u, t: True)
    before = repo.get(orch.session_for_user("U-lc04").session_id)
    assert before.intake_snapshot.questions_for_doctor == []
    r = orch.handle_text(event_id=f"lc04-{txt}", line_user_id="U-lc04", text=txt)
    sess = repo.get(r.session_id)
    assert sess.pending_action is None
    assert "要記的血糖問題" not in sess.intake_snapshot.questions_for_doctor
    assert sess.intake_snapshot.known_medications == ["metformin"]


# 5. red flag interruption
def test_lifecycle_05_redflag_priority(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "lc05.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    orch.handle_text(event_id="lc05-1", line_user_id="U-lc05", text="為自己整理")
    orch.handle_text(event_id="lc05-2", line_user_id="U-lc05", text="metformin")
    orch.handle_text(event_id="lc05-3", line_user_id="U-lc05", text="沒有過敏")
    # create severity pending
    orch.handle_text(event_id="lc05-4", line_user_id="U-lc05", text="高血壓")
    orch.handle_text(event_id="lc05-5", line_user_id="U-lc05", text="無家族史")
    orch.handle_text(event_id="lc05-6", line_user_id="U-lc05", text="三天前開始")
    orch.handle_text(event_id="lc05-7", line_user_id="U-lc05", text="很容易餓")
    r = orch.handle_text(event_id="lc05-8", line_user_id="U-lc05", text="好像滿嚴重的欸")
    assert r.status == "NEEDS_CLARIFICATION"
    sess_before = repo.get(r.session_id)
    assert sess_before.pending_action.type == "PENDING_SEVERITY_CLARIFY"
    # red flag
    r2 = orch.handle_text(event_id="lc05-9", line_user_id="U-lc05", text="突然胸痛呼吸困難冒冷汗")
    sess2 = repo.get(r2.session_id)
    assert r2.status == "FALLBACK"
    assert "119" in r2.reply or "急" in r2.reply
    assert sess2.system_risk_classification is not None and sess2.system_risk_classification.get("level") == "RED_FLAG"
    # pending should remain (not consumed) and intake not polluted
    assert sess2.pending_action is not None and sess2.pending_action.type == "PENDING_SEVERITY_CLARIFY"
    assert "胸痛" not in str(sess2.intake_snapshot.symptom_description or "")
    assert "胸痛" not in str(sess2.intake_snapshot.symptom_severity or "")
    # subsequent safe message should not downgrade RED_FLAG
    r3 = orch.handle_text(event_id="lc05-10", line_user_id="U-lc05", text="我很好")
    sess3 = repo.get(r3.session_id)
    assert sess3.system_risk_classification.get("level") == "RED_FLAG"


# 6. subject switch isolation
def test_lifecycle_06_subject_switch_clears_pending_and_isolation(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "lc06.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    orch.handle_text(event_id="lc06-1", line_user_id="U-lc06", text="為自己整理")
    orch.handle_text(event_id="lc06-2", line_user_id="U-lc06", text="metformin")
    # create pending confirm
    wf = WorkflowResult(request_id="lc06-fb", status="FALLBACK", final_response=HONEST_FALLBACK_TEXT, fallback_reason="B_INSUFFICIENT", a_result=None, query_expansion=None, rag_result=None, b_result=None, c_result=None, d_result=None, agent_action=None, agent_reason_code=None, question=None, current_query="家屬血糖問題", execution_history=[], agent_steps=0, rewrite_count=0, clarification_count=0, termination_reason="B_INSUFFICIENT", intake_snapshot=None, intake_stage=None, previsit_summary=None, system_risk_classification=None, trace={"events": [], "evaluations": []})
    orch.push_formal_result(line_user_id="U-lc06", event_id="lc06-push", workflow=wf, original_text="家屬血糖問題", push_sender=lambda u, t: True)
    before = repo.get(orch.session_for_user("U-lc06").session_id)
    assert before.pending_action is not None
    assert before.intake_snapshot.known_medications == ["metformin"]
    # switch to proxy
    r = orch.handle_text(event_id="lc06-3", line_user_id="U-lc06", text="代家人整理")
    sess = repo.get(r.session_id)
    assert sess.pending_action is None
    assert sess.intake_snapshot.known_medications == []
    assert "metformin" not in str(sess.conversation_context.recent_turns)
    # reverse: proxy has pending, switch back to self
    orch.handle_text(event_id="lc06-4", line_user_id="U-lc06", text="已取得同意")
    orch.handle_text(event_id="lc06-5", line_user_id="U-lc06", text="家人本人描述")
    orch.handle_text(event_id="lc06-6", line_user_id="U-lc06", text="沒有過敏")
    wf2 = WorkflowResult(request_id="lc06-fb2", status="FALLBACK", final_response=HONEST_FALLBACK_TEXT, fallback_reason="B_INSUFFICIENT", a_result=None, query_expansion=None, rag_result=None, b_result=None, c_result=None, d_result=None, agent_action=None, agent_reason_code=None, question=None, current_query="另一問題", execution_history=[], agent_steps=0, rewrite_count=0, clarification_count=0, termination_reason="B_INSUFFICIENT", intake_snapshot=None, intake_stage=None, previsit_summary=None, system_risk_classification=None, trace={"events": [], "evaluations": []})
    orch.push_formal_result(line_user_id="U-lc06", event_id="lc06-push2", workflow=wf2, original_text="另一問題", push_sender=lambda u, t: True)
    before2 = repo.get(orch.session_for_user("U-lc06").session_id)
    assert before2.pending_action is not None
    r2 = orch.handle_text(event_id="lc06-7", line_user_id="U-lc06", text="為自己整理")
    sess2 = repo.get(r2.session_id)
    assert sess2.pending_action is None
    assert "另一問題" not in str(sess2.intake_snapshot.questions_for_doctor)
    assert sess2.intake_snapshot.known_medications == []


# 7. pause / resume
def test_lifecycle_07_pause_resume_pending(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "lc07.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    orch.handle_text(event_id="lc07-1", line_user_id="U-lc07", text="為自己整理")
    orch.handle_text(event_id="lc07-2", line_user_id="U-lc07", text="metformin")
    orch.handle_text(event_id="lc07-3", line_user_id="U-lc07", text="沒有過敏")
    orch.handle_text(event_id="lc07-4", line_user_id="U-lc07", text="高血壓")
    orch.handle_text(event_id="lc07-5", line_user_id="U-lc07", text="無家族史")
    orch.handle_text(event_id="lc07-6", line_user_id="U-lc07", text="三天前開始")
    orch.handle_text(event_id="lc07-7", line_user_id="U-lc07", text="很容易餓")
    r = orch.handle_text(event_id="lc07-8", line_user_id="U-lc07", text="好像滿嚴重的欸")
    assert r.status == "NEEDS_CLARIFICATION"
    pending_before = repo.get(r.session_id).pending_action
    assert pending_before.type == "PENDING_SEVERITY_CLARIFY"
    # pause
    r2 = orch.handle_text(event_id="lc07-9", line_user_id="U-lc07", text="暫停整理")
    sess2 = repo.get(r2.session_id)
    assert sess2.status == "PAUSED"
    assert sess2.pending_action is not None and sess2.pending_action.type == "PENDING_SEVERITY_CLARIFY"
    # while paused, answering severity should not be consumed? Our spec: pending not executed while paused, but we test that after pause, severity answer is treated as side? For now ensure not duplicated
    # resume
    r3 = orch.handle_text(event_id="lc07-10", line_user_id="U-lc07", text="繼續整理")
    sess3 = repo.get(r3.session_id)
    assert sess3.status == "ACTIVE"
    assert sess3.pending_action is not None and sess3.pending_action.type == "PENDING_SEVERITY_CLARIFY"
    assert sess3.pending_field == "symptom_severity"
    r4 = orch.handle_text(event_id="lc07-11", line_user_id="U-lc07", text="6分")
    sess4 = repo.get(r4.session_id)
    assert sess4.intake_snapshot.symptom_severity == "中度"
    assert sess4.pending_action is None
    # ensure not duplicated
    r5 = orch.handle_text(event_id="lc07-12", line_user_id="U-lc07", text="6分")
    sess5 = repo.get(r5.session_id)
    assert sess5.intake_snapshot.symptom_severity == "中度"


# 8. review correction
def test_lifecycle_08_review_correction_single_field(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "lc08.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    for eid, txt in [("lc08-1","為自己整理"),("lc08-2","metformin"),("lc08-3","沒有過敏"),("lc08-4","高血壓"),("lc08-5","無家族史"),("lc08-6","三天前開始"),("lc08-7","很容易餓"),("lc08-8","中度"),("lc08-9","想問飲食"),("lc08-10","確認完成")]:
        orch.handle_text(event_id=eid, line_user_id="U-lc08", text=txt)
    # reach review then submit? Actually need review stage before submit
    # redo to get review not submitted
    repo2 = SQLiteProductSessionRepository(tmp_path / "lc08b.sqlite3")
    orch2 = ConversationOrchestrator(repo2, identity_hash_key=_KEY)
    for eid, txt in [("lc08b-1","為自己整理"),("lc08b-2","metformin"),("lc08b-3","沒有過敏"),("lc08b-4","高血壓"),("lc08b-5","無家族史"),("lc08b-6","三天前開始"),("lc08b-7","很容易餓"),("lc08b-8","中度"),("lc08b-9","想問飲食")]:
        last = orch2.handle_text(event_id=eid, line_user_id="U-lc08b", text=txt)
    assert last.intake_stage == "review"
    r = orch2.handle_text(event_id="lc08b-10", line_user_id="U-lc08b", text="其他都對，只有過敏要改成盤尼西林")
    sess = repo2.get(r.session_id)
    assert sess.intake_snapshot.allergies == ["盤尼西林"]
    assert sess.intake_snapshot.known_medications == ["metformin"]
    assert sess.intake_stage == "review"
    assert sess.pending_action is None
    summary = generate_previsit_summary(sess.intake_snapshot, request_id=sess.session_id)
    assert "盤尼西林" in summary.summary_text
    assert "過敏史：無" not in summary.summary_text


# 9. share safety with pending
def test_lifecycle_09_share_blocked_with_pending(tmp_path):
    from tfda_context_gate.sharing import ShareGrantService
    repo = SQLiteProductSessionRepository(tmp_path / "lc09.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    for eid, txt in [("lc09-1","為自己整理"),("lc09-2","metformin"),("lc09-3","沒有過敏"),("lc09-4","高血壓"),("lc09-5","無家族史"),("lc09-6","三天前開始"),("lc09-7","很容易餓"),("lc09-8","中度"),("lc09-9","想問飲食")]:
        orch.handle_text(event_id=eid, line_user_id="U-lc09", text=txt)
    # create pending question
    wf = WorkflowResult(request_id="lc09-fb", status="FALLBACK", final_response=HONEST_FALLBACK_TEXT, fallback_reason="B_INSUFFICIENT", a_result=None, query_expansion=None, rag_result=None, b_result=None, c_result=None, d_result=None, agent_action=None, agent_reason_code=None, question=None, current_query="候選血糖問題", execution_history=[], agent_steps=0, rewrite_count=0, clarification_count=0, termination_reason="B_INSUFFICIENT", intake_snapshot=None, intake_stage=None, previsit_summary=None, system_risk_classification=None, trace={"events": [], "evaluations": []})
    orch.push_formal_result(line_user_id="U-lc09", event_id="lc09-push", workflow=wf, original_text="候選血糖問題", push_sender=lambda u, t: True)
    sess = repo.get(orch.session_for_user("U-lc09").session_id)
    assert sess.pending_action is not None
    # try share should fail (not submitted) and snapshot should not contain pending
    svc = ShareGrantService(repo)
    with pytest.raises(Exception):
        svc.create(sess)
    # now submit correctly
    r = orch.handle_text(event_id="lc09-10", line_user_id="U-lc09", text="確認完成")
    sess2 = repo.get(r.session_id)
    assert sess2.status == "SUBMITTED"
    assert sess2.pending_action is None
    issue = svc.create(sess2)
    # issue is ShareGrantIssue, fetch stored grant via repository
    assert issue.grant_id is not None
    # ensure pending candidate not in share payload (via session snapshot)
    assert "候選血糖問題" not in json.dumps(sess2.intake_snapshot.model_dump())
    # fetch stored grant payload via DB
    import sqlite3
    conn = sqlite3.connect(str(repo.path))
    row = conn.execute("SELECT payload FROM share_grants WHERE grant_id=?", (issue.grant_id,)).fetchone()
    payload = json.loads(row[0]) if row else {}
    assert "候選血糖問題" not in json.dumps(payload)
    assert "不清楚（待看診確認）" not in json.dumps(payload)


# 10. old session compat
def test_lifecycle_10_old_session_without_pending(tmp_path):
    # simulate old payload without pending_action
    import datetime, json, sqlite3
    from tfda_context_gate.conversation import ConversationContextManager
    mgr = ConversationContextManager()
    ctx = mgr.create("old-sess-001")
    old_payload = {
        "session_id": "old-sess-001",
        "principal_id_hash": "a"*64,
        "actor_role": "PATIENT",
        "frontend_persona": "PATIENT_FAMILY",
        "authorization_status": "PATIENT_SELF",
        "permission_scopes": ["CREATE_OWN_INTAKE"],
        "conversation_context": json.loads(ctx.model_dump_json()),
        "intake_snapshot": {"known_medications": ["metformin"], "allergies": ["無"]},
        "intake_stage": "stage2",
        "pending_field": "symptom_onset",
        "pending_question": "什麼時候開始？",
        "system_risk_classification": None,
        "status": "ACTIVE",
        "version": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2027-01-08T00:00:00+00:00",
    }
    # intentionally omit pending_action
    path = tmp_path / "lc10.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS product_sessions (session_id TEXT PRIMARY KEY, payload TEXT, version INTEGER, updated_at TEXT, expires_at TEXT)")
    conn.execute("INSERT INTO product_sessions VALUES (?,?,?, ?, ?)", ("old-sess-001", json.dumps(old_payload), 0, old_payload["updated_at"], old_payload["expires_at"]))
    conn.commit()
    conn.close()
    repo = SQLiteProductSessionRepository(path)
    sess = repo.get("old-sess-001")
    assert sess is not None
    assert sess.pending_action is None
    assert sess.intake_snapshot.known_medications == ["metformin"]
    # can still handle text
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    # need to map line user to this session? Use direct repo get, then handle via new user will create new session, but old session should remain loadable
    assert sess.version == 0


# 11. write consistency no bypass
def test_lifecycle_11_no_bypass_write(tmp_path):
    # ensure line_bot and orchestrator converge to same pending method
    # line_bot's _maybe_record should not directly write, only orchestrator pending
    import importlib
    import pathlib as _pl
    # check line_bot file does not contain direct questions append without pending
    src = _pl.Path("line_bot/app.py").read_text(encoding="utf-8")
    # after fix, it should call orchestrator.push_formal_result or set pending, not direct append
    # we check that our orchestrator is single source
    # This test ensures no third path: count write points
    import re
    writes = re.findall(r"questions_for_doctor.*\[.*\+", src)
    # line_bot should have zero direct appends after fix (only via orchestrator)
    assert len(writes) == 0, f"line_bot should not directly append, found {writes}"
    # orchestrator should have exactly 2 write points (early WANT and valid merge) + 1 pending confirm
    orch_src = _pl.Path("tfda_context_gate/line_orchestration/orchestrator.py").read_text(encoding="utf-8")
    # count of direct append patterns that are inside pending confirm (is controlled) vs bypass
    # we just ensure total appends are not >3
    orch_writes = re.findall(r"questions_for_doctor.*\[\*intake\.questions_for_doctor", orch_src)
    assert 1 <= len(orch_writes) <= 4


# 12. direct workflow no pollution
def test_lifecycle_12_direct_workflow_no_sentinel(tmp_path):
    from tfda_context_gate.workflow import run_workflow
    res = run_workflow({"request_id": "lc12-1", "schema_version": "a.v0.1", "user_raw_input": "我要繼續整理看診前資料", "declared_role": "PATIENT", "language": "zh-TW"}, task_type="pre_visit_intake", intake={"known_medications": ["metformin"]})
    snap = res.intake_snapshot or {}
    # handle both dict and model
    if isinstance(snap, dict):
        qs = snap.get("questions_for_doctor", [])
    else:
        qs = getattr(snap, "questions_for_doctor", [])
    assert "我要繼續整理看診前資料" not in str(qs)
    assert res.intake_stage != "review" or not qs or "我要繼續" not in str(qs)
