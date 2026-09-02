from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tfda_context_gate.access_control import InformationSource


class ShareGrantIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str
    token: str = Field(min_length=32)
    expires_at: datetime
    single_use: bool


class ClinicianSharedSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    intake_snapshot: dict[str, Any]
    previsit_summary: dict[str, Any]
    output_gate_result: dict[str, Any]
    system_risk_classification: dict[str, Any] | None = None
    information_source: InformationSource | None = None
    intake_field_provenance: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime
    accessed_at: datetime
