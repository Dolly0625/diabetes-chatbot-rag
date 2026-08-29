"""P2B deterministic natural-expression checks.

These tests exercise the production orchestrator construction path while
keeping the workflow local.  They intentionally assert boundaries (no
diagnosis/dose/unsupported claims) in addition to conversational phrasing.
"""

from __future__ import annotations

from pathlib import Path

from tfda_context_gate.conversation.interpreter import (
    ConversationTurnInterpretation,
    DeterministicConversationInterpreter,
    FakeConversationInterpreter,
)
from tfda_context_gate.line_orchestration import ConversationOrchestrator
from tfda_context_gate.line_orchestration.response_composer import (
    compose_intake_question,
    compose_side_answer,
)
from tfda_context_gate.product_session import SQLiteProductSessionRepository
from tfda_context_gate.workflow.fallbacks import WELCOME_VARIANTS, fallback_response
from tfda_context_gate.workflow.schemas import WorkflowResult


_KEY = "p2b-natural-expression-test-key-123456"
_FIELDS = (
    "known_medications",
    "allergies",
    "chronic_conditions",
    "family_history",
    "symptom_onset",
    "symptom_description",
    "symptom_severity",
    "questions_for_doctor",
)


def _workflow(reply: str = "這題依既有衛教資料整理如下。"):
    def run(request, **_kwargs):
        return WorkflowResult(
            request_id=request.get("request_id", "p2b-test"),
            status="COMPLETED",
            final_response=reply,
            fallback_reason=None,
            a_result=None,
            query_expansion=None,
            rag_result=None,
            c_result=None,
            b_result=None,
            d_result=None,
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

    return run


def _new_orchestrator(tmp_path: Path, **kwargs):
    repository = SQLiteProductSessionRepository(tmp_path / "p2b.sqlite3")
    defaults = {
        "identity_hash_key": _KEY,
        "interpreter": DeterministicConversationInterpreter(),
        "use_formal": False,
    }
    defaults.update(kwargs)
    return repository, ConversationOrchestrator(repository, **defaults)


def test_questions_are_focused_natural_and_safe():
    for field in _FIELDS:
        question = compose_intake_question(field)
        assert question
        assert "第" not in question
        assert question.count("？") == 1
        assert not any(token in question for token in ("診斷", "劑量", "療效承諾", "治癒"))

    assert "過敏" in compose_intake_question("allergies")
    assert "什麼時候開始" in compose_intake_question("symptom_onset")


def test_side_answer_keeps_existing_answer_and_returns_to_saved_question():
    result = compose_side_answer("糖尿病飲食衛教內容。", "接著想確認過敏：有沒有藥物或食物過敏？")
    assert result.startswith("糖尿病飲食衛教內容。")
    assert "資料已保留" in result
    assert "繼續整理" in result
    assert "下一步是：接著想確認過敏" in result
    assert "第" not in result


def test_repeated_greeting_rotates_without_form_like_menu():
    seen: set[str] = set()
    replies = [fallback_response("WELCOME", seen=seen) for _ in range(3)]
    assert len(set(replies)) == len(WELCOME_VARIANTS) == 3
    assert any("又見面了" in reply for reply in replies)
    assert not any("1." in reply or "2." in reply or "3." in reply for reply in replies)


def test_production_path_naturalizes_cross_turn_side_answer(tmp_path: Path):
    repository, orchestrator = _new_orchestrator(tmp_path, workflow_runner=_workflow())
    user_id = "U-p2b-side"
    orchestrator.handle_text(event_id="p2b-side-1", line_user_id=user_id, text="為自己整理")
    orchestrator.handle_text(event_id="p2b-side-2", line_user_id=user_id, text="我不太知道欸")

    result = orchestrator.handle_text(
        event_id="p2b-side-3",
        line_user_id=user_id,
        text="欸那糖尿病平常飲食要注意什麼？",
    )
    session = repository.get(result.session_id)
    assert result.status == "SIDE_ANSWER"
    assert "資料已保留" in result.reply
    assert "繼續整理" in result.reply
    assert "過敏" in result.reply
    assert "第" not in result.reply
    assert session is not None and session.pending_field == "allergies"


def test_identity_rotation_is_early_and_does_not_call_interpreter_or_workflow(tmp_path: Path):
    class NeverCalledInterpreter:
        calls = 0

        def interpret(self, _envelope):
            self.calls += 1
            raise AssertionError("identity should be handled before interpreter")

    interpreter = NeverCalledInterpreter()
    workflow_calls = {"count": 0}

    def never_workflow(*_args, **_kwargs):
        workflow_calls["count"] += 1
        raise AssertionError("identity should not call workflow")

    _repository, orchestrator = _new_orchestrator(
        tmp_path,
        interpreter=interpreter,
        workflow_runner=never_workflow,
    )
    first = orchestrator.handle_text(event_id="p2b-id-1", line_user_id="U-p2b-id", text="你是真人嗎？")
    second = orchestrator.handle_text(event_id="p2b-id-2", line_user_id="U-p2b-id", text="現在是機器人在回覆嗎？")
    assert first.status == second.status == "INFORMATION"
    assert first.reply != second.reply
    for reply in (first.reply, second.reply):
        assert "AI" in reply
        assert "不是真人" in reply or "不是醫師" in reply
        assert "不提供診斷" in reply
        assert "119" in reply
    assert interpreter.calls == 0
    assert workflow_calls["count"] == 0


def test_red_flag_still_precedes_natural_identity(tmp_path: Path):
    class NeverCalledInterpreter:
        calls = 0

        def interpret(self, _envelope):
            self.calls += 1
            raise AssertionError("red flag must be handled before interpreter")

    interpreter = NeverCalledInterpreter()
    _repository, orchestrator = _new_orchestrator(tmp_path, interpreter=interpreter)
    result = orchestrator.handle_text(
        event_id="p2b-red-1",
        line_user_id="U-p2b-red",
        text="你是真人嗎？我現在胸痛又呼吸困難",
    )
    assert result.status == "FALLBACK"
    assert "119" in result.reply or "急診" in result.reply
    assert interpreter.calls == 0


def test_low_confidence_interpretation_stays_honest_and_uses_one_call(tmp_path: Path):
    interpretation = ConversationTurnInterpretation(intents=["UNKNOWN"], confidence=0.2)
    class CountingFakeInterpreter(FakeConversationInterpreter):
        calls = 0

        def interpret(self, envelope):
            self.calls += 1
            return super().interpret(envelope)

    interpreter = CountingFakeInterpreter(default=interpretation)
    repository, orchestrator = _new_orchestrator(tmp_path, interpreter=interpreter)
    user_id = "U-p2b-low"
    orchestrator.handle_text(event_id="p2b-low-1", line_user_id=user_id, text="為自己整理")

    result = orchestrator.handle_text(event_id="p2b-low-2", line_user_id=user_id, text="我真的不確定欸")
    session = repository.get(result.session_id)
    assert result.status == "NEEDS_CLARIFICATION"
    assert "待看診確認" in result.reply
    assert "第" not in result.reply
    assert not any(token in result.reply for token in ("診斷", "劑量", "療效承諾"))
    # The narrow deterministic start command is not an interpreter call; this
    # turn consumes exactly one existing interpretation result.
    assert interpreter.calls == 1
    assert session is not None and session.intake_snapshot.known_medications == ["不清楚（待看診確認）"]


def test_unseen_intake_confirmation_does_not_add_claims(tmp_path: Path):
    repository, orchestrator = _new_orchestrator(tmp_path)
    user_id = "U-p2b-confirm"
    orchestrator.handle_text(event_id="p2b-confirm-1", line_user_id=user_id, text="為自己整理")
    result = orchestrator.handle_text(
        event_id="p2b-confirm-2",
        line_user_id=user_id,
        text="平常有吃 metformin",
    )
    session = repository.get(result.session_id)
    assert session is not None
    assert any("metformin" in value.lower() for value in session.intake_snapshot.known_medications)
    assert "metformin" in result.reply.lower() or "記為" in result.reply
    assert "對嗎？" in result.reply
    assert "如果不對" in result.reply
    assert not any(token in result.reply for token in ("診斷", "劑量", "療效承諾", "治癒"))
