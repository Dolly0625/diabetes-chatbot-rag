"""Small contract tests for the LINE patient entry boundary."""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _event(text: str) -> dict:
    return {
        "events": [
            {
                "type": "message",
                "webhookEventId": "entry-boundary-1",
                "replyToken": "reply-entry-boundary-1",
                "source": {"type": "user", "userId": "U-entry-boundary"},
                "message": {"type": "text", "id": "message-entry-boundary-1", "text": text},
            }
        ]
    }


def test_rich_menu_is_one_patient_entry_and_never_calls_line_api(monkeypatch, tmp_path):
    line_app = importlib.import_module("line_bot.app")
    calls: list[str] = []

    def forbidden_api():
        calls.append("called")
        raise AssertionError("Rich Menu definition must not call LINE API")

    monkeypatch.setattr(line_app, "_get_messaging_api", forbidden_api)
    response = TestClient(line_app.app).get(
        "/api/line/rich-menu",
        params={"patient_portal_url": "https://demo.example/demo/previsit"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["areas"]) == 1
    action = payload["areas"][0]["action"]
    assert action == {
        "type": "uri",
        "label": "開始看診前整理",
        "uri": "https://demo.example/demo/previsit",
    }
    assert "clinician" not in response.text.lower()
    assert calls == []


def test_rich_menu_rejects_per_user_token_url():
    line_app = importlib.import_module("line_bot.app")
    response = TestClient(line_app.app).get(
        "/api/line/rich-menu",
        params={"patient_portal_url": "https://demo.example/demo/previsit?token=one-user-secret"},
    )
    assert response.status_code == 422


def test_previsit_trigger_never_falls_back_to_legacy_line_intake(monkeypatch, tmp_path):
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "")
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", "")
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "true")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "entry-boundary-test-key-123456")
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "entry-boundary.sqlite3"))
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    monkeypatch.setattr(line_app, "_portal_available", lambda: False)

    replies: list[str] = []
    monkeypatch.setattr(line_app, "_reply_text", lambda _token, text, **_kwargs: replies.append(text) or True)
    orchestrator = line_app._get_conversation_orchestrator()
    assert orchestrator is not None

    def forbidden_intake(*_args, **_kwargs):
        raise AssertionError("previsit trigger must not enter legacy LINE intake")

    monkeypatch.setattr(orchestrator, "handle_text", forbidden_intake)
    response = TestClient(line_app.app).post("/callback", json=_event("我要準備看診"))

    assert response.status_code == 200
    assert replies == ["看診前整理網頁目前尚未設定，請稍後再試。"]


def test_natural_previsit_intent_is_recognized_without_catching_education():
    line_app = importlib.import_module("line_bot.app")
    # This is the exact text action already installed in the live Rich Menu.
    assert line_app._is_previsit_trigger_text("開始看診前整理") is True
    assert line_app._is_previsit_trigger_text("我下週要看醫生") is True
    assert line_app._is_previsit_trigger_text("我想要回診") is True
    assert line_app._is_previsit_trigger_text("糖尿病要怎麼看醫生的衛教") is False
    assert line_app._is_previsit_trigger_text("糖尿病飲食怎麼吃") is False


def test_demo_line_entry_always_opens_fresh_dedicated_room(monkeypatch):
    """The deployed menu's text action must not resurrect legacy LINE intake."""
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_WEB_ENABLED", "true")
    monkeypatch.setenv("PATIENT_INTAKE_BASE_URL", "https://demo.example")

    url, session_id = line_app._previsit_launch_url()

    assert url == "https://demo.example/demo/previsit"
    assert session_id is None
    assert "token=" not in url
