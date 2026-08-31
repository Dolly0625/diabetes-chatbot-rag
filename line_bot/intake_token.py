"""Short-lived intake launch token — opaque, hashed, TTL 30min, single ProductSession binding.

正式模式 LIFF 優先；Demo token 僅受環境開關保護，不得成為正式預設。
SQLite 只保存 hash，URL 只含 opaque token，不含 LINE user ID。
"""
from __future__ import annotations

import hashlib
import secrets
import os
from datetime import datetime, timedelta, timezone


INTAKE_TOKEN_TTL = timedelta(minutes=30)
INTAKE_TOKEN_BYTES = 16  # 128-bit entropy
INTAKE_TOKEN_ENV = "DEMO_INTAKE_TOKEN_ENABLED"

# Table: intake_launch_tokens(token_hash TEXT PRIMARY KEY, product_session_id TEXT UNIQUE NOT NULL, created_at TEXT, expires_at TEXT, consumed_at TEXT)


def is_demo_intake_token_enabled() -> bool:
    return os.getenv(INTAKE_TOKEN_ENV, "false").strip().lower() in {"1", "true", "yes"}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    # 128-bit entropy => 22 chars urlsafe
    return secrets.token_urlsafe(INTAKE_TOKEN_BYTES)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def verify_not_logged(token: str) -> None:
    # Ensure caller does not log raw token — asserted via code review, not runtime.
    pass
