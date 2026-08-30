"""P0 data correctness regression tests — 8 red tests (先紅後綠)

涵蓋：
1. 完整八欄流程 stage 依序
2. questions_for_doctor 不可含內部控制句
3. 有點嚴重吧會追問
4. 多症狀保留
5. 自然修正過敏
6. fallback 同意前為空
7. 同意後才寫入且去重
8. 最終摘要僅含已確認資料
"""
from __future__ import annotations

from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.line_orchestration.orchestrator import HONEST_FALLBACK_TEXT
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.workflow.schemas import WorkflowResult
from tfda_context_gate.intake.summary import generate_previsit_summary
from tfda_context_gate.intake.schemas import PreVisitIntake

_KEY = "p0-data-correctness-key-at-least-16-chars!!"


def _new_orch(tmp_path, name="sessions.sqlite3"):
    repo = SQLiteProductSessionRepository(tmp_path / name)
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY)
    return orch, repo


# ── 1. 完整八欄流程 ──────────────────────────────────────────────

def test_p0_01_full_eight_field_stage_sequence(tmp_path):
    """Given 為自己整理 → When 依序填 8 欄 → Then stage 依序 stage1/stage2/stage3/review/submitted，絕不可在 severity 後直接跳 review"""
    orch, repo = _new_orch(tmp_path, "p0_01.sqlite3")

    r = orch.handle_text(event_id="p0-01-1", line_user_id="U-p0-01", text="為自己整理")
    assert r.intake_stage == "stage1", f"為自己整理後應為 stage1，實際為 {r.intake_stage}"

    # stage1 4 欄：分次填以觀察 stage 推進
    r = orch.handle_text(event_id="p0-01-2", line_user_id="U-p0-01", text="metformin")
    assert r.intake_stage == "stage1", f"填完 known_medications 後仍應在 stage1，實際 {r.intake_stage}"

    r = orch.handle_text(event_id="p0-01-3", line_user_id="U-p0-01", text="沒有過敏")
    sess = repo.get(r.session_id)
    assert sess.intake_snapshot.allergies == ["無"], f"過敏應為 ['無']，實際 {sess.intake_snapshot.allergies}"

    r = orch.handle_text(event_id="p0-01-4", line_user_id="U-p0-01", text="高血壓")
    assert r.intake_stage == "stage1", f"填完 chronic_conditions 後仍應在 stage1，實際 {r.intake_stage}"

    r = orch.handle_text(event_id="p0-01-5", line_user_id="U-p0-01", text="無家族史")
    assert r.intake_stage == "stage2", f"完成 stage1 四欄後應進入 stage2，實際 {r.intake_stage}，不可滯留 stage1"

    # stage2 3 欄
    r = orch.handle_text(event_id="p0-01-6", line_user_id="U-p0-01", text="三天前開始")
    assert r.intake_stage == "stage2", f"填完 symptom_onset 後應仍在 stage2，實際 {r.intake_stage}"

    r = orch.handle_text(event_id="p0-01-7", line_user_id="U-p0-01", text="常常口渴 晚上頻尿")
    assert r.intake_stage == "stage2", f"填完 symptom_description 後應仍在 stage2 等待 severity，實際 {r.intake_stage}"

    r = orch.handle_text(event_id="p0-01-8", line_user_id="U-p0-01", text="中度")
    # 關鍵斷言：severity 後應進 stage3，不可直接跳 review
    assert r.intake_stage == "stage3", f"填完 symptom_severity 後應進入 stage3（待問醫師問題），不可直接跳 review，實際 {r.intake_stage}"
    assert r.intake_stage != "review", "在 symptom_severity 後直接跳 review 為錯誤，應先經 stage3"

    # stage3
    r = orch.handle_text(event_id="p0-01-9", line_user_id="U-p0-01", text="想問醫師飲食要注意什麼")
    assert r.intake_stage == "review", f"填完 questions_for_doctor 後應進入 review，實際 {r.intake_stage}"

    r = orch.handle_text(event_id="p0-01-10", line_user_id="U-p0-01", text="確認完成")
    sess = repo.get(r.session_id)
    assert sess.status == "SUBMITTED", f"確認後應為 SUBMITTED，實際 {sess.status}"
    assert sess.intake_stage == "submitted", f"SUBMITTED 後 intake_stage 應為 submitted，實際 {sess.intake_stage}"
    # 8 欄皆有值
    snap = sess.intake_snapshot
    assert snap.known_medications, "known_medications 不可為空"
    assert snap.allergies, "allergies 不可為空"
    assert snap.chronic_conditions, "chronic_conditions 不可為空"
    assert snap.family_history, "family_history 不可為空"
    assert snap.symptom_onset, "symptom_onset 不可為空"
    assert snap.symptom_description, "symptom_description 不可為空"
    assert snap.symptom_severity, "symptom_severity 不可為空"
    assert snap.questions_for_doctor, "questions_for_doctor 不可為空"


def test_stage3_doctor_wording_cannot_strand_completed_intake(tmp_path):
    """「醫生」也必須收斂到 review，不能被衛教路由卡在 stage3。"""

    orch, repo = _new_orch(tmp_path, "p0_doctor_wording.sqlite3")
    turns = [
        "為自己整理",
        "metformin",
        "沒有過敏",
        "高血壓",
        "無家族史",
        "三天前開始",
        "常常口渴 晚上頻尿",
        "中度",
        "想問醫生飲食怎麼控制",
    ]
    result = None
    for index, text in enumerate(turns):
        result = orch.handle_text(
            event_id=f"doctor-wording-{index}",
            line_user_id="U-doctor-wording",
            text=text,
        )

    assert result is not None
    session = repo.get(result.session_id)
    assert session is not None
    assert session.intake_snapshot.questions_for_doctor == ["想問醫生飲食怎麼控制"]
    assert session.intake_stage == "review"
    assert session.status == "AWAITING_CONFIRMATION"
    assert result.status == "NEEDS_CONFIRMATION"
    assert "確認完成" in result.reply

    submitted = orch.handle_text(
        event_id="doctor-wording-confirm",
        line_user_id="U-doctor-wording",
        text="確認完成",
    )
    final_session = repo.get(submitted.session_id)
    assert final_session is not None
    assert final_session.status == "SUBMITTED"
    assert final_session.intake_stage == "submitted"


# ── 2. questions_for_doctor 不可含內部控制句 ────────────────

def test_p0_02_questions_for_doctor_excludes_internal_control_sentence(tmp_path):
    """Given 完整流程 → When 檢查 questions_for_doctor → Then 不可含 '我要繼續整理看診前資料'"""
    orch, repo = _new_orch(tmp_path, "p0_02.sqlite3")

    seq = [
        ("p0-02-1", "為自己整理"),
        ("p0-02-2", "metformin"),
        ("p0-02-3", "沒有過敏"),
        ("p0-02-4", "高血壓"),
        ("p0-02-5", "無家族史"),
        ("p0-02-6", "三天前開始"),
        ("p0-02-7", "常常口渴 晚上頻尿"),
        ("p0-02-8", "中度"),
        ("p0-02-9", "想問醫師飲食要注意什麼"),
        ("p0-02-10", "確認完成"),
    ]
    last = None
    for eid, txt in seq:
        last = orch.handle_text(event_id=eid, line_user_id="U-p0-02", text=txt)
    sess = repo.get(last.session_id)
    q_list = sess.intake_snapshot.questions_for_doctor
    assert all("我要繼續整理看診前資料" not in q for q in q_list), f"questions_for_doctor 不可含內部控制句 '我要繼續整理看診前資料'，實際為 {q_list}"
    # 也檢查 summary_text 同步不含
    summary = generate_previsit_summary(sess.intake_snapshot, request_id=sess.session_id)
    assert "我要繼續整理看診前資料" not in summary.summary_text, f"summary_text 不可含內部控制句，實際 {summary.summary_text}"


# ── 3. 有點嚴重吧會追問 ─────────────────────────────────────

def test_p0_03_vague_severity_triggers_clarification_and_preserves_raw(tmp_path):
    """Given 進入 severity 輪 → When 輸入 '有點嚴重吧' → Then severity 保持 None/待追問，reply 含追問文案(1-10/輕中重)，不存成重度，保留 raw"""
    orch, repo = _new_orch(tmp_path, "p0_03.sqlite3")

    orch.handle_text(event_id="p0-03-1", line_user_id="U-p0-03", text="為自己整理")
    orch.handle_text(event_id="p0-03-2", line_user_id="U-p0-03", text="metformin")
    orch.handle_text(event_id="p0-03-3", line_user_id="U-p0-03", text="沒有過敏")
    orch.handle_text(event_id="p0-03-4", line_user_id="U-p0-03", text="高血壓")
    orch.handle_text(event_id="p0-03-5", line_user_id="U-p0-03", text="無家族史")
    orch.handle_text(event_id="p0-03-6", line_user_id="U-p0-03", text="三天前開始")
    orch.handle_text(event_id="p0-03-7", line_user_id="U-p0-03", text="常常口渴")

    # When: 模糊 severity
    r = orch.handle_text(event_id="p0-03-8", line_user_id="U-p0-03", text="有點嚴重吧")
    sess = repo.get(r.session_id)

    # Then: 不應直接存成 重度/中度/輕度
    sev = sess.intake_snapshot.symptom_severity
    assert sev is None or sev == "" or sev == "待確認" or "有點嚴重吧" not in str(sev), f"模糊輸入 '有點嚴重吧' 不應直接存成具體程度，當前 severity={sev} 應為 None/待追問"

    # 若實作為 '待確認' 視為未追問完成，需仍要求追問；若為 None 則更明確
    # 嚴格要求：應保持待追問狀態，不直接映射為 重度
    assert sev not in ("重度", "輕度", "中度"), f"'有點嚴重吧' 不可直接映射為 '{sev}'，應觸發追問"

    # reply 需含追問文案（1-10 或 輕/中/重）
    reply = r.reply or ""
    has_scale = ("1" in reply and "10" in reply) or ("輕" in reply and "中" in reply and "重" in reply) or "1–10" in reply or "1-10" in reply
    assert has_scale, f"模糊 severity 應追問尺度文案(1-10 或 輕/中/重)，實際 reply={reply}"

    # 應仍停留在 stage2 等待具體 severity，而非跳 stage3/review
    assert sess.intake_stage == "stage2", f"模糊 severity 後應仍在 stage2 等待追問，不可推進，實際 {sess.intake_stage}"

    # raw provenance：對話歷史或 pending 應保留原始輸入痕跡
    # 檢查 recent_turns 含原始句或 intake 未被污染為重度
    recent_contents = [t.content for t in sess.conversation_context.recent_turns]
    assert any("有點嚴重吧" in c for c in recent_contents), f"應保留 raw provenance '有點嚴重吧' 於對話歷史，實際 recent={recent_contents[:3]}"


# ── 4. 多症狀保留 ───────────────────────────────────────────

def test_p0_04_multi_symptom_description_retains_all(tmp_path):
    """Given 進入 symptom_description → When 輸入 '我常常口渴，晚上一直起來尿尿' → Then 同時含 口渴 與 夜尿/尿尿"""
    orch, repo = _new_orch(tmp_path, "p0_04.sqlite3")

    orch.handle_text(event_id="p0-04-1", line_user_id="U-p0-04", text="為自己整理")
    orch.handle_text(event_id="p0-04-2", line_user_id="U-p0-04", text="metformin")
    orch.handle_text(event_id="p0-04-3", line_user_id="U-p0-04", text="沒有過敏")
    orch.handle_text(event_id="p0-04-4", line_user_id="U-p0-04", text="高血壓")
    orch.handle_text(event_id="p0-04-5", line_user_id="U-p0-04", text="無家族史")
    orch.handle_text(event_id="p0-04-6", line_user_id="U-p0-04", text="三天前開始")

    r = orch.handle_text(event_id="p0-04-7", line_user_id="U-p0-04", text="我常常口渴，晚上一直起來尿尿")
    sess = repo.get(r.session_id)
    desc = sess.intake_snapshot.symptom_description or ""
    assert "口渴" in desc, f"symptom_description 應保留 '口渴'，實際為 '{desc}'"
    assert ("夜尿" in desc or "尿尿" in desc or "頻尿" in desc or "起來尿" in desc), f"symptom_description 應保留夜尿/尿尿語意，不可只留口渴，實際為 '{desc}'"


# ── 5. 自然修正過敏 ─────────────────────────────────────────

def test_p0_05_natural_correction_of_allergy(tmp_path):
    """Given 先無過敏進 stage2 後修正為盤尼西林過敏 → Then allergies==[盤尼西林]，summary 含盤尼西林不含無，其他欄位保留，stage 正確"""
    orch, repo = _new_orch(tmp_path, "p0_05.sqlite3")

    orch.handle_text(event_id="p0-05-1", line_user_id="U-p0-05", text="為自己整理")
    orch.handle_text(event_id="p0-05-2", line_user_id="U-p0-05", text="metformin")
    orch.handle_text(event_id="p0-05-3", line_user_id="U-p0-05", text="對藥物沒有過敏")
    orch.handle_text(event_id="p0-05-4", line_user_id="U-p0-05", text="高血壓")
    r_after_stage1 = orch.handle_text(event_id="p0-05-5", line_user_id="U-p0-05", text="無家族史")
    assert r_after_stage1.intake_stage == "stage2", f"完成 stage1 後應為 stage2，實際 {r_after_stage1.intake_stage}"

    # 記錄修正前其他欄位
    sess_before = repo.get(r_after_stage1.session_id)
    meds_before = list(sess_before.intake_snapshot.known_medications)

    # When: 自然修正過敏（在 stage2 期間）
    r = orch.handle_text(event_id="p0-05-6", line_user_id="U-p0-05", text="不是，我剛剛說錯了，其實對盤尼西林過敏")
    sess = repo.get(r.session_id)

    assert sess.intake_snapshot.allergies == ["盤尼西林"], f"修正後 allergies 應為 ['盤尼西林']，實際 {sess.intake_snapshot.allergies}，且不應殘留 '無'"
    assert "盤尼西林" not in str(sess.intake_snapshot.known_medications), "修正不應污染 known_medications"

    # summary 含盤尼西林不含 無（指 allergies 部分為 無）
    summary = generate_previsit_summary(sess.intake_snapshot, request_id=sess.session_id)
    assert "盤尼西林" in summary.summary_text, f"summary_text 應含 '盤尼西林'，實際 {summary.summary_text}"
    # 若 allergies 為 ['盤尼西林']，summary 不應出現 '過敏史：無'
    assert "過敏史：無" not in summary.summary_text, f"summary 修正後不應含 '過敏史：無'，實際 {summary.summary_text}"

    # 其他已確認欄位保留
    assert sess.intake_snapshot.known_medications == meds_before, f"其他已確認欄位 known_medications 應保留 {meds_before}，實際 {sess.intake_snapshot.known_medications}"
    assert "高血壓" in sess.intake_snapshot.chronic_conditions, f"chronic_conditions 應保留高血壓，實際 {sess.intake_snapshot.chronic_conditions}"

    # stage 仍正確（應仍在 stage2 等待症狀資訊，或至少不在 submitted/review）
    assert sess.intake_stage in ("stage2", "stage3"), f"修正後 stage 應仍為 stage2/stage3 而非直接跳轉，實際 {sess.intake_stage}"


# ── 6. fallback 同意前為空 ────────────────────────────────

def test_p0_06_fallback_before_consent_questions_empty(tmp_path):
    """Given 觸發 HONEST_FALLBACK → When 未同意前 → Then questions_for_doctor 仍為空"""
    orch, repo = _new_orch(tmp_path, "p0_06.sqlite3")

    # 先建立一個 intake 會話以綁定 fallback 記錄對象
    orch.handle_text(event_id="p0-06-1", line_user_id="U-p0-06", text="為自己整理")
    sess_before = repo.get(orch.handle_text(event_id="p0-06-1b", line_user_id="U-p0-06", text="metformin").session_id)
    # 確保初始為空
    assert sess_before.intake_snapshot.questions_for_doctor == [], f"初始 questions_for_doctor 應為空，實際 {sess_before.intake_snapshot.questions_for_doctor}"

    original_text = "請問糖尿病藥物怎麼選比較好？"
    wf = WorkflowResult(
        request_id="p0-06-fallback-1",
        status="FALLBACK",
        final_response=HONEST_FALLBACK_TEXT,
        fallback_reason="B_INSUFFICIENT",
        a_result=None, query_expansion=None, rag_result=None, b_result=None, c_result=None, d_result=None,
        agent_action=None, agent_reason_code=None, question=None, current_query=original_text,
        execution_history=[], agent_steps=0, rewrite_count=0, clarification_count=0,
        termination_reason="B_INSUFFICIENT", intake_snapshot=None, intake_stage=None,
        previsit_summary=None, system_risk_classification=None, trace={"events": [], "evaluations": []},
    )

    # When: 觸發 HONEST_FALLBACK（經 push_formal 或 _maybe_record 機制）
    pushed = orch.push_formal_result(line_user_id="U-p0-06", event_id="p0-06-push-1", workflow=wf, original_text=original_text, push_sender=lambda uid, text: True)

    sess_after = repo.get(sess_before.session_id)
    assert sess_after is not None
    # Then: 在使用者同意前，questions_for_doctor 仍應為空
    assert sess_after.intake_snapshot.questions_for_doctor == [], f"觸發 HONEST_FALLBACK 後未同意前 questions_for_doctor 應仍為空，實際 {sess_after.intake_snapshot.questions_for_doctor}，不可自動寫入"


# ── 7. 同意後才寫入且去重 ───────────────────────────────

def test_p0_07_fallback_consent_appends_and_dedups(tmp_path):
    """Given 已觸發 HONEST_FALLBACK → When 使用者回覆 好/幫我記/記下來 → Then 才 append，且重複同意同一 original_text 不重複"""
    orch, repo = _new_orch(tmp_path, "p0_07.sqlite3")

    orch.handle_text(event_id="p0-07-1", line_user_id="U-p0-07", text="為自己整理")
    sess0 = repo.get(orch.handle_text(event_id="p0-07-1b", line_user_id="U-p0-07", text="metformin").session_id)
    original_text = "請問胰島素和口服藥差在哪？"
    wf = WorkflowResult(
        request_id="p0-07-fallback-1",
        status="FALLBACK",
        final_response=HONEST_FALLBACK_TEXT,
        fallback_reason="B_INSUFFICIENT",
        a_result=None, query_expansion=None, rag_result=None, b_result=None, c_result=None, d_result=None,
        agent_action=None, agent_reason_code=None, question=None, current_query=original_text,
        execution_history=[], agent_steps=0, rewrite_count=0, clarification_count=0,
        termination_reason="B_INSUFFICIENT", intake_snapshot=None, intake_stage=None,
        previsit_summary=None, system_risk_classification=None, trace={"events": [], "evaluations": []},
    )
    # 模擬系統已記錄 pending fallback（若實作需 pending 機制，則由 push 觸發後等待同意）
    # 先 push 但依新契約不應自動寫入，需等待同意；此處 push 僅為觸發記錄 pending 狀態
    orch.push_formal_result(line_user_id="U-p0-07", event_id="p0-07-push-1", workflow=wf, original_text=original_text, push_sender=lambda uid, text: True)
    sess_after_push = repo.get(sess0.session_id)
    # Then: 同意前仍應為空（與 p0_06 同契約），若自動寫入即為錯誤
    assert sess_after_push.intake_snapshot.questions_for_doctor == [], f"同意前 questions_for_doctor 應仍為空才等待使用者同意，實際 {sess_after_push.intake_snapshot.questions_for_doctor}，不可在 push 時自動寫入"

    # When: 使用者同意
    # 以 handle_text 送同意語，orchestrator 應將 pending original_text append 至 questions_for_doctor
    # 若實作為額外 consent API，也應透過 handle_text 的「好」觸發
    r1 = orch.handle_text(event_id="p0-07-2", line_user_id="U-p0-07", text="好")
    sess1 = repo.get(r1.session_id)
    # 亦接受 "幫我記" / "記下來" 作為同意
    # 為兼容不同實作，嘗試第二輪同意用不同同意詞
    r2 = orch.handle_text(event_id="p0-07-3", line_user_id="U-p0-07", text="幫我記")
    sess2 = repo.get(r2.session_id)
    # When: 重複同意同一 original_text
    r3 = orch.handle_text(event_id="p0-07-4", line_user_id="U-p0-07", text="記下來")
    sess3 = repo.get(r3.session_id)

    q = sess3.intake_snapshot.questions_for_doctor if sess3 else []
    # Then: 應恰好一筆 original_text，不重複
    assert original_text in q, f"同意後 questions_for_doctor 應含 original_text '{original_text}'，實際 {q}"
    assert q.count(original_text) == 1, f"重複同意同一 original_text 不應重複寫入，應僅一筆，實際 {q}"

    # 額外：至少一次同意後長度為 1（避免 '好' 被誤當成 intake 欄位值）
    assert len([x for x in q if x == original_text]) == 1, f"去重後應僅一筆 original_text，實際 {q}"
    assert "好" not in q or original_text in q, f"'好' 不應被當成問題內容寫入 questions_for_doctor，實際 {q}"


# ── 8. 最終摘要僅已確認資料 ──────────────────────────────

def test_p0_08_final_summary_contains_only_confirmed_data(tmp_path):
    """Given SUBMITTED → When generate_previsit_summary → Then summary_text 僅含已確認欄位，不含 sentinel"""
    # sentinel 包含 "不清楚（待看診確認）" 與 "待確認"
    intake = PreVisitIntake(
        known_medications=["不清楚（待看診確認）"],
        allergies=["無"],
        chronic_conditions=["高血壓"],
        family_history=[],
        symptom_onset="待確認",
        symptom_description="常常口渴",
        symptom_severity="待確認",
        questions_for_doctor=[],
    )
    # 模擬已 SUBMIT 的 intake 快照（即使有 sentinel，summary 也不應出現）
    summary = generate_previsit_summary(intake, request_id="p0-08-req")

    assert "不清楚（待看診確認）" not in summary.summary_text, f"summary_text 不可含 sentinel '不清楚（待看診確認）'，實際 {summary.summary_text}"
    assert summary.summary_text.count("待確認") == 0 or "症狀起始：待確認" not in summary.summary_text, f"summary_text 不可含 sentinel '待確認' 對應欄位，實際 {summary.summary_text}"
    # 已確認欄位應仍在
    assert "高血壓" in summary.summary_text, f"已確認的慢性病 '高血壓' 應在 summary_text，實際 {summary.summary_text}"
    assert "口渴" in summary.summary_text, f"已確認的症狀描述 '口渴' 應在 summary_text，實際 {summary.summary_text}"
    # sentinel 欄位不應出現在 provided_fields 的 summary 實作若仍視為 provided 則失敗
    # 更嚴格：provided_fields 不應含 sentinel 欄位，或 summary_text 不含其值
    for sentinel_field in ["known_medications", "symptom_onset", "symptom_severity"]:
        val = getattr(intake, sentinel_field)
        if val and any(s in str(val) for s in ["不清楚", "待確認"]):
            assert str(val) not in summary.summary_text, f"sentinel 欄位 {sentinel_field}={val} 不可出現在 summary_text，實際 {summary.summary_text}"


# ── 9. review 階段指代修正 ──────────────────────────────────

def test_p0_09_review_correction_refers_allergy(tmp_path):
    """Given 走到 review → When 輸入『喔不對，過敏那邊要改成阿斯匹靈』→ Then allergies==['阿斯匹靈'] 且 stage 正確，不可殘留『無』"""
    orch, repo = _new_orch(tmp_path, "p0_09.sqlite3")
    seq = [
        ("p0-09-1", "為自己整理"),
        ("p0-09-2", "metformin"),
        ("p0-09-3", "沒有過敏"),
        ("p0-09-4", "高血壓"),
        ("p0-09-5", "無家族史"),
        ("p0-09-6", "三天前開始"),
        ("p0-09-7", "常常口渴 晚上頻尿"),
        ("p0-09-8", "中度"),
        ("p0-09-9", "想問醫師飲食要注意什麼"),
    ]
    last = None
    for eid, txt in seq:
        last = orch.handle_text(event_id=eid, line_user_id="U-p0-09", text=txt)
    assert last.intake_stage == "review", f"應在 review，實際 {last.intake_stage}"
    r = orch.handle_text(event_id="p0-09-10", line_user_id="U-p0-09", text="喔不對，過敏那邊要改成阿斯匹靈")
    sess = repo.get(r.session_id)
    assert sess.intake_snapshot.allergies == ["阿斯匹靈"], f"review 指代修正後 allergies 應為 ['阿斯匹靈']，實際 {sess.intake_snapshot.allergies}"
    assert "過敏史：無" not in generate_previsit_summary(sess.intake_snapshot, request_id=sess.session_id).summary_text


# ── 10. stage3 否定不入 questions，append 句式 ───────────────

def test_p0_10_stage3_negative_not_stored_and_append(tmp_path):
    """Given stage3 → When 輸入『沒有別的了』→ Then questions_for_doctor 仍空；隨後『還要幫我記一個：糖尿病人可以吃水果嗎』應 append"""
    orch, repo = _new_orch(tmp_path, "p0_10.sqlite3")
    for eid, txt in [
        ("p0-10-1", "為自己整理"),
        ("p0-10-2", "metformin"),
        ("p0-10-3", "沒有過敏"),
        ("p0-10-4", "高血壓"),
        ("p0-10-5", "無家族史"),
        ("p0-10-6", "三天前開始"),
        ("p0-10-7", "常常口渴"),
        ("p0-10-8", "中度"),
    ]:
        orch.handle_text(event_id=eid, line_user_id="U-p0-10", text=txt)
    sess = repo.get(orch.handle_text(event_id="p0-10-8a", line_user_id="U-p0-10", text="中度").session_id)  # ensure stage3
    # 實際 stage3 check
    r = orch.handle_text(event_id="p0-10-9", line_user_id="U-p0-10", text="沒有別的了")
    sess = repo.get(r.session_id)
    assert sess.intake_snapshot.questions_for_doctor == [], f"否定句『沒有別的了』不可寫入 questions_for_doctor，實際 {sess.intake_snapshot.questions_for_doctor}"
    # append 句式
    r2 = orch.handle_text(event_id="p0-10-10", line_user_id="U-p0-10", text="還要幫我記一個：糖尿病人可以吃水果嗎")
    sess2 = repo.get(r2.session_id)
    assert any("水果" in q for q in sess2.intake_snapshot.questions_for_doctor), f"append 句式應寫入水果問題，實際 {sess2.intake_snapshot.questions_for_doctor}"


# ── 11. severity 數字標準化 ─────────────────────────────────

def test_p0_11_severity_numeric_standardized(tmp_path):
    """Given hedge 追問後 → When 輸入『6分』→ Then symptom_severity 標準化為『中度』(4-6中度)，provenance 保留 6分"""
    orch, repo = _new_orch(tmp_path, "p0_11.sqlite3")
    orch.handle_text(event_id="p0-11-1", line_user_id="U-p0-11", text="為自己整理")
    orch.handle_text(event_id="p0-11-2", line_user_id="U-p0-11", text="metformin")
    orch.handle_text(event_id="p0-11-3", line_user_id="U-p0-11", text="沒有過敏")
    orch.handle_text(event_id="p0-11-4", line_user_id="U-p0-11", text="高血壓")
    orch.handle_text(event_id="p0-11-5", line_user_id="U-p0-11", text="無家族史")
    orch.handle_text(event_id="p0-11-6", line_user_id="U-p0-11", text="三天前開始")
    orch.handle_text(event_id="p0-11-7", line_user_id="U-p0-11", text="常常口渴")
    r = orch.handle_text(event_id="p0-11-8", line_user_id="U-p0-11", text="有點嚴重吧")
    sess = repo.get(r.session_id)
    assert sess.intake_stage == "stage2"
    r2 = orch.handle_text(event_id="p0-11-9", line_user_id="U-p0-11", text="6分")
    sess2 = repo.get(r2.session_id)
    assert sess2.intake_snapshot.symptom_severity == "中度", f"6分 應標準化為 中度，實際 {sess2.intake_snapshot.symptom_severity}"
    # provenance
    recent = [t.content for t in sess2.conversation_context.recent_turns]
    assert any("6分" in c for c in recent), f"應保留 provenance 6分，recent={recent[:4]}"


# ── 12. 第二輪泛化：stage3 收斂 + review 修正 + 症狀防污染 + 冒號萃取 ─

def test_p0_12_round2_stage3_review_and_pollution(tmp_path):
    """重現第二輪泛化序列，4 項一次驗：
    h1-7 基礎→h8 hedge→h9 6分→中度→h10 沒有別的了→進 review→h11 有，血糖多少正常→append→h12 沒了→仍 review→h13 review 指令不存→h14 喔不對過敏改阿斯匹靈→仍 review→h15 還要幫我記一個：... →冒號後萃取；期間 symptom_description 不被問題污染"""
    orch, repo = _new_orch(tmp_path, "p0_12.sqlite3")
    seq = [
        ("h1", "為自己整理"),
        ("h2", "metformin"),
        ("h3", "沒有過敏"),
        ("h4", "高血壓"),
        ("h5", "無家族史"),
        ("h6", "三天前開始"),
        ("h7", "很容易餓，手會抖"),
        ("h8", "好像滿嚴重的欸"),
        ("h9", "6分"),
        ("h10", "沒有別的了"),
        ("h11", "有，血糖多少正常"),
        ("h12", "沒了"),
        ("h13", "review"),
    ]
    last = None
    for eid, txt in seq:
        last = orch.handle_text(event_id=eid, line_user_id="U-p0-12", text=txt)
    sess = repo.get(last.session_id)
    # 1) stage3 否定後應收斂至 review，且 review 指令不應被當成問題存入
    assert sess.intake_stage == "review", f"h13 後應為 review，實際 {sess.intake_stage}, questions={sess.intake_snapshot.questions_for_doctor}"
    assert "review" not in [q.lower() for q in sess.intake_snapshot.questions_for_doctor], f"'review' 不可被當成問題存入，實際 {sess.intake_snapshot.questions_for_doctor}"
    assert sess.intake_snapshot.questions_for_doctor == [] or "沒有別的了" not in sess.intake_snapshot.questions_for_doctor, f"否定句不可入 questions，實際 {sess.intake_snapshot.questions_for_doctor}"
    # h11 應被 append 且前綴已剝除
    assert any(q == "血糖多少正常" for q in sess.intake_snapshot.questions_for_doctor), f"h11 應剝除前綴存為『血糖多少正常』，實際 {sess.intake_snapshot.questions_for_doctor}"
    assert not any(q.startswith("有，") for q in sess.intake_snapshot.questions_for_doctor), f"前綴 有， 應已剝除，實際 {sess.intake_snapshot.questions_for_doctor}"
    assert sess.pending_action is None, f"review 後 pending_action 應為 None (stale 已清)，實際 {sess.pending_action}"
    # 2) review 修正後 stage 必須維持 review
    r14 = orch.handle_text(event_id="h14", line_user_id="U-p0-12", text="喔不對，過敏那邊要改成阿斯匹靈")
    sess14 = repo.get(r14.session_id)
    assert sess14.intake_snapshot.allergies == ["阿斯匹靈"], f"review 修正後 allergies 應為 ['阿斯匹靈']，實際 {sess14.intake_snapshot.allergies}"
    assert sess14.intake_stage == "review", f"review 修正後 stage 必須維持 review，實際 {sess14.intake_stage}"
    # 3) symptom_description 不被問題污染
    assert sess14.intake_snapshot.symptom_description == "很容易餓，手會抖", f"symptom_description 應保留『很容易餓，手會抖』不被血糖問題污染，實際 {sess14.intake_snapshot.symptom_description}"
    # 4) 冒號後萃取統一
    r15 = orch.handle_text(event_id="h15", line_user_id="U-p0-12", text="還要幫我記一個：糖尿病人可以吃水果嗎")
    sess15 = repo.get(r15.session_id)
    assert any(q == "糖尿病人可以吃水果嗎" for q in sess15.intake_snapshot.questions_for_doctor), f"應萃取冒號後『糖尿病人可以吃水果嗎』不含前綴，實際 {sess15.intake_snapshot.questions_for_doctor}"
    assert not any("還要幫我記一個" in q for q in sess15.intake_snapshot.questions_for_doctor), f"前綴不可殘留，實際 {sess15.intake_snapshot.questions_for_doctor}"
    # severity 已標準化且 stale 已清
    assert sess15.intake_snapshot.symptom_severity == "中度"
    assert sess15.pending_action is None
    # prefix 已剝除的驗證已在上方完成
