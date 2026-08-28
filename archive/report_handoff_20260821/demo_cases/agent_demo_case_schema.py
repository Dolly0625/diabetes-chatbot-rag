from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


AgentAction = Literal["ASK_USER", "REWRITE_QUERY", "FALLBACK"]
Role = Literal["PATIENT", "CAREGIVER", "HEALTHCARE_PROFESSIONAL"]


class AgentDemoCase(BaseModel):
    """Evaluation ground truth only; this is not an Agent runtime contract."""

    model_config = ConfigDict(extra="allow")

    case_id: str = Field(min_length=1)
    role: Role
    user_query: str = Field(min_length=1)
    expected_a_route: str = Field(min_length=1)
    expected_initial_b_decision: str = Field(min_length=1)
    expected_agent_action: AgentAction | None = None
    # B/evaluation observation only; never an action recommendation.
    identified_missing_information: list[str] = Field(default_factory=list, max_length=8)
    expected_evidence_id: str | None = None
    rewritten_query: str | None = None
    simulated_user_reply: str | None = None
    expected_final_outcome: str = Field(min_length=1)
    demo_purpose: str = Field(min_length=1)
    baseline_behavior: str = Field(min_length=1)
    expected_agent_behavior: str = Field(min_length=1)
    recovery_attempts: list[dict[str, Any]] = Field(default_factory=list)


def load_agent_demo_cases(path: str | Path | None = None) -> list[AgentDemoCase]:
    resolved = Path(path) if path is not None else Path(__file__).with_name("agent_demo_cases.json")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("agent demo cases must be a JSON list")
    cases = [AgentDemoCase.model_validate(item) for item in payload]
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("agent demo case IDs must be unique")
    return cases
