from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tfda_context_gate.agent import AGENT_LIMITS, AgentLimits, AgentPlanner, QueryRewriter
from tfda_context_gate.a_router.schemas import RequestContext
from tfda_context_gate.b_context_gate.gate import ContextGate, DeterministicContextGate
from tfda_context_gate.c_generator.workflow_adapter import (
    CGenerator,
    DeterministicFixtureCGenerator,
)
from tfda_context_gate.d_output_gate.verifier import SemanticVerifier
from tfda_context_gate.e_observability import TraceRecorder
from tfda_context_gate.e_observability.sinks import TraceSink
from tfda_context_gate.query_expansion import IdentityQueryExpander, QueryExpander
from tfda_context_gate.rag import FixtureRetriever, Retriever

from .fallbacks import fallback_response
from .graph import WorkflowState, build_workflow_graph
from .schemas import WorkflowResult


def _dump(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _finish(
    trace: TraceRecorder,
    *,
    request_id: str,
    state: WorkflowState,
    status: str,
    final_response: str,
    fallback_reason: str | None,
) -> WorkflowResult:
    decision = state.get("agent_decision")
    if status == "FALLBACK":
        trace.record(
            "FALLBACK",
            "termination",
            "FALLBACK",
            agent_action=decision.action if decision is not None else None,
            reason_codes=[fallback_reason] if fallback_reason else [],
            fallback_reason=fallback_reason,
            termination_reason=state.get("termination_reason"),
        )
    trace.record_evaluation(
        actual_decision="PASS" if status == "COMPLETED" else status,
        outcome=status,
        failure_type=None if status == "COMPLETED" else status,
        reason_codes=[fallback_reason] if fallback_reason else [],
        metadata={"source": "workflow.run_workflow", "orchestration": "langgraph"},
    )
    system_status = (
        "BLOCKED"
        if status == "BLOCKED"
        else "NEEDS_CLARIFICATION"
        if status == "NEEDS_CLARIFICATION"
        else "COMPLETED"
    )
    trace.close(
        status=system_status,
        decision="PASS" if status == "COMPLETED" else status,
        outcome=status,
        fallback_reason=fallback_reason,
    )
    attempts = [item.model_dump(mode="json") for item in state.get("previous_attempts", [])]
    return WorkflowResult(
        request_id=request_id,
        status=status,
        final_response=final_response,
        fallback_reason=fallback_reason,
        a_result=_dump(state.get("a_result")),
        query_expansion=_dump(state.get("query_expansion")),
        rag_result=_dump(state.get("rag_result")),
        b_result=_dump(state.get("b_result")),
        c_result=_dump(state.get("c_result")),
        d_result=_dump(state.get("d_result")),
        agent_action=decision.action if decision is not None else None,
        agent_reason_code=state.get("agent_reason_code"),
        question=state.get("question"),
        current_query=state.get("current_query"),
        execution_history=attempts,
        agent_steps=state.get("agent_steps", 0),
        rewrite_count=state.get("rewrite_count", 0),
        clarification_count=state.get("clarification_count", 0),
        termination_reason=state.get("termination_reason"),
        trace=trace.snapshot(),
    )


def _request_metadata(request: Any) -> tuple[str, str | None, str | None]:
    if isinstance(request, Mapping):
        return (
            str(request.get("request_id", "unknown")),
            request.get("declared_role"),
            request.get("user_raw_input"),
        )
    return "unknown", None, None


def run_workflow(
    request: RequestContext | dict[str, Any],
    *,
    prompt_injection_guard: Any | None = None,
    query_expander: QueryExpander | None = None,
    retriever: Retriever | None = None,
    context_gate: ContextGate | None = None,
    generator: CGenerator | None = None,
    verifier: SemanticVerifier | None = None,
    trace_sink: TraceSink | None = None,
    agent_planner: AgentPlanner | None = None,
    query_rewriter: QueryRewriter | None = None,
    agent_limits: AgentLimits = AGENT_LIMITS,
) -> WorkflowResult:
    """Run the A-E baseline or bounded Agent LangGraph.

    With no ``agent_planner`` this preserves the original deterministic
    baseline: B INSUFFICIENT ends in fallback. Supplying a Planner enables the
    LangGraph recovery branch; the Planner still only returns AgentDecision.
    """

    request_id, declared_role, original_query = _request_metadata(request)
    try:
        request_context = RequestContext.model_validate(request)
        request_id = request_context.request_id
        declared_role = request_context.declared_role.value
        original_query = request_context.user_raw_input
    except Exception as exc:
        trace = TraceRecorder(
            request_id,
            declared_role=str(declared_role) if declared_role else None,
            original_query=original_query,
            sink=trace_sink,
        )
        trace.record_failure(
            "SYSTEM",
            "workflow",
            failure_type="SCHEMA",
            status="ERROR",
            reason_codes=["REQUEST_SCHEMA_INVALID"],
            fallback_reason="SYSTEM_DEPENDENCY",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return _finish(
            trace,
            request_id=request_id,
            state={"current_query": original_query or "invalid"},
            status="FALLBACK",
            final_response=fallback_response("SYSTEM_DEPENDENCY"),
            fallback_reason="SYSTEM_DEPENDENCY",
        )

    trace = TraceRecorder(
        request_context.request_id,
        declared_role=request_context.declared_role.value,
        original_query=request_context.user_raw_input,
        sink=trace_sink,
    )
    query_expander = query_expander or IdentityQueryExpander()
    retriever = retriever or FixtureRetriever()
    context_gate = context_gate or DeterministicContextGate()
    generator = generator or DeterministicFixtureCGenerator()
    state: WorkflowState = {
        "request_context": request_context,
        "request_id": request_context.request_id,
        "original_query": request_context.user_raw_input,
        "current_query": request_context.user_raw_input,
        "trace": trace,
        "agent_planner": agent_planner,
        "query_rewriter": query_rewriter,
        "agent_limits": agent_limits,
        "previous_attempts": [],
        "pending_agent_action": None,
        "agent_steps": 0,
        "rewrite_count": 0,
        "clarification_count": 0,
        "retrieval_attempt": 0,
        "b_attempt": 0,
        "actions_taken": [],
    }

    try:
        graph, runtime_stage = build_workflow_graph(
            trace=trace,
            query_expander=query_expander,
            retriever=retriever,
            context_gate=context_gate,
            generator=generator,
            verifier=verifier,
            agent_planner=agent_planner,
            query_rewriter=query_rewriter,
            prompt_injection_guard=prompt_injection_guard,
            agent_limits=agent_limits,
        )
        state = graph.invoke(state)
        status = state.get("status") or "FALLBACK"
        final_response = state.get("final_response") or fallback_response("SYSTEM_DEPENDENCY")
        return _finish(
            trace,
            request_id=request_context.request_id,
            state=state,
            status=status,
            final_response=final_response,
            fallback_reason=state.get("fallback_reason"),
        )
    except Exception as exc:
        current_stage = locals().get("runtime_stage", {"current": "SYSTEM"}).get("current", "SYSTEM")
        stage_reason = {
            "A": "A_DEPENDENCY",
            "C": "C_FAILURE",
            "D": "D_FALLBACK",
            "QUERY_REWRITER": "AGENT_FAILURE",
            "AGENT": "AGENT_FAILURE",
        }.get(current_stage, "SYSTEM_DEPENDENCY")
        stage_reason_code = {
            "A": "A_DEPENDENCY_FAILURE",
            "C": "C_GENERATOR_FAILURE",
            "D": "D_WORKFLOW_FAILURE",
            "QUERY_REWRITER": "QUERY_REWRITER_FAILURE",
            "AGENT": "AGENT_FAILURE",
        }.get(current_stage, "WORKFLOW_DEPENDENCY_FAILURE")
        trace.record_failure(
            "SYSTEM",
            "workflow",
            failure_type="DEPENDENCY",
            status="ERROR",
            reason_codes=[stage_reason_code],
            fallback_reason=stage_reason,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return _finish(
            trace,
            request_id=request_context.request_id,
            state=state,
            status="FALLBACK",
            final_response=fallback_response(stage_reason),
            fallback_reason=stage_reason,
        )
