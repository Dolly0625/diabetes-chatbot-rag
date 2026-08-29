"""v0.2 Tool Executor — timeout, allowlist, E trace, never bypass B/C/D.

Position: workflow (deterministic) → ToolExecutor.execute(ToolRequest) → ToolResult
          → B gate (rag_to_b_input) → C → D

Invariants:
- Allowlist check before execution (tool_name + source_id).
- Timeout enforced via ThreadPoolExecutor; on timeout returns ToolResult status=ERROR.
- E trace recorded via TraceRecorder.span("TOOL", tool_name) if trace provided.
- Never bypasses B/C/D: returns candidate_evidence only; caller must feed to B.
- Fail-closed: any exception → ToolResult ERROR, never raises to workflow.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .registry import ToolRegistry
from .schemas import ToolError, ToolRequest, ToolResult
from tfda_context_gate.e_observability.deadline import run_with_deadline


class ToolExecutor:
    """Allowlist + timeout + trace wrapper for tool execution (v0.2).

    - Registry owns allowlist; executor enforces it.
    - Timeout is per-call (default 5000ms).
    - Trace integration is optional but recommended (E observability).
    - All evidence is candidate_evidence for B gate.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        timeout_ms: float = 5000,
        trace: Optional[Any] = None,
    ) -> None:
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be > 0")
        self.registry = registry
        self.timeout_ms = float(timeout_ms)
        self.trace = trace

    def _record_tool_span(
        self,
        request: ToolRequest,
        result: ToolResult,
        *,
        started_at: Optional[Any] = None,
    ) -> None:
        if self.trace is None:
            return
        try:
            if hasattr(self.trace, "span"):
                with self.trace.span("TOOL", request.tool_name) as span:
                    span.set(
                        status="COMPLETED" if result.status != "ERROR" else "ERROR",
                        tool_name=result.tool_name,
                        decision=result.status,
                        reason_codes=[result.status] + ([result.source_id] if result.source_id else []) + ([result.error.code] if result.error else []),
                        retrieved_count=len(result.candidate_evidence),
                        retrieved_evidence_ids=[e.evidence_id for e in result.candidate_evidence[:5]],
                        retrieval_query=result.retrieval_queries[0] if result.retrieval_queries else request.params.query,  # type: ignore[union-attr]
                        retrieval_latency_ms=result.latency_ms,
                        latency_ms=result.latency_ms,
                        error_type=result.error.error_type if result.error and result.error.error_type else (result.error.code if result.error else None),
                        error_message=result.error.message if result.error else None,
                        termination_reason=result.task_type,
                    )
            else:
                self.trace.record(
                    "TOOL",
                    request.tool_name,
                    "COMPLETED" if result.status != "ERROR" else "ERROR",
                    tool_name=result.tool_name,
                    decision=result.status,
                    reason_codes=[result.status],
                    latency_ms=result.latency_ms,
                )
        except Exception:
            pass

    def execute(self, request: ToolRequest) -> ToolResult:
        """Execute a ToolRequest with allowlist, timeout, and trace.

        Args:
            request: validated ToolRequest (tool_name, request_id, params {source_id, query, filters}, task_type)
        Returns:
            ToolResult with status SUCCESS/EMPTY/PARTIAL/STALE/CONFLICT/ERROR,
            candidate_evidence for B gate, latency_ms, and error if any.
            Never raises; always returns a ToolResult.
        """
        started = time.perf_counter()

        # ── 1. Allowlist: tool_name ──
        if not self.registry.is_allowed(request.tool_name):
            latency_ms = (time.perf_counter() - started) * 1000
            result = ToolResult(
                tool_name=request.tool_name,
                request_id=request.request_id,
                status="ERROR",
                candidate_evidence=[],
                retrieval_queries=[request.params.query] if request.params else [],
                latency_ms=latency_ms,
                error=ToolError(
                    code="TOOL_NOT_ALLOWLISTED",
                    message=f"tool not allowlisted: {request.tool_name!r}",
                    details={"allowed": sorted(self.registry.allowed_tools)},
                ),
                source_id=getattr(request.params, "source_id", None) if hasattr(request, "params") else None,
                task_type=request.task_type,
            )
            self._record_tool_span(request, result)
            return result

        # ── 2. Allowlist: source_id ──
        try:
            self.registry.validate_source_id(request.params.source_id)  # type: ignore[union-attr]
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            result = ToolResult(
                tool_name=request.tool_name,
                request_id=request.request_id,
                status="ERROR",
                candidate_evidence=[],
                retrieval_queries=[request.params.query],
                latency_ms=latency_ms,
                error=ToolError(
                    code="SOURCE_NOT_ALLOWLISTED",
                    message=str(exc),
                    error_type=type(exc).__name__,
                    details={"source_id": request.params.source_id},  # type: ignore[union-attr]
                ),
                source_id=None,
                task_type=request.task_type,
            )
            self._record_tool_span(request, result)
            return result

        # ── 3. Lookup tool ──
        tool = self.registry.get(request.tool_name)
        if tool is None:
            latency_ms = (time.perf_counter() - started) * 1000
            result = ToolResult(
                tool_name=request.tool_name,
                request_id=request.request_id,
                status="ERROR",
                candidate_evidence=[],
                retrieval_queries=[request.params.query],
                latency_ms=latency_ms,
                error=ToolError(
                    code="TOOL_NOT_REGISTERED",
                    message=f"tool not registered: {request.tool_name!r}",
                ),
                source_id=request.params.source_id,  # type: ignore[union-attr]
                task_type=request.task_type,
            )
            self._record_tool_span(request, result)
            return result

        # ── 4. Execute with timeout ──
        def _call() -> ToolResult:
            # Tool's execute returns ToolResult directly
            return tool.execute(request.params, request_id=request.request_id)  # type: ignore[union-attr]

        try:
            # Do not use a per-call executor context manager here: on timeout
            # its ``__exit__`` waits for the blocking tool and defeats the
            # caller's wall-clock bound.  The shared bounded deadline pool
            # retains the worker until it exits and fails closed when full.
            result, timed_out, guard = run_with_deadline(
                _call,
                timeout_s=self.timeout_ms / 1000.0,
            )
            if timed_out or result is None or guard.should_abort():
                raise TimeoutError
            # Ensure latency_ms is set (tool may have set it)
            if result.latency_ms is None:
                result.latency_ms = (time.perf_counter() - started) * 1000
            # Echo task_type if not set by tool
            if result.task_type is None and request.task_type is not None:
                result.task_type = request.task_type
            self._record_tool_span(request, result)
            return result
        except TimeoutError:
            latency_ms = (time.perf_counter() - started) * 1000
            result = ToolResult(
                tool_name=request.tool_name,
                request_id=request.request_id,
                status="ERROR",
                candidate_evidence=[],
                retrieval_queries=[request.params.query],
                latency_ms=latency_ms,
                error=ToolError(
                    code="TOOL_TIMEOUT",
                    message=f"tool execution timed out after {self.timeout_ms}ms",
                    details={"timeout_ms": self.timeout_ms},
                ),
                source_id=request.params.source_id,  # type: ignore[union-attr]
                task_type=request.task_type,
            )
            self._record_tool_span(request, result)
            return result
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            result = ToolResult(
                tool_name=request.tool_name,
                request_id=request.request_id,
                status="ERROR",
                candidate_evidence=[],
                retrieval_queries=[request.params.query],
                latency_ms=latency_ms,
                error=ToolError(
                    code="TOOL_EXECUTION_FAILED",
                    message=str(exc)[:500],
                    error_type=type(exc).__name__,
                ),
                source_id=request.params.source_id,  # type: ignore[union-attr]
                task_type=request.task_type,
            )
            self._record_tool_span(request, result)
            return result

    def execute_simple(
        self,
        *,
        request_id: str,
        source_id: str,
        query: str,
        filters: Optional[dict[str, Any]] = None,
        task_type: Optional[str] = None,
        tool_name: str = "EvidenceRetrievalTool",
    ) -> ToolResult:
        """Convenience wrapper: EvidenceRetrievalTool(source_id, query, filters).

        This is the v0.2 ergonomic API described in proposal p5.4:
          EvidenceRetrievalTool(source_id, query, filters) → ToolResult

        Args:
            request_id: correlation id
            source_id: TFDA_RISK | HPA_DIET_GUIDE
            query: retrieval query
            filters: optional filters dict
            task_type: optional task type
            tool_name: tool name (default EvidenceRetrievalTool)
        Returns:
            ToolResult (candidate_evidence for B gate)
        """
        from .schemas import ToolRequestParams

        params = ToolRequestParams(source_id=source_id, query=query, filters=filters or {})  # type: ignore[arg-type]
        req = ToolRequest(tool_name=tool_name, request_id=request_id, params=params, task_type=task_type)  # type: ignore[arg-type]
        return self.execute(req)
