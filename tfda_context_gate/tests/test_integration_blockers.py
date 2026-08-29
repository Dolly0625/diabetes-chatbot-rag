"""Production-boundary regressions for async LINE and deadline handling."""

from __future__ import annotations

import time
import threading
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


def test_orchestrator_marker_failure_retries_marker_without_transport_retry(
    tmp_path: Path, monkeypatch
):
    repo = SQLiteProductSessionRepository(tmp_path / "orchestrator-marker-retry.sqlite3")
    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        interpreter=DeterministicConversationInterpreter(),
        use_formal=False,
    )
    claim = repo.claim_webhook_event("orchestrator-marker-event", orch._hash("U-marker-orch"))
    assert claim
    repo.complete_webhook_event(
        "orchestrator-marker-event",
        {
            "event_id": "orchestrator-marker-event",
            "session_id": "s",
            "reply": "placeholder",
            "status": "ASYNC_PENDING",
        },
        claim_token=claim,
    )
    original_marker = repo.mark_webhook_event_pushed
    marker_calls = 0

    def flaky_marker(event_id: str):
        nonlocal marker_calls
        marker_calls += 1
        if marker_calls == 1:
            raise RuntimeError("temporary database failure")
        return original_marker(event_id)

    monkeypatch.setattr(repo, "mark_webhook_event_pushed", flaky_marker)
    from tfda_context_gate.line_orchestration import orchestrator as orchestrator_module

    orchestrator_module._pushed_events.clear()
    orchestrator_module._pushing_events.clear()
    orchestrator_module._marker_pending_events.clear()
    orchestrator_module._marker_retrying_events.clear()
    transport_calls: list[str] = []
    wf = _workflow({"request_id": "orchestrator-marker-event", "user_raw_input": "answer"}, "answer")

    assert orch.push_formal_result(
        line_user_id="U-marker-orch",
        event_id="orchestrator-marker-event",
        workflow=wf,
        original_text="answer",
        push_sender=lambda _uid, text: transport_calls.append(text) or True,
    ) is True
    assert transport_calls == ["answer"]
    assert marker_calls == 1
    record = repo.get_webhook_event("orchestrator-marker-event")
    assert record is not None and record.result and record.result.get("pushed") is not True

    # The local delivered marker suppresses a second transport call, while
    # _is_duplicate_push retries only the durable marker.
    assert orch.push_formal_result(
        line_user_id="U-marker-orch",
        event_id="orchestrator-marker-event",
        workflow=wf,
        original_text="answer",
        push_sender=lambda _uid, text: transport_calls.append(text) or True,
    ) is False
    assert marker_calls == 2
    assert transport_calls == ["answer"]
    record = repo.get_webhook_event("orchestrator-marker-event")
    assert record is not None and record.result and record.result.get("pushed") is True


def test_line_marker_failure_retries_marker_without_transport_retry(tmp_path: Path, monkeypatch):
    import line_bot.app as line_app

    repo = SQLiteProductSessionRepository(tmp_path / "line-marker-retry.sqlite3")
    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        interpreter=DeterministicConversationInterpreter(),
        use_formal=False,
    )
    claim = repo.claim_webhook_event("line-marker-event", orch._hash("U-marker-line"))
    assert claim
    repo.complete_webhook_event(
        "line-marker-event",
        {"event_id": "line-marker-event", "session_id": "s", "reply": "placeholder", "status": "ASYNC_PENDING"},
        claim_token=claim,
    )
    original_marker = repo.mark_webhook_event_pushed
    marker_calls = 0

    def flaky_marker(event_id: str):
        nonlocal marker_calls
        marker_calls += 1
        if marker_calls == 1:
            raise RuntimeError("temporary database failure")
        return original_marker(event_id)

    monkeypatch.setattr(repo, "mark_webhook_event_pushed", flaky_marker)
    line_app._pushed_events.clear()
    line_app._pushing_events.clear()
    line_app._marker_pending_events.clear()
    line_app._marker_retrying_events.clear()
    transport_calls: list[str] = []

    class FakeApi:
        def push_message(self, request, **kwargs):
            transport_calls.append(request.messages[0].text)

    monkeypatch.setattr(line_app, "_get_messaging_api", lambda: FakeApi())
    assert line_app._push_text("U-marker-line", "answer", event_id="line-marker-event") is True
    assert line_app._mark_event_pushed(orch, "line-marker-event") is False
    assert marker_calls == 1
    assert transport_calls == ["answer"]

    assert line_app._is_duplicate_push("line-marker-event", repo) is True
    assert marker_calls == 2
    assert line_app._push_text("U-marker-line", "answer", event_id="line-marker-event") is False
    assert transport_calls == ["answer"]
    record = repo.get_webhook_event("line-marker-event")
    assert record is not None and record.result and record.result.get("pushed") is True


def test_async_spawn_guard_reaches_nested_workflow_and_push(tmp_path: Path):
    """The manually-created async thread must propagate its job guard."""

    observed: list[object] = []
    pushed = threading.Event()

    def guarded_runner(request, **_kwargs):
        from tfda_context_gate.e_observability.deadline import current_deadline_guard

        observed.append(current_deadline_guard())
        return _workflow(request, "guarded answer")

    def guarded_push(_user: str, _text: str) -> bool:
        from tfda_context_gate.e_observability.deadline import current_deadline_guard

        observed.append(current_deadline_guard())
        pushed.set()
        return True

    repo = SQLiteProductSessionRepository(tmp_path / "guard-propagation.sqlite3")
    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        workflow_runner=guarded_runner,
        interpreter=DeterministicConversationInterpreter(),
        use_formal=True,
        async_formal_timeout_s=1.0,
    )
    session = orch._load_or_create("U-guard-propagation")
    orch._spawn_async_formal(
        event_id="guard-propagation-event",
        line_user_id="U-guard-propagation",
        text="糖尿病可以吃水果嗎？",
        session_id=session.session_id,
        push_sender=guarded_push,
    )

    assert pushed.wait(timeout=2.0)
    assert len(observed) == 2
    assert all(value is not None for value in observed)
    assert all(hasattr(value, "should_abort") for value in observed)


def test_orchestrator_saturated_admission_creates_no_delayed_threads(tmp_path: Path, monkeypatch):
    """Thirty rejected jobs fail closed without push blocking or threads."""

    import tfda_context_gate.line_orchestration.orchestrator as orchestrator_module

    deadline = time.time() + 2.0
    while time.time() < deadline and orchestrator_module._async_jobs:
        time.sleep(0.01)
    monkeypatch.setattr(orchestrator_module, "_FORMAL_SEMAPHORE", threading.Semaphore(0))
    created = 0

    def forbidden_thread(*_args, **_kwargs):
        nonlocal created
        created += 1
        raise AssertionError("saturated admission must not create a delayed thread")

    monkeypatch.setattr(orchestrator_module.threading, "Thread", forbidden_thread)
    repo = SQLiteProductSessionRepository(tmp_path / "orch-saturation.sqlite3")
    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        interpreter=DeterministicConversationInterpreter(),
        use_formal=True,
    )
    session = orch._load_or_create("U-orch-saturation")
    pushes: list[str] = []

    def slow_push(_user: str, text: str) -> bool:
        time.sleep(0.2)
        pushes.append(text)
        return True

    started = time.monotonic()
    for index in range(30):
        orch._spawn_async_formal(
            event_id=f"orch-saturated-{index}",
            line_user_id="U-orch-saturation",
            text="糖尿病可以吃水果嗎？",
            session_id=session.session_id,
            push_sender=slow_push,
        )
    assert created == 0
    assert pushes == []
    assert time.monotonic() - started < 0.5


def test_line_saturated_admission_creates_no_delayed_threads(tmp_path: Path, monkeypatch):
    """LINE's adapter has the same bounded fail-closed admission contract."""

    import line_bot.app as line_app

    deadline = time.time() + 2.0
    while time.time() < deadline and line_app._async_jobs:
        time.sleep(0.01)
    monkeypatch.setattr(line_app, "_FORMAL_SEMAPHORE", threading.Semaphore(0))
    created = 0
    scheduled: list[str] = []

    def forbidden_thread(*_args, **_kwargs):
        nonlocal created
        created += 1
        raise AssertionError("saturated admission must not create a delayed thread")

    monkeypatch.setattr(line_app.threading, "Thread", forbidden_thread)
    def slow_push(_user, text, event_id=None, **_kwargs):
        time.sleep(0.2)
        scheduled.append(text)
        return True

    monkeypatch.setattr(line_app, "_push_text", slow_push)
    line_app._text_dedup.clear()
    line_app._async_jobs.clear()
    repo = SQLiteProductSessionRepository(tmp_path / "line-saturation.sqlite3")
    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        interpreter=DeterministicConversationInterpreter(),
        use_formal=True,
    )
    started = time.monotonic()
    for index in range(30):
        line_app._schedule_formal_push(
            orch,
            "U-line-saturation",
            f"line-saturated-{index}",
            f"糖尿病可以吃水果嗎？第{index}次",
        )
    assert created == 0
    assert scheduled == []
    assert time.monotonic() - started < 0.5


def test_pending_async_replay_reschedules_without_duplicate_turns(tmp_path: Path):
    """A restart-style replay resumes pending work and keeps one turn pair."""

    repo = SQLiteProductSessionRepository(tmp_path / "pending-replay.sqlite3")

    def successful_runner(request, **_kwargs):
        return _workflow(request, "replayed answer")

    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        workflow_runner=successful_runner,
        interpreter=DeterministicConversationInterpreter(),
        use_formal=True,
        async_formal_timeout_s=1.0,
    )
    session = orch._load_or_create("U-pending-replay")
    context = orch.context_manager.append_turn(
        session.conversation_context,
        role="user",
        content="糖尿病可以吃水果嗎？",
    )
    context = orch.context_manager.append_turn(
        context,
        role="assistant",
        content="查詢中，請稍候，資料整理完成後會推送給你 📋",
    )
    saved = repo.save(session.model_copy(update={"conversation_context": context}, deep=True), expected_version=session.version)
    claim = repo.claim_webhook_event("pending-replay-event", orch._hash("U-pending-replay"))
    assert claim
    repo.complete_webhook_event(
        "pending-replay-event",
        {
            "event_id": "pending-replay-event",
            "session_id": saved.session_id,
            "reply": "查詢中，請稍候，資料整理完成後會推送給你 📋",
            "status": "ASYNC_PENDING",
            "intake_stage": saved.intake_stage,
            "async_original_text": "糖尿病可以吃水果嗎？",
        },
        claim_token=claim,
    )

    pushes: list[str] = []
    replay = orch.handle_text(
        event_id="pending-replay-event",
        line_user_id="U-pending-replay",
        text="這是重送時不可信的不同內容",
        push_sender=lambda _user, text: pushes.append(text) or True,
    )
    assert replay.replayed is True
    assert replay.status == "ASYNC_PENDING"
    deadline = time.time() + 2.0
    while time.time() < deadline and not pushes:
        time.sleep(0.01)
    assert pushes == ["replayed answer"]
    latest = repo.get(saved.session_id)
    assert latest is not None
    turns = latest.conversation_context.recent_turns
    assert [turn.content for turn in turns].count("糖尿病可以吃水果嗎？") == 1
    assert sum(1 for turn in turns if "查詢中" in turn.content) == 1
    event = repo.get_webhook_event("pending-replay-event")
    assert event is not None and event.result and event.result.get("pushed") is True


def test_line_callback_pending_replay_does_not_append_placeholder_again(tmp_path: Path, monkeypatch):
    """The LINE adapter resumes durable pending work before its write path."""

    import importlib

    from fastapi.testclient import TestClient

    line_app = importlib.import_module("line_bot.app")
    monkeypatch.setenv("LINE_SESSION_DB_PATH", str(tmp_path / "callback-pending.sqlite3"))
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "")
    monkeypatch.setenv("LINE_ALLOW_UNSIGNED_WEBHOOK", "true")
    monkeypatch.setenv("LINE_DEMO_MODE", "true")
    monkeypatch.setenv("LINE_USE_FORMAL", "false")
    monkeypatch.setenv("LINE_IDENTITY_HASH_KEY", _KEY)
    monkeypatch.setattr(line_app, "LINE_CHANNEL_SECRET", "")
    monkeypatch.setattr(line_app, "_conversation_orchestrator", None)
    replies: list[str] = []
    scheduled: list[tuple[object, ...]] = []
    monkeypatch.setattr(line_app, "_reply_text", lambda _token, text, **_kwargs: replies.append(text) or True)
    monkeypatch.setattr(line_app, "_schedule_formal_push", lambda *args, **_kwargs: scheduled.append(args))

    orch = line_app._get_conversation_orchestrator()
    assert orch is not None
    session = orch._load_or_create("U-callback-pending")
    original = "糖尿病可以吃水果嗎？"
    placeholder = line_app.ASYNC_PLACEHOLDER_REPLY
    context = orch.context_manager.append_turn(session.conversation_context, role="user", content=original)
    context = orch.context_manager.append_turn(context, role="assistant", content=placeholder)
    saved = orch.repository.save(
        session.model_copy(update={"conversation_context": context}, deep=True),
        expected_version=session.version,
    )
    claim = orch.repository.claim_webhook_event("callback-pending-event", orch._hash("U-callback-pending"))
    assert claim
    orch.repository.complete_webhook_event(
        "callback-pending-event",
        {
            "event_id": "callback-pending-event",
            "session_id": saved.session_id,
            "reply": placeholder,
            "status": "ASYNC_PENDING",
            "intake_stage": saved.intake_stage,
            "async_original_text": original,
        },
        claim_token=claim,
    )

    payload = {
        "events": [{
            "type": "message",
            "webhookEventId": "callback-pending-event",
            "replyToken": "reply-callback-pending",
            "source": {"type": "user", "userId": "U-callback-pending"},
            "message": {"type": "text", "id": "message-callback-pending", "text": "重送時的不同內容"},
        }],
    }
    response = TestClient(line_app.app).post("/callback", json=payload)
    assert response.status_code == 200
    assert replies == [placeholder]
    assert len(scheduled) == 1
    assert scheduled[0][3] == original
    latest = orch.repository.get(saved.session_id)
    assert latest is not None
    turns = latest.conversation_context.recent_turns
    assert [turn.content for turn in turns].count(original) == 1
    assert sum(1 for turn in turns if turn.content == placeholder) == 1
