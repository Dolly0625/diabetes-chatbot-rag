"""v0.2 Tool Registry — allowlist, source_id enum, EvidenceRetrievalTool wrapper.

Proposal p5.4 / stage 1:
  EvidenceRetrievalTool(source_id, query, filters) → candidate evidence[]
  Registry owns allowlist; Executor owns timeout/trace; Tool never bypasses B.

Allowlist (v0.2):
  - tool_name: EvidenceRetrievalTool only
  - source_id: TFDA_RISK | HPA_DIET_GUIDE

EvidenceRetrievalTool delegates to:
  - TFDA_RISK → TFDADrugSafetyRetriever (or FixtureRetriever fallback)
  - HPA_DIET_GUIDE → HPA diet guide retriever (fixture-backed until corpus ready)
"""

from __future__ import annotations

import time
from typing import Any, Optional, Protocol

from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult

from .schemas import (
    ALLOWED_SOURCE_IDS,
    CanonicalObservation,
    SourceId,
    ToolError,
    ToolRequest,
    ToolRequestParams,
    ToolResult,
)

# ── Allowlist constants ──
ALLOWED_TOOL_NAMES: set[str] = {"EvidenceRetrievalTool"}
TOOL_NAME_EVIDENCE_RETRIEVAL = "EvidenceRetrievalTool"


class Tool(Protocol):
    """Tool protocol: any registered tool must implement execute."""

    name: str

    def execute(self, params: ToolRequestParams, *, request_id: str) -> ToolResult:
        ...


class RegistryError(ValueError):
    """Raised when allowlist or registration invariant is violated."""

    pass


class ToolRegistry:
    """Allowlist-enforced tool registry (v0.2).

    - Only allowlisted tool_name and source_id can be registered/executed.
    - Duplicate registration is rejected.
    - get() returns None for unknown tools (executor maps to ERROR).
    """

    def __init__(self, *, allowed_tools: Optional[set[str]] = None) -> None:
        self._tools: dict[str, Any] = {}
        self._allowed = set(allowed_tools) if allowed_tools is not None else set(ALLOWED_TOOL_NAMES)

    @property
    def allowed_tools(self) -> set[str]:
        return set(self._allowed)

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in self._allowed

    def validate_source_id(self, source_id: str) -> None:
        if source_id not in ALLOWED_SOURCE_IDS:
            raise RegistryError(f"source_id not allowlisted: {source_id!r}; allowed={sorted(ALLOWED_SOURCE_IDS)}")

    def register(self, tool: Any) -> None:
        name = getattr(tool, "name", None)
        if not name or not isinstance(name, str):
            raise RegistryError("tool must have a non-empty string attribute 'name'")
        if name not in self._allowed:
            raise RegistryError(f"tool not allowlisted: {name!r}; allowed={sorted(self._allowed)}")
        if name in self._tools:
            raise RegistryError(f"duplicate tool registration: {name!r}")
        self._tools[name] = tool

    def get(self, tool_name: str) -> Optional[Any]:
        return self._tools.get(tool_name)

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def list_allowed(self) -> list[str]:
        return sorted(self._allowed)


# ── EvidenceRetrievalTool (data-source-neutral adapter) ──

# HPA diet guide fixture evidence (until real HPA corpus is curated).
# Kept minimal and clearly marked as fixture; B gate will filter via fixture_b_approved.
_HPA_FIXTURE_EVIDENCE: list[CanonicalEvidence] = [
    CanonicalEvidence(
        evidence_id="HPA-DIET-001",
        content="糖尿病飲食建議：均衡飲食、控制總熱量、適量全穀與蔬菜，依個人狀況諮詢醫療專業人員。",
        source="HPA_DIET_GUIDE",
        metadata={"source_id": "HPA_DIET_GUIDE", "fixture_b_approved": True, "source_dataset": "HPA_DIET_GUIDE"},
        score=0.85,
        date="2024-01-01",
        version="v0.2-fixture",
    ),
    CanonicalEvidence(
        evidence_id="HPA-DIET-002",
        content="國民健康署建議：糖尿病患者應定期監測血糖，並配合醫囑調整飲食與運動計畫。",
        source="HPA_DIET_GUIDE",
        metadata={"source_id": "HPA_DIET_GUIDE", "fixture_b_approved": True, "source_dataset": "HPA_DIET_GUIDE"},
        score=0.80,
        date="2024-01-01",
        version="v0.2-fixture",
    ),
]


class EvidenceRetrievalTool:
    """Data-source-neutral evidence retrieval adapter (v0.2).

    Signature (proposal p5.4):
      EvidenceRetrievalTool(source_id, query, filters)
        → status, retrieval_queries, candidate_evidence[], source/date/score/metadata, latency/error

    Delegation:
      - TFDA_RISK → TFDADrugSafetyRetriever (or injected retriever)
      - HPA_DIET_GUIDE → HPA retriever (fixture-backed until corpus ready)

    All returned evidence is candidate_evidence for B gate; never pre-approved.
    """

    name = TOOL_NAME_EVIDENCE_RETRIEVAL
    description = "Data-source-neutral evidence retrieval (TFDA_RISK / HPA_DIET_GUIDE) → candidate evidence for B gate"

    def __init__(
        self,
        *,
        tfda_retriever: Optional[Any] = None,
        hpa_retriever: Optional[Any] = None,
        top_k: int = 5,
    ) -> None:
        """Initialize adapter with optional injected retrievers.

        Args:
            tfda_retriever: retriever for TFDA_RISK (must have retrieve(QueryExpansionResult) -> RAGResult)
            hpa_retriever: retriever for HPA_DIET_GUIDE (same protocol); if None, uses HPA fixture
            top_k: top-k for fixture fallback (ignored if retriever has its own top_k)
        """
        self.tfda_retriever = tfda_retriever
        self.hpa_retriever = hpa_retriever
        self.top_k = top_k

    def _get_retriever(self, source_id: SourceId) -> Optional[Any]:
        if source_id == "TFDA_RISK":
            return self.tfda_retriever
        if source_id == "HPA_DIET_GUIDE":
            return self.hpa_retriever
        return None

    def _hpa_fixture_retrieve(self, params: ToolRequestParams, *, request_id: str) -> ToolResult:
        """HPA fixture path: keyword filter over _HPA_FIXTURE_EVIDENCE."""
        started = time.perf_counter()
        query = params.query.strip()
        filters = params.filters or {}
        # Simple keyword match; if no match, return all fixture (deterministic)
        candidates = list(_HPA_FIXTURE_EVIDENCE)
        if query:
            lowered = query.lower()
            matched = [e for e in candidates if lowered in e.content.lower() or lowered in e.evidence_id.lower()]
            if matched:
                candidates = matched
        # Apply optional filters (e.g. evidence_id allowlist)
        if isinstance(filters, dict) and filters.get("evidence_ids"):
            allow = set(str(x) for x in filters["evidence_ids"])
            candidates = [e for e in candidates if e.evidence_id in allow]
        latency_ms = (time.perf_counter() - started) * 1000
        # Build observations for ledger
        observations = [
            CanonicalObservation(
                observation_id=e.evidence_id,
                content=e.content,
                source=e.source,
                source_id="HPA_DIET_GUIDE",
                tool_name=self.name,
                metadata=dict(e.metadata),
                score=e.score,
                date=e.date,
                version=e.version,
                retrieval_queries=[query],
            )
            for e in candidates
        ]
        status: str = "SUCCESS" if candidates else "EMPTY"
        return ToolResult(
            tool_name=self.name,
            request_id=request_id,
            status=status,  # type: ignore[arg-type]
            candidate_evidence=candidates,
            retrieval_queries=[query],
            latency_ms=latency_ms,
            error=None,
            source_id="HPA_DIET_GUIDE",
            observations=observations,
        )

    def execute(self, params: ToolRequestParams, *, request_id: str) -> ToolResult:
        """Execute retrieval for given params.

        Args:
            params: validated ToolRequestParams (source_id, query, filters)
            request_id: correlation id for trace
        Returns:
            ToolResult with candidate_evidence (never bypasses B)
        """
        source_id: SourceId = params.source_id  # type: ignore[assignment]
        query = params.query.strip()
        if not query:
            return ToolResult(
                tool_name=self.name,
                request_id=request_id,
                status="ERROR",
                candidate_evidence=[],
                retrieval_queries=[query],
                latency_ms=0,
                error=ToolError(code="INVALID_QUERY", message="query must be non-empty", details={"source_id": source_id}),
                source_id=source_id,
            )

        # HPA path: fixture-backed if no retriever injected
        if source_id == "HPA_DIET_GUIDE" and self.hpa_retriever is None:
            return self._hpa_fixture_retrieve(params, request_id=request_id)

        retriever = self._get_retriever(source_id)
        if retriever is None:
            # No retriever for this source → try lazy TFDA fallback for TFDA_RISK
            if source_id == "TFDA_RISK":
                try:
                    from tfda_context_gate.rag import FixtureRetriever

                    retriever = FixtureRetriever()
                except Exception as exc:
                    return ToolResult(
                        tool_name=self.name,
                        request_id=request_id,
                        status="ERROR",
                        candidate_evidence=[],
                        retrieval_queries=[query],
                        latency_ms=0,
                        error=ToolError(code="RETRIEVER_UNAVAILABLE", message=str(exc), error_type=type(exc).__name__),
                        source_id=source_id,
                    )
            else:
                return ToolResult(
                    tool_name=self.name,
                    request_id=request_id,
                    status="ERROR",
                    candidate_evidence=[],
                    retrieval_queries=[query],
                    latency_ms=0,
                    error=ToolError(code="RETRIEVER_UNAVAILABLE", message=f"No retriever for source_id={source_id}"),
                    source_id=source_id,
                )

        # Delegate to retriever via QueryExpansionResult (preserves original_query)
        started = time.perf_counter()
        try:
            expansion = QueryExpansionResult(
                request_id=request_id,
                original_query=query,
                retrieval_queries=[query],
                strategy="tool-contract-v0.2",
            )
            rag_result = retriever.retrieve(expansion)
            latency_ms = (time.perf_counter() - started) * 1000
            # Use retriever's latency if available
            if getattr(rag_result, "retrieval_latency_ms", None) is not None:
                latency_ms = float(rag_result.retrieval_latency_ms)  # type: ignore[arg-type]

            candidate = list(rag_result.evidence)
            # Tag source_id into metadata for provenance (B gate透傳)
            for ev in candidate:
                if "source_id" not in ev.metadata:
                    ev.metadata["source_id"] = source_id
                if "tool_name" not in ev.metadata:
                    ev.metadata["tool_name"] = self.name

            observations = [
                CanonicalObservation(
                    observation_id=e.evidence_id,
                    content=e.content,
                    source=e.source,
                    source_id=source_id,
                    tool_name=self.name,
                    metadata=dict(e.metadata),
                    score=e.score,
                    date=e.date,
                    version=e.version,
                    retrieval_queries=list(rag_result.retrieval_queries),
                )
                for e in candidate
            ]

            if not candidate:
                return ToolResult(
                    tool_name=self.name,
                    request_id=request_id,
                    status="EMPTY",
                    candidate_evidence=[],
                    retrieval_queries=list(rag_result.retrieval_queries),
                    latency_ms=latency_ms,
                    error=None,
                    source_id=source_id,
                    observations=observations,
                )

            return ToolResult(
                tool_name=self.name,
                request_id=request_id,
                status="SUCCESS",
                candidate_evidence=candidate,
                retrieval_queries=list(rag_result.retrieval_queries),
                latency_ms=latency_ms,
                error=None,
                source_id=source_id,
                observations=observations,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            return ToolResult(
                tool_name=self.name,
                request_id=request_id,
                status="ERROR",
                candidate_evidence=[],
                retrieval_queries=[query],
                latency_ms=latency_ms,
                error=ToolError(
                    code="TOOL_EXECUTION_FAILED",
                    message=str(exc)[:500],
                    error_type=type(exc).__name__,
                    details={"source_id": source_id},
                ),
                source_id=source_id,
            )


def create_default_registry(
    *,
    tfda_retriever: Optional[Any] = None,
    hpa_retriever: Optional[Any] = None,
) -> ToolRegistry:
    """Create a registry with EvidenceRetrievalTool pre-registered (v0.2 default).

    Args:
        tfda_retriever: optional TFDA retriever to inject (defaults to lazy Fixture)
        hpa_retriever: optional HPA retriever (defaults to fixture)
    Returns:
        ToolRegistry with EvidenceRetrievalTool registered and allowlisted
    """
    registry = ToolRegistry()
    tool = EvidenceRetrievalTool(tfda_retriever=tfda_retriever, hpa_retriever=hpa_retriever)
    registry.register(tool)
    return registry
