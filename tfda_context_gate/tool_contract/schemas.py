"""v0.2 Tool Contract Schemas — ToolRequest / ToolResult / ToolError / CanonicalObservation.

Position in architecture (proposal p5.4 / p5.5):
  workflow (deterministic) → ToolExecutor → ToolResult(candidate_evidence) → B → C → D
  Tool never bypasses B/C/D; all candidate_evidence must go through B gate.

Design notes:
- StrictModel (extra="forbid") keeps contract stable and auditable.
- SourceId enum is data-source-neutral: TFDA_RISK (TFDA drug safety) and
  HPA_DIET_GUIDE (HPA diet guide) are the two v0.2 allowlisted sources.
- ToolRequest carries tool_name, request_id, params {source_id, query, filters}, task_type.
- ToolResult status is 6-valued: SUCCESS/EMPTY/PARTIAL/STALE/CONFLICT/ERROR.
- CanonicalObservation is the raw tool observation; candidate_evidence is
  list[CanonicalEvidence] (b_context_gate 15-field contract) for B gate.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence

TOOL_CONTRACT_VERSION = "tool_contract.v0.2"

# ── SourceId: data-source-neutral allowlist (v0.2) ──
SourceId = Literal["TFDA_RISK", "HPA_DIET_GUIDE"]
ALLOWED_SOURCE_IDS: set[str] = {"TFDA_RISK", "HPA_DIET_GUIDE"}

# ── Tool status: 6-valued (proposal v0.2 stage 1) ──
ToolStatus = Literal["SUCCESS", "EMPTY", "PARTIAL", "STALE", "CONFLICT", "ERROR"]

# ── Task type (v0.2 three flows, optional) ──
TaskType = Literal["patient_education", "pre_visit_intake", "clinician_evidence"]


class StrictModel(BaseModel):
    """Strict model: forbid extra fields to keep tool contract auditable."""

    model_config = ConfigDict(extra="forbid")


class ToolRequestParams(StrictModel):
    """EvidenceRetrievalTool params: source_id, query, filters.

    - source_id: TFDA_RISK | HPA_DIET_GUIDE (allowlisted)
    - query: retrieval query string (min_length=1)
    - filters: optional structured filters (e.g. date range, ingredient)
    """

    source_id: SourceId = Field(description="Allowlisted source identifier")
    query: str = Field(min_length=1, description="Retrieval query")
    filters: dict[str, Any] = Field(default_factory=dict, description="Optional structured filters")


class ToolRequest(StrictModel):
    """Tool invocation request (v0.2 contract).

    Fields:
      tool_name: must be EvidenceRetrievalTool (allowlisted)
      request_id: trace correlation id (min_length=1)
      params: {source_id, query, filters}
      task_type: optional product task type (patient_education / pre_visit_intake / clinician_evidence)
    """

    tool_name: str = Field(default="EvidenceRetrievalTool", min_length=1, description="Allowlisted tool name")
    request_id: str = Field(min_length=1, description="Request correlation id")
    params: ToolRequestParams = Field(description="Tool params {source_id, query, filters}")
    task_type: Optional[Union[TaskType, str]] = Field(default=None, description="Optional task type for role-aware routing")
    schema_version: str = Field(default=TOOL_CONTRACT_VERSION, min_length=1, description="Tool contract version")


class ToolError(StrictModel):
    """Structured tool error (never raises raw exception to caller)."""

    code: str = Field(min_length=1, description="Machine-readable error code")
    message: str = Field(min_length=1, description="Human-readable error message")
    details: dict[str, Any] = Field(default_factory=dict, description="Optional error details")
    error_type: Optional[str] = Field(default=None, description="Original exception type if any")


class CanonicalObservation(StrictModel):
    """Raw tool observation before B gate (candidate observation).

    Observations are NOT approved evidence. They must be normalized to
    CanonicalEvidence and passed through B gate before C/D.

    Fields mirror CanonicalEvidence for easy conversion, plus tool provenance:
      observation_id — unique observation id (maps to evidence_id after B)
      content        — observation text
      source         — source label (e.g. TFDA_RISK / HPA_DIET_GUIDE)
      source_id      — structured source enum
      tool_name      — producing tool
      metadata/score/date/version — provenance
    """

    observation_id: str = Field(min_length=1, description="Unique observation id")
    content: str = Field(min_length=1, description="Observation content")
    source: Optional[str] = Field(default=None, description="Source label")
    source_id: Optional[SourceId] = Field(default=None, description="Structured source id")
    tool_name: Optional[str] = Field(default=None, description="Producing tool name")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    score: Optional[float] = Field(default=None, description="Retrieval score if any")
    date: Optional[str] = Field(default=None, description="Publication date if any")
    version: Optional[str] = Field(default=None, description="Version if any")
    retrieval_queries: list[str] = Field(default_factory=list, description="Queries that produced this observation")

    def to_canonical_evidence(self) -> CanonicalEvidence:
        """Convert observation to CanonicalEvidence for B gate input."""
        return CanonicalEvidence(
            evidence_id=self.observation_id,
            content=self.content,
            source=self.source,
            metadata={
                **self.metadata,
                **({"source_id": self.source_id} if self.source_id else {}),
                **({"tool_name": self.tool_name} if self.tool_name else {}),
            },
            score=self.score,
            date=self.date,
            version=self.version,
        )


class ToolResult(StrictModel):
    """Tool execution result (v0.2 contract).

    All evidence is candidate_evidence (list[CanonicalEvidence]) and must go
    through B gate. Tool never bypasses B/C/D.

    Fields:
      tool_name: echo of request tool_name
      request_id: echo of request request_id
      status: SUCCESS | EMPTY | PARTIAL | STALE | CONFLICT | ERROR
      candidate_evidence: normalized evidence for B gate (never pre-approved)
      retrieval_queries: queries actually executed
      latency_ms: execution latency (ms, >=0)
      error: structured error if status==ERROR else None
      source_id/task_type: echo for trace correlation
    """

    tool_name: str = Field(min_length=1, description="Tool name")
    request_id: str = Field(min_length=1, description="Request correlation id")
    status: ToolStatus = Field(description="6-valued execution status")
    candidate_evidence: list[CanonicalEvidence] = Field(default_factory=list, description="Candidate evidence for B gate")
    retrieval_queries: list[str] = Field(default_factory=list, description="Queries executed")
    latency_ms: Optional[float] = Field(default=None, ge=0, description="Execution latency ms")
    error: Optional[ToolError] = Field(default=None, description="Structured error if status==ERROR")
    source_id: Optional[SourceId] = Field(default=None, description="Echo of source_id")
    task_type: Optional[Union[TaskType, str]] = Field(default=None, description="Echo of task_type")
    schema_version: str = Field(default=TOOL_CONTRACT_VERSION, min_length=1, description="Tool contract version")
    # Optional observation ledger (raw observations before normalization)
    observations: list[CanonicalObservation] = Field(default_factory=list, description="Raw observations (optional)")

    @property
    def is_success(self) -> bool:
        return self.status == "SUCCESS"

    @property
    def is_empty(self) -> bool:
        return self.status == "EMPTY"

    @property
    def is_error(self) -> bool:
        return self.status == "ERROR"


def tool_result_to_rag_result(tool_result: ToolResult, *, original_query: str):
    """Convert ToolResult candidate_evidence to RAGResult for B gate adapter.

    This is the ONLY path from tool to B: ToolResult → RAGResult → rag_to_b_input → B.
    No tool evidence bypasses B.

    Args:
        tool_result: ToolResult with candidate_evidence
        original_query: original user query for provenance (never overwritten)
    Returns:
        RAGResult compatible with rag_to_b_input
    """
    from tfda_context_gate.rag.schemas import RAGResult

    return RAGResult(
        request_id=tool_result.request_id,
        original_query=original_query,
        retrieval_queries=tool_result.retrieval_queries or [original_query],
        evidence=list(tool_result.candidate_evidence),
        retrieval_latency_ms=tool_result.latency_ms,
    )


def tool_result_to_b_input(tool_result: ToolResult, *, original_query: str):
    """Direct ToolResult → CanonicalBInput for B gate (via RAGResult)."""
    from tfda_context_gate.rag.schemas import rag_to_b_input

    rag_result = tool_result_to_rag_result(tool_result, original_query=original_query)
    b_input = rag_to_b_input(rag_result)
    # Preserve tool provenance in tool_context (B gate透傳不判讀, v0.1 compatible)
    b_input.tool_context = {
        "tool_name": tool_result.tool_name,
        "source_id": tool_result.source_id,
        "status": tool_result.status,
        "task_type": tool_result.task_type,
        "latency_ms": tool_result.latency_ms,
        "error": tool_result.error.model_dump(mode="json") if tool_result.error else None,
    }
    return b_input
