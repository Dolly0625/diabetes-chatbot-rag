from __future__ import annotations

from types import SimpleNamespace

from tfda_context_gate.a_router.labels import (
    DeclaredRole,
    IntentTag,
    LanguageCode,
    PolicyReasonCode,
    Polarity,
    RouterStatus,
    TargetSubject,
    TimeFrame,
)
from tfda_context_gate.a_router.schemas import AResult, ContextModifiers
from tfda_context_gate.b_context_gate.gate import DeterministicContextGate
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult
from tfda_context_gate.rag.diabetes_rag_retriever import DiabetesRAGRetriever
from tfda_context_gate.rag.schemas import rag_to_b_input
from tfda_context_gate.workflow.runner import run_workflow


def _a_result(*, allowed: bool = True) -> AResult:
    return AResult(
        request_id="diabetes-rag-integration",
        schema_version="a.v0.1",
        user_raw_input="糖尿病飲食怎麼吃？",
        declared_role=DeclaredRole.PATIENT,
        language=LanguageCode.ZH_TW,
        intent_tags=[IntentTag.GENERAL_EDUCATION],
        risk_flags=[],
        context_modifiers=ContextModifiers(
            time_frame=TimeFrame.CURRENT,
            target_subject=TargetSubject.SELF,
            polarity=Polarity.AFFIRMATIVE,
            language=LanguageCode.ZH_TW,
        ),
        router_status=RouterStatus.G_GENERAL_EDUCATION,
        reason_codes=[PolicyReasonCode.MEETS_SAFE_SCOPE],
        rag_allowed=allowed,
    )


def _expansion() -> QueryExpansionResult:
    return QueryExpansionResult(
        request_id="diabetes-rag-integration",
        original_query="糖尿病飲食怎麼吃？",
        retrieval_queries=["糖尿病飲食怎麼吃？"],
    )


def test_diabetes_rag_adapter_translates_rag_v1_and_keeps_b_gate() -> None:
    chunk = SimpleNamespace(
        model_dump=lambda mode: {
            "chunk_id": "graph-demo-1",
            "source": "TFDA",
            "version": "v1",
            "date": "2026-01-01",
            "score": 0.91,
            "score_type": "graph_traversal",
            "status": "active",
            "content": "糖尿病飲食衛教示範證據。",
            "retriever": "graph",
            "entities": [{"id": "d1", "type": "Condition", "label": "糖尿病"}],
            "relations": [{
                "subject": "糖尿病", "subject_type": "Condition", "relation": "IS_A",
                "object": "慢性病", "object_type": "Condition",
            }],
            "evidence_risk_level": "LOW",
            "safety_signal_types": ["GENERAL"],
            "risk_basis": "relation",
        }
    )
    response = SimpleNamespace(
        request_id="diabetes-rag-integration",
        retrieval_status=SimpleNamespace(value="SUCCESS"),
        retrieval_route=SimpleNamespace(value="HYBRID"),
        graph_path_status=SimpleNamespace(value="NOT_APPLICABLE"),
        rerun_suggested=False,
        warnings=[SimpleNamespace(code=SimpleNamespace(value="SOURCE_NOT_CLINICALLY_REVIEWED"))],
        chunks=[chunk],
    )
    tool = SimpleNamespace(retrieve=lambda payload: response)

    result = DiabetesRAGRetriever(tool=tool).retrieve_with_guardrail(_a_result(), _expansion())

    assert result.retrieval_status == "SUCCESS"
    assert result.retrieval_route == "HYBRID"
    assert result.graph_path_status is None
    assert result.warnings == ["SOURCE_NOT_CLINICALLY_REVIEWED"]
    assert result.evidence[0].evidence_id == "graph-demo-1"
    assert DeterministicContextGate(approval_mode="all_retrieved").evaluate(rag_to_b_input(result)).decision == "PASS"


def test_diabetes_rag_adapter_denies_call_when_a_did_not_allow_rag() -> None:
    calls: list[object] = []
    tool = SimpleNamespace(retrieve=lambda payload: calls.append(payload))
    result = DiabetesRAGRetriever(tool=tool).retrieve_with_guardrail(_a_result(allowed=False), _expansion())

    assert calls == []
    assert result.retrieval_status == "ERROR"
    assert result.warnings == ["DIABETES_RAG_A_GUARDRAIL_DENIED"]


def test_diabetes_rag_package_runs_in_process_with_its_real_contract() -> None:
    result = DiabetesRAGRetriever().retrieve_with_guardrail(_a_result(), _expansion())

    # GEMINI_API_KEY is deliberately optional for this test environment.  The
    # package returns PARTIAL graph evidence when vector retrieval degrades;
    # enums from its real rag-v1 models must still be converted to canonical
    # main-project evidence instead of failing B before generation.
    assert result.retrieval_status in {"SUCCESS", "PARTIAL"}
    assert result.request_id == "diabetes-rag-integration"
    assert result.evidence
    assert result.evidence[0].evidence_risk_level in {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}


def test_diabetes_rag_is_usable_by_the_workflow_not_just_the_adapter() -> None:
    result = run_workflow(
        {
            "request_id": "diabetes-rag-workflow",
            "user_raw_input": "糖尿病飲食怎麼吃？",
            "declared_role": "PATIENT",
            "language": "zh-TW",
        },
        retriever=DiabetesRAGRetriever(),
        context_gate=DeterministicContextGate(approval_mode="all_retrieved"),
    )

    assert result.status == "COMPLETED"
    assert result.rag_result is not None
    assert result.rag_result["retrieval_status"] in {"SUCCESS", "PARTIAL"}
    assert result.b_result is not None and result.b_result["decision"] == "PASS"
