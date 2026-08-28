"""Human-readable rendering for the existing structured E trace.

This module is display-only. It does not make routing decisions, mutate graph
state, or expose model hidden reasoning.
"""

from __future__ import annotations

from typing import Any, Mapping


def _value(event: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = event.get(key, default)
    return default if value in (None, "", []) else value


def _reason_code(event: Mapping[str, Any]) -> str | None:
    return _value(event, "reason_code") or next(
        iter(event.get("reason_codes", [])), None
    )


def _label(event: Mapping[str, Any]) -> str:
    component = event.get("component", "UNKNOWN")
    status = event.get("status", "")
    if component == "RAG":
        attempt = _value(event, "retrieval_attempt")
        return f"RAG #{attempt}" if attempt is not None else "RAG"
    if component == "B":
        attempt = _value(event, "b_attempt")
        return f"B #{attempt}" if attempt is not None else "B"
    if component == "AGENT":
        step = _value(event, "agent_step") or _value(event, "step_count")
        if event.get("node_name") == "question_builder":
            return "AGENT / ASK_USER"
        return f"AGENT #{step}" if step is not None else "AGENT"
    if component == "QUERY_REWRITER":
        return "QUERY_REWRITE"
    if component == "SYSTEM":
        return "SYSTEM"
    return str(component)


def _append_evidence(lines: list[str], event: Mapping[str, Any]) -> None:
    evidence = event.get("retrieved_evidence", [])
    for item in evidence[:5]:
        evidence_id = item.get("evidence_id", "?")
        rank = item.get("rank", "?")
        score = item.get("score")
        score_text = f" score={score:.6f}" if isinstance(score, (int, float)) else ""
        source = f" source={item['source']}" if item.get("source") else ""
        date = f" date={item['date']}" if item.get("date") else ""
        lines.append(f"    top{rank}: {evidence_id}{score_text}{source}{date}")


def format_trace_trajectory(trace: Mapping[str, Any]) -> str:
    """Render completed structured events as a compact execution trajectory."""

    request = trace.get("request", {})
    request_id = request.get("request_id", "unknown")
    trace_id = request.get("trace_id") or request_id
    lines = ["=" * 60, f"TRACE: {trace_id} (request_id={request_id})", "=" * 60]
    display_index = 0

    for event in trace.get("events", []):
        if event.get("status") == "STARTED":
            continue
        display_index += 1
        label = _label(event)
        status = event.get("status", "UNKNOWN")
        lines.append(f"\n[{display_index}] {label}")
        lines.append(f"    status: {status}")
        if event.get("latency_ms") is not None:
            lines.append(f"    latency: {event['latency_ms']:.2f} ms")

        component = event.get("component")
        if component == "A":
            if _value(event, "router_status"):
                lines.append(f"    route: {event['router_status']}")
            if _value(event, "prompt_guard_result"):
                lines.append(f"    prompt_guard: {event['prompt_guard_result']}")
        elif component == "QUERY_EXPANSION":
            if _value(event, "retrieval_query"):
                lines.append(f"    query: {event['retrieval_query']}")
        elif component == "RAG":
            if _value(event, "retrieval_query"):
                lines.append(f"    query: {event['retrieval_query']}")
            _append_evidence(lines, event)
        elif component == "B":
            if _value(event, "decision"):
                lines.append(f"    decision: {event['decision']}")
            if _value(event, "approved_evidence_count") is not None:
                lines.append(f"    approved_evidence_count: {event['approved_evidence_count']}")
            for key in ("relevance", "sufficiency", "conflict", "safety"):
                if _value(event, key) is not None:
                    lines.append(f"    {key}: {event[key]}")
            if _value(event, "reason_codes"):
                lines.append(f"    reason_codes: {', '.join(event['reason_codes'])}")
        elif component == "AGENT":
            planner_context = event.get("planner_context")
            if isinstance(planner_context, Mapping) and planner_context.get(
                "identified_missing_information"
            ):
                lines.append(
                    "    identified_missing_information: "
                    f"{planner_context['identified_missing_information']}"
                )
            if _value(event, "requested_action"):
                lines.append(f"    requested_action: {event['requested_action']}")
            if _value(event, "agent_action"):
                lines.append(f"    action: {event['agent_action']}")
            if _reason_code(event):
                lines.append(f"    reason_code: {_reason_code(event)}")
            for key in ("agent_step", "rewrite_count", "clarification_count"):
                if _value(event, key) is not None:
                    lines.append(f"    {key}: {event[key]}")
            if _value(event, "termination_reason"):
                lines.append(f"    termination_reason: {event['termination_reason']}")
        elif component == "ASK_USER":
            if _value(event, "missing_information"):
                lines.append(f"    missing_information: {event['missing_information']}")
            if _value(event, "question"):
                lines.append(f"    question: {event['question']}")
        elif component == "QUERY_REWRITER":
            if _value(event, "current_query"):
                lines.append(f"    old: {event['current_query']}")
            if _value(event, "rewritten_query"):
                lines.append(f"    new: {event['rewritten_query']}")
            if _value(event, "rewrite_attempt") is not None:
                lines.append(f"    attempt: {event['rewrite_attempt']}")
        elif component == "C":
            if _value(event, "candidate_decision"):
                lines.append(f"    decision: {event['candidate_decision']}")
        elif component == "D":
            if _value(event, "decision"):
                lines.append(f"    decision: {event['decision']}")
        elif component == "FALLBACK":
            if _value(event, "termination_reason"):
                lines.append(f"    termination_reason: {event['termination_reason']}")
            if _value(event, "fallback_reason"):
                lines.append(f"    fallback_reason: {event['fallback_reason']}")
        elif component == "SYSTEM":
            if _value(event, "outcome"):
                lines.append(f"    outcome: {event['outcome']}")
            if _value(event, "fallback_reason"):
                lines.append(f"    fallback_reason: {event['fallback_reason']}")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)
