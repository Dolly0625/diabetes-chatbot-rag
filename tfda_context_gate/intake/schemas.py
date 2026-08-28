"""Pre-visit structured intake schemas (p6.2) — 3-stage topic-chunked.

Proposal p6.2 看診前資訊整理流程（3-stage topic-chunked）：
  患者選擇「準備看診」→ Stage1 用藥/過敏/慢性病/家族史 → Stage2 症狀(時間/描述/程度) → Stage3 待問醫師問題 + Review&Confirm
  → A 在每次補充前 deterministic 紅旗 pre-check → 紅旗固定轉介 U_URGENT_HUMAN → C 只整理已提供內容 → D 檢查無診斷/治療指令

This module defines the intake contract. All models are StrictModel (extra="forbid")
so unknown fields are rejected. C must never fabricate history; only organize provided facts.

FHIR mapping: QuestionnaireResponse → $extract → Bundle (Questionnaire.linkId → FHIR resource)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tfda_context_gate.a_router.labels import TargetSubject, TimeFrame
from tfda_context_gate.clinical_safety import SystemRiskClassification


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntakeQuestion(StrictModel):
    """Structured question for ASK_USER to collect one intake field.

    field_name maps to PreVisitIntake field or B identified_missing_information.
    question is the user-facing prompt (closed, no medical guessing).
    """

    field_name: str = Field(min_length=1, description="Intake field name, e.g. known_medications")
    question: str = Field(min_length=1, description="User-facing question text")
    required: bool = Field(default=True, description="Whether this field is required for intake completeness")


class PreVisitIntake(StrictModel):
    """Structured pre-visit intake (p6.2) — 8 fields + FHIR linkId.

    8 core fields (topic-chunked 3 stages):
      Stage1 用藥/過敏:
        - known_medications: 已知藥品 (list[str]) → FHIR MedicationStatement
        - allergies: 過敏史 (list[str]) → FHIR AllergyIntolerance
        - chronic_conditions: 慢性病史 (list[str]) → FHIR Condition
        - family_history: 家族史 (list[str]) → FHIR FamilyMemberHistory
      Stage2 症狀:
        - symptom_onset: 發生時間 (str|None) → FHIR Observation.effectiveDateTime
        - symptom_description: 症狀描述 (str|None) → FHIR Condition.code.text / Observation.valueString
        - symptom_severity: 症狀程度 (str|None) → FHIR Observation.valueString / Condition.severity
      Stage3 待問:
        - questions_for_doctor: 想問醫師的問題 (list[str]) → FHIR CommunicationRequest / QuestionnaireResponse

    Plus provenance:
        - time_frame: 時間框架 (TimeFrame enum, from A context_modifiers)
        - target_subject: 目標對象 (TargetSubject enum, from A context_modifiers)

    Validation: extra="forbid" — unknown fields rejected.
    C must only organize provided facts, never invent history.
    """

    # ── Stage1: 用藥/過敏/慢性病/家族史 ──
    known_medications: list[str] = Field(default_factory=list, max_length=20, description="已知藥品清單，僅使用者提供")
    allergies: list[str] = Field(default_factory=list, max_length=20, description="過敏史，僅使用者提供，用藥安全關鍵")
    chronic_conditions: list[str] = Field(default_factory=list, max_length=20, description="慢性病史，僅使用者提供")
    family_history: list[str] = Field(default_factory=list, max_length=20, description="家族史，僅使用者提供")

    # ── Stage2: 症狀 ──
    symptom_onset: str | None = Field(default=None, max_length=500, description="症狀發生時間，僅使用者提供")
    symptom_description: str | None = Field(default=None, max_length=2000, description="症狀描述，僅使用者提供")
    symptom_severity: str | None = Field(default=None, max_length=500, description="症狀程度/嚴重度，僅使用者提供")

    # ── Stage3: 待問醫師 ──
    questions_for_doctor: list[str] = Field(default_factory=list, max_length=10, description="想問醫師的問題清單")

    # ── Provenance ──
    time_frame: TimeFrame | str | None = Field(default=None, description="時間框架，來自 A context_modifiers")
    target_subject: TargetSubject | str | None = Field(default=None, description="目標對象，來自 A context_modifiers")

    # Optional provenance (not required but useful for timeline)
    request_id: str | None = Field(default=None, min_length=1, description="關聯請求 ID")


class TimelineEntry(StrictModel):
    """Single timeline entry sorted by onset.

    Never fabricates history: only entries with provided onset/description are created.
    """

    onset: str | None = Field(default=None, description="發生時間原始字串，未提供則 None")
    description: str = Field(min_length=1, description="症狀或事件描述，僅使用者提供")
    medications: list[str] = Field(default_factory=list, description="當時已知藥品快照")
    sort_key: str | None = Field(default=None, description="用於排序的正規化時間鍵，僅內部使用")


class PreVisitSummary(StrictModel):
    """Output summary for pre-visit intake (C → D).

    C only organizes provided facts; D checks no diagnosis/treatment指令.
    """

    request_id: str = Field(min_length=1, description="請求 ID")
    intake: PreVisitIntake = Field(description="原始 intake 快照，僅整理不推定")
    timeline: list[TimelineEntry] = Field(default_factory=list, description="按發生時間排序的時間軸，僅基於提供事實")
    summary_text: str = Field(min_length=1, description="可攜帶摘要本文，僅整理已提供內容，無診斷/治療指令")
    disclaimer: str = Field(min_length=1, description="免責聲明：非診斷、需醫師確認")
    # Provenance: which fields were actually provided vs empty
    provided_fields: list[str] = Field(default_factory=list, description="實際有值的欄位名，供稽核")
    missing_fields: list[str] = Field(default_factory=list, description="仍缺漏的欄位名")
    reported_severity: str | None = Field(default=None, description="病患自述程度；不是系統風險判斷")
    system_risk_classification: SystemRiskClassification = Field(description="依明確文字訊號產生的安全分流；不是診斷")


# ── FHIR linkId mapping (Questionnaire.linkId → FHIR resource) ──
# Reference: HL7 FHIR R5 Questionnaire + SDC, Medplum linkId examples
# QuestionnaireResponse → $extract → Bundle
FHIR_LINKID_MAP: dict[str, dict[str, str]] = {
    "known_medications": {"linkId": "medication-current", "resource": "MedicationStatement", "path": "MedicationStatement.medicationCodeableConcept"},
    "allergies": {"linkId": "allergy-substance", "resource": "AllergyIntolerance", "path": "AllergyIntolerance.code"},
    "chronic_conditions": {"linkId": "condition-history", "resource": "Condition", "path": "Condition.code"},
    "family_history": {"linkId": "family-history", "resource": "FamilyMemberHistory", "path": "FamilyMemberHistory.condition.code"},
    "symptom_onset": {"linkId": "symptom-onset", "resource": "Observation", "path": "Observation.effectiveDateTime"},
    "symptom_description": {"linkId": "symptom-description", "resource": "Condition", "path": "Condition.code.text"},
    "symptom_severity": {"linkId": "symptom-severity", "resource": "Observation", "path": "Observation.valueString"},
    "questions_for_doctor": {"linkId": "questions-for-doctor", "resource": "CommunicationRequest", "path": "CommunicationRequest.payload.contentString"},
    # Provenance fields (not FHIR clinical but useful for QuestionnaireResponse)
    "time_frame": {"linkId": "time-frame", "resource": "QuestionnaireResponse", "path": "QuestionnaireResponse.item.answer.valueString"},
    "target_subject": {"linkId": "target-subject", "resource": "QuestionnaireResponse", "path": "QuestionnaireResponse.subject"},
}

# Reverse map: linkId → field_name
FHIR_LINKID_REVERSE: dict[str, str] = {v["linkId"]: k for k, v in FHIR_LINKID_MAP.items()}

# ── Medication clarification: Brown Bag Review gold standard, 2-attempt fallback ──
# Confidence <0.7 triggers clarification; max 2 attempts before marking unknown.
# Attempt 1: ask to check medication bag; Attempt 2: ask color/shape/time; then unknown.
MEDICATION_CLARIFICATION_QUESTIONS: dict[int, str] = {
    1: "如果方便的話，幫我看一下藥袋上的藥名嗎？",
    2: "請問藥物的顏色、形狀或服用時間？（如白色圓形、早上服用等）",
}

# Colloquial medication patterns that indicate low confidence (<0.7) — never hallucinate drug name
COLLOQUIAL_MEDICATION_PATTERNS: list[str] = [
    r"白色.*藥丸",
    r"小藥丸",
    r"藥丸",
    r"膠囊",
    r"紅色.*藥",
    r"黃色.*藥",
    r"藍色.*藥",
    r"圓形.*藥",
    r"長條.*藥",
    r"大顆.*藥",
    r"小顆.*藥",
]

# FHIR unknown handling for colloquial meds that remain unclear after 2 attempts
FHIR_MEDICATION_UNKNOWN_STATUS: str = "unknown"
FHIR_MEDICATION_UNKNOWN_SUFFIX: str = "待確認"

# ── Intake field → question mapping (used by build_agent_question) ──
INTAKE_FIELD_QUESTIONS: dict[str, str] = {
    "known_medications": "第 1/8 題｜目前有固定吃藥或打胰島素嗎？知道藥名就直接說；不確定也沒關係。",
    "allergies": "第 2/8 題｜有沒有藥物或食物過敏？沒有、不確定都可以直接說。",
    "chronic_conditions": "第 3/8 題｜除了糖尿病，還有高血壓、高血脂等慢性病嗎？",
    "family_history": "第 4/8 題｜家人中有人有糖尿病或相關疾病嗎？",
    "symptom_onset": "第 5/8 題｜這次想看診的狀況大約從什麼時候開始？",
    "symptom_description": "第 6/8 題｜目前最主要的症狀或困擾是什麼？",
    "symptom_severity": "第 7/8 題｜程度大約是輕度、中度、重度，或 1–10 分中的幾分？",
    "questions_for_doctor": "第 8/8 題｜這次最想問醫師什麼？還沒想到也可以先跳過。",
    "time_frame": "請問這些症狀是現在發生、過去曾發生，還是假設性詢問？",
    "target_subject": "請問這些症狀是您本人、家人，還是其他對象的情況？",
    # Backward compat: generic B fields map to intake equivalents
    "medicine_name": "請問目前使用的藥物名稱或成分是什麼？",
    "medication_class": "請問家人目前使用的是哪一類糖尿病藥物？",
    "drug_type": "請問家人目前使用的是哪一類糖尿病藥物？",
    "symptom": "請問目前具體有哪些症狀？",
}

# Medication clarification need_clarify template (for known_medications with low confidence)
MEDICATION_NEED_CLARIFY_TEMPLATE: dict[int, dict[str, str]] = {
    1: {"question": MEDICATION_CLARIFICATION_QUESTIONS[1], "reason": "medication_bag_check"},
    2: {"question": MEDICATION_CLARIFICATION_QUESTIONS[2], "reason": "medication_appearance_check"},
    3: {"question": "已記錄為待確認，將在看診時請醫師協助確認。", "reason": "medication_unknown_after_2_attempts"},
}

# Structured intake fields that ASK_USER should prefer over generic medicine_name
STRUCTURED_INTAKE_FIELDS: set[str] = {
    "known_medications",
    "allergies",
    "chronic_conditions",
    "family_history",
    "symptom_onset",
    "symptom_description",
    "symptom_severity",
    "questions_for_doctor",
    "time_frame",
    "target_subject",
}

# ── 3-stage topic-chunked grouping ──
INTAKE_STAGES: dict[str, list[str]] = {
    "stage1": ["known_medications", "allergies", "chronic_conditions", "family_history"],
    "stage2": ["symptom_onset", "symptom_description", "symptom_severity"],
    "stage3": ["questions_for_doctor"],
}

# Stage → user-facing question (topic-chunked, not per-field)
STAGE_QUESTIONS: dict[str, str] = {
    "stage1": "為了幫您整理看診資料，請問目前使用的藥品、過敏史、慢性病史及家族史？（可一次說明多項，如「吃 metformin，無過敏，有高血壓，家族無糖尿病」）",
    "stage2": "請問症狀的相關資訊？（可一次說明，如「三個月前開始，早上血糖偏高約180，程度中等」包含時間、描述與嚴重度）",
    "stage3": "請問您想在看診時詢問醫師哪些問題？（可列多個問題）",
}

# Stage → FHIR Questionnaire section linkId
STAGE_FHIR_SECTION: dict[str, str] = {
    "stage1": "section-meds-allergies",
    "stage2": "section-symptoms",
    "stage3": "section-questions",
}
