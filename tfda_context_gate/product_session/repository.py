from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .schemas import ClinicianAccessLog, ProductSession, ShareGrant, WebhookEventRecord


class ProductSessionConflict(RuntimeError):
    """Optimistic version 不一致，呼叫端必須重新讀取後重試。"""


class ShareGrantDenied(RuntimeError):
    """Grant 不存在、過期、撤銷、已使用或未授權給此醫護。"""


class WebhookEventIdentityMismatch(RuntimeError):
    """相同 webhookEventId 不得跨 principal 重播。"""


class ProductSessionRepository(Protocol):
    def create(self, session: ProductSession) -> ProductSession: ...
    def get(self, session_id: str, *, now: datetime | None = None) -> ProductSession | None: ...
    def save(self, session: ProductSession, *, expected_version: int) -> ProductSession: ...
    def claim_webhook_event(self, event_id: str, principal_id_hash: str, *, lease_seconds: int = 120) -> str | None: ...
    def complete_webhook_event(self, event_id: str, result: dict[str, Any], *, claim_token: str) -> WebhookEventRecord: ...
    def mark_webhook_event_pushed(self, event_id: str) -> WebhookEventRecord | None: ...
    def fail_webhook_event(self, event_id: str, *, claim_token: str) -> None: ...
    def get_webhook_event(self, event_id: str) -> WebhookEventRecord | None: ...
    def create_share_grant(self, grant: ShareGrant) -> ShareGrant: ...
    def consume_share_grant(self, token_hash: str, practitioner_hash: str, *, now: datetime | None = None) -> ShareGrant: ...
    def revoke_share_grant(self, grant_id: str, grantor_hash: str) -> ShareGrant: ...
    def get_share_grant_for_session(self, session_id: str, practitioner_hash: str, *, now: datetime | None = None) -> ShareGrant | None: ...
    def append_clinician_access_log(self, log: ClinicianAccessLog) -> None: ...
    def list_clinician_access_logs(self, practitioner_hash: str) -> list[ClinicianAccessLog]: ...
    def purge_expired(self, *, now: datetime | None = None) -> dict[str, int]: ...
    def create_intake_token(self, token_hash: str, session_id: str, expires_at: datetime) -> None: ...
    def get_intake_token(self, token_hash: str) -> dict[str, Any] | None: ...
    def consume_intake_token(self, token_hash: str) -> None: ...
    def delete_intake_token_for_session(self, session_id: str) -> None: ...


class SQLiteProductSessionRepository:
    """SQLite demo adapter；JSON payload 由 Pydantic 嚴格驗證。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._last_purge_at: datetime | None = None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS product_sessions ("
                "session_id TEXT PRIMARY KEY, payload TEXT NOT NULL, version INTEGER NOT NULL, "
                "expires_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS webhook_events ("
                "event_id TEXT PRIMARY KEY, principal_id_hash TEXT, status TEXT NOT NULL, result TEXT, "
                "claim_token TEXT, lease_expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(webhook_events)")}
            for name, definition in {
                "principal_id_hash": "TEXT",
                "claim_token": "TEXT",
                "lease_expires_at": "TEXT",
            }.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE webhook_events ADD COLUMN {name} {definition}")
            # 舊版 event 沒有 principal provenance，不能安全重播；遷移時直接捨棄。
            connection.execute("DELETE FROM webhook_events WHERE principal_id_hash IS NULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS share_grants ("
                "grant_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, "
                "status TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS clinician_access_logs ("
                "log_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS intake_launch_tokens ("
                "token_hash TEXT PRIMARY KEY, product_session_id TEXT NOT NULL, "
                "created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT)"
            )
        self.purge_expired()

    def create(self, session: ProductSession) -> ProductSession:
        session = ProductSession.model_validate(session.model_dump(mode="python"))
        if session.version != 0:
            raise ValueError("new session version must be 0")
        stored = session.model_copy(update={"version": 1}, deep=True)
        payload = stored.model_dump_json()
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    "INSERT INTO product_sessions(session_id,payload,version,expires_at,updated_at) VALUES(?,?,?,?,?)",
                    (stored.session_id, payload, stored.version, stored.expires_at.isoformat(), stored.updated_at.isoformat()),
                )
        except sqlite3.IntegrityError as exc:
            raise ProductSessionConflict("session already exists") from exc
        return stored

    def get(self, session_id: str, *, now: datetime | None = None) -> ProductSession | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM product_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            session = ProductSession.model_validate_json(row["payload"])
            if session.is_expired(now):
                connection.execute("DELETE FROM product_sessions WHERE session_id=?", (session_id,))
                return None
            return session

    def save(self, session: ProductSession, *, expected_version: int) -> ProductSession:
        session = ProductSession.model_validate(session.model_dump(mode="python"))
        if session.session_id == "":
            raise ValueError("session_id is required")
        stored = session.model_copy(
            update={"version": expected_version + 1, "updated_at": datetime.now(timezone.utc)},
            deep=True,
        )
        payload = stored.model_dump_json()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE product_sessions SET payload=?,version=?,expires_at=?,updated_at=? "
                "WHERE session_id=? AND version=?",
                (payload, stored.version, stored.expires_at.isoformat(), stored.updated_at.isoformat(), stored.session_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise ProductSessionConflict("session version conflict or session missing")
        return stored

    def claim_webhook_event(
        self,
        event_id: str,
        principal_id_hash: str,
        *,
        lease_seconds: int = 120,
    ) -> str | None:
        self._maybe_purge_expired()
        if len(principal_id_hash) != 64:
            raise ValueError("principal_id_hash must contain 64 characters")
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        lease_expires_at = (now_value + timedelta(seconds=max(1, lease_seconds))).isoformat()
        claim_token = uuid.uuid4().hex
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    "INSERT INTO webhook_events(event_id,principal_id_hash,status,result,claim_token,lease_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, principal_id_hash, "PROCESSING", None, claim_token, lease_expires_at, now, now),
                )
            return claim_token
        except sqlite3.IntegrityError:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT principal_id_hash,status,lease_expires_at FROM webhook_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if row is None:
                    return None
                if row["principal_id_hash"] != principal_id_hash:
                    raise WebhookEventIdentityMismatch("webhook event belongs to another principal")
                cursor = connection.execute(
                    "UPDATE webhook_events SET status='PROCESSING',result=NULL,claim_token=?,lease_expires_at=?,updated_at=? "
                    "WHERE event_id=? AND principal_id_hash=? AND (status='FAILED' OR (status='PROCESSING' AND lease_expires_at<=?))",
                    (claim_token, lease_expires_at, now, event_id, principal_id_hash, now),
                )
            return claim_token if cursor.rowcount == 1 else None

    def complete_webhook_event(self, event_id: str, result: dict[str, Any], *, claim_token: str) -> WebhookEventRecord:
        now = datetime.now(timezone.utc)
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE webhook_events SET status='COMPLETED',result=?,claim_token=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE event_id=? AND status='PROCESSING' AND claim_token=?",
                (encoded, now.isoformat(), event_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise ProductSessionConflict("webhook event was not claimed or already completed")
        record = self.get_webhook_event(event_id)
        if record is None:
            raise RuntimeError("completed webhook event disappeared")
        return record

    def mark_webhook_event_pushed(self, event_id: str) -> WebhookEventRecord | None:
        """Persist the post-transport push marker for replay idempotency.

        This update intentionally occurs *after* the external LINE call has
        succeeded.  It is therefore not a pre-send claim masquerading as a
        successful delivery, while a later webhook replay can still consult a
        durable marker after the process-local cache has been lost.
        """

        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT result FROM webhook_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["result"]) if row["result"] else {}
            if not isinstance(payload, dict):
                payload = {}
            payload["pushed"] = True
            connection.execute(
                "UPDATE webhook_events SET result=?,updated_at=? WHERE event_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), datetime.now(timezone.utc).isoformat(), event_id),
            )
        return self.get_webhook_event(event_id)

    def fail_webhook_event(self, event_id: str, *, claim_token: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE webhook_events SET status='FAILED',result=NULL,claim_token=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE event_id=? AND status='PROCESSING' AND claim_token=?",
                (now, event_id, claim_token),
            )

    def get_webhook_event(self, event_id: str) -> WebhookEventRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM webhook_events WHERE event_id=?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        return WebhookEventRecord(
            event_id=row["event_id"],
            principal_id_hash=row["principal_id_hash"],
            status=row["status"],
            claim_token=row["claim_token"],
            lease_expires_at=datetime.fromisoformat(row["lease_expires_at"]) if row["lease_expires_at"] else None,
            result=json.loads(row["result"]) if row["result"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def create_share_grant(self, grant: ShareGrant) -> ShareGrant:
        grant = ShareGrant.model_validate(grant.model_dump(mode="python"))
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    "INSERT INTO share_grants(grant_id,token_hash,payload,status,expires_at) VALUES(?,?,?,?,?)",
                    (grant.grant_id, grant.token_hash, grant.model_dump_json(), grant.status, grant.expires_at.isoformat()),
                )
        except sqlite3.IntegrityError as exc:
            raise ProductSessionConflict("share grant already exists") from exc
        return grant

    def consume_share_grant(
        self,
        token_hash: str,
        practitioner_hash: str,
        *,
        now: datetime | None = None,
    ) -> ShareGrant:
        consumed_at = now or datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM share_grants WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if row is None:
                raise ShareGrantDenied("share grant not found")
            grant = ShareGrant.model_validate_json(row["payload"])
            if grant.status != "ACTIVE":
                raise ShareGrantDenied("share grant is not active")
            if grant.is_expired(consumed_at):
                raise ShareGrantDenied("share grant expired")
            if grant.allowed_practitioner_hash not in (None, practitioner_hash):
                raise ShareGrantDenied("share grant is assigned to another practitioner")
            consumed = grant.model_copy(
                update={"status": "USED" if grant.single_use else "ACTIVE", "used_at": consumed_at},
                deep=True,
            )
            cursor = connection.execute(
                "UPDATE share_grants SET payload=?,status=? WHERE grant_id=? AND status='ACTIVE'",
                (consumed.model_dump_json(), consumed.status, consumed.grant_id),
            )
            if cursor.rowcount != 1:
                raise ShareGrantDenied("share grant was consumed concurrently")
            return consumed

    def revoke_share_grant(self, grant_id: str, grantor_hash: str) -> ShareGrant:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM share_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if row is None:
                raise ShareGrantDenied("share grant not found")
            grant = ShareGrant.model_validate_json(row["payload"])
            if grant.grantor_principal_hash != grantor_hash:
                raise ShareGrantDenied("only the grantor can revoke this share grant")
            if grant.status != "ACTIVE":
                raise ShareGrantDenied("share grant is not active")
            revoked = grant.model_copy(update={"status": "REVOKED", "revoked_at": now}, deep=True)
            connection.execute(
                "UPDATE share_grants SET payload=?,status=? WHERE grant_id=?",
                (revoked.model_dump_json(), revoked.status, grant_id),
            )
            return revoked

    def get_share_grant_for_session(
        self,
        session_id: str,
        practitioner_hash: str,
        *,
        now: datetime | None = None,
    ) -> ShareGrant | None:
        check_at = now or datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM share_grants WHERE payload LIKE ? ORDER BY expires_at DESC",
                (f'%"session_id":"{session_id}"%',),
            ).fetchall()
            candidates: list[ShareGrant] = []
            for row in rows:
                try:
                    grant = ShareGrant.model_validate_json(row["payload"])
                except Exception:
                    continue
                if grant.session_id != session_id:
                    continue
                if grant.is_expired(check_at):
                    continue
                if grant.status not in ("ACTIVE", "USED"):
                    continue
                if grant.allowed_practitioner_hash not in (None, practitioner_hash):
                    continue
                candidates.append(grant)
            if not candidates:
                return None
            candidates.sort(key=lambda g: g.created_at, reverse=True)
            return candidates[0]

    def append_clinician_access_log(self, log: ClinicianAccessLog) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO clinician_access_logs(log_id,payload,created_at) VALUES(?,?,?)",
                (log.log_id, log.model_dump_json(), log.created_at.isoformat()),
            )

    def list_clinician_access_logs(self, practitioner_hash: str) -> list[ClinicianAccessLog]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM clinician_access_logs ORDER BY created_at"
            ).fetchall()
        logs = [ClinicianAccessLog.model_validate_json(row["payload"]) for row in rows]
        return [log for log in logs if log.practitioner_hash == practitioner_hash]

    def purge_expired(
        self,
        *,
        now: datetime | None = None,
        webhook_retention: timedelta = timedelta(days=1),
        access_log_retention: timedelta = timedelta(days=90),
    ) -> dict[str, int]:
        """清除過期健康 payload；audit 僅保留雜湊識別且採獨立期限。"""
        cutoff = now or datetime.now(timezone.utc)
        webhook_cutoff = (cutoff - webhook_retention).isoformat()
        audit_cutoff = (cutoff - access_log_retention).isoformat()
        deleted: dict[str, int] = {}
        with self._lock, self._connect() as connection:
            deleted["product_sessions"] = connection.execute(
                "DELETE FROM product_sessions WHERE expires_at<=?", (cutoff.isoformat(),)
            ).rowcount
            deleted["share_grants"] = connection.execute(
                "DELETE FROM share_grants WHERE expires_at<=?", (cutoff.isoformat(),)
            ).rowcount
            deleted["webhook_events"] = connection.execute(
                "DELETE FROM webhook_events WHERE updated_at<=?", (webhook_cutoff,)
            ).rowcount
            deleted["clinician_access_logs"] = connection.execute(
                "DELETE FROM clinician_access_logs WHERE created_at<=?", (audit_cutoff,)
            ).rowcount
            deleted["intake_tokens"] = connection.execute(
                "DELETE FROM intake_launch_tokens WHERE expires_at<=? OR consumed_at IS NOT NULL", (cutoff.isoformat(),)
            ).rowcount
        # 讓 secure_delete 的結果離開 WAL，避免已刪除健康 payload 長期留在 -wal。
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._last_purge_at = cutoff
        return deleted

    # ── Intake launch token (opaque, hashed, 30min TTL, single session binding) ──
    def create_intake_token(self, token_hash: str, session_id: str, expires_at: datetime) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM intake_launch_tokens WHERE product_session_id=?", (session_id,))
            connection.execute(
                "INSERT INTO intake_launch_tokens(token_hash, product_session_id, created_at, expires_at, consumed_at) VALUES(?,?,?,?,?)",
                (token_hash, session_id, now, expires_at.isoformat(), None),
            )

    def get_intake_token(self, token_hash: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT token_hash, product_session_id, created_at, expires_at, consumed_at FROM intake_launch_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def consume_intake_token(self, token_hash: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE intake_launch_tokens SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL",
                (now, token_hash),
            )

    def delete_intake_token_for_session(self, session_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM intake_launch_tokens WHERE product_session_id=?", (session_id,))

    def _maybe_purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_purge_at is None or now - self._last_purge_at >= timedelta(hours=1):
            self.purge_expired(now=now)
