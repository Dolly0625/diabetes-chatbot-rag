from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OrchestratorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    reply: str = Field(min_length=1)
    status: str = Field(min_length=1)
    intake_stage: str | None = None
    # Present for smoke/telemetry classification; never contains user text.
    fallback_reason: str | None = None
    replayed: bool = False
    # ── Semantic router observation (never contains raw user text / PII) ──────
    semantic_route: str | None = None
    semantic_confidence: float | None = None
    semantic_margin: float | None = None
    semantic_latency_ms: float | None = None
    semantic_degraded: bool | None = None
    semantic_mode: str | None = None
    # Generic metadata for extensibility when schema is frozen (no PII).
    metadata: dict | None = None
