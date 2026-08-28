"""PreVisitIntakeTool — structured intake collection & timeline (p6.2) — 3-stage topic-chunked.

Responsibilities:
- Store intake per request_id (in-memory, no persistence beyond process)
- Build timeline sorted by onset, never fabricates history
- Build PreVisitSummary that only organizes provided facts (C must not invent)
- Map B identified_missing_information → IntakeQuestion (structured fields)
- Support 3-stage topic-chunked extraction (single utterance fills 2-3 fields)

Never fabricates: all outputs are derived solely from provided intake fields.
"""

from __future__ import annotations

import re
from typing import Any

from .schemas import (
    COLLOQUIAL_MEDICATION_PATTERNS,
    FHIR_LINKID_MAP,
    FHIR_MEDICATION_UNKNOWN_STATUS,
    FHIR_MEDICATION_UNKNOWN_SUFFIX,
    INTAKE_FIELD_QUESTIONS,
    INTAKE_STAGES,
    IntakeQuestion,
    MEDICATION_CLARIFICATION_QUESTIONS,
    MEDICATION_NEED_CLARIFY_TEMPLATE,
    PreVisitIntake,
    PreVisitSummary,
    STAGE_QUESTIONS,
    TimelineEntry,
)
from .timeline import build_timeline


PREVISIT_DISCLAIMER = "本摘要僅整理您已提供的資訊，未包含診斷或治療建議；請攜帶此摘要與醫師討論，最終判斷由醫師負責。"

MEDICATION_CONFIDENCE_THRESHOLD = 0.7
MEDICATION_MAX_CLARIFICATION_ATTEMPTS = 2


class PreVisitIntakeTool:
    """Tool for pre-visit structured intake (p6.2) — 8 fields, 3 stages.

    Stores intake, builds timeline, builds summary. Never fabricates history.

    Usage:
        tool = PreVisitIntakeTool()
        intake = PreVisitIntake(known_medications=["metformin"], symptom_onset="2024-01-01", ...)
        tool.store_intake(intake, request_id="req-001")
        timeline = tool.build_timeline(intake)
        summary = tool.build_summary(intake, request_id="req-001")
    """

    name = "PreVisitIntakeTool"
    description = "Collects and organizes pre-visit intake (8 fields: known_medications, allergies, chronic_conditions, family_history, symptom_onset, symptom_description, symptom_severity, questions_for_doctor) → timeline & summary, never fabricates"

    def __init__(self) -> None:
        # In-memory store: request_id → PreVisitIntake
        self._store: dict[str, PreVisitIntake] = {}

    # ── Store / Retrieve ──

    def store_intake(self, intake: PreVisitIntake | dict[str, Any], *, request_id: str | None = None) -> PreVisitIntake:
        """Validate and store intake. Never fabricates missing fields.

        Args:
            intake: PreVisitIntake or dict (validated via StrictModel)
            request_id: optional override for intake.request_id
        Returns:
            Validated PreVisitIntake
        """
        if isinstance(intake, dict):
            validated = PreVisitIntake.model_validate(intake)
        else:
            # Re-validate to ensure extra=forbid
            validated = PreVisitIntake.model_validate(intake.model_dump(mode="json"))
        rid = request_id or validated.request_id
        if rid:
            validated.request_id = rid
            self._store[rid] = validated
        elif validated.request_id:
            self._store[validated.request_id] = validated
        return validated

    def get_intake(self, request_id: str) -> PreVisitIntake | None:
        return self._store.get(request_id)

    def clear(self, request_id: str | None = None) -> None:
        if request_id:
            self._store.pop(request_id, None)
        else:
            self._store.clear()

    # ── Timeline ──

    def build_timeline(self, intake: PreVisitIntake | dict[str, Any]) -> list[TimelineEntry]:
        """Build timeline sorted by onset, never fabricates.

        Delegates to timeline.build_timeline.
        """
        if isinstance(intake, dict):
            intake = PreVisitIntake.model_validate(intake)
        return build_timeline(intake)

    # ── Summary (C must only organize provided facts) ──

    def build_summary(self, intake: PreVisitIntake | dict[str, Any], *, request_id: str) -> PreVisitSummary:
        """Build PreVisitSummary that only organizes provided facts.

        - Never invents patient history
        - Timeline sorted by onset
        - Summary text is concatenation of provided fields, no diagnosis/treatment
        - Disclaimer always present
        """
        if isinstance(intake, dict):
            intake = PreVisitIntake.model_validate(intake)
        # Ensure intake is validated (extra=forbid)
        intake = PreVisitIntake.model_validate(intake.model_dump(mode="json"))

        timeline = self.build_timeline(intake)

        # Determine provided vs missing — 8 core fields
        provided: list[str] = []
        missing: list[str] = []
        for field in ["known_medications", "allergies", "chronic_conditions", "family_history",
                      "symptom_onset", "symptom_description", "symptom_severity", "questions_for_doctor"]:
            val = getattr(intake, field)
            if val:  # list non-empty or str non-empty
                provided.append(field)
            else:
                missing.append(field)

        # Build summary_text only from provided facts, no diagnosis/treatment
        parts: list[str] = []
        if intake.known_medications:
            parts.append(f"已知用藥：{', '.join(intake.known_medications)}")
        if intake.allergies:
            parts.append(f"過敏史：{', '.join(intake.allergies)}")
        if intake.chronic_conditions:
            parts.append(f"慢性病史：{', '.join(intake.chronic_conditions)}")
        if intake.family_history:
            parts.append(f"家族史：{', '.join(intake.family_history)}")
        if intake.symptom_onset:
            parts.append(f"症狀起始：{intake.symptom_onset}")
        if intake.symptom_description:
            parts.append(f"症狀描述：{intake.symptom_description}")
        if intake.symptom_severity:
            parts.append(f"症狀程度：{intake.symptom_severity}")
        if intake.questions_for_doctor:
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

        # Ensure no diagnosis/treatment指令 in summary_text (defensive)
        # If detected, we still return but D will catch; we don't fabricate alternative
        from tfda_context_gate.clinical_safety import RiskSignalPolicy
        risk_text = "；".join(
            value for value in [intake.symptom_description, intake.symptom_severity] if value
        )
        return PreVisitSummary(
            request_id=request_id,
            intake=intake,
            timeline=timeline,
            summary_text=summary_text,
            disclaimer=PREVISIT_DISCLAIMER,
            provided_fields=provided,
            missing_fields=missing,
            reported_severity=intake.symptom_severity,
            system_risk_classification=RiskSignalPolicy().classify(risk_text),
        )

    # ── Medication clarification: Brown Bag Review 2-attempt fallback ──

    def is_colloquial_medication(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        for pat in COLLOQUIAL_MEDICATION_PATTERNS:
            if re.search(pat, text):
                return True
        return False

    def assess_medication_confidence(self, text: str) -> float:
        if not text or not text.strip():
            return 0.0
        known_drugs = ["metformin", "二甲雙胍", "胰島素", "insulin", "SGLT2", "GLP-1", "semaglutide", "阿卡波糖", "格列美脲"]
        for drug in known_drugs:
            if drug.lower() in text.lower():
                return 0.95
        if self.is_colloquial_medication(text):
            return 0.4
        if re.search(r"藥", text):
            return 0.5
        return 0.0

    def get_medication_clarification_question(self, attempt: int) -> dict[str, str]:
        if attempt in MEDICATION_NEED_CLARIFY_TEMPLATE:
            return dict(MEDICATION_NEED_CLARIFY_TEMPLATE[attempt])
        return dict(MEDICATION_NEED_CLARIFY_TEMPLATE[3])

    def mark_medication_unknown(self, original_text: str) -> str:
        cleaned = original_text.strip()[:50]
        if FHIR_MEDICATION_UNKNOWN_SUFFIX not in cleaned:
            return f"{cleaned}-{FHIR_MEDICATION_UNKNOWN_SUFFIX}"
        return cleaned

    def handle_medication_clarification(self, utterance: str, *, attempt: int) -> dict[str, Any]:
        confidence = self.assess_medication_confidence(utterance)
        if confidence >= MEDICATION_CONFIDENCE_THRESHOLD:
            meds = self._extract_medications(utterance)
            if meds:
                return {"status": "resolved", "medications": meds, "confidence": confidence}
        if attempt < MEDICATION_MAX_CLARIFICATION_ATTEMPTS:
            next_attempt = attempt + 1
            q = self.get_medication_clarification_question(next_attempt)
            return {"status": "need_clarify", "attempt": next_attempt, "question": q["question"], "reason": q["reason"], "confidence": confidence}
        unknown_text = self.mark_medication_unknown(utterance)
        return {"status": "unknown", "medications": [unknown_text], "reason": "medication_unknown_after_2_attempts", "confidence": confidence, "original_text": utterance}

    def to_fhir_medication_statement(self, medication_text: str, *, request_id: str) -> dict[str, Any]:
        is_unknown = FHIR_MEDICATION_UNKNOWN_SUFFIX in medication_text
        base: dict[str, Any] = {
            "resourceType": "MedicationStatement",
            "status": FHIR_MEDICATION_UNKNOWN_STATUS if is_unknown else "active",
            "subject": {"reference": f"Patient/{request_id}"},
            "medicationCodeableConcept": {"text": medication_text},
        }
        if is_unknown:
            base["note"] = [{"text": "待確認：需於看診時請醫師協助確認藥名"}]
        return base

    def to_fhir_bundle(self, intake: PreVisitIntake | dict[str, Any], *, request_id: str) -> dict[str, Any]:
        if isinstance(intake, dict):
            intake = PreVisitIntake.model_validate(intake)
        entries: list[dict[str, Any]] = []
        qr = self.to_fhir_questionnaire_response(intake, request_id=request_id)
        entries.append({"resource": qr})
        for med_text in intake.known_medications:
            ms = self.to_fhir_medication_statement(med_text, request_id=request_id)
            entries.append({"resource": ms})
        return {"resourceType": "Bundle", "type": "collection", "entry": entries}

    # ── FHIR linkId helpers ──

    def to_fhir_questionnaire_response(self, intake: PreVisitIntake | dict[str, Any], *, request_id: str) -> dict[str, Any]:
        """Convert intake to FHIR QuestionnaireResponse (linkId → answer).

        Only includes provided fields, never fabricates.
        When medication is unknown (contains 待確認), includes status unknown extension.
        """
        if isinstance(intake, dict):
            intake = PreVisitIntake.model_validate(intake)
        items = []
        for field, fhir_info in FHIR_LINKID_MAP.items():
            val = getattr(intake, field, None)
            if val:
                if isinstance(val, list):
                    answers = []
                    for v in val:
                        ans: dict[str, Any] = {"valueString": v}
                        if field == "known_medications" and FHIR_MEDICATION_UNKNOWN_SUFFIX in v:
                            ans["extension"] = [{"url": "http://hl7.org/fhir/StructureDefinition/questionnaire-response-status", "valueCode": FHIR_MEDICATION_UNKNOWN_STATUS}]
                        answers.append(ans)
                else:
                    v_str = val.value if hasattr(val, "value") else str(val)
                    answers = [{"valueString": v_str}]
                item: dict[str, Any] = {"linkId": fhir_info["linkId"], "answer": answers}
                if field == "known_medications":
                    has_unknown = any(FHIR_MEDICATION_UNKNOWN_SUFFIX in v for v in val) if isinstance(val, list) else False
                    if has_unknown:
                        item["extension"] = [{"url": "http://hl7.org/fhir/StructureDefinition/questionnaire-response-unknown", "valueCode": FHIR_MEDICATION_UNKNOWN_STATUS}]
                items.append(item)
        return {
            "resourceType": "QuestionnaireResponse",
            "status": "completed",
            "subject": {"reference": f"Patient/{request_id}"},
            "item": items,
        }

    # ── IntakeQuestion mapping (for ASK_USER) ──

    def to_intake_questions(self, missing_fields: list[str]) -> list[IntakeQuestion]:
        """Map B identified_missing_information → structured IntakeQuestion list.

        Prefers structured intake fields over generic medicine_name.
        """
        questions: list[IntakeQuestion] = []
        for field in missing_fields:
            q_text = INTAKE_FIELD_QUESTIONS.get(field)
            if q_text:
                questions.append(IntakeQuestion(field_name=field, question=q_text, required=True))
            else:
                # Fallback for unknown field: generic prompt
                questions.append(
                    IntakeQuestion(
                        field_name=field,
                        question=f"為了縮小可可靠查找的範圍，請補充以下資訊：{field}。",
                        required=True,
                    )
                )
        return questions

    def build_intake_question(self, field: str) -> IntakeQuestion:
        """Single field → IntakeQuestion."""
        q_text = INTAKE_FIELD_QUESTIONS.get(field, f"請補充：{field}")
        return IntakeQuestion(field_name=field, question=q_text, required=True)

    def get_stage_question(self, stage: str) -> str:
        """Get topic-chunked question for a stage."""
        return STAGE_QUESTIONS.get(stage, f"請補充：{stage}")

    def get_stage_for_field(self, field: str) -> str | None:
        """Find which stage a field belongs to."""
        for stage, fields in INTAKE_STAGES.items():
            if field in fields:
                return stage
        return None

    def get_missing_stages(self, intake: PreVisitIntake | dict[str, Any]) -> list[str]:
        """Determine which stages still have missing fields."""
        if isinstance(intake, dict):
            intake = PreVisitIntake.model_validate(intake)
        missing_stages = []
        for stage, fields in INTAKE_STAGES.items():
            for f in fields:
                val = getattr(intake, f, None)
                if not val:
                    missing_stages.append(stage)
                    break
        return missing_stages

    # ── Multi-field extraction (single utterance → 2-3 fields) ──

    def extract_fields_from_utterance(self, utterance: str, *, stage: str | None = None) -> dict[str, Any]:
        """Extract multiple intake fields from a single utterance.

        Deterministic rule-based extraction (no LLM fabrication).
        Only extracts fields that are explicitly mentioned; never invents.

        Args:
            utterance: user utterance (single turn may contain 2-3 fields)
            stage: optional stage hint to limit extraction scope

        Returns:
            dict of field_name → extracted value (only provided fields)
        """
        result: dict[str, Any] = {}
        text = utterance.strip()
        if not text:
            return result

        # ── Stage1: meds/allergies/chronic/family ──
        if stage is None or stage == "stage1":
            # known_medications: look for drug names
            meds = self._extract_medications(text)
            if meds:
                result["known_medications"] = meds
            # allergies: look for 過敏 patterns
            allergies = self._extract_allergies(text)
            if allergies is not None:
                result["allergies"] = allergies
            # chronic_conditions
            chronic = self._extract_chronic(text)
            if chronic is not None:
                result["chronic_conditions"] = chronic
            # family_history
            fam = self._extract_family(text)
            if fam is not None:
                result["family_history"] = fam

        # ── Stage2: symptoms ──
        if stage is None or stage == "stage2":
            onset = self._extract_onset(text)
            if onset:
                result["symptom_onset"] = onset
            desc = self._extract_description(text)
            if desc:
                result["symptom_description"] = desc
            severity = self._extract_severity(text)
            if severity:
                result["symptom_severity"] = severity

        # ── Stage3: questions ──
        if stage is None or stage == "stage3":
            questions = self._extract_questions(text)
            if questions:
                result["questions_for_doctor"] = questions

        return result

    def _extract_medications(self, text: str) -> list[str] | None:
        known_drugs = ["metformin", "二甲雙胍", "胰島素", "insulin", "SGLT2", "GLP-1", "semaglutide", "阿卡波糖", "格列美脲"]
        found = []
        for drug in known_drugs:
            if drug.lower() in text.lower():
                found.append(drug)
        if found:
            return found
        if self.is_colloquial_medication(text):
            return None
        m = re.search(r"吃\s*([^\s，,。；;]+)", text)
        if m and m.group(1) not in found and len(m.group(1)) > 1:
            candidate = m.group(1).strip()
            if candidate not in ["藥", "藥物"] and candidate not in found:
                pass
        return None

    def extract_medication_with_confidence(self, text: str) -> dict[str, Any]:
        confidence = self.assess_medication_confidence(text)
        meds = self._extract_medications(text)
        is_colloquial = self.is_colloquial_medication(text)
        return {"medications": meds, "confidence": confidence, "is_colloquial": is_colloquial, "needs_clarify": confidence < MEDICATION_CONFIDENCE_THRESHOLD and (is_colloquial or "藥" in text)}

    def _extract_allergies(self, text: str) -> list[str] | None:
        if re.search(r"無過敏|沒有過敏|不過敏", text):
            return ["無"]
        m = re.search(r"過敏[^，,。；;]*?([^\s，,。；;]+)過敏|對\s*([^\s，,。；;]+)\s*過敏", text)
        if m:
            val = m.group(1) or m.group(2)
            if val:
                return [val.strip()]
        if "過敏" in text:
            # Generic allergy mention without specific substance
            return [text.strip()[:50]]
        return None

    def _extract_chronic(self, text: str) -> list[str] | None:
        if re.search(r"無慢性病|沒有慢性病|無其他疾病", text):
            return ["無"]
        chronic_keywords = ["高血壓", "高血脂", "心臟病", "腎臟病", "高脂血症"]
        found = [kw for kw in chronic_keywords if kw in text]
        if found:
            return found
        if re.search(r"慢性病|高血壓|高血脂", text):
            return [text.strip()[:50]]
        return None

    def _extract_family(self, text: str) -> list[str] | None:
        if re.search(r"家族無|無家族史|沒有家族史|家族沒有", text):
            return ["無"]
        if re.search(r"家族.*糖尿病|家人.*糖尿病|父親.*糖尿病|母親.*糖尿病", text):
            return ["家族糖尿病史"]
        if "家族" in text or "家人" in text:
            return [text.strip()[:50]]
        return None

    def _extract_onset(self, text: str) -> str | None:
        # Look for time expressions
        patterns = [
            r"(\d+\s*年\s*\d+\s*月\s*\d+\s*日)",
            r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
            r"(\d+\s*個月前|\d+\s*天前|\d+\s*週前|\d+\s*周前)",
            r"(三個月前|兩個月前|一個月前|昨天|前天|上週|上周|最近|上個月)",
            r"(三個月|兩個月|一個月|\d+\s*個月|\d+\s*天|\d+\s*週)",
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(1).strip()
        if re.search(r"開始|起|發生|出現", text) and re.search(r"月|天|週|年|前|最近", text):
            # Fallback: return the sentence containing time
            return text.strip()[:100]
        return None

    def _extract_description(self, text: str) -> str | None:
        # Look for symptom descriptions
        symptom_keywords = ["血糖", "頭暈", "口渴", "頻尿", "疲倦", "不舒服", "疼痛", "麻", "視力", "傷口", "感染", "血壓"]
        for kw in symptom_keywords:
            if kw in text:
                # Return the relevant fragment
                # Find sentence containing keyword
                sentences = re.split(r"[。；;，,]", text)
                for s in sentences:
                    if kw in s:
                        return s.strip()[:200]
                return text.strip()[:200]
        # If text is long and contains symptom-like content, return it
        if len(text) > 5 and re.search(r"症狀|不適|感覺|血糖|血壓", text):
            return text.strip()[:200]
        return None

    def _extract_severity(self, text: str) -> str | None:
        if re.search(r"輕度|輕微|還好|不嚴重", text):
            return "輕度"
        if re.search(r"中度|中等|普通", text):
            return "中度"
        if re.search(r"重度|嚴重|很嚴重|非常", text):
            return "重度"
        m = re.search(r"(\d+)\s*分|程度\s*(\d+)|嚴重度\s*(\d+)", text)
        if m:
            val = m.group(1) or m.group(2) or m.group(3)
            return f"{val}分"
        if re.search(r"程度|嚴重", text):
            return text.strip()[:50]
        return None

    def _extract_questions(self, text: str) -> list[str] | None:
        # Look for question patterns
        if re.search(r"想問|想請問|想了解|問題是|疑問", text):
            # Split by question marks or semicolons
            parts = re.split(r"[？?；;]", text)
            questions = [p.strip() for p in parts if p.strip() and len(p.strip()) > 3]
            if questions:
                return questions[:5]
            return [text.strip()[:200]]
        if "？" in text or "?" in text or "嗎" in text or "如何" in text or "怎麼" in text:
            parts = re.split(r"[？?；;]", text)
            questions = [p.strip() for p in parts if p.strip()]
            if questions:
                return questions[:5]
        # If text looks like a question list
        if len(text) > 5:
            return [text.strip()[:200]]
        return None

    def revalidate_via_a(self, supplement_text: str, *, request_id: str, declared_role: str = "PATIENT") -> dict[str, Any]:
        """Re-validate supplement via A gate (fixed referral, not planner decision).

        After each ASK_USER supplement, the new request must go through A again.
        Red-flag (POSSIBLE_EMERGENCY) goes to fixed U_URGENT_HUMAN/E_EMERGENCY, not planner.

        Returns:
            dict with router_status, rag_allowed, is_red_flag, fixed_referral
        """
        from tfda_context_gate.a_router.router import route_request

        req = {
            "request_id": request_id,
            "schema_version": "a.v0.1",
            "user_raw_input": supplement_text,
            "declared_role": declared_role,
            "language": "zh-TW",
        }
        result = route_request(req)
        is_red_flag = any(str(f) == "POSSIBLE_EMERGENCY" for f in result.risk_flags)
        is_fixed_referral = result.router_status.value in ("E_EMERGENCY", "U_URGENT_HUMAN")
        return {
            "router_status": result.router_status.value,
            "rag_allowed": result.rag_allowed,
            "risk_flags": [str(f) for f in result.risk_flags],
            "is_red_flag": is_red_flag,
            "is_fixed_referral": is_fixed_referral,
            "fixed_via_a": is_red_flag and is_fixed_referral,
            "a_result": result.model_dump(mode="json"),
        }
