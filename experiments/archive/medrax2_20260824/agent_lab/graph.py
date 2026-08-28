from __future__ import annotations

import operator
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .corpus import TFDACorpus
from .gates import EvidenceGate, InputPolicyGate, OutputGate, fallback_message
from .models import AgentModel, RuleBasedTFDAModel
from .schemas import (
    AgentLimits,
    AgentMessage,
    CandidateEvidence,
    RunResult,
    ToolCall,
    ToolResult,
    TraceEvent,
)
from .tools import ToolRegistry, build_default_registry
from .tracing import event


class AgentState(TypedDict, total=False):
    messages: Annotated[List[AgentMessage], operator.add]
    trace_events: Annotated[List[TraceEvent], operator.add]
    run_id: str
    thread_id: str
    original_query: str
    started_monotonic: float
    policy_allowed: bool
    pending_tool_calls: List[ToolCall]
    tool_results: List[ToolResult]
    candidate_evidence: List[CandidateEvidence]
    approved_evidence_ids: List[str]
    draft_response: str
    final_response: str
    status: str
    termination_reason: str
    agent_steps: int
    tool_call_counts: Dict[str, int]


class TFDAToolAgent:
    """A bounded MedRAX2-style tool loop surrounded by mandatory TFDA gates."""

    def __init__(
        self,
        model: AgentModel,
        registry: ToolRegistry,
        limits: Optional[AgentLimits] = None,
        checkpointer: Optional[Any] = None,
    ):
        self.model = model
        self.registry = registry
        self.limits = limits or AgentLimits()
        self.input_gate = InputPolicyGate()
        self.evidence_gate = EvidenceGate()
        self.output_gate = OutputGate()
        self._cache: Dict[str, ToolResult] = {}
        self._cache_lock = Lock()
        self.workflow = self._build_graph().compile(checkpointer=checkpointer or MemorySaver())

    def _build_graph(self) -> StateGraph:
        graph = StateGraph(AgentState)
        graph.add_node("input_gate", self._input_gate_node)
        graph.add_node("agent", self._agent_node)
        graph.add_node("tools", self._tools_node)
        graph.add_node("evidence_gate", self._evidence_gate_node)
        graph.add_node("output_gate", self._output_gate_node)
        graph.add_edge(START, "input_gate")
        graph.add_conditional_edges("input_gate", self._input_route, {"AGENT": "agent", "END": END})
        graph.add_conditional_edges(
            "agent",
            self._agent_route,
            {"TOOLS": "tools", "EVIDENCE": "evidence_gate", "END": END},
        )
        graph.add_edge("tools", "agent")
        graph.add_conditional_edges(
            "evidence_gate",
            self._evidence_route,
            {"OUTPUT": "output_gate", "END": END},
        )
        graph.add_edge("output_gate", END)
        return graph

    def run(self, query: str, thread_id: Optional[str] = None) -> RunResult:
        run_id = "run_%s" % uuid.uuid4().hex
        effective_thread_id = thread_id or "thread_%s" % uuid.uuid4().hex
        initial: AgentState = {
            "messages": [AgentMessage(role="user", content=query, run_id=run_id)],
            "trace_events": [],
            "run_id": run_id,
            "thread_id": effective_thread_id,
            "original_query": query,
            "started_monotonic": time.monotonic(),
            "policy_allowed": False,
            "pending_tool_calls": [],
            "tool_results": [],
            "candidate_evidence": [],
            "approved_evidence_ids": [],
            "draft_response": "",
            "final_response": "",
            "status": "RUNNING",
            "termination_reason": "",
            "agent_steps": 0,
            "tool_call_counts": {},
        }
        state = self.workflow.invoke(
            initial,
            {"configurable": {"thread_id": effective_thread_id}},
        )
        current_trace = [item for item in state.get("trace_events", []) if item.run_id == run_id]
        current_messages = [item for item in state.get("messages", []) if item.run_id == run_id]
        current_results = [item.tool_result for item in current_messages if item.tool_result is not None]
        return RunResult(
            run_id=run_id,
            thread_id=effective_thread_id,
            status=state.get("status", "FALLBACK"),
            final_response=state.get("final_response") or fallback_message("SYSTEM_FAILURE"),
            termination_reason=state.get("termination_reason") or "SYSTEM_FAILURE",
            approved_evidence_ids=state.get("approved_evidence_ids", []),
            tool_results=current_results,
            trace=current_trace,
            agent_steps=state.get("agent_steps", 0),
            tool_call_counts=state.get("tool_call_counts", {}),
        )

    def _deadline_exceeded(self, state: AgentState) -> bool:
        return time.monotonic() - state["started_monotonic"] > self.limits.deadline_seconds

    def _input_gate_node(self, state: AgentState) -> Dict[str, Any]:
        decision = self.input_gate.evaluate(state["original_query"])
        update: Dict[str, Any] = {
            "policy_allowed": decision.allowed,
            "trace_events": [
                event(
                    state["run_id"],
                    "A",
                    "input_policy",
                    "PASS" if decision.allowed else "BLOCK",
                    {"reason_code": decision.reason_code},
                )
            ],
        }
        if not decision.allowed:
            update.update(
                status="BLOCKED",
                termination_reason=decision.reason_code,
                final_response=fallback_message(decision.reason_code),
            )
        return update

    @staticmethod
    def _input_route(state: AgentState) -> str:
        return "AGENT" if state.get("policy_allowed") else "END"

    def _agent_node(self, state: AgentState) -> Dict[str, Any]:
        if state.get("status") == "FALLBACK":
            return {"pending_tool_calls": []}
        if self._deadline_exceeded(state):
            return self._limit_update(state, "DEADLINE_EXCEEDED")
        steps = state.get("agent_steps", 0)
        if steps >= self.limits.max_agent_steps:
            return self._limit_update(state, "MAX_AGENT_STEPS_EXCEEDED")

        turn = self.model.next_turn(dict(state))
        steps += 1
        message = AgentMessage(
            role="assistant",
            content=turn.content,
            tool_calls=turn.tool_calls,
            run_id=state["run_id"],
        )
        return {
            "messages": [message],
            "pending_tool_calls": turn.tool_calls,
            "draft_response": turn.content if not turn.tool_calls else "",
            "agent_steps": steps,
            "trace_events": [
                event(
                    state["run_id"],
                    "AGENT",
                    "model_turn",
                    "TOOL_CALL" if turn.tool_calls else "DRAFT",
                    {"step": steps, "tool_names": [call.name for call in turn.tool_calls]},
                )
            ],
        }

    @staticmethod
    def _agent_route(state: AgentState) -> str:
        if state.get("status") == "FALLBACK":
            return "END"
        return "TOOLS" if state.get("pending_tool_calls") else "EVIDENCE"

    def _limit_update(self, state: AgentState, reason: str) -> Dict[str, Any]:
        return {
            "status": "FALLBACK",
            "termination_reason": reason,
            "final_response": fallback_message(reason),
            "pending_tool_calls": [],
            "trace_events": [event(state["run_id"], "SYSTEM", "limit", "BLOCK", {"reason": reason})],
        }

    def _tools_node(self, state: AgentState) -> Dict[str, Any]:
        if self._deadline_exceeded(state):
            return self._limit_update(state, "DEADLINE_EXCEEDED")
        calls = list(state.get("pending_tool_calls", []))
        counts = dict(state.get("tool_call_counts", {}))
        total = sum(counts.values())
        immediate: List[ToolResult] = []
        executable: List[ToolCall] = []

        for call in calls:
            tool = self.registry.get(call.name)
            if tool is None:
                immediate.append(
                    ToolResult(
                        call_id=call.call_id,
                        tool_name=call.name,
                        status="BLOCKED",
                        error_code="TOOL_NOT_ALLOWED",
                    )
                )
                continue
            if total >= self.limits.max_total_tool_calls:
                return self._limit_update(state, "MAX_TOOL_CALLS_EXCEEDED")
            current = counts.get(call.name, 0)
            if current >= tool.max_calls_per_run:
                immediate.append(
                    ToolResult(
                        call_id=call.call_id,
                        tool_name=call.name,
                        status="BLOCKED",
                        error_code="PER_TOOL_LIMIT_EXCEEDED",
                    )
                )
                continue
            counts[call.name] = current + 1
            total += 1
            executable.append(call)

        executed: List[ToolResult] = []
        if executable:
            workers = min(4, len(executable))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_call = {pool.submit(self._execute_with_cache, call): call for call in executable}
                for future in as_completed(future_to_call):
                    executed.append(future.result())

        by_call_id = {item.call_id: item for item in immediate + executed}
        ordered = [by_call_id[call.call_id] for call in calls if call.call_id in by_call_id]
        candidates = self._merge_candidates(state.get("candidate_evidence", []), ordered)
        messages = [
            AgentMessage(
                role="tool",
                content=json_summary(result),
                tool_result=result,
                run_id=state["run_id"],
            )
            for result in ordered
        ]
        return {
            "messages": messages,
            "tool_results": state.get("tool_results", []) + ordered,
            "candidate_evidence": candidates,
            "pending_tool_calls": [],
            "tool_call_counts": counts,
            "trace_events": [
                event(
                    state["run_id"],
                    "TOOLS",
                    "tool_batch",
                    "COMPLETED",
                    {
                        "results": [
                            {
                                "tool": item.tool_name,
                                "status": item.status,
                                "error_code": item.error_code,
                                "cache_hit": item.cache_hit,
                            }
                            for item in ordered
                        ]
                    },
                )
            ],
        }

    def _execute_with_cache(self, call: ToolCall) -> ToolResult:
        tool = self.registry.get(call.name)
        if tool is None:
            raise RuntimeError("preflight failed to resolve tool")
        key = tool.cache_key(call)
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached.model_copy(update={"call_id": call.call_id, "cache_hit": True})
        result = tool.invoke(call)
        if result.status == "OK":
            with self._cache_lock:
                self._cache[key] = result.model_copy(update={"cache_hit": False})
        return result

    @staticmethod
    def _merge_candidates(
        existing: List[CandidateEvidence], results: List[ToolResult]
    ) -> List[CandidateEvidence]:
        by_id = {item.evidence_id: item for item in existing}
        for result in results:
            if result.status != "OK":
                continue
            for item in result.candidate_evidence:
                previous = by_id.get(item.evidence_id)
                if previous is None or item.score > previous.score:
                    by_id[item.evidence_id] = item
        return sorted(by_id.values(), key=lambda item: (-item.score, item.evidence_id))

    def _evidence_gate_node(self, state: AgentState) -> Dict[str, Any]:
        decision = self.evidence_gate.evaluate(state.get("candidate_evidence", []))
        update: Dict[str, Any] = {
            "approved_evidence_ids": decision.approved_evidence_ids,
            "trace_events": [
                event(
                    state["run_id"],
                    "B",
                    "evidence_gate",
                    decision.decision,
                    {
                        "approved_evidence_ids": decision.approved_evidence_ids,
                        "reason_codes": decision.reason_codes,
                    },
                )
            ],
        }
        if decision.decision != "PASS":
            update.update(
                status="FALLBACK",
                termination_reason="EVIDENCE_INSUFFICIENT",
                final_response=fallback_message("EVIDENCE_INSUFFICIENT"),
            )
        return update

    @staticmethod
    def _evidence_route(state: AgentState) -> str:
        return "OUTPUT" if state.get("approved_evidence_ids") else "END"

    def _output_gate_node(self, state: AgentState) -> Dict[str, Any]:
        decision = self.output_gate.evaluate(
            state.get("draft_response", ""), state.get("approved_evidence_ids", [])
        )
        passed = decision.decision == "PASS"
        return {
            "status": "COMPLETED" if passed else "FALLBACK",
            "termination_reason": "OUTPUT_PASS" if passed else "OUTPUT_BLOCKED",
            "final_response": state.get("draft_response", "") if passed else fallback_message("OUTPUT_BLOCKED"),
            "trace_events": [
                event(
                    state["run_id"],
                    "D",
                    "output_gate",
                    decision.decision,
                    {"reason_codes": decision.reason_codes},
                )
            ],
        }


def json_summary(result: ToolResult) -> str:
    return "%s:%s:%s" % (result.tool_name, result.status, result.error_code or "OK")


def build_experimental_agent(
    corpus_path: Optional[str] = None,
    selected_tools: Optional[List[str]] = None,
    model: Optional[AgentModel] = None,
    limits: Optional[AgentLimits] = None,
) -> TFDAToolAgent:
    corpus = TFDACorpus(path=corpus_path) if corpus_path else TFDACorpus()
    registry = build_default_registry(corpus, selected_tools)
    return TFDAToolAgent(model=model or RuleBasedTFDAModel(), registry=registry, limits=limits)
