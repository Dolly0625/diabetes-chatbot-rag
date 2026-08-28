from __future__ import annotations

import time

from tfda_context_gate.line_orchestration.orchestrator import (
    ASYNC_FORMAL_TIMEOUT,
    ASYNC_FORMAL_TIMEOUT_S,
    ASYNC_PLACEHOLDER_REPLY,
    FORMAL_WORKFLOW_TIMEOUT_S,
    HONEST_FALLBACK_TEXT,
    SYNC_FORMAL_TIMEOUT,
    SYNC_FORMAL_TIMEOUT_S,
)
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.workflow.schemas import WorkflowResult


_KEY = "p3-async-test-key-12345678"


def _fake_success_runner(request, **kwargs):
    # Simulate slow RAG + generation ~0.3s
    time.sleep(0.3)
    return WorkflowResult(
        request_id=request.get("request_id", "test"),
        status="COMPLETED",
        final_response="幫你整理了衛教重點（依 TFDA／國健署）：一般糖尿病飲食原則包括均衡飲食、控制總熱量、少糖少油、多蔬果，並依醫囑調整。",
        fallback_reason=None,
        a_result={"router_status": "G_GENERAL_EDUCATION"},
        query_expansion=None,
        rag_result={"evidences": [{"source": "TFDA_129", "title": "糖尿病飲食"}]},
        b_result={"decision": "PASS"},
        c_result={"decision": "ANSWER"},
        d_result={"decision": "PASS"},
        agent_action=None,
        agent_reason_code=None,
        question=None,
        current_query=request.get("user_raw_input"),
        execution_history=[],
        agent_steps=0,
        rewrite_count=0,
        clarification_count=0,
        termination_reason=None,
        intake_snapshot=None,
        intake_stage=None,
        previsit_summary=None,
        system_risk_classification=None,
        trace={"events": [], "evaluations": []},
    )


def _fake_honest_fallback_runner(request, **kwargs):
    time.sleep(0.2)
    return WorkflowResult(
        request_id=request.get("request_id", "test"),
        status="FALLBACK",
        final_response=HONEST_FALLBACK_TEXT,
        fallback_reason="B_INSUFFICIENT",
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
        termination_reason="B_INSUFFICIENT",
        intake_snapshot=None,
        intake_stage=None,
        previsit_summary=None,
        system_risk_classification=None,
        trace={"events": [], "evaluations": []},
    )


def _fake_exception_runner(request, **kwargs):
    time.sleep(0.15)
    raise RuntimeError("simulated retriever failure")


def test_timeout_constants_split():
    assert FORMAL_WORKFLOW_TIMEOUT_S == 45 or float(FORMAL_WORKFLOW_TIMEOUT_S) == 45.0
    assert SYNC_FORMAL_TIMEOUT_S == 45 or float(SYNC_FORMAL_TIMEOUT_S) == 45.0
    assert SYNC_FORMAL_TIMEOUT == 45 or float(SYNC_FORMAL_TIMEOUT) == 45.0
    assert ASYNC_FORMAL_TIMEOUT_S == 120 or float(ASYNC_FORMAL_TIMEOUT_S) == 120.0
    assert ASYNC_FORMAL_TIMEOUT == 120 or float(ASYNC_FORMAL_TIMEOUT) == 120.0


def test_diet_first_reply_under_2s_contains_querying_and_push_contains_true_answer(tmp_path, monkeypatch):
    pushes: list[tuple[str, str]] = []

    def fake_push(self, line_user_id: str, text: str) -> bool:
        pushes.append((line_user_id, text))
        return True

    monkeypatch.setattr(ConversationOrchestrator, "_default_push_sender", fake_push)

    repo = SQLiteProductSessionRepository(tmp_path / "p3_diet.sqlite3")
    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        workflow_runner=_fake_success_runner,
        use_formal=True,
    )
    # sanity: async timeout is 120
    assert orch.async_formal_timeout_s == 120

    start = time.time()
    result = orch.handle_text(event_id="diet-evt-1", line_user_id="U-diet-1", text="請說明糖尿病的一般飲食原則。")
    elapsed = time.time() - start

    assert elapsed < 2.0, f"diet first reply must be <2s, got {elapsed}"
    assert result.reply == ASYNC_PLACEHOLDER_REPLY or "幫你查衛教資料中" in result.reply, f"placeholder must be spec reply, got {result.reply}"
    # status is async pending (either ASYNC_PENDING or PROCESSING placeholder)
    assert result.status in ("ASYNC_PENDING", "PROCESSING")

    # wait background thread to push
    deadline = time.time() + 4
    while time.time() < deadline and not pushes:
        time.sleep(0.1)
    assert pushes, "background push not received within 4s"
    _, pushed_text = pushes[0]
    assert "衛教重點" in pushed_text or "飲食原則" in pushed_text
    # ensure true retrieval answer, not honest fallback placeholder
    assert HONEST_FALLBACK_TEXT not in pushed_text or "飲食" in pushed_text
    assert "TFDA" in pushed_text or "衛教" in pushed_text


def test_injection_failure_pushes_honest(tmp_path, monkeypatch):
    pushes: list[str] = []

    def fake_push(self, line_user_id: str, text: str) -> bool:
        pushes.append(text)
        return True

    monkeypatch.setattr(ConversationOrchestrator, "_default_push_sender", fake_push)

    repo = SQLiteProductSessionRepository(tmp_path / "p3_inject.sqlite3")
    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        workflow_runner=_fake_honest_fallback_runner,
        use_formal=True,
    )

    result = orch.handle_text(event_id="inject-evt-1", line_user_id="U-inject-1", text="請說明糖尿病的一般飲食原則。")
    assert result.reply == ASYNC_PLACEHOLDER_REPLY or "幫你查衛教資料中" in result.reply

    deadline = time.time() + 4
    while time.time() < deadline and not pushes:
        time.sleep(0.1)
    assert pushes, "injection fallback push not received"
    assert pushes[0] == HONEST_FALLBACK_TEXT or HONEST_FALLBACK_TEXT in pushes[0]


def test_injection_exception_retries_and_pushes_honest(tmp_path, monkeypatch):
    pushes: list[str] = []
    call_count: list[int] = [0]

    def counting_runner(request, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            time.sleep(0.1)
            raise RuntimeError("first attempt fail")
        time.sleep(0.1)
        raise RuntimeError("second attempt fail")

    def fake_push(self, line_user_id: str, text: str) -> bool:
        pushes.append(text)
        return True

    monkeypatch.setattr(ConversationOrchestrator, "_default_push_sender", fake_push)

    repo = SQLiteProductSessionRepository(tmp_path / "p3_retry.sqlite3")
    orch = ConversationOrchestrator(
        repo,
        identity_hash_key=_KEY,
        workflow_runner=counting_runner,
        use_formal=True,
        async_formal_timeout_s=1.0,
    )

    result = orch.handle_text(event_id="retry-evt-1", line_user_id="U-retry-1", text="請說明糖尿病的一般飲食原則。")
    assert result.reply == ASYNC_PLACEHOLDER_REPLY or "幫你查衛教資料中" in result.reply

    deadline = time.time() + 4
    while time.time() < deadline and not pushes:
        time.sleep(0.1)
    assert pushes, "retry fallback push not received"
    assert HONEST_FALLBACK_TEXT in pushes[0]
    assert call_count[0] == 2, f"should retry once, got {call_count[0]} calls"


def test_red_flag_fast_under_1s(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "p3_red.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, use_formal=True, workflow_runner=_fake_success_runner)
    red_texts = [
        "我胸痛呼吸困難快昏倒",
        "我大量吐血",
        "我意識不清",
    ]
    for idx, txt in enumerate(red_texts):
        start = time.time()
        result = orch.handle_text(event_id=f"red-{idx}", line_user_id="U-red", text=txt)
        elapsed = time.time() - start
        assert elapsed < 1.0, f"red flag must be fast <1s, got {elapsed} for {txt}"
        assert result.status == "FALLBACK"
        assert result.reply != ASYNC_PLACEHOLDER_REPLY and "幫你查衛教資料中" not in result.reply
        # fallback must be emergency related
        assert ("急" in result.reply or "119" in result.reply or "緊急" in result.reply)


def test_t1_t2_chitchat_fast_under_1s(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "p3_t1t2.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, use_formal=True, workflow_runner=_fake_success_runner)
    chitchats = [
        "你可以跟我說什麼？",
        "你會什麼功能？",
        "help",
        "？",
    ]
    for idx, txt in enumerate(chitchats):
        start = time.time()
        result = orch.handle_text(event_id=f"t1t2-{idx}", line_user_id="U-t1t2", text=txt)
        elapsed = time.time() - start
        assert elapsed < 1.0, f"chitchat must be fast <1s, got {elapsed}"
        assert result.reply != ASYNC_PLACEHOLDER_REPLY and "幫你查衛教資料中" not in result.reply


def test_intake_fast_under_1s(tmp_path):
    repo = SQLiteProductSessionRepository(tmp_path / "p3_intake_fast.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, use_formal=False)
    # Start intake via role selection, then answer intake, measure
    r1 = orch.handle_text(event_id="intake-1", line_user_id="U-intake-fast", text="為自己整理")
    assert r1.status == "NEEDS_CLARIFICATION"
    start = time.time()
    r2 = orch.handle_text(event_id="intake-2", line_user_id="U-intake-fast", text="我不知道")
    elapsed = time.time() - start
    assert elapsed < 1.0, f"intake must be fast <1s, got {elapsed}"
    assert r2.reply != ASYNC_PLACEHOLDER_REPLY and "幫你查衛教資料中" not in r2.reply
    assert "待看診確認" in r2.reply or "過敏" in r2.reply


def test_fake_pusher_via_monkeypatch_get_messaging_api(tmp_path, monkeypatch):
    # Alternative path: monkeypatch _get_messaging_api style – ensure orchestrator push can be swapped
    pushes: list[str] = []

    def fake_default_push(self, line_user_id: str, text: str) -> bool:
        pushes.append(text)
        return True

    monkeypatch.setattr(ConversationOrchestrator, "_default_push_sender", fake_default_push)

    repo = SQLiteProductSessionRepository(tmp_path / "p3_pusher2.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key=_KEY, workflow_runner=_fake_success_runner, use_formal=True)
    result = orch.handle_text(event_id="pusher2-evt", line_user_id="U-pusher2", text="請說明糖尿病的一般飲食原則。")
    assert result.reply == ASYNC_PLACEHOLDER_REPLY or "幫你查衛教資料中" in result.reply
    deadline = time.time() + 4
    while time.time() < deadline and not pushes:
        time.sleep(0.05)
    assert pushes and "衛教重點" in pushes[0]
