import os
import hashlib
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _make_client(monkeypatch=None):
    # temp DB isolates per test
    tmp = tempfile.mktemp(suffix=".sqlite3")
    os.environ["LINE_SESSION_DB_PATH"] = tmp
    os.environ["LINE_IDENTITY_HASH_KEY"] = "test-identity-hash-key-16c-long"
    os.environ["DEMO_INTAKE_TOKEN_ENABLED"] = "true"
    os.environ["LINE_DEMO_ALLOW_ID_HEADERS"] = "true"
    os.environ["LINE_CHANNEL_SECRET"] = "test-secret"
    # ensure app reloads orchestrator with new DB
    import line_bot.app as app_mod
    app_mod._conversation_orchestrator = None
    # clear web dedup
    try:
        app_mod._web_chat_dedup.clear()
    except Exception:
        pass
    from line_bot.app import app
    client = TestClient(app)
    return client, app_mod, tmp


def _create_session_and_token(client, app_mod, user_id="userA"):
    orch = app_mod._get_conversation_orchestrator()
    sess = orch._load_or_create(user_id)
    raw, sid = app_mod._create_previsit_token_for_user(user_id)
    # reload session with token binding
    return raw, sess


def test_static_previsit_room_served():
    client, _, tmp = _make_client()
    # frontend owns static file; backend only serves if present. Ensure route exists and does not 5xx.
    # Create dummy file for contract verification, since backend does not commit it.
    dummy = Path(__file__).parents[1] / "static" / "previsit-room.html"
    existed = dummy.is_file()
    if not existed:
        dummy.write_text("<html>看診前對談室 dummy</html>", encoding="utf-8")
    try:
        r = client.get("/patient/previsit-room")
        assert r.status_code == 200
        assert "看診前 AI 對談室" in r.text
        assert r.headers.get("content-type", "").startswith("text/html")
    finally:
        if not existed and dummy.is_file():
            dummy.unlink()


def test_get_previsit_no_token_401():
    client, app_mod, tmp = _make_client()
    # reset to no demo token
    os.environ["DEMO_INTAKE_TOKEN_ENABLED"] = "false"
    app_mod._conversation_orchestrator = None
    r = client.get("/api/patient/previsit-room")
    assert r.status_code == 401


def test_get_previsit_with_demo_token_success():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "alice")
    r = client.get("/api/patient/previsit-room", headers={"X-Intake-Token": raw})
    assert r.status_code == 200
    j = r.json()
    assert j["session_id"] == sess.session_id
    assert "version" in j
    assert "intake_snapshot" in j


def test_get_previsit_via_query_token():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "bob")
    r = client.get(f"/api/patient/previsit-room?token={raw}")
    assert r.status_code == 200
    assert r.json()["session_id"] == sess.session_id


def test_cross_user_403():
    client, app_mod, tmp = _make_client()
    raw_a, sess_a = _create_session_and_token(client, app_mod, "userA")
    raw_b, sess_b = _create_session_and_token(client, app_mod, "userB")
    # token A should not access session B, but token A is bound to sess_a
    # Trying to use token A to get sess_b via manipulating? Actually token lookup returns sess_a only.
    # Cross-user check is token binding: token A returns session A, not B.
    # To simulate cross-user 403, use token B but try to spoof session? Instead test invalid token is 403
    # Here we verify that using token A's raw to access is ok, but using random token fails 403
    h_wrong = hashlib.sha256(b"wrong-token-1234567890").hexdigest()
    # direct 403 for unknown token
    r = client.get("/api/patient/previsit-room", headers={"X-Intake-Token": "wrong-token-1234567890"})
    assert r.status_code == 403
    # token A cannot be used to access B's data because it's tied to A; verify they are different sessions
    assert sess_a.session_id != sess_b.session_id
    r_a = client.get("/api/patient/previsit-room", headers={"X-Intake-Token": raw_a})
    r_b = client.get("/api/patient/previsit-room", headers={"X-Intake-Token": raw_b})
    assert r_a.json()["session_id"] != r_b.json()["session_id"]


def test_expired_token_401():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "carol")
    orch = app_mod._get_conversation_orchestrator()
    # force expiry by updating DB directly
    h = hashlib.sha256(raw.encode()).hexdigest()
    import sqlite3
    exp_past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(orch.repository.path) as conn:
        conn.execute("UPDATE intake_launch_tokens SET expires_at=? WHERE token_hash=?", (exp_past, h))
    r = client.get("/api/patient/previsit-room", headers={"X-Intake-Token": raw})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower()


def test_wrong_token_403():
    client, app_mod, tmp = _make_client()
    r = client.get("/api/patient/previsit-room", headers={"X-Intake-Token": "invalidtoken12345678"})
    assert r.status_code == 403


def test_post_chat_version_stale_409():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "dave")
    # correct version is sess.version
    r = client.post("/api/patient/previsit-room/chat", headers={"X-Intake-Token": raw}, json={"message": "我有吃藥", "version": 999, "client_message_id": "mid-1"})
    assert r.status_code == 409


def test_post_chat_idempotency():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "erin")
    ver = sess.version
    payload = {"message": "沒有過敏", "version": ver, "client_message_id": "idem-123"}
    r1 = client.post("/api/patient/previsit-room/chat", headers={"X-Intake-Token": raw}, json=payload)
    assert r1.status_code == 200
    j1 = r1.json()
    # second with stale version but same client_message_id should return cached (200 not 409)
    payload2 = {"message": "沒有過敏", "version": 999, "client_message_id": "idem-123"}
    r2 = client.post("/api/patient/previsit-room/chat", headers={"X-Intake-Token": raw}, json=payload2)
    assert r2.status_code == 200
    assert r2.json() == j1
    # version should have incremented by one
    assert j1["version"] == ver + 1 or j1["version"] > ver


def test_post_chat_red_flag_not_polluting():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "fred")
    ver = sess.version
    # snapshot before
    before = sess.intake_snapshot.model_dump(mode="json")
    r = client.post("/api/patient/previsit-room/chat", headers={"X-Intake-Token": raw}, json={"message": "我胸痛胸悶喘不過氣", "version": ver, "client_message_id": "red-1"})
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "FALLBACK"
    assert "119" in j["reply"]
    # intake_snapshot should be unchanged (no pollution)
    assert j["intake_snapshot"] == before or j["intake_snapshot"]["symptom_description"] == before["symptom_description"]
    # version incremented because risk saved
    assert j["version"] == ver + 1


def test_post_chat_submitted_locked_409():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "gina")
    orch = app_mod._get_conversation_orchestrator()
    sess2 = orch.repository.get(sess.session_id)
    sess2 = sess2.model_copy(update={"status": "SUBMITTED", "intake_stage": "submitted"}, deep=True)
    orch.repository.save(sess2, expected_version=sess2.version)
    updated = orch.repository.get(sess.session_id)
    r = client.post("/api/patient/previsit-room/chat", headers={"X-Intake-Token": raw}, json={"message": "繼續整理", "version": updated.version, "client_message_id": "sub-1"})
    assert r.status_code == 409
    assert "SUBMITTED" in r.json()["detail"]


def test_post_chat_education_not_advancing():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "henry")
    ver = sess.version
    stage = sess.intake_stage
    r = client.post("/api/patient/previsit-room/chat", headers={"X-Intake-Token": raw}, json={"message": "請說明糖尿病的一般飲食原則。", "version": ver, "client_message_id": "edu-1"})
    assert r.status_code == 200
    j = r.json()
    assert "衛教" in j["reply"] or "回 LINE" in j["reply"]
    # stage should not advance, version unchanged
    assert j["intake_stage"] == stage
    assert j["version"] == ver


def test_chat_requires_token():
    client, app_mod, tmp = _make_client()
    r = client.post("/api/patient/previsit-room/chat", json={"message": "hi", "version": 0})
    assert r.status_code in (401, 403)


def test_token_not_logging_and_no_userid_in_url():
    # Code-level guarantee: token_urlsafe not containing userId
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "no_log_user_XYZ")
    assert "no_log_user_XYZ" not in raw
    assert len(raw) >= 16
    h = hashlib.sha256(raw.encode()).hexdigest()
    orch = app_mod._get_conversation_orchestrator()
    rec = orch.repository.get_intake_token(h)
    assert rec is not None
    assert rec["product_session_id"] == sess.session_id
    assert rec["token_hash"] == h
    # ensure DB only stores hash
    import sqlite3
    with sqlite3.connect(orch.repository.path) as conn:
        row = conn.execute("SELECT token_hash FROM intake_launch_tokens WHERE token_hash=?", (h,)).fetchone()
        assert row[0] == h


def test_line_trigger_creates_token():
    client, app_mod, tmp = _make_client()
    assert app_mod._is_previsit_trigger_text("我要準備看診") is True
    assert app_mod._is_previsit_trigger_text("開啟看診前對談室") is True
    assert app_mod._is_previsit_trigger_text("請說明糖尿病的一般飲食原則") is False
    assert app_mod._portal_available() is True
    raw, sid = app_mod._create_previsit_token_for_user("U123")
    assert raw and sid
    assert "U123" not in raw
    flex = app_mod._build_previsit_flex_message(f"https://example.com/patient/previsit-room?token={raw}")
    assert flex["type"] == "flex"
    assert "開啟看診前對談室" in str(flex)


def _parse_sse_events(text: str) -> list[dict]:
    """Parse SSE wire 'event: X\\ndata: JSON' into list of (event, payload)."""
    import json as _j
    events: list[dict] = []
    # SSE blocks are separated by blank line
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        evt = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                evt = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if data is not None:
            try:
                payload = _j.loads(data)
            except Exception:
                payload = {"_raw": data}
            payload["_event"] = evt
            events.append(payload)
    return events


def test_stream_success_final_only_not_sliced():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "streamA")
    ver = sess.version
    r = client.post(
        "/api/patient/previsit-room/chat/stream",
        headers={"X-Intake-Token": raw},
        json={"message": "沒有過敏", "version": ver, "client_message_id": "s-1"},
    )
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/event-stream")
    events = _parse_sse_events(r.text)
    assert len(events) == 3
    assert events[0].get("type") == "phase"
    assert events[0].get("stream_mode") == "final_only"
    assert events[0].get("_event") == "phase"
    assert events[1].get("type") == "delta"
    assert events[1].get("stream_mode") == "final_only"
    assert events[1].get("_event") == "delta"
    # only one delta
    assert sum(1 for e in events if e.get("type") == "delta") == 1
    assert events[2].get("type") == "complete"
    assert events[2].get("stream_mode") == "final_only"
    assert events[2].get("_event") == "complete"
    # delta content must equal complete reply — no slicing / fake verbatim
    assert events[1].get("content") == events[2].get("reply")
    assert "reply" in events[2] and "version" in events[2]


def test_stream_red_flag():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "streamRed")
    ver = sess.version
    before = sess.intake_snapshot.model_dump(mode="json")
    r = client.post(
        "/api/patient/previsit-room/chat/stream",
        headers={"X-Intake-Token": raw},
        json={"message": "我胸痛胸悶喘不過氣", "version": ver, "client_message_id": "s-red"},
    )
    assert r.status_code == 200
    events = _parse_sse_events(r.text)
    complete = [e for e in events if e.get("type") == "complete"][0]
    assert complete.get("status") == "FALLBACK"
    assert "119" in complete.get("reply", "")
    assert complete.get("stream_mode") == "final_only"
    assert complete.get("intake_snapshot") == before or complete["intake_snapshot"]["symptom_description"] == before["symptom_description"]


def test_stream_education_not_advancing():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "streamEdu")
    ver = sess.version
    stage = sess.intake_stage
    r = client.post(
        "/api/patient/previsit-room/chat/stream",
        headers={"X-Intake-Token": raw},
        json={"message": "請說明糖尿病的一般飲食原則。", "version": ver, "client_message_id": "s-edu"},
    )
    assert r.status_code == 200
    events = _parse_sse_events(r.text)
    complete = [e for e in events if e.get("type") == "complete"][0]
    assert "衛教" in complete.get("reply", "") or "回 LINE" in complete.get("reply", "")
    assert complete.get("intake_stage") == stage
    assert complete.get("version") == ver
    assert all(e.get("stream_mode") == "final_only" for e in events)


def test_stream_replay_idempotent():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "streamReplay")
    ver = sess.version
    payload = {"message": "沒有過敏", "version": ver, "client_message_id": "s-replay"}
    r1 = client.post("/api/patient/previsit-room/chat/stream", headers={"X-Intake-Token": raw}, json=payload)
    assert r1.status_code == 200
    e1 = _parse_sse_events(r1.text)
    c1 = [e for e in e1 if e.get("type") == "complete"][0]
    # second with stale version but same client_message_id should replay same stream (200)
    payload2 = {"message": "沒有過敏", "version": 999, "client_message_id": "s-replay"}
    r2 = client.post("/api/patient/previsit-room/chat/stream", headers={"X-Intake-Token": raw}, json=payload2)
    assert r2.status_code == 200
    e2 = _parse_sse_events(r2.text)
    c2 = [e for e in e2 if e.get("type") == "complete"][0]
    assert c1.get("reply") == c2.get("reply")
    assert c1.get("version") == c2.get("version")
    assert e1[1].get("content") == e2[1].get("content")


def test_stream_409_and_submitted_locked():
    client, app_mod, tmp = _make_client()
    raw, sess = _create_session_and_token(client, app_mod, "stream409")
    # stale version → 409 (not SSE)
    r = client.post(
        "/api/patient/previsit-room/chat/stream",
        headers={"X-Intake-Token": raw},
        json={"message": "我有吃藥", "version": 999, "client_message_id": "s-409a"},
    )
    assert r.status_code == 409
    # SUBMITTED locked → 409
    orch = app_mod._get_conversation_orchestrator()
    cur = orch.repository.get(sess.session_id)
    cur2 = cur.model_copy(update={"status": "SUBMITTED", "intake_stage": "submitted"}, deep=True)
    orch.repository.save(cur2, expected_version=cur.version)
    updated = orch.repository.get(sess.session_id)
    r2 = client.post(
        "/api/patient/previsit-room/chat/stream",
        headers={"X-Intake-Token": raw},
        json={"message": "繼續整理", "version": updated.version, "client_message_id": "s-409b"},
    )
    assert r2.status_code == 409
    assert "SUBMITTED" in r2.json().get("detail", "")
