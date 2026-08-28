from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any


_ASSIGNED_SECRET = re.compile(
    r"(?P<name>\b(?:api[_ -]?key|password|passwd|secret|authorization|access[_ -]?token|token)\b)"
    r"(?P<separator>\s*[:=]\s*|\s+)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(\bBearer\s+)[^\s,;]+", re.IGNORECASE)
_COMMON_API_KEY = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_ -]?key|password|passwd|secret|authorization|access[_ -]?token|token)",
    re.IGNORECASE,
)


def redact_text(value: str) -> str:
    """Redact common credential forms while preserving useful log context."""

    text = str(value)
    text = _ASSIGNED_SECRET.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}[REDACTED]",
        text,
    )
    text = _BEARER.sub(r"\1[REDACTED]", text)
    return _COMMON_API_KEY.sub("[REDACTED]", text)


def hash_text(value: str | None) -> str | None:
    """Return a non-reversible correlation hash for optional raw text."""

    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def sanitize_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively sanitize event payloads before they reach a sink."""

    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(item_key): sanitize_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    return value

