from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _configured_client(tmp_path, monkeypatch):
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "api-test-identity-key-123456")
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "line.sqlite3"))
    monkeypatch.setenv("DEMO_CLINICIAN_IDS", "doctor-01,doctor-02")
    monkeypatch.setenv("LINE_DEMO_MODE", "true")
    monkeypatch.setenv("LINE_DEMO_ALLOW_ID_HEADERS", "true")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    return line_app, TestClient(line_app.app)


def _complete_intake(line_app, user_id: str = "U-api"):
    orchestrator = line_app._get_conversation_orchestrator()
    messages = [
        "為自己整理",
        "吃 metformin，無過敏，有高血壓，家族無糖尿病",
        "三天前開始，早晨血糖偏高，程度4/10",
        "我想問醫師飲食要注意什麼？",
        "確認完成",
    ]
    for index, text in enumerate(messages):
        orchestrator.handle_text(event_id=f"api-event-{index}", line_user_id=user_id, text=text)
    return orchestrator.session_for_user(user_id)


def test_patient_share_and_clinician_read_only_redeem(tmp_path, monkeypatch):
    line_app, client = _configured_client(tmp_path, monkeypatch)
    session = _complete_intake(line_app)

    issued = client.post(
        f"/api/patient/sessions/{session.session_id}/share",
        headers={"X-Line-User-Id": "U-api"},
        json={"allowed_clinician_id": "doctor-01"},
    )
    assert issued.status_code == 200
    token = issued.json()["token"]

    denied = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "doctor-02"},
        json={"token": token},
    )
    assert denied.status_code == 403

    allowed = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "doctor-01"},
        json={"token": token},
    )
    assert allowed.status_code == 200
    payload = allowed.json()
    assert payload["intake_snapshot"]["known_medications"] == ["metformin"]
    assert "grantor_principal_hash" not in payload
    assert "token_hash" not in payload

    audit = client.get(
        "/api/clinician/audit", headers={"X-Demo-Clinician-Id": "doctor-01"}
    )
    assert audit.status_code == 200
    assert any(event["result"] == "ALLOWED" for event in audit.json()["events"])

    replay = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "doctor-01"},
        json={"token": token},
    )
    assert replay.status_code == 403


def test_patient_cannot_share_another_users_session(tmp_path, monkeypatch):
    line_app, client = _configured_client(tmp_path, monkeypatch)
    session = _complete_intake(line_app)

    response = client.post(
        f"/api/patient/sessions/{session.session_id}/share",
        headers={"X-Line-User-Id": "U-attacker"},
        json={},
    )
    assert response.status_code == 403


def test_unverified_clinician_cannot_redeem_or_read_audit(tmp_path, monkeypatch):
    _line_app, client = _configured_client(tmp_path, monkeypatch)

    redeem = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "unknown"},
        json={"token": "x" * 40},
    )
    audit = client.get(
        "/api/clinician/audit", headers={"X-Demo-Clinician-Id": "unknown"}
    )

    assert redeem.status_code == 403
    assert audit.status_code == 403


def test_demo_clinician_header_is_disabled_unless_demo_mode_is_explicit(tmp_path, monkeypatch):
    _line_app, client = _configured_client(tmp_path, monkeypatch)
    monkeypatch.setenv("LINE_DEMO_MODE", "false")

    response = client.get(
        "/api/clinician/audit", headers={"X-Demo-Clinician-Id": "doctor-01"}
    )

    assert response.status_code == 503


def test_patient_can_revoke_before_clinician_access(tmp_path, monkeypatch):
    line_app, client = _configured_client(tmp_path, monkeypatch)
    session = _complete_intake(line_app)
    issued = client.post(
        f"/api/patient/sessions/{session.session_id}/share",
        headers={"X-Line-User-Id": "U-api"}, json={}
    ).json()

    revoked = client.post(
        f"/api/patient/share/{issued['grant_id']}/revoke",
        headers={"X-Line-User-Id": "U-api"},
    )
    denied = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "doctor-01"},
        json={"token": issued["token"]},
    )

    assert revoked.status_code == 200
    assert revoked.json()["status"] == "REVOKED"
    assert denied.status_code == 403


def test_patient_api_rejects_unverified_identity_by_default(tmp_path, monkeypatch):
    _line_app, client = _configured_client(tmp_path, monkeypatch)
    monkeypatch.setenv("LINE_DEMO_ALLOW_ID_HEADERS", "false")
    response = client.get("/api/patient/session", headers={"X-Line-User-Id": "U-api"})
    assert response.status_code == 401


def test_patient_api_uses_server_verified_line_subject(tmp_path, monkeypatch):
    line_app, client = _configured_client(tmp_path, monkeypatch)
    _complete_intake(line_app)
    monkeypatch.setenv("LINE_DEMO_ALLOW_ID_HEADERS", "false")
    monkeypatch.setattr(line_app, "_verify_line_id_token", lambda token: "U-api" if token == "valid-token" else "")

    response = client.get(
        "/api/patient/session",
        headers={"Authorization": "Bearer valid-token", "X-Line-User-Id": "U-attacker"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "SUBMITTED"


def test_portals_and_rich_menu_definition_are_available(tmp_path, monkeypatch):
    _line_app, client = _configured_client(tmp_path, monkeypatch)
    assert client.get("/patient").status_code == 200
    assert client.get("/clinician").status_code == 200
    invalid = client.get("/api/line/rich-menu", params={"patient_portal_url": "http://local"})
    valid = client.get("/api/line/rich-menu", params={"patient_portal_url": "https://demo.example/patient"})
    assert invalid.status_code == 422
    assert valid.status_code == 200
    payload = valid.json()
    assert len(payload["areas"]) == 1
    assert payload["areas"][0]["action"] == {
        "type": "uri",
        "label": "開始看診前整理",
        "uri": "https://demo.example/patient",
    }
    assert "clinician" not in valid.text.lower()
