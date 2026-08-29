"""Production-boundary regressions for async LINE and deadline handling."""

from __future__ import annotations

import time
from pathlib import Path

from tfda_context_gate.conversation.interpreter import DeterministicConversationInterpreter
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.workflow.schemas import WorkflowResult


_KEY = "integration-blocker-test-key-123456789"


def _workflow(request: dict, text: str, *, status: str = "COMPLETED", reason: str | None = None) -> WorkflowResult:
    return WorkflowResult(
        request_id=request.get("request_id", "test"),
        status=status,
        final_response=text,
        fallback_reason=reason,
        a_result=None,
        query_expansion=None,
        rag_result=None,
        b_result=None,
        c_result=None,
        d_result=None,
        agent_action=None,
        agent_reason_code=None,
        question=None,
        current_query=request.get("user_raw_input"),
        execution_history=[],
        agent_steps=0,
        rewrite_count=0,
        clarification_count=0,
        termination_reason=reason,
        intake_snapshot=None,
        intake_stage=None,
        previsit_summary=None,
        system_risk_classification=None,
        trace={"events": [], "evaluations": []},
    )


def test_line_push_marks_only_after_api_success_and_deduplicates(monkeypatch):
    import line_bot.app as line_app

    line_app._pushed_events.clear()
    line_app._pushing_events.clear()
    calls: list[object] = []

    class FakeApi:
        def push_message(self, request, **kwargs):
            # Reservation is not a success marker while the transport runs.
            assert line_app._is_duplicate_push("line-push-once") is False
            calls.append((request, kwargs))

    monkeypatch.setattr(line_app, "_get_messaging_api", lambda: FakeApi())
    assert line_app._push_text("U-once", "answer", event_id="line-push-once") is True
    assert line_app._push_text("U-once", "answer", event_id="line-push-once") is False
    assert len(calls) == 1


def test_line_push_failure_releases_reservation_for_safe_retry(monkeypatch):
    import line_bot.app as line_app

    line_app._pushed_events.clear()
    line_app._pushing_events.clear()
    calls = {"count": 0}

    class FakeApi:
        def push_message(self, request, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("transient transport failure")

    monkeypatch.setattr(line_app, "_get_messaging_api", lambda: FakeApi())
    # _push_text owns one retry; a single successful delivery is made.
    assert line_app._push_text("U-retry", "answer", event_id="line-push-retry") is True
    assert calls["count"] == 2
    assert line_app._push_text("U-retry", "answer", event_id="line-push-retry") is False
    assert calls["count"] == 2


def test_line_schedule_timeout_never_pushes_or_writes_late_answer(tmp_path: Path, monkeypatch):
    import line_bot.app as line_app

    repo = SQLiteProductSessionRepository(tmp_path / "line-schedule.sqlite3")

    def slow_runner(request, **kwargs):
        time.sleep(0.12)
        return _workflow(request, "LATE ANSWER MUST NOT LEAK")

    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        interpreter=DeterministicConversationInterpreter(),
        workflow_runner=slow_runner,
        use_formal=True,
        async_formal_timeout_s=0.03,
    )
    session = orch._load_or_create("U-line-schedule")
    pushes: list[str] = []

    class FakeApi:
        def push_message(self, request, **kwargs):
            pushes.append(request.messages[0].text)

    monkeypatch.setattr(line_app, "_get_messaging_api", lambda: FakeApi())
    monkeypatch.setattr(line_app, "ASYNC_FORMAL_TIMEOUT_S", 0.03)
    line_app._pushed_events.clear()
    line_app._pushing_events.clear()
    line_app._text_dedup.clear()
    line_app._schedule_formal_push(
        orch,
        "U-line-schedule",
        "line-schedule-event",
        "請說明糖尿病的一般飲食原則。",
    )
    deadline = time.time() + 1.0
    while time.time() < deadline and not pushes:
        time.sleep(0.01)
    assert pushes
    assert all("LATE ANSWER MUST NOT LEAK" not in text for text in pushes)
    latest = repo.get(session.session_id)
    assert latest is not None
    assert all("LATE ANSWER MUST NOT LEAK" not in str(turn) for turn in latest.conversation_context)


def test_nested_deadline_uses_effective_min_without_deadlock():
    from tfda_context_gate.e_observability.deadline import run_with_deadline

    def child():
        time.sleep(0.12)
        return "late-child"

    def parent():
        return run_with_deadline(child, timeout_s=0.01)

    started = time.monotonic()
    result, timed_out, _guard = run_with_deadline(parent, timeout_s=0.2)
    elapsed = time.monotonic() - started
    assert timed_out is False
    assert result is not None
    child_result, child_timed_out, child_guard = result
    assert child_timed_out is True
    assert child_result is None
    assert child_guard.is_abandoned() is True
    assert elapsed < 0.08


def test_orchestrator_push_replay_is_exactly_once_and_durable(tmp_path: Path):
    repo = SQLiteProductSessionRepository(tmp_path / "push-replay.sqlite3")
    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        interpreter=DeterministicConversationInterpreter(),
        use_formal=False,
    )
    principal = orch._hash("U-push-replay")
    claim = repo.claim_webhook_event("push-replay-event", principal)
    assert claim
    repo.complete_webhook_event(
        "push-replay-event",
        {"event_id": "push-replay-event", "session_id": "s", "reply": "placeholder", "status": "ASYNC_PENDING"},
        claim_token=claim,
    )
    calls: list[str] = []
    wf = _workflow({"request_id": "push-replay-event", "user_raw_input": "answer"}, "answer")
    assert orch.push_formal_result(
        line_user_id="U-push-replay",
        event_id="push-replay-event",
        workflow=wf,
        original_text="answer",
        push_sender=lambda _uid, text: calls.append(text) or True,
    ) is True
    assert orch.push_formal_result(
        line_user_id="U-push-replay",
        event_id="push-replay-event",
        workflow=wf,
        original_text="answer",
        push_sender=lambda _uid, text: calls.append(text) or True,
    ) is False
    assert len(calls) == 1
    record = repo.get_webhook_event("push-replay-event")
    assert record is not None and record.result and record.result.get("pushed") is True
