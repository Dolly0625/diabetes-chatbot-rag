from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SystemRiskClassification(BaseModel):
    """系統安全分流，不是診斷或完整臨床檢傷。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["NO_DEFINED_SIGNAL", "RED_FLAG"]
    signals: list[str] = Field(default_factory=list, max_length=32)
    action: Literal["CONTINUE_BOUNDED_WORKFLOW", "URGENT_HUMAN"]
    basis: Literal["explicit_user_report", "no_defined_signal_detected"]
    limitations: str = Field(min_length=1)
