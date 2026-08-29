from __future__ import annotations

# ── 工作流程執行器（瘦身版 ≤200 行編排）──────────────────────────────────────
# 職責：組裝依賴、OCR 收斂、formal 工廠、圖執行、錯誤映射與收尾。
# stream_workflow 為薄包裝：呼叫 run_workflow 得 WorkflowResult 後走 buffered_stream_after_d

import concurrent.futures
import os
import time

from collections.abc import Iterator, Mapping
from typing import Any

from tfda_context_gate.e_observability.deadline import deadline_scope_active, run_with_deadline
from tfda_context_gate.e_observability.staged_latency import StagedLatencyRecorder

from tfda_context_gate.agent import AGENT_LIMITS, AgentLimits, AgentPlanner, QueryRewriter
from tfda_context_gate.a_router.schemas import RequestContext
from tfda_context_gate.b_context_gate.gate import ContextGate, DeterministicContextGate
from tfda_context_gate.c_generator.workflow_adapter import CGenerator, DeterministicFixtureCGenerator
from tfda_context_gate.d_output_gate.verifier import SemanticVerifier
from tfda_context_gate.e_observability import TraceRecorder
from tfda_context_gate.e_observability.sinks import TraceSink
from tfda_context_gate.query_expansion import IdentityQueryExpander, QueryExpander
from tfda_context_gate.rag import FixtureRetriever, Retriever

from .fallbacks import fallback_response
from .formal_factory import _build_formal_extractor, _build_formal_generator, _build_formal_retriever
from .graph import WorkflowState, build_workflow_graph
from .ocr_adapter import ImageInput, _merge_ocr_meds_into_intake_data, _process_ocr_images, _sanitize_ocr_meds
from .schemas import WorkflowResult
from .stream import buffered_stream_after_d

FORMAL_WORKFLOW_TIMEOUT_S = float(os.getenv("FORMAL_WORKFLOW_TIMEOUT_S", "45"))

def _is_formal_eligible(request_context: Any, task_type: str | None) -> bool:
    """P3 A+B narrow path: only G general-education with RAG candidate goes formal.

    Fast path (45ms RuleBased+Fixture) for:
    - pre_visit_intake (task_type or rule text)
    - chitchat / capability / out-of-scope
    - vague Q_CLARIFICATION (short/ambiguous)
    - any non-G router_status (M/R/O/F/E/U) — deterministic policy blocks

    Red-flag remains deterministic upstream (graph a_node), so returning False
    here just avoids LLM/retriever instantiation — a_node still ABORTs.
    """
    # Explicit intake task_type — always fast
    if task_type == "pre_visit_intake":
        return False
    raw = getattr(request_context, "user_raw_input", "") or ""
    try:
        from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor
        from tfda_context_gate.workflow.intake_router import is_red_flag as _is_red

        if _is_red(raw):
            return False
        if RuleBasedSignalExtractor.is_pre_visit_intake_text(raw):
            return False
        if RuleBasedSignalExtractor.is_chit_chat_text(raw):
            return False
        if RuleBasedSignalExtractor.is_identity_text(raw):
            return False
        import re as _re_emp
        if _re_emp.search(r"不人性化|好笨|很怪|無言|敷衍", raw):
            return False
    except Exception:
        pass
    try:
        import re as _re
        import unicodedata as _ud

        n = _ud.normalize("NFKC", raw).strip()
        if len(n) < 4 or n in ("怎麼辦", "怎辦", "怎麼半", "help", "？", "?", "…"):
            return False
        if _re.search(r"可以跟我說什麼|可以說什麼|能做什麼|會做什麼|能幫什麼|我能幫什麼|介紹一下|你會什麼|功能有哪些", n, _re.IGNORECASE):
            return False
        from tfda_context_gate.a_router.policy import DEFAULT_POLICY, policy_gate
        from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor as _RB

        lang = getattr(request_context, "language", None)
        try:
            sig = _RB().extract(n, language=lang)  # type: ignore[arg-type]
        except Exception:
            return False
        decision = policy_gate(sig, DEFAULT_POLICY)
        if getattr(decision.status, "value", str(decision.status)) != "G_GENERAL_EDUCATION":
            return False
        return True
    except Exception:
        return False


# Re-export for backward compat (demo.py imports from runner)
__all__ = ["run_workflow", "stream_workflow", "WorkflowState", "WorkflowResult", "_sanitize_ocr_meds", "_merge_ocr_meds_into_intake_data", "_process_ocr_images", "_build_formal_extractor", "_build_formal_retriever", "_build_formal_generator", "ImageInput"]


def _dump(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _finish(trace: TraceRecorder, *, request_id: str, state: WorkflowState, status: str, final_response: str, fallback_reason: str | None, staged_recorder: StagedLatencyRecorder | None = None) -> WorkflowResult:
    decision = state.get("agent_decision")
    if status == "FALLBACK":
        trace.record("FALLBACK", "termination", "FALLBACK", agent_action=decision.action if decision is not None else None, reason_codes=[fallback_reason] if fallback_reason else [], fallback_reason=fallback_reason, termination_reason=state.get("termination_reason"))
    trace.record_evaluation(actual_decision="PASS" if status == "COMPLETED" else status, outcome=status, failure_type=None if status == "COMPLETED" else status, reason_codes=[fallback_reason] if fallback_reason else [], metadata={"source": "workflow.run_workflow", "orchestration": "langgraph"})
    system_status = "BLOCKED" if status == "BLOCKED" else "NEEDS_CLARIFICATION" if status == "NEEDS_CLARIFICATION" else "COMPLETED"
    trace.close(status=system_status, decision="PASS" if status == "COMPLETED" else status, outcome=status, fallback_reason=fallback_reason)
    attempts = [item.model_dump(mode="json") for item in state.get("previous_attempts", [])]
    intake_snapshot = _dump(state.get("intake") or state.get("intake_data"))
    previsit_summary = _dump(state.get("previsit_summary"))
    risk_snapshot = (
        previsit_summary.get("system_risk_classification")
        if previsit_summary is not None
        else None
    )
    snapshot = trace.snapshot()
    if staged_recorder is not None:
        try:
            staged = staged_recorder.snapshot()
            # Overlay workflow-derived stage timings from trace metrics if available
            try:
                metrics = trace.metrics()
                lb = getattr(metrics, "latency_by_component", {}) or {}
                # Map component latencies to staged keys where missing
                comp_map = {"RAG": "rag_retrieval_ms", "B": "b_gate_ms", "C": "answer_generator_ms", "D": "d_gate_ms", "SYSTEM": "total_ms"}
                for comp, key in comp_map.items():
                    if comp in lb and key not in staged:
                        staged[key] = lb[comp].total_ms if hasattr(lb[comp], "total_ms") else 0.0
                    elif comp in lb and staged.get(key, 0.0) == 0.0:
                        # Fill missing stage from metrics if recorder didn't measure
                        staged[key] = lb[comp].total_ms if hasattr(lb[comp], "total_ms") else staged[key]
                # Also derive from events directly for finer granularity if needed
                for ev in getattr(trace, "_events", []):
                    if ev.component == "RAG" and ev.latency_ms:
                        staged["rag_retrieval_ms"] = max(staged.get("rag_retrieval_ms", 0.0) or 0.0, ev.latency_ms)
                    elif ev.component == "B" and ev.latency_ms:
                        staged["b_gate_ms"] = max(staged.get("b_gate_ms", 0.0) or 0.0, ev.latency_ms)
                    elif ev.component == "C" and ev.latency_ms:
                        staged["answer_generator_ms"] = max(staged.get("answer_generator_ms", 0.0) or 0.0, ev.latency_ms)
                    elif ev.component == "D" and ev.latency_ms:
                        staged["d_gate_ms"] = max(staged.get("d_gate_ms", 0.0) or 0.0, ev.latency_ms)
            except Exception:
                pass
            snapshot["staged_latency"] = staged
        except Exception:
            pass
    return WorkflowResult(request_id=request_id, status=status, final_response=final_response, fallback_reason=fallback_reason, a_result=_dump(state.get("a_result")), query_expansion=_dump(state.get("query_expansion")), rag_result=_dump(state.get("rag_result")), b_result=_dump(state.get("b_result")), c_result=_dump(state.get("c_result")), d_result=_dump(state.get("d_result")), agent_action=decision.action if decision is not None else None, agent_reason_code=state.get("agent_reason_code"), question=state.get("question"), current_query=state.get("current_query"), execution_history=attempts, agent_steps=state.get("agent_steps", 0), rewrite_count=state.get("rewrite_count", 0), clarification_count=state.get("clarification_count", 0), termination_reason=state.get("termination_reason"), intake_snapshot=intake_snapshot, intake_stage=state.get("intake_stage"), previsit_summary=previsit_summary, system_risk_classification=risk_snapshot, trace=snapshot)


def _request_metadata(request: Any) -> tuple[str, str | None, str | None]:
    if isinstance(request, Mapping):
        return (str(request.get("request_id", "unknown")), request.get("declared_role"), request.get("user_raw_input"))
    return "unknown", None, None


def stream_workflow(request: RequestContext | dict[str, Any], *, prompt_injection_guard: Any | None = None, extractor: Any | None = None, query_expander: QueryExpander | None = None, retriever: Retriever | None = None, context_gate: ContextGate | None = None, generator: CGenerator | None = None, verifier: SemanticVerifier | None = None, trace_sink: TraceSink | None = None, agent_planner: AgentPlanner | None = None, query_rewriter: QueryRewriter | None = None, agent_limits: AgentLimits = AGENT_LIMITS, use_formal: bool = False, tool_executor: Any | None = None, tool_source_id: str | None = None, task_type: str | None = None, intake: Any | None = None, intake_data: Any | None = None, image_bytes: bytes | None = None, image_bytes_front: bytes | None = None, image_bytes_back: bytes | None = None, front_image_bytes: bytes | None = None, back_image_bytes: bytes | None = None, ocr_service: Any | None = None, chunk_size: int = 20, sse_format: bool = False, **kwargs: Any) -> Iterator[str]:
    _ = kwargs
    request_id, declared_role, original_query = _request_metadata(request)
    try:
        RequestContext.model_validate(request)
    except Exception as exc:
        trace = TraceRecorder(request_id, declared_role=str(declared_role) if declared_role else None, original_query=original_query, sink=trace_sink)
        trace.record_failure("SYSTEM", "workflow", failure_type="SCHEMA", status="ERROR", reason_codes=["REQUEST_SCHEMA_INVALID"], fallback_reason="SYSTEM_DEPENDENCY", error_type=type(exc).__name__, error_message=str(exc))
        trace.record_evaluation(actual_decision="FALLBACK", outcome="FALLBACK", failure_type="FALLBACK", reason_codes=["SYSTEM_DEPENDENCY"], metadata={"source": "workflow.stream_workflow", "streaming": True})
        trace.close(status="COMPLETED", decision="FALLBACK", outcome="FALLBACK", fallback_reason="SYSTEM_DEPENDENCY")
        yield from buffered_stream_after_d(fallback_response("SYSTEM_DEPENDENCY"), chunk_size=chunk_size, sse_format=sse_format, d_pass=False)
        return
    try:
        result = run_workflow(request, prompt_injection_guard=prompt_injection_guard, extractor=extractor, query_expander=query_expander, retriever=retriever, context_gate=context_gate, generator=generator, verifier=verifier, trace_sink=trace_sink, agent_planner=agent_planner, query_rewriter=query_rewriter, agent_limits=agent_limits, use_formal=use_formal, tool_executor=tool_executor, tool_source_id=tool_source_id, task_type=task_type, intake=intake, intake_data=intake_data, image_bytes=image_bytes, image_bytes_front=image_bytes_front, image_bytes_back=image_bytes_back, front_image_bytes=front_image_bytes, back_image_bytes=back_image_bytes, ocr_service=ocr_service)
    except Exception as exc:
        trace = TraceRecorder(request_id, declared_role=str(declared_role) if declared_role else None, original_query=original_query, sink=trace_sink)
        trace.record_failure("SYSTEM", "workflow", failure_type="DEPENDENCY", status="ERROR", reason_codes=["WORKFLOW_DEPENDENCY_FAILURE"], fallback_reason="SYSTEM_DEPENDENCY", error_type=type(exc).__name__, error_message=str(exc)[:500])
        trace.record_evaluation(actual_decision="FALLBACK", outcome="FALLBACK", failure_type="FALLBACK", reason_codes=["SYSTEM_DEPENDENCY"], metadata={"source": "workflow.stream_workflow", "streaming": True})
        trace.close(status="COMPLETED", decision="FALLBACK", outcome="FALLBACK", fallback_reason="SYSTEM_DEPENDENCY")
        yield from buffered_stream_after_d(fallback_response("SYSTEM_DEPENDENCY"), chunk_size=chunk_size, sse_format=sse_format, d_pass=False)
        return
    d_pass = result.status == "COMPLETED"
    yield from buffered_stream_after_d(result.final_response, chunk_size=chunk_size, sse_format=sse_format, d_pass=d_pass)


def run_workflow(request: RequestContext | dict[str, Any], *, prompt_injection_guard: Any | None = None, extractor: Any | None = None, query_expander: QueryExpander | None = None, retriever: Retriever | None = None, context_gate: ContextGate | None = None, generator: CGenerator | None = None, verifier: SemanticVerifier | None = None, trace_sink: TraceSink | None = None, agent_planner: AgentPlanner | None = None, query_rewriter: QueryRewriter | None = None, agent_limits: AgentLimits = AGENT_LIMITS, use_formal: bool = False, tool_executor: Any | None = None, tool_source_id: str | None = None, task_type: str | None = None, intake: Any | None = None, intake_data: Any | None = None, image_bytes: bytes | None = None, image_bytes_front: bytes | None = None, image_bytes_back: bytes | None = None, front_image_bytes: bytes | None = None, back_image_bytes: bytes | None = None, ocr_service: Any | None = None, **kwargs: Any) -> WorkflowResult:
    _ = kwargs
    request_id, declared_role, original_query = _request_metadata(request)
    try:
        request_context = RequestContext.model_validate(request)
        request_id = request_context.request_id
        declared_role = request_context.declared_role.value
        original_query = request_context.user_raw_input
    except Exception as exc:
        trace = TraceRecorder(request_id, declared_role=str(declared_role) if declared_role else None, original_query=original_query, sink=trace_sink)
        trace.record_failure("SYSTEM", "workflow", failure_type="SCHEMA", status="ERROR", reason_codes=["REQUEST_SCHEMA_INVALID"], fallback_reason="SYSTEM_DEPENDENCY", error_type=type(exc).__name__, error_message=str(exc))
        return _finish(trace, request_id=request_id, state={"current_query": original_query or "invalid"}, status="FALLBACK", final_response=fallback_response("SYSTEM_DEPENDENCY"), fallback_reason="SYSTEM_DEPENDENCY")
    trace = TraceRecorder(request_context.request_id, declared_role=request_context.declared_role.value, original_query=request_context.user_raw_input, sink=trace_sink)
    staged_recorder = StagedLatencyRecorder()
    if task_type is None:
        try:
            from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor
            if RuleBasedSignalExtractor.is_pre_visit_intake_text(request_context.user_raw_input):
                task_type = "pre_visit_intake"
        except Exception:
            pass
    _ocr_base = intake_data if intake_data is not None else intake
    _ocr_base, _ocr_result = _process_ocr_images(intake_data=_ocr_base, image_bytes=image_bytes, image_bytes_front=image_bytes_front, image_bytes_back=image_bytes_back, front_image_bytes=front_image_bytes, back_image_bytes=back_image_bytes, ocr_service=ocr_service, trace=trace)
    if _ocr_base is not None:
        intake_data = _ocr_base
        if intake is not None:
            intake = _ocr_base
        elif _ocr_result is not None and _ocr_result.get("known_medications"):
            intake_data = _ocr_base
    state: WorkflowState = {"request_context": request_context, "request_id": request_context.request_id, "original_query": request_context.user_raw_input, "current_query": request_context.user_raw_input, "trace": trace, "agent_planner": agent_planner, "query_rewriter": query_rewriter, "agent_limits": agent_limits, "previous_attempts": [], "pending_agent_action": None, "agent_steps": 0, "rewrite_count": 0, "clarification_count": 0, "retrieval_attempt": 0, "b_attempt": 0, "actions_taken": [], "intake": intake, "intake_data": intake_data, "task_type": task_type}
    runtime_stage: dict[str, str] = {"current": "SYSTEM"}
    try:
        _eligible_for_formal = _is_formal_eligible(request_context, task_type) if use_formal else False
        if use_formal and not _eligible_for_formal:
            with trace.span("SYSTEM", "narrow_path_gate") as span:
                span.set(status="COMPLETED", reason_codes=["NARROW_PATH_FAST"], termination_reason="FAST_PATH", fallback_reason=None)
        if use_formal and _eligible_for_formal:
            _ext = extractor
            _ret = retriever
            _gen = generator
            _cg = context_gate
            _te = tool_executor
            _qe = query_expander
            _tsid = tool_source_id

            def _run_formal() -> WorkflowState:
                nonlocal _ext, _ret, _gen, _cg, _te, _qe, _tsid, runtime_stage
                from tfda_context_gate.workflow.intake_router import is_red_flag as _is_red

                _is_red_flag_text = _is_red(request_context.user_raw_input)
                if not _is_red_flag_text:
                    # Narrow path: rule routing + true RAG + LLM generation (save 20s)
                    # Do NOT build formal extractor; use RuleBased (None) for 5ms
                    _ext = None
                    if _ret is None:
                        _ret = _build_formal_retriever()
                    if _gen is None:
                        _gen = _build_formal_generator()
                    if _cg is None:
                        _cg = DeterministicContextGate(approval_mode="all_retrieved")
                else:
                    if _cg is None:
                        _cg = DeterministicContextGate(approval_mode="all_retrieved")
                if _te is not None and _ret is not None:
                    try:
                        reg = getattr(_te, "registry", None)
                        if reg is not None and reg.get("EvidenceRetrievalTool") is not None:
                            tool = reg.get("EvidenceRetrievalTool")
                            if getattr(tool, "tfda_retriever", None) is None:
                                tool.tfda_retriever = _ret
                    except Exception:
                        pass
                if _te is None and (_tsid is not None or task_type is not None) and _ret is not None:
                    try:
                        from tfda_context_gate.tool_contract.registry import create_default_registry
                        from tfda_context_gate.tool_contract.executor import ToolExecutor
                        _registry = create_default_registry(tfda_retriever=_ret)
                        _te = ToolExecutor(_registry, timeout_ms=5000, trace=trace)
                        if _tsid is None:
                            _tsid = "TFDA_RISK"
                    except Exception:
                        _te = None
                qe_local = _qe or IdentityQueryExpander()
                ret_local = _ret or FixtureRetriever()
                cg_local = _cg or DeterministicContextGate()
                gen_local = _gen or DeterministicFixtureCGenerator()
                local_state: WorkflowState = {"request_context": request_context, "request_id": request_context.request_id, "original_query": request_context.user_raw_input, "current_query": request_context.user_raw_input, "trace": trace, "agent_planner": agent_planner, "query_rewriter": query_rewriter, "agent_limits": agent_limits, "previous_attempts": [], "pending_agent_action": None, "agent_steps": 0, "rewrite_count": 0, "clarification_count": 0, "retrieval_attempt": 0, "b_attempt": 0, "actions_taken": [], "intake": intake, "intake_data": intake_data, "task_type": task_type}
                g, _rs = build_workflow_graph(trace=trace, query_expander=qe_local, retriever=ret_local, context_gate=cg_local, generator=gen_local, verifier=verifier, agent_planner=agent_planner, query_rewriter=query_rewriter, prompt_injection_guard=prompt_injection_guard, extractor=_ext, agent_limits=agent_limits, tool_executor=_te, tool_source_id=_tsid, task_type=task_type)
                runtime_stage = _rs
                return g.invoke(local_state)  # type: ignore[no-any-return]

            # The orchestrator may already own the outer deadline worker.  In
            # that case run inline to avoid nested shared-pool deadlock; the
            # outer guard remains the hard caller bound.  Direct runner calls
            # use the bounded deadline helper.
            if deadline_scope_active():
                state = _run_formal()
            else:
                state_result, timed_out, _guard = run_with_deadline(
                    _run_formal,
                    timeout_s=FORMAL_WORKFLOW_TIMEOUT_S,
                )
                if timed_out or state_result is None:
                    raise TimeoutError(f"formal workflow timed out after {FORMAL_WORKFLOW_TIMEOUT_S}s")
                state = state_result
            status = state.get("status") or "FALLBACK"
            final_response = state.get("final_response") or fallback_response("SYSTEM_DEPENDENCY")
            return _finish(trace, request_id=request_context.request_id, state=state, status=status, final_response=final_response, fallback_reason=state.get("fallback_reason"), staged_recorder=staged_recorder)
        if tool_executor is not None and retriever is not None:
            try:
                reg = getattr(tool_executor, "registry", None)
                if reg is not None and reg.get("EvidenceRetrievalTool") is not None:
                    tool = reg.get("EvidenceRetrievalTool")
                    if getattr(tool, "tfda_retriever", None) is None:
                        tool.tfda_retriever = retriever
            except Exception:
                pass
        if tool_executor is None and (tool_source_id is not None or task_type is not None) and retriever is not None:
            try:
                from tfda_context_gate.tool_contract.registry import create_default_registry
                from tfda_context_gate.tool_contract.executor import ToolExecutor
                _registry = create_default_registry(tfda_retriever=retriever)
                tool_executor = ToolExecutor(_registry, timeout_ms=5000, trace=trace)
                if tool_source_id is None:
                    tool_source_id = "TFDA_RISK"
            except Exception:
                tool_executor = None
        query_expander = query_expander or IdentityQueryExpander()
        retriever = retriever or FixtureRetriever()
        context_gate = context_gate or DeterministicContextGate()
        generator = generator or DeterministicFixtureCGenerator()
        state = {"request_context": request_context, "request_id": request_context.request_id, "original_query": request_context.user_raw_input, "current_query": request_context.user_raw_input, "trace": trace, "agent_planner": agent_planner, "query_rewriter": query_rewriter, "agent_limits": agent_limits, "previous_attempts": [], "pending_agent_action": None, "agent_steps": 0, "rewrite_count": 0, "clarification_count": 0, "retrieval_attempt": 0, "b_attempt": 0, "actions_taken": [], "intake": intake, "intake_data": intake_data, "task_type": task_type}
        graph, _rs = build_workflow_graph(trace=trace, query_expander=query_expander, retriever=retriever, context_gate=context_gate, generator=generator, verifier=verifier, agent_planner=agent_planner, query_rewriter=query_rewriter, prompt_injection_guard=prompt_injection_guard, extractor=extractor, agent_limits=agent_limits, tool_executor=tool_executor, tool_source_id=tool_source_id, task_type=task_type)
        runtime_stage = _rs
        state = graph.invoke(state)
        status = state.get("status") or "FALLBACK"
        final_response = state.get("final_response") or fallback_response("SYSTEM_DEPENDENCY")
        return _finish(trace, request_id=request_context.request_id, state=state, status=status, final_response=final_response, fallback_reason=state.get("fallback_reason"), staged_recorder=staged_recorder)
    except Exception as exc:
        is_timeout = isinstance(exc, TimeoutError) or isinstance(exc, concurrent.futures.TimeoutError)
        current_stage = runtime_stage.get("current", "SYSTEM") if isinstance(runtime_stage, dict) else "SYSTEM"
        if is_timeout:
            stage_reason = "FORMAL_TIMEOUT"
            stage_reason_code = "WORKFLOW_TIMEOUT"
        else:
            stage_reason = {"A": "A_DEPENDENCY", "C": "C_FAILURE", "D": "D_FALLBACK", "QUERY_REWRITER": "AGENT_FAILURE", "AGENT": "AGENT_FAILURE"}.get(current_stage, "SYSTEM_DEPENDENCY")
            stage_reason_code = {"A": "A_DEPENDENCY_FAILURE", "C": "C_GENERATOR_FAILURE", "D": "D_WORKFLOW_FAILURE", "QUERY_REWRITER": "QUERY_REWRITER_FAILURE", "AGENT": "AGENT_FAILURE"}.get(current_stage, "WORKFLOW_DEPENDENCY_FAILURE")
        trace.record_failure("SYSTEM", "workflow", failure_type="DEPENDENCY", status="ERROR", reason_codes=[stage_reason_code], fallback_reason=stage_reason, error_type=type(exc).__name__, error_message=str(exc)[:500])
        safe_state: WorkflowState = state if isinstance(state, dict) and state.get("request_id") else {"current_query": original_query or "invalid", "request_id": request_id}  # type: ignore[typeddict-item]
        if is_timeout and isinstance(safe_state, dict):
            safe_state["termination_reason"] = "FORMAL_TIMEOUT"  # type: ignore[typeddict-item]
        return _finish(trace, request_id=request_context.request_id, state=safe_state, status="FALLBACK", final_response=fallback_response(stage_reason), fallback_reason=stage_reason, staged_recorder=staged_recorder)
