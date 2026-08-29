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
    IMPLICIT_CONFIRM_BANNED_PHRASES,  # noqa: F401
    IMPLICIT_CONFIRM_TEMPLATE,
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


def build_implicit_confirm(raw_text: str, normalized_value: str) -> str:
    raw = (raw_text or "").strip()[:30]
    normalized = (normalized_value or "").strip()[:25]
    if not raw:
        raw = normalized[:30] if normalized else "剛才的內容"
    if not normalized:
        normalized = raw
    return IMPLICIT_CONFIRM_TEMPLATE.format(raw=raw, normalized=normalized)


def build_implicit_confirm_for_fields(
    extracted: dict[str, Any],
    raw_text: str | None = None,
) -> str | None:
    if not extracted:
        return None
    filtered = {k: v for k, v in extracted.items() if not k.startswith("_")}
    if not filtered:
        return None
    parts: list[str] = []
    for _field, value in list(filtered.items())[:2]:
        if isinstance(value, list):
            parts.append("、".join(str(x) for x in value if str(x).strip()))
        elif value is not None:
            parts.append(str(value).strip())
    normalized = "；".join(p for p in parts if p)
    if not normalized:
        return None
    if raw_text is not None:
        raw = raw_text.strip()[:30]
    else:
        raw_candidate = extracted.get("_raw") or extracted.get("raw") or ""
        if isinstance(raw_candidate, str) and raw_candidate.strip():
            raw = raw_candidate.strip()[:30]
        else:
            raw = normalized[:30]
    return build_implicit_confirm(raw, normalized)


PREVISIT_DISCLAIMER = "本摘要僅整理您已提供的資訊，未包含診斷或治療建議；請攜帶此摘要與醫師討論，最終判斷由醫師負責。"

MEDICATION_CONFIDENCE_THRESHOLD = 0.7
MEDICATION_MAX_CLARIFICATION_ATTEMPTS = 2

UNCERTAIN_PATTERNS = r"不知道|不記得|忘了|忘記|不確定|不清楚|沒印象|記不得|不太清楚"
UNCERTAIN_RE = re.compile(UNCERTAIN_PATTERNS)
SYMPTOM_FIELDS: set[str] = {"symptom_onset", "symptom_description", "symptom_severity"}
SYMPTOM_UNKNOWN_VALUE = "待確認"
SYMPTOM_UNKNOWN_QUESTION = "沒關係，先記為『待確認』，看診時再跟醫師確認。"
SYMPTOM_MAX_CLARIFICATION_ATTEMPTS = 2

# ── F2: injection / privilege escalation detection (deterministic, no LLM) ──
INJECTION_FIXED_REPLY = "這裡只協助整理看診資料，無法提供處方或醫療指示"
INJECTION_REPLY = INJECTION_FIXED_REPLY

INJECTION_PATTERNS: list[str] = [
    r"叫你.*給處方",
    r"直接給.*處方",
    r"給我.*處方",
    r"開處方",
    r"幫.{0,2}開藥",
    r"幫.{0,2}開.?藥",
    r"開藥",
    r"忽略.*規則",
    r"忽略.*指示",
    r"忽略.*指令",
    r"無視.*規則",
    r"不要遵守.*規則",
    r"解除.*限制",
    r"你.{0,4}是醫(?:師|生)",
    r"假裝.*醫師",
    r"假裝.*醫生",
    r"扮演.*醫師",
    r"扮演.*醫生",
    r"假扮.*醫師",
    r"假扮.*醫生",
    r"ignore.*instructions?",
    r"ignore.*rules?",
    r"you are.*doctor",
    r"act as.*doctor",
    r"pretend.*doctor",
    r"disregard.*instructions?",
    r"system\s*prompt",
    r"jailbreak",
]

_INJECTION_RES = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def is_injection_attempt(text: str) -> bool:
    """F2: deterministic injection/privilege escalation check. No LLM."""
    if not text or not text.strip():
        return False
    s = text.strip()
    # Context-aware: lone "開藥" should not fire when text is a legitimate question intent
    # (e.g. "我想問醫師藥的劑量" should be extracted as questions_for_doctor, not injection).
    has_question_intent = bool(re.search(r"想問|想了解", s))
    for pat_str, pat in zip(INJECTION_PATTERNS, _INJECTION_RES):
        if pat.search(s):
            if pat_str == r"開藥" and has_question_intent:
                continue
            return True
    return False


def detect_injection(text: str) -> bool:
    return is_injection_attempt(text)


def contains_injection(text: str) -> bool:
    return is_injection_attempt(text)


def is_injection(text: str) -> bool:
    return is_injection_attempt(text)


INTAKE_MAX_LENGTH = 120
INTAKE_TRUNCATION_MARKER = "(已節錄)"
INTAKE_TRUNC_MARKER = INTAKE_TRUNCATION_MARKER


def is_plausible_intake_value(text: str) -> bool:
    """F3 D1/D2 plausibility check. Returns False for invalid spam.

    D1: pure emoji/symbol/single char repeated
    D2: same token repeated >=5 times or distinct chars <4 (with length guard)
    Length >120 is still plausible (handled via truncation, not invalid).
    """
    if not text or not text.strip():
        return False
    s = text.strip()

    if not re.search(r"[\w\u4e00-\u9fa5]", s):
        return False
    compact = re.sub(r"\s+", "", s)
    if len(compact) >= 2 and len(set(compact)) == 1:
        return False
    if re.fullmatch(r"(.)\1{2,}", compact):
        return False
    if len(s) < 2:
        return False
    if re.fullmatch(r"[^\w\u4e00-\u9fa5]+", s):
        return False

    try:
        if re.search(r"(.{1,10})\1{4,}", s):
            m = re.search(r"(.{1,10})\1{4,}", s)
            if m and len(m.group(0)) >= 5:
                return False
    except Exception:
        pass

    try:
        tokens = re.split(r"\s+", s)
        if len(tokens) >= 5:
            for i in range(len(tokens) - 4):
                if tokens[i] and all(t == tokens[i] for t in tokens[i : i + 5]):
                    return False
            from collections import Counter

            cnt = Counter(tokens)
            most_common, most_count = cnt.most_common(1)[0] if cnt else ("", 0)
            if most_count >= 5 and len(tokens) >= 5:
                if len(set(tokens)) < 4:
                    return False
    except Exception:
        pass

    compact_no_space = re.sub(r"\s+", "", compact)
    compact_for_distinct = re.sub(r"[，。,\.，、；;！!？?·•\-—_—#\/\*\(\)\[\]{}]", "", compact_no_space)
    distinct = set(compact_for_distinct)
    if len(compact_for_distinct) >= 8 and len(distinct) < 4:
        return False
    if len(compact_for_distinct) >= 5 and len(distinct) < 3:
        return False
    if len(compact_for_distinct) >= 10 and len(distinct) < 4:
        return False

    return True


def is_plausible(text: str) -> bool:
    return is_plausible_intake_value(text)


def truncate_intake_value(text: str, limit: int = INTAKE_MAX_LENGTH) -> tuple[str, bool]:
    if not text:
        return text, False
    s = text.strip()
    if len(s) > limit:
        return s[:limit], True
    return s, False


def truncate_intake_text(text: str, limit: int = INTAKE_MAX_LENGTH) -> str:
    if not text:
        return text
    s = text.strip()
    if len(s) > limit:
        return s[:limit]
    return s


def is_uncertain_answer(text: str) -> bool:
    if not text or not text.strip():
        return False
    if "不太知道" in text:
        return True
    return bool(UNCERTAIN_RE.search(text))


def handle_symptom_clarification(field: str, utterance: str, attempt: int = 1) -> dict[str, Any]:
    if is_uncertain_answer(utterance):
        return {
            "status": "unknown",
            "field": field,
            "value": SYMPTOM_UNKNOWN_VALUE,
            "question": SYMPTOM_UNKNOWN_QUESTION,
            "attempt": attempt,
        }
    if attempt >= SYMPTOM_MAX_CLARIFICATION_ATTEMPTS:
        return {
            "status": "unknown",
            "field": field,
            "value": SYMPTOM_UNKNOWN_VALUE,
            "question": SYMPTOM_UNKNOWN_QUESTION,
            "attempt": attempt,
        }
    next_attempt = attempt + 1
    q_text = INTAKE_FIELD_QUESTIONS.get(field, f"請補充：{field}")
    return {
        "status": "need_clarify",
        "field": field,
        "attempt": next_attempt,
        "question": q_text,
        "reason": "symptom_needs_clarify",
    }


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

        # Determine provided vs missing — 8 core fields (sentinel filtered)
        def _is_sentinel(v: Any) -> bool:
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

        # Build summary_text only from provided facts, no diagnosis/treatment (sentinel excluded)
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

    def is_uncertain_answer(self, text: str) -> bool:
        return is_uncertain_answer(text)

    def handle_symptom_clarification(self, field: str, utterance: str, *, attempt: int = 1) -> dict[str, Any]:
        return handle_symptom_clarification(field, utterance, attempt)

    def handle_medication_clarification(self, utterance: str, *, attempt: int) -> dict[str, Any]:
        if is_uncertain_answer(utterance):
            return {
                "status": "unknown",
                "medications": [SYMPTOM_UNKNOWN_VALUE],
                "value": SYMPTOM_UNKNOWN_VALUE,
                "question": SYMPTOM_UNKNOWN_QUESTION,
                "reason": "medication_unknown_uncertain",
                "confidence": self.assess_medication_confidence(utterance),
                "original_text": utterance,
            }
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
        return {"status": "unknown", "medications": [unknown_text], "value": SYMPTOM_UNKNOWN_VALUE, "question": SYMPTOM_UNKNOWN_QUESTION, "reason": "medication_unknown_after_2_attempts", "confidence": confidence, "original_text": utterance}

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

    def format_stage_progress(self, intake: PreVisitIntake | dict[str, Any]) -> str:
        return format_stage_progress(intake)

    def get_stage_progress_text(self, intake: PreVisitIntake | dict[str, Any]) -> str:
        return format_stage_progress(intake)

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
        # Look for symptom descriptions - collect all matching sentences
        # Include colloquial variants that map via NORMALIZATION_MAP (嘴巴乾→口乾, 跑廁所→頻尿)
        symptom_keywords = ["血糖", "頭暈", "口渴", "頻尿", "疲倦", "不舒服", "疼痛", "麻", "視力", "傷口", "感染", "血壓", "尿尿", "夜尿", "起夜", "多尿", "嘴巴乾", "嘴巴很乾", "口乾", "跑廁所", "上廁所", "口乾舌燥", "很渴", "很乾"]
        sentences = re.split(r"[。；;，,]", text)
        found: list[str] = []
        for s in sentences:
            if any(kw in s for kw in symptom_keywords):
                if s.strip():
                    found.append(s.strip())
        if found:
            return "；".join(found)[:2000]
        # If text is long and contains symptom-like content, return it
        if len(text) > 5 and re.search(r"症狀|不適|感覺|血糖|血壓", text):
            return text.strip()[:200]
        return None

    def _extract_severity(self, text: str) -> str | None:
        HEDGE_RE = re.compile(r"有點|稍微|好像|吧|大概|有點嚴重")
        if HEDGE_RE.search(text):
            if text.strip() in ("有點嚴重吧", "有點嚴重", "稍微嚴重"):
                return None
            cleaned = text.replace("有點嚴重吧", "").replace("有點嚴重", "").replace("稍微嚴重", "")
            if not re.search(r"\d+分|輕度|中度|重度|\d+/\d+", cleaned):
                return None
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
        if not re.search(r"想問|想請問|？|\?|嗎|如何|怎麼|為何|為什麼|多少|是否", text):
            return None
        if re.search(r"想問|想請問|想了解|問題是|疑問", text):
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
        if len(text.strip()) > 5:
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
STAGE_LABELS: dict[str, str] = {"stage1": "用藥與過敏", "stage2": "症狀", "stage3": "想問醫師"}
_STAGE_ORDER: list[str] = ["stage1", "stage2", "stage3"]


def _normalize_intake(intake: PreVisitIntake | dict[str, Any] | None) -> PreVisitIntake:
    if intake is None:
        return PreVisitIntake()
    if isinstance(intake, dict):
        try:
            return PreVisitIntake.model_validate(intake)
        except Exception:
            return PreVisitIntake()
    if isinstance(intake, PreVisitIntake):
        return intake
    try:
        return PreVisitIntake.model_validate(intake)  # type: ignore[arg-type]
    except Exception:
        return PreVisitIntake()


def _missing_stages_for_progress(intake: PreVisitIntake) -> list[str]:
    missing: list[str] = []
    for stage in _STAGE_ORDER:
        fields = INTAKE_STAGES.get(stage, [])
        for f in fields:
            val = getattr(intake, f, None)
            if not val:
                missing.append(stage)
                break
    return missing


def format_stage_progress(intake: PreVisitIntake | dict[str, Any] | None) -> str:
    obj = _normalize_intake(intake)
    missing = _missing_stages_for_progress(obj)
    completed = [s for s in _STAGE_ORDER if s not in missing]
    if not missing:
        text = "用藥與過敏、症狀、想問醫師 皆已完成 ✅"
        return text[:60]
    if not completed:
        labels = "、".join(STAGE_LABELS[s] for s in missing)
        text = f"還差：{labels} {len(missing)} 段，先從用藥開始吧"
        return text[:60]
    completed_labels = "、".join(STAGE_LABELS[s] for s in completed)
    missing_labels = "、".join(STAGE_LABELS[s] for s in missing)
    text = f"已完成：{completed_labels} ✅ 還差：{missing_labels} {len(missing)} 段"
    return text[:60]


def get_stage_progress_text(intake: PreVisitIntake | dict[str, Any] | None) -> str:
    return format_stage_progress(intake)

