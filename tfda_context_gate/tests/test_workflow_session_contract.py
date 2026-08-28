from __future__ import annotations

from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.workflow import run_workflow


def test_workflow_returns_intake_stage_summary_and_risk_snapshot():
    intake = PreVisitIntake(
        known_medications=["metformin"],
        allergies=["無"],
        chronic_conditions=["糖尿病"],
        family_history=["無"],
        symptom_onset="三天前",
        symptom_description="早晨血糖偏高",
        symptom_severity="4/10",
        questions_for_doctor=["是否需要調整飲食？"],
    )
    result = run_workflow(
        {
            "request_id": "session-contract-001",
            "schema_version": "a.v0.1",
            "user_raw_input": "我要準備回診",
            "declared_role": "PATIENT",
            "language": "zh-TW",
        },
        task_type="pre_visit_intake",
        intake=intake,
    )

    assert result.status == "NEEDS_CONFIRMATION"
    assert result.intake_stage == "review"
    assert result.intake_snapshot is not None
    assert result.intake_snapshot["known_medications"] == ["metformin"]
    assert result.previsit_summary is not None
    assert result.previsit_summary["reported_severity"] == "4/10"
    assert result.system_risk_classification is not None
    assert result.system_risk_classification["level"] == "NO_DEFINED_SIGNAL"
