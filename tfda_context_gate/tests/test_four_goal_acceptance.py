from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from tfda_context_gate.a_router.labels import RiskFlag, RouterStatus
from tfda_context_gate.a_router.router import route_request
from tfda_context_gate.agent.schemas import AgentDecision
from tfda_context_gate.clinical_safety import RiskSignalPolicy
from tfda_context_gate.conversation import CompactionPolicy, ConversationContextManager
from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.intake.summary import generate_previsit_summary
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult
from tfda_context_gate.rag.schemas import RAGResult
from tfda_context_gate.workflow import run_workflow


def _request(text: str, role: str = "PATIENT") -> dict[str, str]:
    return {
        "request_id": "four-goal-001",
        "schema_version": "a.v0.1",
        "user_raw_input": text,
        "declared_role": role,
        "language": "zh-TW",
    }


class EmptyRetriever:
    name = "empty-four-goal-retriever"

    def retrieve(self, expansion: QueryExpansionResult) -> RAGResult:
        return RAGResult(
            request_id=expansion.request_id,
            original_query=expansion.original_query,
            retrieval_queries=expansion.retrieval_queries,
            evidence=[],
            retrieval_latency_ms=0,
        )


def test_goal_1_scaffold_rejects_unbounded_agent_action():
    with pytest.raises(ValidationError):
        TypeAdapter(AgentDecision).validate_python(
            {"action": "CALL_TOOL", "reason_code": "MISSING_REQUIRED_CONTEXT"}
        )


def test_goal_2_compaction_never_drops_structured_facts():
    manager = ConversationContextManager(
        CompactionPolicy(max_context_tokens=1_000, compact_at_ratio=0.60, recent_exchanges=2)
    )
    context = manager.create("patient-session", original_query="我要準備回診")
    context = manager.apply_structured_updates(
        context,
        {"allergies": ["penicillin"], "authorization_status": "PATIENT_SELF"},
    )
    for index in range(4):
        context = manager.append_turn(context, role="user", content=f"訊息 {index}")
        context = manager.append_turn(context, role="assistant", content=f"回覆 {index}")

    compacted, decision = manager.compact(context)

    assert decision.should_compact is True
    assert compacted.clinical_state.allergies == ["penicillin"]
    assert compacted.clinical_state.authorization_status == "PATIENT_SELF"
    assert len(compacted.recent_turns) == 4


def test_goal_3_general_flow_actively_clarifies_without_llm_planner():
    result = run_workflow(_request("我的藥有什麼副作用？"), retriever=EmptyRetriever())

    assert result.status == "NEEDS_CLARIFICATION"
    assert result.agent_action == "ASK_USER"
    assert result.question is not None and "藥名或成分" in result.question
    assert result.agent_steps == 1


@pytest.mark.parametrize(
    ("text", "expected_signal"),
    [
        ("我的腳趾傷口流膿而且皮膚變黑", "FOOT_ULCER_OR_WOUND"),
        ("低血糖後抽搐而且叫不醒", "SEVERE_HYPOGLYCEMIA"),
        ("高血糖又持續嘔吐和腹痛", "POSSIBLE_DKA"),
    ],
)
def test_goal_4_explicit_diabetes_red_flags_change_route(text: str, expected_signal: str):
    classification = RiskSignalPolicy().classify(text)
    routed = route_request(_request(text))

    assert classification.level == "RED_FLAG"
    assert expected_signal in classification.signals
    assert classification.action == "URGENT_HUMAN"
    assert routed.router_status in {RouterStatus.E_EMERGENCY, RouterStatus.U_URGENT_HUMAN}
    assert RiskFlag.POSSIBLE_EMERGENCY in routed.risk_flags
    assert routed.rag_allowed is False


def test_goal_4_negated_signal_is_not_misrepresented_as_emergency():
    classification = RiskSignalPolicy().classify("我沒有胸痛，想了解糖尿病飲食")
    routed = route_request(_request("我沒有胸痛，想了解糖尿病飲食"))

    assert classification.level == "NO_DEFINED_SIGNAL"
    assert routed.router_status is RouterStatus.G_GENERAL_EDUCATION


def test_goal_4_negation_does_not_hide_red_flag_after_contrast():
    classification = RiskSignalPolicy().classify("我沒有胸痛，但現在呼吸困難")

    assert classification.level == "RED_FLAG"
    assert classification.signals == ["BREATHING_DIFFICULTY"]


def test_goal_4_summary_separates_reported_severity_from_system_risk():
    summary = generate_previsit_summary(
        PreVisitIntake(
            symptom_description="腳趾傷口流膿",
            symptom_severity="4/10",
        ),
        request_id="summary-risk-001",
    )

    assert summary.reported_severity == "4/10"
    assert summary.system_risk_classification.level == "RED_FLAG"
    assert "FOOT_ULCER_OR_WOUND" in summary.system_risk_classification.signals
    assert "不代表已排除急症" in summary.system_risk_classification.limitations
