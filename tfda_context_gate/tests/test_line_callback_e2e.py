from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tfda_context_gate.conversation.interpreter import DeterministicConversationInterpreter
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository


SECRET = "hermetic-line-signature-secret-123456"
IDENTITY_KEY = "hermetic-line-identity-key-123456"


@pytest.fixture
def line_test_app(tmp_path: Path, monkeypatch):
    import line_bot.app as line_app

    monkeypatch.setenv("LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "false")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", IDENTITY_KEY)
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "line-e2e.sqlite3"))
    monkeypatch.setenv("LINE_USE_FORMAL", "false")
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setattr(line_app, "LINE_CHANNEL_ACCESS_TOKEN", "")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    monkeypatch.setattr(line_app, "_reply_text", lambda *_args, **_kwargs: True)
    line_app._pushed_events.clear()
    line_app._pushing_events.clear()
    line_app._marker_pending_events.clear()
    line_app._marker_retrying_events.clear()
    line_app._async_jobs.clear()
    line_app._text_dedup.clear()
    yield line_app
    line_app._conversation_orchestrator = None
    line_app._text_dedup.clear()


def _signed_request(client: TestClient, payload: dict, *, secret: str = SECRET, signature: str | None = None):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = signature or base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return client.post(
        "/callback",
        content=body,
        headers={"Content-Type": "application/json", "X-Line-Signature": sig},
    )


def _text_event(event_id: str, text: str, *, user_id: str = "U-hermetic") -> dict:
    return {
        "type": "message",
        "webhookEventId": event_id,
        "replyToken": f"reply-{event_id}",
        "source": {"type": "user", "userId": user_id},
        "message": {"type": "text", "id": f"message-{event_id}", "text": text},
    }


def test_callback_signature_valid_and_invalid_are_hermetic(line_test_app):
    client = TestClient(line_test_app.app)
    payload = {"events": []}

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    valid_signature = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    missing = client.post("/callback", content=body, headers={"Content-Type": "application/json"})
    invalid = client.post(
        "/callback",
        content=body,
        headers={"Content-Type": "application/json", "X-Line-Signature": "invalid"},
    )
    valid = client.post(
        "/callback",
        content=body,
        headers={"Content-Type": "application/json", "X-Line-Signature": valid_signature},
    )

    assert missing.status_code == invalid.status_code == 400
    assert valid.status_code == 200
    assert valid.json() == {"status": "ok", "events": 0}


def test_callback_event_replay_does_not_duplicate_product_session_turns(line_test_app):
    replies: list[str] = []
    line_test_app._reply_text = lambda _token, text, **_kwargs: replies.append(text) or True
    client = TestClient(line_test_app.app)
    first_payload = {"events": [_text_event("replay-e2e", "為自己整理")]}
    replay_payload = {"events": [_text_event("replay-e2e", "完全不同的重播內容")]}

    first = _signed_request(client, first_payload)
    orchestrator = line_test_app._get_conversation_orchestrator()
    session_after_first = orchestrator.session_for_user("U-hermetic")
    assert first.status_code == 200
    assert session_after_first is not None
    turns_after_first = len(session_after_first.conversation_context.recent_turns)

    replay = _signed_request(client, replay_payload)
    session_after_replay = orchestrator.session_for_user("U-hermetic")
    assert replay.status_code == 200
    assert session_after_replay is not None
    assert len(session_after_replay.conversation_context.recent_turns) == turns_after_first
    assert len(replies) == 2
    assert replies[0] == replies[1]


def test_callback_formal_turn_admits_one_async_job_without_line_transport(line_test_app, monkeypatch):
    scheduled: list[tuple[str, str, str]] = []
    line_test_app._reply_text = lambda *_args, **_kwargs: True
    monkeypatch.setenv("LINE_USE_FORMAL", "true")
    monkeypatch.setattr(
        line_test_app,
        "_schedule_formal_push",
        lambda _orch, user_id, event_id, text: scheduled.append((user_id, event_id, text)),
    )
    client = TestClient(line_test_app.app)
    payload = {"events": [_text_event("async-admit-e2e", "糖尿病一天可以吃幾份水果？")]}

    response = _signed_request(client, payload)

    assert response.status_code == 200
    assert scheduled == [("U-hermetic", "async-admit-e2e", "糖尿病一天可以吃幾份水果？")]
    orchestrator = line_test_app._get_conversation_orchestrator()
    session = orchestrator.session_for_user("U-hermetic")
    assert session is not None
    assert len(session.conversation_context.recent_turns) == 2


def test_async_formal_timeout_has_no_late_push_or_session_write(tmp_path: Path, monkeypatch):
    import line_bot.app as line_app

    repo = SQLiteProductSessionRepository(tmp_path / "async-timeout-e2e.sqlite3")

    def slow_runner(request, **_kwargs):
        time.sleep(0.12)
        raise AssertionError("late workflow must never become a push")

    orchestrator = ConversationOrchestrator(
        repo,
        identity_hash_key=IDENTITY_KEY,
        workflow_runner=slow_runner,
        interpreter=DeterministicConversationInterpreter(),
        use_formal=True,
        async_formal_timeout_s=0.03,
    )
    orchestrator._load_or_create("U-timeout-e2e")
    pushes: list[str] = []

    class FakeApi:
        def push_message(self, request, **_kwargs):
            pushes.append(request.messages[0].text)

    monkeypatch.setattr(line_app, "_get_messaging_api", lambda: FakeApi())
    monkeypatch.setattr(line_app, "ASYNC_FORMAL_TIMEOUT_S", 0.03)
    line_app._pushed_events.clear()
    line_app._pushing_events.clear()
    line_app._text_dedup.clear()

    line_app._schedule_formal_push(
        orchestrator,
        "U-timeout-e2e",
        "timeout-e2e-event",
        "請說明糖尿病的一般飲食原則。",
    )
    time.sleep(0.25)

    session = orchestrator.session_for_user("U-timeout-e2e")
    # Timeout may emit the existing deterministic honest fallback when the
    # async worker is admitted in time.  If the bounded admission deadline
    # expires before a worker starts, fail-closed means no push is also valid.
    # In either case, the late workflow answer must never leak.
    assert len(pushes) <= 1
    assert all("LATE" not in text for text in pushes)
    assert session is not None
    assert all("late workflow" not in str(turn) for turn in session.conversation_context.recent_turns)
    # The event is reserved as delivered after the one safe fallback push;
    # replay cannot transport a second message in this process.
    if pushes:
        assert line_app._push_text("U-timeout-e2e", "duplicate", event_id="timeout-e2e-event") is False
        assert len(pushes) == 1
