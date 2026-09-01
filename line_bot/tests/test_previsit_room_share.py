from __future__ import annotations

from fastapi.testclient import TestClient

from tfda_context_gate.access_control import AuthorizationStatus, InformationSource, PermissionScope
from tfda_context_gate.intake.schemas import PreVisitIntake


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", "test-identity-hash-key-16c-long")
    monkeypatch.setenv("DEMO_INTAKE_TOKEN_ENABLED", "true")
    monkeypatch.setenv("LINE_DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_CLINICIAN_IDS", "doctor-demo")
    import line_bot.app as app_mod

    app_mod._conversation_orchestrator = None
    app_mod._web_chat_dedup.clear()
    return TestClient(app_mod.app), app_mod


def _session_with_state(app_mod, user_id: str, *, submitted: bool):
    orchestrator = app_mod._get_conversation_orchestrator()
    session = orchestrator._load_or_create(user_id)
    if submitted:
        update = {
            "authorization_status": AuthorizationStatus.PATIENT_SELF,
            "subject_id_hash": session.principal_id_hash,
            "information_source": InformationSource.SELF_REPORTED,
            "permission_scopes": [
                PermissionScope.CREATE_OWN_INTAKE,
                PermissionScope.VIEW_OWN_SUMMARY,
                PermissionScope.SHARE_OWN_SUMMARY,
            ],
            "intake_snapshot": PreVisitIntake(
                known_medications=["metformin"],
                allergies=["無"],
                chronic_conditions=["糖尿病"],
                family_history=["無"],
                symptom_onset="三天前",
                symptom_description="早晨血糖偏高",
                symptom_severity="4/10",
                questions_for_doctor=["飲食要注意什麼？"],
            ),
            "intake_stage": "submitted",
            "status": "SUBMITTED",
            "pending_field": None,
            "pending_question": None,
            "pending_action": None,
        }
    else:
        update = {
            "authorization_status": AuthorizationStatus.PATIENT_SELF,
            "subject_id_hash": session.principal_id_hash,
            "information_source": InformationSource.SELF_REPORTED,
            "permission_scopes": [
                PermissionScope.CREATE_OWN_INTAKE,
                PermissionScope.VIEW_OWN_SUMMARY,
                PermissionScope.SHARE_OWN_SUMMARY,
            ],
            "intake_stage": "stage1",
            "status": "ACTIVE",
            "pending_field": "known_medications",
            "pending_question": "目前有固定吃藥嗎？",
        }
    saved = orchestrator.repository.save(session.model_copy(update=update, deep=True), expected_version=session.version)
    raw_token, _ = app_mod._create_previsit_token_for_user(user_id)
    return saved, raw_token


def test_web_room_share_e2e_is_confirmed_token_bound_and_single_use(monkeypatch, tmp_path):
    client, app_mod = _client(monkeypatch, tmp_path)
    _, draft_token = _session_with_state(app_mod, "draft-patient", submitted=False)
    submitted, submitted_token = _session_with_state(app_mod, "submitted-patient", submitted=True)
    other, _ = _session_with_state(app_mod, "other-patient", submitted=True)

    # 未提交／仍有 pending 的 room session 不得建立分享碼。
    response = client.post(
        "/api/patient/previsit-room/share",
        headers={"X-Intake-Token": draft_token},
        json={"allowed_clinician_id": "doctor-demo"},
    )
    assert response.status_code == 409
    assert "token_hash" not in response.text

    # Explicit session paths cannot be used to make token A operate on B.
    response = client.post(
        f"/api/patient/previsit-room/share/{other.session_id}",
        headers={"X-Intake-Token": submitted_token},
        json={"allowed_clinician_id": "doctor-demo"},
    )
    assert response.status_code == 403

    issue_response = client.post(
        "/api/patient/previsit-room/share",
        headers={"X-Intake-Token": submitted_token},
        json={"allowed_clinician_id": "doctor-demo"},
    )
    assert issue_response.status_code == 200
    issue = issue_response.json()
    assert set(issue) == {"grant_id", "token", "expires_at", "single_use", "qr_code_data_uri"}
    assert issue["single_use"] is True
    assert len(issue["token"]) >= 32
    assert "token_hash" not in issue
    assert issue["qr_code_data_uri"].startswith("data:image/png;base64,")
    assert issue["token"].encode() not in (tmp_path / "sessions.sqlite3").read_bytes()

    # The clinician allowlist is enforced before the one-time grant can be
    # consumed, so a non-allowlisted ID cannot read the summary.
    unauthorized_response = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "not-allowlisted"},
        json={"token": issue["token"]},
    )
    assert unauthorized_response.status_code == 403

    redeem_response = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "doctor-demo"},
        json={"token": issue["token"]},
    )
    assert redeem_response.status_code == 200
    view = redeem_response.json()
    assert view["intake_snapshot"]["known_medications"] == ["metformin"]
    assert view["output_gate_result"]["decision"] == "PASS"
    assert "token_hash" not in view

    # The same share code is consumed exactly once.
    second_response = client.post(
        "/api/clinician/share/redeem",
        headers={"X-Demo-Clinician-Id": "doctor-demo"},
        json={"token": issue["token"]},
    )
    assert second_response.status_code == 403


def test_web_room_share_rejects_non_allowlisted_clinician(monkeypatch, tmp_path):
    client, app_mod = _client(monkeypatch, tmp_path)
    _, token = _session_with_state(app_mod, "allowlist-patient", submitted=True)
    response = client.post(
        "/api/patient/previsit-room/share",
        headers={"X-Intake-Token": token},
        json={"allowed_clinician_id": "not-allowlisted"},
    )
    assert response.status_code == 403
