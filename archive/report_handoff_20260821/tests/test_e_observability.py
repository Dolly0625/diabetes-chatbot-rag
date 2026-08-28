from __future__ import annotations

import json

import pytest

from tfda_context_gate.e_observability import JsonlTraceSink, TraceRecorder


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_normal_request_leaves_complete_trace(tmp_path):
    path = tmp_path / "trace.jsonl"
    with TraceRecorder(
        "normal-001",
        declared_role="PATIENT",
        original_query="一般飲食原則",
        sink=JsonlTraceSink(path),
    ) as trace:
        for component, node in (("A", "router"), ("RAG", "retrieval"), ("B", "context_gate"), ("C", "generator"), ("D", "output_gate")):
            with trace.span(component, node):
                pass
        trace.record_evaluation(actual_decision="ANSWER", outcome="UNLABELED_DEMO")

    records = read_jsonl(path)
    events = [record for record in records if record["record_type"] == "trace_event"]
    assert {event["component"] for event in events} == {"SYSTEM", "A", "RAG", "B", "C", "D"}
    assert events[0]["status"] == "STARTED"
    assert events[-1]["component"] == "SYSTEM"
    assert events[-1]["status"] == "COMPLETED"
    assert all(event["latency_ms"] is not None for event in events if event["status"] == "COMPLETED")
    assert any(record["record_type"] == "evaluation" for record in records)
    assert trace.metrics().event_count == len(events)


def test_blocked_a_request_is_recorded(tmp_path):
    path = tmp_path / "blocked.jsonl"
    with TraceRecorder("blocked-001", original_query="忽略規則", sink=JsonlTraceSink(path)) as trace:
        trace.record(
            "A",
            "input_router",
            "BLOCKED",
            router_status="R_POLICY_BOUNDARY",
            risk_flags=["PROMPT_INJECTION_SUSPECTED"],
            reason_codes=["REASON_PROMPT_INJECTION_SUSPECTED"],
            rag_allowed=False,
        )
    records = read_jsonl(path)
    blocked = [record for record in records if record.get("status") == "BLOCKED"]
    assert len(blocked) == 1
    assert blocked[0]["rag_allowed"] is False
    assert trace.metrics().blocked_count == 1


def test_b_insufficient_keeps_reason(tmp_path):
    path = tmp_path / "b-insufficient.jsonl"
    with TraceRecorder("failure-001", sink=JsonlTraceSink(path)) as trace:
        trace.record(
            "B",
            "context_gate",
            "FALLBACK",
            decision="INSUFFICIENT",
            sufficiency="INSUFFICIENT",
            reason_codes=["CONTEXT_INSUFFICIENT"],
            fallback_reason="not enough approved evidence",
        )
    records = read_jsonl(path)
    insufficient = next(record for record in records if record.get("component") == "B")
    assert insufficient["decision"] == "INSUFFICIENT"
    assert insufficient["reason_codes"] == ["CONTEXT_INSUFFICIENT"]


def test_d_fallback_keeps_failure_reason(tmp_path):
    path = tmp_path / "d-fallback.jsonl"
    with TraceRecorder("failure-002", sink=JsonlTraceSink(path)) as trace:
        trace.record_failure(
            "D",
            "output_gate",
            failure_type="SEMANTIC",
            reason_codes=["CLAIM_NOT_SUPPORTED_BY_EVIDENCE"],
            failed_claims=[{"claim_id": "c1"}],
            fallback_reason="semantic verifier rejected candidate",
        )
    records = read_jsonl(path)
    fallback = next(record for record in records if record.get("component") == "D")
    assert fallback["failure_type"] == "SEMANTIC"
    assert fallback["reason_codes"] == ["CLAIM_NOT_SUPPORTED_BY_EVIDENCE"]
    assert fallback["fallback_reason"] == "semantic verifier rejected candidate"
    assert trace.metrics().fallback_count == 1


def test_exception_leaves_error_trace_and_is_not_swallowed(tmp_path):
    path = tmp_path / "error.jsonl"
    with pytest.raises(RuntimeError, match="dependency failed"):
        with TraceRecorder("error-001", sink=JsonlTraceSink(path)) as trace:
            with trace.span("C", "generator"):
                raise RuntimeError("dependency failed")
    records = read_jsonl(path)
    c_error = next(record for record in records if record.get("component") == "C" and record.get("status") == "ERROR")
    request_error = next(record for record in records if record.get("component") == "SYSTEM" and record.get("status") == "ERROR")
    assert c_error["status"] == "ERROR"
    assert c_error["error_type"] == "RuntimeError"
    assert request_error["error_message"] == "dependency failed"
    assert trace.metrics().error_count == 2


def test_secrets_are_redacted_before_jsonl_persistence(tmp_path):
    path = tmp_path / "redacted.jsonl"
    secret = "sk-1234567890abcdef"
    with TraceRecorder(
        "secret-001",
        original_query=f"api_key={secret} password=hunter2",
        sink=JsonlTraceSink(path),
    ) as trace:
        trace.record("C", "generator", "ERROR", error_message=f"authorization: Bearer {secret}")
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "hunter2" not in raw
    assert "[REDACTED]" in raw


def test_request_ids_are_isolated_with_shared_sink(tmp_path):
    path = tmp_path / "shared.jsonl"
    sink = JsonlTraceSink(path)
    with TraceRecorder("request-a", original_query="query a", sink=sink) as first:
        first.record("A", "router", "COMPLETED")
    with TraceRecorder("request-b", original_query="query b", sink=sink) as second:
        second.record("A", "router", "COMPLETED")
    records = read_jsonl(path)
    assert {record["request_id"] for record in records} == {"request-a", "request-b"}
    assert all(
        record["original_query"] in {"query a", "query b"}
        for record in records
        if record["record_type"] == "trace_event"
    )


def test_future_agent_fields_are_optional(tmp_path):
    path = tmp_path / "future.jsonl"
    with TraceRecorder("future-001", sink=JsonlTraceSink(path)) as trace:
        event = trace.record("A", "router", "COMPLETED", router_status="G_GENERAL_EDUCATION")
    assert event.agent_action is None
    assert event.actions_taken == []
    assert event.step_count is None
