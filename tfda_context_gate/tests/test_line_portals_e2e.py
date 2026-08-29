from __future__ import annotations

import importlib
from html.parser import HTMLParser

from fastapi.testclient import TestClient


class _PortalControls(HTMLParser):
    """Small DOM-level assertion helper; avoids treating the page as a string snapshot."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: dict[str, tuple[str, dict[str, str]]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.elements[values["id"]] = (tag, values)


def _configured_client(tmp_path, monkeypatch):
    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "portal-e2e-identity-key-123456")
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "portal.sqlite3"))
    monkeypatch.setenv("LINE_DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_CLINICIAN_IDS", "doctor-portal")
    monkeypatch.setenv("LINE_DEMO_ALLOW_ID_HEADERS", "true")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    return line_app, TestClient(line_app.app)


def _complete_intake(line_app, user_id: str = "U-portal"):
    orchestrator = line_app._get_conversation_orchestrator()
    for index, text in enumerate(
        [
            "為自己整理",
            "吃 metformin，無過敏，有高血壓，家族無糖尿病",
            "三天前開始，早晨血糖偏高，程度4/10",
            "我想問醫師飲食要注意什麼？",
            "確認完成",
        ]
    ):
        orchestrator.handle_text(
            event_id=f"portal-event-{user_id}-{index}",
            line_user_id=user_id,
            text=text,
        )
    return orchestrator.session_for_user(user_id)


def test_patient_review_and_clinician_read_only_journey(tmp_path, monkeypatch):
    line_app, client = _configured_client(tmp_path, monkeypatch)
    session = _complete_intake(line_app)

    patient = client.get("/api/patient/session", headers={"X-Line-User-Id": "U-portal"})
    assert patient.status_code == 200
    patient_data = patient.json()
    review = patient_data["review"]
    assert patient_data["status"] == "SUBMITTED"
    assert review["confirmation_status"] == "CONFIRMED"
    assert review["can_share"] is True
    assert [field["name"] for field in review["fields"]] == [
        "known_medications",
        "allergies",
        "chronic_conditions",
        "family_history",
        "symptom_onset",
        "symptom_description",
        "symptom_severity",
        "questions_for_doctor",
    ]
    assert all(field["state"] == "PROVIDED" for field in review["fields"])
    assert review["disclaimer"]

    issued = client.post(
        f"/api/patient/sessions/{session.session_id}/share",
        headers={"X-Line-User-Id": "U-portal"},
        json={"allowed_clinician_id": "doctor-portal"},
    )
    assert issued.status_code == 200
    token = issued.json()["token"]

    redeemed = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "doctor-portal"},
        json={"token": token},
    )
    assert redeemed.status_code == 200
    clinician_data = redeemed.json()
    assert clinician_data["previsit_summary"]["disclaimer"]
    assert clinician_data["previsit_summary"]["intake"]["known_medications"] == ["metformin"]
    assert clinician_data["output_gate_result"]["decision"] == "PASS"
    assert "principal_id_hash" not in redeemed.text
    assert "token_hash" not in redeemed.text

    # Redeeming only consumes the grant/audit record; it cannot update a patient
    # session.  There is intentionally no clinician write endpoint.
    after = client.get("/api/patient/session", headers={"X-Line-User-Id": "U-portal"})
    assert after.json()["status"] == "SUBMITTED"
    assert after.json()["intake_snapshot"] == patient_data["intake_snapshot"]
    assert client.put("/api/clinician/share/redeem", json={"token": token}).status_code == 405

    audit = client.get(
        "/api/clinician/audit", headers={"X-Demo-Clinician-Id": "doctor-portal"}
    )
    assert audit.status_code == 200
    assert any(event["result"] == "ALLOWED" for event in audit.json()["events"])


def test_patient_review_separates_pending_from_missing_and_blocks_share(tmp_path, monkeypatch):
    line_app, client = _configured_client(tmp_path, monkeypatch)
    orchestrator = line_app._get_conversation_orchestrator()
    orchestrator.handle_text(event_id="pending-1", line_user_id="U-pending", text="我要準備看診")
    orchestrator.handle_text(event_id="pending-2", line_user_id="U-pending", text="為自己整理")

    patient = client.get("/api/patient/session", headers={"X-Line-User-Id": "U-pending"})
    assert patient.status_code == 200
    data = patient.json()
    states = {field["name"]: field["state"] for field in data["review"]["fields"]}
    assert data["review"]["confirmation_status"] == "IN_PROGRESS"
    assert data["review"]["can_share"] is False
    assert states["known_medications"] == "PENDING"
    assert states["allergies"] == "MISSING"
    assert "known_medications" in data["review"]["pending_fields"]
    assert "allergies" in data["review"]["missing_fields"]
    blocked = client.post(
        f"/api/patient/sessions/{data['session_id']}/share",
        headers={"X-Line-User-Id": "U-pending"},
        json={},
    )
    assert blocked.status_code == 409


def test_demo_capability_flag_and_portal_controls_are_safe_and_operational(tmp_path, monkeypatch):
    line_app, client = _configured_client(tmp_path, monkeypatch)
    config = client.get("/api/line/client-config")
    assert config.status_code == 200
    assert config.json()["demo_clinician_enabled"] is True
    assert "DEMO_CLINICIAN_IDS" not in config.text
    assert "doctor-portal" not in config.text

    patient_markup = client.get("/patient")
    clinician_markup = client.get("/clinician")
    assert patient_markup.status_code == clinician_markup.status_code == 200

    patient_controls = _PortalControls()
    patient_controls.feed(patient_markup.text)
    assert patient_controls.elements["fields"][0] == "dl"
    for element_id in ("load", "share", "revoke", "pending-fields", "missing-fields", "disclaimer"):
        assert element_id in patient_controls.elements
    assert patient_controls.elements["error"][1].get("role") == "alert"
    assert patient_controls.elements["share"][1].get("disabled") == ""

    clinician_controls = _PortalControls()
    clinician_controls.feed(clinician_markup.text)
    for element_id in ("cid", "token", "redeem", "fields", "missing-fields", "questions", "load-audit"):
        assert element_id in clinician_controls.elements
    assert clinician_controls.elements["audit-table-wrap"][0] == "div"
    assert clinician_controls.elements["error"][1].get("role") == "alert"

    monkeypatch.setenv("LINE_DEMO_MODE", "false")
    assert client.get("/api/line/client-config").json()["demo_clinician_enabled"] is False
    disabled = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "doctor-portal"},
        json={"token": "x" * 40},
    )
    assert disabled.status_code == 503
