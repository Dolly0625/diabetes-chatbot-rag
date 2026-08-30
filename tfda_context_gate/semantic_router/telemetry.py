"""Telemetry for semantic routing — never logs raw user text."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SemanticRouteObservation:
    """Outcome of one semantic route decision.

    The router must not persist patient data; this object carries only
    coarse signals.  Full user text is never stored — optionally the caller
    may supply ``text_length`` and ``text_hash8`` (first 8 hex of sha256).

    Attributes:
        route: winning label (one of ROUTE_LABELS).
        confidence: top cosine score.
        margin: top_score - second_score.
        latency_ms: wall time for embedding + scoring.
        mode: off|shadow|guarded at decision time.
        degraded: True when fake embedder was used.
        matched_labels: labels whose score >= threshold.
        scores: per-label max cosine (may be empty on failure).
        text_length: optional length of original text.
        text_hash8: optional first 8 hex chars of sha256(text).
    """

    route: str
    confidence: float
    margin: float
    latency_ms: float
    mode: str
    degraded: bool
    matched_labels: tuple[str, ...] = ()
    scores: dict[str, float] = field(default_factory=dict)
    text_length: int | None = None
    text_hash8: str | None = None

    def to_trace_dict(self) -> dict[str, Any]:
        """Return a TraceRecorder-safe dict (no raw text).

        Returns:
            Dict with route, confidence, margin, latency_ms, mode,
            degraded, matched_labels, scores, and optional text_length /
            text_hash8 — never the original utterance.
        """
        return {
            "route": self.route,
            "confidence": self.confidence,
            "margin": self.margin,
            "latency_ms": self.latency_ms,
            "mode": self.mode,
            "degraded": self.degraded,
            "matched_labels": list(self.matched_labels),
            "scores": dict(self.scores),
            **({"text_length": self.text_length} if self.text_length is not None else {}),
            **({"text_hash8": self.text_hash8} if self.text_hash8 is not None else {}),
        }


def hash_text_prefix(text: str, length: int = 8) -> str:
    """Return first ``length`` hex chars of sha256(text).

    Args:
        text: original utterance (not stored).
        length: hex chars to keep.

    Returns:
        Lower-case hex prefix.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:length]
