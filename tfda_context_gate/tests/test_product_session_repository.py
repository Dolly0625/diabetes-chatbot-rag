from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tfda_context_gate.conversation import ConversationContextManager
from tfda_context_gate.access_control import PermissionScope
from tfda_context_gate.product_session import (
    ProductSession,
    ProductSessionConflict,
    SQLiteProductSessionRepository,
    WebhookEventIdentityMismatch,
)

_PRINCIPAL = "f" * 64


def _session(*, expires_delta: timedelta = timedelta(days=7)) -> ProductSession:
    now = datetime.now(timezone.utc)
    return ProductSession(
        session_id="session-001",
        principal_id_hash="a" * 64,
        actor_role="PATIENT",
        frontend_persona="PATIENT_FAMILY",
        authorization_status="PATIENT_SELF",
        permission_scopes=["CREATE_OWN_INTAKE", "VIEW_OWN_SUMMARY", "SHARE_OWN_SUMMARY"],
        conversation_context=ConversationContextManager().create("session-001"),
        created_at=now,
        updated_at=now,
        expires_at=now + expires_delta,
    )


def test_sqlite_session_survives_repository_restart(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    first = SQLiteProductSessionRepository(path)
    created = first.create(_session())

    second = SQLiteProductSessionRepository(path)
    loaded = second.get("session-001")

    assert created.version == 1
    assert loaded is not None
    assert loaded.principal_id_hash == "a" * 64
    assert loaded.authorization_status == "PATIENT_SELF"


def test_optimistic_version_rejects_stale_writer(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    created = repository.create(_session())
    first_writer = created.model_copy(update={"pending_question": "第一個問題"}, deep=True)
    saved = repository.save(first_writer, expected_version=created.version)

    stale_writer = created.model_copy(update={"pending_question": "覆蓋資料"}, deep=True)
    with pytest.raises(ProductSessionConflict, match="version conflict"):
        repository.save(stale_writer, expected_version=created.version)

    assert saved.version == 2
    assert repository.get("session-001").pending_question == "第一個問題"  # type: ignore[union-attr]


def test_expired_session_is_removed_on_read(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    created = repository.create(_session())

    assert repository.get("session-001", now=created.expires_at + timedelta(seconds=1)) is None
    assert repository.get("session-001") is None


def test_webhook_event_is_claimed_once_and_result_is_replayable(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")

    claim = repository.claim_webhook_event("event-001", _PRINCIPAL)
    assert claim is not None
    assert repository.claim_webhook_event("event-001", _PRINCIPAL) is None
    completed = repository.complete_webhook_event("event-001", {"reply": "ok"}, claim_token=claim)

    assert completed.status == "COMPLETED"
    assert completed.result == {"reply": "ok"}
    assert repository.get_webhook_event("event-001") == completed


def test_completed_event_cannot_be_completed_twice(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    claim = repository.claim_webhook_event("event-001", _PRINCIPAL)
    repository.complete_webhook_event("event-001", {"reply": "ok"}, claim_token=claim)

    with pytest.raises(ProductSessionConflict, match="already completed"):
        repository.complete_webhook_event("event-001", {"reply": "duplicate"}, claim_token=claim)


def test_failed_webhook_event_can_be_claimed_again(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    claim = repository.claim_webhook_event("event-retry", _PRINCIPAL)
    assert claim is not None
    repository.fail_webhook_event("event-retry", claim_token=claim)

    assert repository.get_webhook_event("event-retry").status == "FAILED"  # type: ignore[union-attr]
    assert repository.claim_webhook_event("event-retry", _PRINCIPAL) is not None
    assert repository.get_webhook_event("event-retry").status == "PROCESSING"  # type: ignore[union-attr]


def test_webhook_event_cannot_replay_across_principals(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    assert repository.claim_webhook_event("event-bound", _PRINCIPAL) is not None

    with pytest.raises(WebhookEventIdentityMismatch):
        repository.claim_webhook_event("event-bound", "e" * 64)


def test_expired_processing_lease_can_be_reclaimed_but_old_worker_cannot_complete(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    repository = SQLiteProductSessionRepository(path)
    old_claim = repository.claim_webhook_event("event-stale", _PRINCIPAL)
    assert old_claim is not None
    import sqlite3
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE webhook_events SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE event_id='event-stale'"
        )

    new_claim = repository.claim_webhook_event("event-stale", _PRINCIPAL)
    assert new_claim is not None and new_claim != old_claim
    with pytest.raises(ProductSessionConflict):
        repository.complete_webhook_event("event-stale", {"reply": "old"}, claim_token=old_claim)
    completed = repository.complete_webhook_event("event-stale", {"reply": "new"}, claim_token=new_claim)
    assert completed.result == {"reply": "new"}


def test_product_session_rejects_role_scope_mismatch():
    payload = _session().model_dump(mode="python")
    payload.update({
        "actor_role": "RELATED_PERSON",
        "authorization_status": "AUTHORIZED_CAREGIVER",
        "permission_scopes": ["VIEW_OWN_SUMMARY"],
        "subject_id_hash": "b" * 64,
        "information_source": "PROXY_OBSERVED",
    })
    with pytest.raises(ValidationError, match="permission scope"):
        ProductSession.model_validate(payload)


def test_repository_revalidates_model_copy_before_save(tmp_path):
    repository = SQLiteProductSessionRepository(tmp_path / "sessions.sqlite3")
    created = repository.create(_session())
    invalid_copy = created.model_copy(update={"permission_scopes": [PermissionScope.MANAGE_SYSTEM]})

    with pytest.raises(ValidationError, match="permission scope"):
        repository.save(invalid_copy, expected_version=created.version)


def test_purge_removes_expired_health_payloads_and_old_webhook_results(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    repository = SQLiteProductSessionRepository(path)
    created = repository.create(_session())
    claim = repository.claim_webhook_event("health-event", _PRINCIPAL)
    repository.complete_webhook_event(
        "health-event", {"reply": "敏感健康資料 metformin"}, claim_token=claim
    )

    deleted = repository.purge_expired(
        now=created.expires_at + timedelta(days=2),
        webhook_retention=timedelta(days=1),
    )

    assert deleted["product_sessions"] == 1
    assert deleted["webhook_events"] == 1
    assert repository.get("session-001") is None
    assert repository.get_webhook_event("health-event") is None
