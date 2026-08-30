"""LINE Messaging API retry-key helpers."""

from __future__ import annotations

import uuid


def make_line_retry_key(seed: str) -> str:
    """Return a stable, RFC 4122-style UUID accepted by LINE.

    ``UUID.hex`` omits hyphens and is rejected by LINE's
    ``X-Line-Retry-Key`` validation.  The canonical string form preserves
    idempotency while matching the documented 36-character representation.
    """

    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
