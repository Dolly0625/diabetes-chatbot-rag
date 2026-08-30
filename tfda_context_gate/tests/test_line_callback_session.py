from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _setup(tmp_path, monkeypatch):
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "callback-test-key-123456789")
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "callback.sqlite3"))
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "")
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "true")
    monkeypatch.setenv("LINE_DEMO_MODE", "true")
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", "")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    replies: list[str] = []
    def capture_reply(_token, text, **_kwargs):
        replies.append(text)
        return True
    monkeypatch.setattr(line_app, "_reply_text", capture_reply)
    return line_app, TestClient(line_app.app), replies


def _text_event(event_id: str, text: str, user_id: str = "U-callback"):
    return {"events": [{
        "type": "message", "webhookEventId": event_id, "replyToken": f"reply-{event_id}",
        "source": {"type": "user", "userId": user_id},
        "message": {"type": "text", "id": f"message-{event_id}", "text": text},
    }]}


def test_callback_uses_persistent_orchestrator_and_replays_duplicate(tmp_path, monkeypatch):
    line_app, client, replies = _setup(tmp_path, monkeypatch)

    first = client.post("/callback", json=_text_event("evt-1", "我要準備看診"))
    duplicate = client.post("/callback", json=_text_event("evt-1", "不同內容"))

    assert first.status_code == duplicate.status_code == 200
    assert replies == [
        "我是 AI 看診前整理助理，只協助衛教與資料整理，不做診斷，也不是緊急醫療服務。Demo session 最多保存 7 天；確認前不會分享給醫護。這份資料是為誰整理？請選擇「為自己整理」或「代家人整理」。",
        "我是 AI 看診前整理助理，只協助衛教與資料整理，不做診斷，也不是緊急醫療服務。Demo session 最多保存 7 天；確認前不會分享給醫護。這份資料是為誰整理？請選擇「為自己整理」或「代家人整理」。",
    ]
    session = line_app._get_conversation_orchestrator().session_for_user("U-callback")
    assert session is not None and len(session.conversation_context.recent_turns) == 2


def test_callback_image_requires_role_before_ocr(tmp_path, monkeypatch):
    line_app, client, replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(line_app, "_download_image_content", lambda _message_id: b"raw-image")
    payload = {"events": [{
        "type": "message", "webhookEventId": "img-event", "replyToken": "reply-img",
        "source": {"type": "user", "userId": "U-image-new"},
        "message": {"type": "image", "id": "image-001"},
    }]}

    response = client.post("/callback", json=payload)

    assert response.status_code == 200
    assert replies == ["請先選擇「為自己整理」或「代家人整理」，再上傳藥袋。"]


def test_quick_reply_actions_match_role_and_review_states():
    line_app = importlib.import_module("line_bot.app")
    role_actions = line_app._quick_actions_for_status(
        "NEEDS_AUTHORIZATION", "請先選擇「為自己整理」或「代家人整理」"
    )
    consent_actions = line_app._quick_actions_for_status(
        "NEEDS_AUTHORIZATION", "是否已取得家人同意"
    )
    review_actions = line_app._quick_actions_for_status("NEEDS_CONFIRMATION", "請確認")

    assert [item["text"] for item in role_actions] == ["為自己整理", "代家人整理"]
    assert [item["text"] for item in consent_actions] == ["已取得同意"]
    assert [item["text"] for item in review_actions] == ["確認完成", "修改看診資料"]


def test_quick_reply_actions_support_unknown_skip_pause_and_resume():
    line_app = importlib.import_module("line_bot.app")
    medication_actions = line_app._quick_actions_for_status(
        "NEEDS_CLARIFICATION", "第 1/8 題｜目前有固定吃藥嗎？"
    )
    symptom_actions = line_app._quick_actions_for_status(
        "NEEDS_CLARIFICATION", "第 6/8 題｜目前最主要的症狀是什麼？"
    )
    resume_actions = line_app._quick_actions_for_status("SIDE_ANSWER", "衛教回答")

    assert [item["text"] for item in medication_actions] == ["目前沒有用藥", "不清楚", "暫停整理"]
    assert [item["text"] for item in symptom_actions] == ["不清楚", "跳過", "暫停整理"]
    assert [item["text"] for item in resume_actions] == ["繼續整理"]


def test_active_intake_medication_answer_never_bypasses_to_async_rag(tmp_path, monkeypatch):
    """Regression for the real LINE phrase that was mistaken for education."""

    line_app, _client, _replies = _setup(tmp_path, monkeypatch)
    orchestrator = line_app._get_conversation_orchestrator()
    assert orchestrator is not None
    orchestrator.use_formal = True
    orchestrator.handle_text(
        event_id="intake-routing-start",
        line_user_id="U-intake-routing",
        text="我要準備看診",
    )
    orchestrator.handle_text(
        event_id="intake-routing-self",
        line_user_id="U-intake-routing",
        text="為自己整理",
    )

    assert line_app._should_use_async_formal("有固定吃藥 沒有打胰島素", None) is True
    assert line_app._should_schedule_formal_push(
        orchestrator,
        "U-intake-routing",
        "有固定吃藥 沒有打胰島素",
    ) is False


def test_callback_to_patient_share_to_clinician_read_only_end_to_end(tmp_path, monkeypatch):
    line_app, client, _replies = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("LINE_DEMO_ALLOW_ID_HEADERS", "true")
    monkeypatch.setenv("DEMO_CLINICIAN_IDS", "doctor-e2e")
    messages = [
        "我要準備看診",
        "為自己整理",
        "吃 metformin，無過敏，有高血壓，家族無糖尿病",
        "三天前開始，早晨血糖偏高，程度4/10",
        "我想問醫師飲食要注意什麼？",
        "確認完成",
    ]
    for index, value in enumerate(messages):
        response = client.post("/callback", json=_text_event(f"e2e-{index}", value, "U-e2e"))
        assert response.status_code == 200

    patient = client.get("/api/patient/session", headers={"X-Line-User-Id": "U-e2e"})
    assert patient.status_code == 200
    assert patient.json()["status"] == "SUBMITTED"
    issued = client.post(
        f"/api/patient/sessions/{patient.json()['session_id']}/share",
        headers={"X-Line-User-Id": "U-e2e"},
        json={"allowed_clinician_id": "doctor-e2e"},
    )
    viewed = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "doctor-e2e"},
        json={"token": issued.json()["token"]},
    )

    assert issued.status_code == viewed.status_code == 200
    assert viewed.json()["intake_snapshot"]["known_medications"] == ["metformin"]
    assert "principal_id_hash" not in viewed.text
    assert b"U-e2e" not in (tmp_path / "callback.sqlite3").read_bytes()
