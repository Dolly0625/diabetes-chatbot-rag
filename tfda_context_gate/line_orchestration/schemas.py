from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OrchestratorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    reply: str = Field(min_length=1)
    status: str = Field(min_length=1)
    intake_stage: str | None = None
    replayed: bool = False
