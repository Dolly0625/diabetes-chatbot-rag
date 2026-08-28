from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AgentLimits(BaseModel):
    """System-owned bounds; these values are never exposed to the Planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_agent_steps: int = Field(default=2, ge=1)
    max_rewrites: int = Field(default=1, ge=0)
    max_clarifications: int = Field(default=1, ge=0)


AGENT_LIMITS = AgentLimits()
