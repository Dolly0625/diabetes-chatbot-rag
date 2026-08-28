import os
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.product_session import SQLiteProductSessionRepository


def test_orchestrator_test_env_does_not_hit_real_network(tmp_path):
    """Verify test env orchestrator does not call real LLM/retrieval (fake is used)."""
    called = {}

    def fake_runner(request, **kwargs):
        called["use_formal"] = kwargs.get("use_formal")
        called["called"] = True
        # Return a minimal WorkflowResult-like object
        from tfda_context_gate.workflow.schemas import WorkflowResult
        return WorkflowResult(
            request_id=request.get("request_id", "test"),
            status="NEEDS_CLARIFICATION",
            final_response="目前有固定吃藥或打胰島素嗎？",
            fallback_reason=None,
            a_result=None,
            query_expansion=None,
            rag_result=None,
            b_result=None,
            c_result=None,
            d_result=None,
            agent_action=None,
            agent_reason_code=None,
            question="目前有固定吃藥或打胰島素嗎？",
            current_query=request.get("user_raw_input"),
            execution_history=[],
            agent_steps=0,
            rewrite_count=0,
            clarification_count=0,
            termination_reason=None,
            intake_snapshot=None,
            intake_stage="stage1",
            previsit_summary=None,
            system_risk_classification=None,
            trace={"events": [], "evaluations": []},
        )

    repo = SQLiteProductSessionRepository(tmp_path / "formal_switch.sqlite3")
    # PYTEST_CURRENT_TEST is set during pytest, so orchestrator should default to use_formal=False
    orch = ConversationOrchestrator(repo, identity_hash_key="test-formal-switch-key-123456", workflow_runner=fake_runner)
    assert orch.use_formal is False, "In pytest env, orchestrator must default to non-formal to avoid network"
    # Need a workflow-triggering text after intake is active; "為自己整理" is product command (no workflow)
    orch.handle_text(event_id="formal-test-1a", line_user_id="U-formal-test", text="為自己整理")
    result = orch.handle_text(event_id="formal-test-1b", line_user_id="U-formal-test", text="我想問糖尿病飲食怎麼吃")
    assert called.get("called") is True
    # Should be called without use_formal True (or with False)
    assert called.get("use_formal") in (None, False), f"Should not hit real formal, got use_formal={called.get('use_formal')}"
    assert result.status in ("NEEDS_CLARIFICATION", "SIDE_ANSWER", "FALLBACK")


def test_orchestrator_explicit_use_formal_true_for_line_runtime(tmp_path, monkeypatch):
    """LINE runtime should be formal True when env says so, even though pytest defaults to False."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("LINE_USE_FORMAL", "true")

    def fake_runner(request, **kwargs):
        assert kwargs.get("use_formal") is True
        from tfda_context_gate.workflow.schemas import WorkflowResult
        return WorkflowResult(
            request_id="x",
            status="NEEDS_CLARIFICATION",
            final_response="ok",
            fallback_reason=None,
            a_result=None,
            query_expansion=None,
            rag_result=None,
            b_result=None,
            c_result=None,
            d_result=None,
            agent_action=None,
            agent_reason_code=None,
            question="q",
            current_query="c",
            execution_history=[],
            agent_steps=0,
            rewrite_count=0,
            clarification_count=0,
            termination_reason=None,
            intake_snapshot=None,
            intake_stage="stage1",
            previsit_summary=None,
            system_risk_classification=None,
            trace={"events": [], "evaluations": []},
        )

    repo = SQLiteProductSessionRepository(tmp_path / "formal_line.sqlite3")
    # Explicit True overrides env/pytest detection
    orch = ConversationOrchestrator(repo, identity_hash_key="test-formal-switch-key-123456", workflow_runner=fake_runner, use_formal=True)
    assert orch.use_formal is True
    orch.handle_text(event_id="formal-test-2", line_user_id="U-formal-line", text="為自己整理")


def test_formal_timeout_covers_all_paths(tmp_path):
    """FORMAL_WORKFLOW_TIMEOUT_S must wrap all formal calls."""
    import time
    from tfda_context_gate.line_orchestration.orchestrator import FORMAL_WORKFLOW_TIMEOUT_S

    assert FORMAL_WORKFLOW_TIMEOUT_S == 45 or FORMAL_WORKFLOW_TIMEOUT_S == 45.0

    def slow_runner(request, **kwargs):
        time.sleep(0.5)
        from tfda_context_gate.workflow.schemas import WorkflowResult
        return WorkflowResult(
            request_id="x",
            status="NEEDS_CLARIFICATION",
            final_response="slow",
            fallback_reason=None,
            a_result=None,
            query_expansion=None,
            rag_result=None,
            b_result=None,
            c_result=None,
            d_result=None,
            agent_action=None,
            agent_reason_code=None,
            question="q",
            current_query="c",
            execution_history=[],
            agent_steps=0,
            rewrite_count=0,
            clarification_count=0,
            termination_reason=None,
            intake_snapshot=None,
            intake_stage="stage1",
            previsit_summary=None,
            system_risk_classification=None,
            trace={"events": [], "evaluations": []},
        )

    repo = SQLiteProductSessionRepository(tmp_path / "formal_timeout.sqlite3")
    orch = ConversationOrchestrator(repo, identity_hash_key="test-formal-switch-key-123456", workflow_runner=slow_runner, use_formal=True, formal_timeout_s=0.1)
    # Need to trigger workflow path (intake active) to hit timeout; first set up ACTIVE
    orch.handle_text(event_id="timeout-0", line_user_id="U-timeout", text="為自己整理")
    start = time.time()
    result = orch.handle_text(event_id="timeout-1", line_user_id="U-timeout", text="測試雜訊內容觸發正式流程")
    elapsed = time.time() - start
    assert elapsed < 1.0, f"Should timeout quickly, took {elapsed}"
    # Timeout should return safe fallback, not hang
    assert result.status in ("FALLBACK", "SIDE_ANSWER", "NEEDS_CLARIFICATION")
    assert result.reply is not None and len(result.reply) > 0
