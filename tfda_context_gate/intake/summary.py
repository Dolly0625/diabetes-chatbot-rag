"""Pre-visit summary generation (C) — only organizes provided facts, never fabricates — 8 fields."""

from __future__ import annotations

from .schemas import PreVisitIntake, PreVisitSummary
from .timeline import build_timeline
from .tool import PREVISIT_DISCLAIMER
from tfda_context_gate.clinical_safety import RiskSignalPolicy


def generate_previsit_summary(intake: PreVisitIntake | dict, *, request_id: str) -> PreVisitSummary:
    """Generate PreVisitSummary that only organizes provided facts.

    Never invents patient history, diagnosis, or treatment.
    Only concatenates provided fields into summary_text.
    8 fields: known_medications, allergies, chronic_conditions, family_history,
              symptom_onset, symptom_description, symptom_severity, questions_for_doctor
    """
    if isinstance(intake, dict):
        intake = PreVisitIntake.model_validate(intake)
    intake = PreVisitIntake.model_validate(intake.model_dump(mode="json"))

    timeline = build_timeline(intake)

    def _is_sentinel(v) -> bool:
        if isinstance(v, list):
            if v == ["不清楚（待看診確認）"] or v == ["待確認"] or v == ["目前沒有特別想問的問題"]:
                return True
        elif isinstance(v, str):
            if v in ("待確認", "不清楚（待看診確認）"):
                return True
        return False

    provided: list[str] = []
    missing: list[str] = []
    for field in ["known_medications", "allergies", "chronic_conditions", "family_history",
                  "symptom_onset", "symptom_description", "symptom_severity", "questions_for_doctor"]:
        val = getattr(intake, field)
        if val and not _is_sentinel(val):
            provided.append(field)
        else:
            missing.append(field)

    parts: list[str] = []
    if intake.known_medications and not _is_sentinel(intake.known_medications):
        parts.append(f"已知用藥：{', '.join(intake.known_medications)}")
    if intake.allergies and not _is_sentinel(intake.allergies):
        parts.append(f"過敏史：{', '.join(intake.allergies)}")
    if intake.chronic_conditions and not _is_sentinel(intake.chronic_conditions):
        parts.append(f"慢性病史：{', '.join(intake.chronic_conditions)}")
    if intake.family_history and not _is_sentinel(intake.family_history):
        parts.append(f"家族史：{', '.join(intake.family_history)}")
    if intake.symptom_onset and not _is_sentinel(intake.symptom_onset):
        parts.append(f"症狀起始：{intake.symptom_onset}")
    if intake.symptom_description and not _is_sentinel(intake.symptom_description):
        parts.append(f"症狀描述：{intake.symptom_description}")
    if intake.symptom_severity and not _is_sentinel(intake.symptom_severity):
        parts.append(f"症狀程度：{intake.symptom_severity}")
    if intake.questions_for_doctor and not _is_sentinel(intake.questions_for_doctor):
        parts.append(f"想問醫師的問題：{'；'.join(intake.questions_for_doctor)}")
    if intake.time_frame:
        tf = intake.time_frame.value if hasattr(intake.time_frame, "value") else str(intake.time_frame)
        parts.append(f"時間框架：{tf}")
    if intake.target_subject:
        ts = intake.target_subject.value if hasattr(intake.target_subject, "value") else str(intake.target_subject)
        parts.append(f"對象：{ts}")

    if not parts:
        summary_text = "目前未提供任何診前資訊。"
    else:
        summary_text = "；".join(parts) + "。"

    risk_text = "；".join(
        value for value in [intake.symptom_description, intake.symptom_severity] if value
    )
    risk_classification = RiskSignalPolicy().classify(risk_text)

    return PreVisitSummary(
        request_id=request_id,
        intake=intake,
        timeline=timeline,
        summary_text=summary_text,
        disclaimer=PREVISIT_DISCLAIMER,
        provided_fields=provided,
        missing_fields=missing,
        reported_severity=intake.symptom_severity,
        system_risk_classification=risk_classification,
    )
