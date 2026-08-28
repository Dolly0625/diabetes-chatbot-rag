from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from tfda_context_gate.access_control import ActorAccessContext
from tfda_context_gate.conversation import ConversationContextManager
from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.product_session import (
    ProductSession,
    ShareGrantDenied,
    SQLiteProductSessionRepository,
)
from tfda_context_gate.sharing import ShareGrantService


def _submitted_session() -> ProductSession:
    now = datetime.now(timezone.utc)
    return ProductSession(
        session_id="submitted-001",
        principal_id_hash="a" * 64,
        actor_role="PATIENT",
        frontend_persona="PATIENT_FAMILY",
        subject_id_hash="a" * 64,
        information_source="SELF_REPORTED",
        authorization_status="PATIENT_SELF",
        permission_scopes=["CREATE_OWN_INTAKE", "VIEW_OWN_SUMMARY", "SHARE_OWN_SUMMARY"],
        conversation_context=ConversationContextManager().create("submitted-001"),
        intake_snapshot=PreVisitIntake(
            known_medications=["metformin"], allergies=["無"], chronic_conditions=["糖尿病"],
            family_history=["無"], symptom_onset="三天前", symptom_description="早晨血糖偏高",
            symptom_severity="4/10", questions_for_doctor=["飲食要注意什麼？"],
        ),
        intake_stage="submitted",
        status="SUBMITTED",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=7),
    )


def _practitioner(value: str = "b") -> ActorAccessContext:
    return ActorAccessContext(
        principal_id_hash=value * 64,
        actor_role="PRACTITIONER",
        frontend_persona="CLINICIAN",
        authorization_status="CLINICIAN_VERIFIED",
        permission_scopes=["VIEW_GRANTED_CLINICAL_SUMMARY", "VIEW_EVIDENCE"],
    )


def test_short_lived_grant_redeems_to_read_only_summary_and_is_single_use(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    service = ShareGrantService(repository)
    issue = service.create(_submitted_session())

    view = service.redeem(issue.token, _practitioner())

    assert view.intake_snapshot["known_medications"] == ["metformin"]
    assert view.previsit_summary["reported_severity"] == "4/10"
    assert view.output_gate_result["decision"] == "PASS"
    assert "principal" not in view.model_dump_json()
    with pytest.raises(ShareGrantDenied, match="not active"):
        service.redeem(issue.token, _practitioner())


def test_raw_token_is_not_persisted(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    repository = SQLiteProductSessionRepository(path)
    issue = ShareGrantService(repository).create(_submitted_session())

    assert issue.token not in path.read_bytes().decode("utf-8", errors="ignore")


def test_grant_can_be_bound_to_one_practitioner(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    service = ShareGrantService(repository)
    issue = service.create(_submitted_session(), allowed_practitioner_hash="b" * 64)

    with pytest.raises(ShareGrantDenied, match="another practitioner"):
        service.redeem(issue.token, _practitioner("c"))
    assert service.redeem(issue.token, _practitioner("b")).grant_id == issue.grant_id


def test_grantor_can_revoke_before_use(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    service = ShareGrantService(repository)
    issue = service.create(_submitted_session())
    revoked = service.revoke(issue.grant_id, "a" * 64)

    assert revoked.status == "REVOKED"
    with pytest.raises(ShareGrantDenied, match="not active"):
        service.redeem(issue.token, _practitioner())


def test_unconfirmed_session_cannot_create_grant(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    session = _submitted_session().model_copy(update={"status": "ACTIVE", "intake_stage": "review"})

    with pytest.raises(ShareGrantDenied, match="not confirmed"):
        ShareGrantService(repository).create(session)


def test_expired_grant_cannot_be_consumed(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    issue = ShareGrantService(repository).create(_submitted_session())

    with pytest.raises(ShareGrantDenied, match="expired"):
        repository.consume_share_grant(
            hashlib.sha256(issue.token.encode()).hexdigest(),
            "b" * 64,
            now=issue.expires_at + timedelta(seconds=1),
        )


def test_expired_grant_snapshot_is_physically_purged(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    repository = SQLiteProductSessionRepository(path)
    issue = ShareGrantService(repository).create(_submitted_session())

    deleted = repository.purge_expired(now=issue.expires_at + timedelta(seconds=1))

    assert deleted["share_grants"] == 1
    import sqlite3
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM share_grants").fetchone()[0] == 0


def test_red_flag_session_cannot_create_ungated_share_snapshot(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    session = _submitted_session().model_copy(update={
        "system_risk_classification": {
            "level": "RED_FLAG",
            "signals": ["BREATHING_DIFFICULTY"],
            "action": "URGENT_HUMAN",
            "basis": "explicit_user_report",
            "limitations": "文字安全分流",
        }
    })

    with pytest.raises(ShareGrantDenied, match="mandatory output gate"):
        ShareGrantService(repository).create(session)
