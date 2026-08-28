"""LangGraph orchestration for the bounded Agent v0.1 workflow.

The graph owns execution authority. The Planner only emits a bounded action;
it cannot choose nodes, approve evidence, or change limits.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional, TypedDict

from tfda_context_gate.a_router.router import route_request
from tfda_context_gate.a_router.schemas import AResult, RequestContext
from tfda_context_gate.agent import (
    AGENT_LIMITS,
    AgentDecision,
    AgentLimits,
    AgentPlanner,
    AgentAttempt,
    FallbackDecision,
    PlannerError,
    QueryRewriter,
    build_agent_decision_context,
)
from tfda_context_gate.b_context_gate.gate import ContextGate
from tfda_context_gate.b_context_gate.schemas import CanonicalBResult
from tfda_context_gate.c_generator.schemas import EvidenceAwareV2Answer
from tfda_context_gate.c_generator.workflow_adapter import CGenerator
from tfda_context_gate.d_output_gate.gate import run_output_gate
from tfda_context_gate.d_output_gate.verifier import SemanticVerifier
from tfda_context_gate.e_observability import TraceRecorder
from tfda_context_gate.query_expansion import QueryExpander
from tfda_context_gate.query_expansion.schemas import QueryExpansionInput, QueryExpansionResult
from tfda_context_gate.rag import Retriever
from tfda_context_gate.agent.rewriter import validate_meaning_preserving_rewrite

from .adapters import a_to_query_expansion, b_to_c, c_to_d, rag_to_b
from .fallbacks import fallback_response

try:
    from langgraph.graph import END, START, StateGraph
except ImportError as exc:  # pragma: no cover - exercised only in an incomplete install
    raise RuntimeError("Agent v0.1 requires langgraph") from exc


class WorkflowState(TypedDict, total=False):
    """Internal graph state; never passed wholesale to the Agent Planner."""

    request_context: RequestContext
    request_id: str
    original_query: str
    current_query: str
    a_result: AResult
    query_expansion: QueryExpansionResult
    rag_result: Any
    b_result: CanonicalBResult
    c_result: EvidenceAwareV2Answer
    d_result: Any
    trace: TraceRecorder
    # Protocol objects are runtime dependencies held by graph closures. Keep
    # them out of LangGraph's state-schema type resolution.
    agent_planner: Any
    query_rewriter: Any
    agent_limits: AgentLimits
    # A discriminated Annotated union is validated at the Agent boundary;
    # LangGraph only carries its already-validated instance.
    agent_decision: Any
    previous_attempts: list[AgentAttempt]
    pending_agent_action: Optional[str]
    agent_steps: int
    rewrite_count: int
    clarification_count: int
    retrieval_attempt: int
    b_attempt: int
    actions_taken: list[str]
    agent_reason_code: Optional[str]
    question: Optional[str]
    status: Optional[str]
    final_response: Optional[str]
    fallback_reason: Optional[str]
    termination_reason: Optional[str]


def build_agent_question(missing_information: list[str]) -> str:
    questions = {
        "drug_type": "請問家人目前使用的是哪一類糖尿病藥物？",
        "medication_class": "請問家人目前使用的是哪一類糖尿病藥物？",
        "medicine_name": "請問目前使用的藥物名稱或成分是什麼？",
        "symptom": "請問目前具體有哪些症狀？",
    }
    for field in missing_information:
        if field in questions:
            return questions[field]
    labels = "、".join(missing_information)
    return f"為了縮小可可靠查找的範圍，請補充以下資訊：{labels}。"


def _expand_current_query(
    a_result: AResult,
    *,
    current_query: str,
    query_expander: QueryExpander,
) -> QueryExpansionResult:
    """Call the existing expansion boundary while preserving original_query."""

    if current_query == a_result.user_raw_input:
        return query_expander.expand(a_to_query_expansion(a_result))
    input_value = QueryExpansionInput(
        request_id=a_result.request_id,
        original_query=current_query,
        router_status=a_result.router_status.value,
        intent_tags=[item.value for item in a_result.intent_tags],
        declared_role=a_result.declared_role.value,
        language=a_result.language.value,
    )
    expanded = query_expander.expand(input_value)
    return QueryExpansionResult(
        request_id=expanded.request_id,
        original_query=a_result.user_raw_input,
        retrieval_queries=expanded.retrieval_queries,
        strategy=expanded.strategy,
    )


def _retrieval_outcome(b_result: CanonicalBResult) -> dict[str, object]:
    return {
        "evidence_count": len(b_result.evidence),
        "top_evidence_ids": [item.evidence_id for item in b_result.evidence[:5]],
        "retrieval_queries": list(
            b_result.retrieval_feedback.get("retrieval_queries", [])
            if isinstance(b_result.retrieval_feedback, dict)
            else []
        ),
    }


def _retrieved_evidence_trace(evidence: list[Any]) -> list[dict[str, Any]]:
    """Return compact provenance summaries, never raw document content."""

    return [
        {
            "evidence_id": item.evidence_id,
            "rank": rank,
            "score": item.score,
            "source": item.source,
            "date": item.date,
        }
        for rank, item in enumerate(evidence, start=1)
    ]


def build_workflow_graph(
    *,
    trace: TraceRecorder,
    query_expander: QueryExpander,
    retriever: Retriever,
    context_gate: ContextGate,
    generator: CGenerator,
    verifier: SemanticVerifier | None,
    agent_planner: AgentPlanner | None,
    query_rewriter: QueryRewriter | None,
    prompt_injection_guard: Any | None = None,
    agent_limits: AgentLimits = AGENT_LIMITS,
) -> tuple[Any, dict[str, str]]:
    """Compile the A-E + bounded Agent StateGraph.

    The returned mutable stage marker is used only by the outer error boundary
    to attribute dependency failures; it is not part of Planner context.
    """

    runtime_stage = {"current": "SYSTEM"}

    def stage(name: str) -> None:
        runtime_stage["current"] = name

    def a_node(state: WorkflowState) -> dict[str, Any]:
        stage("A")
        request = state["request_context"]
        with trace.span("A", "input_router") as span:
            result = route_request(
                request,
                prompt_injection_guard=prompt_injection_guard,
            )
            span.set(
                status=(
                    "COMPLETED"
                    if result.rag_allowed
                    else (
                        "FALLBACK"
                        if result.router_status.value == "F_ROUTER_DEPENDENCY"
                        else "BLOCKED"
                    )
                ),
                router_status=result.router_status.value,
                intent_tags=[item.value for item in result.intent_tags],
                risk_flags=[item.value for item in result.risk_flags],
                reason_codes=[item.value for item in result.reason_codes],
                rag_allowed=result.rag_allowed,
                prompt_guard_result="BLOCKED" if not result.rag_allowed else "ALLOWED",
            )
        if not result.rag_allowed:
            reason = (
                "A_DEPENDENCY"
                if result.router_status.value == "F_ROUTER_DEPENDENCY"
                else "A_BLOCKED"
            )
            return {
                "a_result": result,
                "status": "FALLBACK" if reason == "A_DEPENDENCY" else "BLOCKED",
                "final_response": fallback_response(reason),
                "fallback_reason": reason,
                "termination_reason": "A_POLICY_BOUNDARY" if reason == "A_BLOCKED" else "A_DEPENDENCY",
            }
        return {"a_result": result}

    def a_route(state: WorkflowState) -> str:
        return "END" if not state["a_result"].rag_allowed else "QUERY_EXPANSION"

    def query_expansion_node(state: WorkflowState) -> dict[str, Any]:
        stage("QUERY_EXPANSION")
        with trace.span("QUERY_EXPANSION", "query_expansion") as span:
            result = _expand_current_query(
                state["a_result"],
                current_query=state["current_query"],
                query_expander=query_expander,
            )
            span.set(
                retrieval_query=result.retrieval_queries[0],
                reason_codes=[
                    "ORIGINAL_QUERY_PRESERVED"
                    if state["current_query"] == state["original_query"]
                    else "AGENT_REWRITTEN_QUERY"
                ],
            )
        return {"query_expansion": result}

    def rag_node(state: WorkflowState) -> dict[str, Any]:
        stage("RAG")
        attempt = state.get("retrieval_attempt", 0) + 1
        with trace.span("RAG", "retrieval") as span:
            result = retriever.retrieve(state["query_expansion"])
            span.set(
                retrieval_query=state["query_expansion"].retrieval_queries[0],
                retrieved_count=len(result.evidence),
                retrieved_evidence_ids=[item.evidence_id for item in result.evidence],
                retrieved_evidence=_retrieved_evidence_trace(result.evidence),
                retrieval_latency_ms=result.retrieval_latency_ms,
                retrieval_attempt=attempt,
            )
        return {"rag_result": result, "retrieval_attempt": attempt}

    def b_node(state: WorkflowState) -> dict[str, Any]:
        stage("B")
        attempt = state.get("b_attempt", 0) + 1
        with trace.span("B", "context_gate") as span:
            result = context_gate.evaluate(rag_to_b(state["rag_result"]))
            b_status = (
                "COMPLETED"
                if result.decision == "PASS"
                else "INSUFFICIENT"
                if result.decision == "INSUFFICIENT"
                else "FALLBACK"
            )
            span.set(
                status=b_status,
                decision=result.decision,
                approved_evidence_ids=result.approved_evidence_ids,
                approved_evidence_count=len(result.approved_evidence_ids),
                b_attempt=attempt,
                reason_codes=result.reason_codes,
                identified_missing_information=result.identified_missing_information,
                relevance=result.relevance,
                sufficiency=result.sufficiency,
                conflict=result.conflict,
                safety=result.safety,
                step_count=state.get("agent_steps", 0),
                retry_count=state.get("rewrite_count", 0),
                rewrite_count=state.get("rewrite_count", 0),
                clarification_count=state.get("clarification_count", 0),
                actions_taken=state.get("actions_taken", []),
            )
        attempts = list(state.get("previous_attempts", []))
        pending_action = state.get("pending_agent_action")
        if pending_action is not None:
            attempts.append(
                AgentAttempt(
                    query=state["current_query"],
                    completed_agent_action=pending_action,
                    b_decision=result.decision,
                    b_reason_codes=list(result.reason_codes)[:8],
                    retrieval_outcome=_retrieval_outcome(result),
                )
            )
        result_state: dict[str, Any] = {
            "b_result": result,
            "b_attempt": attempt,
            "previous_attempts": attempts,
            "pending_agent_action": None,
        }
        if result.decision == "PASS":
            # Clear an earlier B insufficiency after a successful recovery.
            result_state.update(
                fallback_reason=None,
                termination_reason=None,
            )
        else:
            result_state.update(
                status="FALLBACK",
                final_response=fallback_response(
                    "B_INSUFFICIENT" if result.decision == "INSUFFICIENT" else "B_UNSAFE"
                ),
                fallback_reason=(
                    "B_INSUFFICIENT" if result.decision == "INSUFFICIENT" else "B_UNSAFE"
                ),
                termination_reason=(
                    "B_INSUFFICIENT" if result.decision == "INSUFFICIENT" else "B_NON_RECOVERABLE"
                ),
            )
        return result_state

    def b_route(state: WorkflowState) -> str:
        result = state["b_result"]
        if result.decision == "PASS":
            return "C"
        if result.decision == "INSUFFICIENT" and agent_planner is not None:
            return "AGENT_PLANNER"
        return "END"

    def planner_node(state: WorkflowState) -> dict[str, Any]:
        stage("AGENT")
        steps = state.get("agent_steps", 0)
        actions = list(state.get("actions_taken", []))
        if steps >= agent_limits.max_agent_steps:
            decision: AgentDecision = FallbackDecision(
                action="FALLBACK", reason_code="LIMIT_EXCEEDED"
            )
            with trace.span("AGENT", "planner") as span:
                span.set(
                    status="FALLBACK",
                    agent_action=decision.action,
                    requested_action=decision.action,
                    reason_codes=[decision.reason_code],
                    reason_code=decision.reason_code,
                    requested_reason_code=decision.reason_code,
                    agent_step=steps,
                    step_count=steps,
                    retry_count=state.get("rewrite_count", 0),
                    rewrite_count=state.get("rewrite_count", 0),
                    clarification_count=state.get("clarification_count", 0),
                    actions_taken=actions,
                    termination_reason="MAX_AGENT_STEPS_EXCEEDED",
                    model_name=getattr(agent_planner, "model_name", getattr(agent_planner, "name", None)),
                )
            return {
                "agent_decision": decision,
                "agent_reason_code": decision.reason_code,
                "status": "FALLBACK",
                "final_response": fallback_response("B_INSUFFICIENT"),
                "fallback_reason": "AGENT_BOUNDED_FALLBACK",
                "termination_reason": "MAX_AGENT_STEPS_EXCEEDED",
                "agent_steps": steps,
            }

        context = build_agent_decision_context(
            original_query=state["original_query"],
            current_query=state["current_query"],
            b_result=state["b_result"],
            previous_attempts=state.get("previous_attempts", []),
        )
        planner_context = context.model_dump(mode="json")
        steps += 1
        try:
            decision = agent_planner.decide(context)  # type: ignore[union-attr]
            action = decision.action
            reason_code = decision.reason_code
            bounded = False
            termination_reason = None
            if action == "REWRITE_QUERY" and state.get("rewrite_count", 0) >= agent_limits.max_rewrites:
                decision = FallbackDecision(action="FALLBACK", reason_code="LIMIT_EXCEEDED")
                bounded = True
                termination_reason = "MAX_REWRITES_EXCEEDED"
            elif action == "ASK_USER" and state.get("clarification_count", 0) >= agent_limits.max_clarifications:
                decision = FallbackDecision(action="FALLBACK", reason_code="LIMIT_EXCEEDED")
                bounded = True
                termination_reason = "MAX_CLARIFICATIONS_EXCEEDED"
            if not bounded:
                termination_reason = (
                    "AGENT_SELECTED_FALLBACK"
                    if decision.action == "FALLBACK"
                    else "ACTION_SELECTED"
                )
            with trace.span("AGENT", "planner") as span:
                span.set(
                    status="FALLBACK" if bounded else "COMPLETED",
                    agent_action=decision.action,
                    requested_action=action,
                    reason_codes=[decision.reason_code],
                    reason_code=decision.reason_code,
                    requested_reason_code=reason_code,
                    agent_step=steps,
                    step_count=steps,
                    retry_count=state.get("rewrite_count", 0),
                    rewrite_count=state.get("rewrite_count", 0),
                    clarification_count=state.get("clarification_count", 0),
                    actions_taken=actions + [decision.action],
                    identified_missing_information=context.identified_missing_information,
                    planner_context=planner_context,
                    termination_reason=termination_reason,
                    model_name=getattr(agent_planner, "model_name", getattr(agent_planner, "name", None)),
                )
            actions.append(decision.action)
            result: dict[str, Any] = {
                "agent_decision": decision,
                "agent_reason_code": decision.reason_code,
                "agent_steps": steps,
                "actions_taken": actions,
            }
            if decision.action == "FALLBACK":
                result.update(
                    status="FALLBACK",
                    final_response=fallback_response("B_INSUFFICIENT"),
                    fallback_reason=(
                        "AGENT_SELECTED_FALLBACK"
                        if termination_reason == "AGENT_SELECTED_FALLBACK"
                        else "AGENT_BOUNDED_FALLBACK"
                    ),
                    termination_reason=termination_reason or "PLANNER_FALLBACK",
                )
            return result
        except Exception as exc:
            # A malformed/failed Planner cannot control execution. Fail closed.
            with trace.span("AGENT", "planner") as span:
                span.set(
                    status="ERROR",
                    agent_action="FALLBACK",
                    requested_action=None,
                    reason_codes=["PLANNER_FAILURE"],
                    reason_code="PLANNER_FAILURE",
                    agent_step=steps,
                    step_count=steps,
                    retry_count=state.get("rewrite_count", 0),
                    rewrite_count=state.get("rewrite_count", 0),
                    clarification_count=state.get("clarification_count", 0),
                    actions_taken=actions,
                    identified_missing_information=context.identified_missing_information,
                    planner_context=planner_context,
                    termination_reason="PLANNER_FAILURE",
                    error_type=type(exc).__name__,
                    error_message="Planner invocation or schema validation failed",
                    model_name=getattr(agent_planner, "model_name", getattr(agent_planner, "name", None)),
                )
            decision = FallbackDecision(action="FALLBACK", reason_code="PLANNER_FAILURE")
            return {
                "agent_decision": decision,
                "agent_reason_code": decision.reason_code,
                "agent_steps": steps,
                "actions_taken": actions + ["FALLBACK"],
                "status": "FALLBACK",
                "final_response": fallback_response("B_INSUFFICIENT"),
                "fallback_reason": "AGENT_FAILURE",
                "termination_reason": "PLANNER_FAILURE",
            }

    def agent_route(state: WorkflowState) -> str:
        action = state["agent_decision"].action
        if action == "ASK_USER":
            return "ASK_USER"
        if action == "REWRITE_QUERY":
            return "QUERY_REWRITER"
        return "END"

    def ask_user_node(state: WorkflowState) -> dict[str, Any]:
        stage("AGENT")
        decision = state["agent_decision"]
        with trace.span("ASK_USER", "question_builder") as span:
            question = build_agent_question(decision.missing_information)  # type: ignore[union-attr]
            span.set(
                status="NEEDS_CLARIFICATION",
                agent_action="ASK_USER",
                reason_codes=[decision.reason_code],
                reason_code=decision.reason_code,
                missing_information=decision.missing_information,
                question=question,
                agent_step=state.get("agent_steps", 0),
                step_count=state.get("agent_steps", 0),
                retry_count=state.get("rewrite_count", 0),
                rewrite_count=state.get("rewrite_count", 0),
                clarification_count=state.get("clarification_count", 0) + 1,
                actions_taken=state.get("actions_taken", []),
                termination_reason="NEEDS_CLARIFICATION",
            )
        return {
            "question": question,
            "clarification_count": state.get("clarification_count", 0) + 1,
            "status": "NEEDS_CLARIFICATION",
            "final_response": question,
            "fallback_reason": None,
            "termination_reason": "NEEDS_CLARIFICATION",
        }

    def rewrite_node(state: WorkflowState) -> dict[str, Any]:
        stage("QUERY_REWRITER")
        if query_rewriter is None:
            raise RuntimeError("REWRITE_QUERY selected but no QueryRewriter was configured")
        with trace.span("QUERY_REWRITER", "query_rewriter") as span:
            rewritten = query_rewriter.rewrite(
                original_query=state["original_query"],
                current_query=state["current_query"],
            )
            rewritten_query = rewritten.rewritten_query.strip()
            if not rewritten_query:
                raise RuntimeError("QueryRewriter returned an empty query")
            validate_meaning_preserving_rewrite(state["original_query"], rewritten_query)
            span.set(
                status="COMPLETED",
                retrieval_query=rewritten_query,
                current_query=state["current_query"],
                rewritten_query=rewritten_query,
                rewrite_attempt=state.get("rewrite_count", 0) + 1,
                reason_codes=["MEANING_PRESERVING_REWRITE"],
                step_count=state.get("agent_steps", 0),
                retry_count=state.get("rewrite_count", 0) + 1,
                actions_taken=state.get("actions_taken", []),
                termination_reason="REENTER_RAG_B",
                model_name=getattr(query_rewriter, "model_name", getattr(query_rewriter, "name", None)),
            )
        return {
            "current_query": rewritten_query,
            "rewrite_count": state.get("rewrite_count", 0) + 1,
            "pending_agent_action": "REWRITE_QUERY",
        }

    def c_node(state: WorkflowState) -> dict[str, Any]:
        stage("C")
        with trace.span("C", "generator") as span:
            result = EvidenceAwareV2Answer.model_validate(
                generator.generate(b_to_c(state["b_result"], original_query=state["original_query"]))
            )
            span.set(
                candidate_decision=result.decision,
                claim_count=len(result.supported_claims),
                evidence_ids=[
                    evidence_id
                    for claim in result.supported_claims
                    for evidence_id in claim.evidence_ids
                ],
            )
        return {"c_result": result}

    def d_node(state: WorkflowState) -> dict[str, Any]:
        stage("D")
        with trace.span("D", "output_gate") as span:
            result = run_output_gate(
                c_to_d(
                    request_id=state["request_context"].request_id,
                    a_result=state["a_result"],
                    b_result=state["b_result"],
                    c_result=state["c_result"],
                ),
                verifier=verifier,
            )
            span.set(
                status="COMPLETED" if result.decision == "PASS" else "FALLBACK",
                decision=result.decision,
                failure_type=result.failure_type,
                reason_codes=result.reason_codes,
                failed_claims=[claim.model_dump(mode="json") for claim in result.failed_claims],
                invalid_evidence_ids=result.invalid_evidence_ids,
                fallback_reason=None if result.decision == "PASS" else "D_FALLBACK",
            )
        if result.decision == "PASS":
            return {"d_result": result, "status": "COMPLETED", "final_response": result.final_response}
        return {
            "d_result": result,
            "status": "FALLBACK",
            "final_response": result.final_response,
            "fallback_reason": "D_FALLBACK",
            "termination_reason": "D_FALLBACK",
        }

    graph = StateGraph(WorkflowState)
    graph.add_node("A", a_node)
    graph.add_node("QUERY_EXPANSION", query_expansion_node)
    graph.add_node("RAG", rag_node)
    graph.add_node("B", b_node)
    graph.add_node("AGENT_PLANNER", planner_node)
    graph.add_node("ASK_USER", ask_user_node)
    graph.add_node("QUERY_REWRITER", rewrite_node)
    graph.add_node("C", c_node)
    graph.add_node("D", d_node)
    graph.add_edge(START, "A")
    graph.add_conditional_edges("A", a_route, {"QUERY_EXPANSION": "QUERY_EXPANSION", "END": END})
    graph.add_edge("QUERY_EXPANSION", "RAG")
    graph.add_edge("RAG", "B")
    graph.add_conditional_edges(
        "B",
        b_route,
        {"C": "C", "AGENT_PLANNER": "AGENT_PLANNER", "END": END},
    )
    graph.add_conditional_edges(
        "AGENT_PLANNER",
        agent_route,
        {"ASK_USER": "ASK_USER", "QUERY_REWRITER": "QUERY_REWRITER", "END": END},
    )
    graph.add_edge("ASK_USER", END)
    graph.add_edge("QUERY_REWRITER", "QUERY_EXPANSION")
    graph.add_edge("C", "D")
    graph.add_edge("D", END)
    return graph.compile(), runtime_stage
