from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


WORKFLOW_SCHEMA_VERSION = "workflow.v0.1"
WorkflowStatus = Literal["COMPLETED", "BLOCKED", "FALLBACK", "NEEDS_CLARIFICATION"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowResult(StrictModel):
    request_id: str = Field(min_length=1)
    schema_version: str = Field(default=WORKFLOW_SCHEMA_VERSION, min_length=1)
    status: WorkflowStatus
    final_response: str = Field(min_length=1)
    fallback_reason: str | None = None
    a_result: dict[str, Any] | None = None
    query_expansion: dict[str, Any] | None = None
    rag_result: dict[str, Any] | None = None
    b_result: dict[str, Any] | None = None
    c_result: dict[str, Any] | None = None
    d_result: dict[str, Any] | None = None
    agent_action: str | None = None
    agent_reason_code: str | None = None
    question: str | None = None
    current_query: str | None = None
    execution_history: list[dict[str, Any]] = Field(default_factory=list)
    agent_steps: int = Field(default=0, ge=0)
    rewrite_count: int = Field(default=0, ge=0)
    clarification_count: int = Field(default=0, ge=0)
    termination_reason: str | None = None
    trace: dict[str, Any]
