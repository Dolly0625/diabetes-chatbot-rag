from __future__ import annotations

import re

from .schemas import AgentDecision, AgentDecisionContext, AskUserDecision, FallbackDecision, RewriteQueryDecision


class DeterministicClarificationPolicy:
    """Risk-first 之後的必要事實釐清；不推定醫療事實、不回答問題。"""

    _VAGUE_SYMPTOM = re.compile(r"(?:我|本人|家人|媽媽|爸爸).{0,8}(?:不舒服|怪怪的|症狀|狀況不好)(?:怎麼辦|嗎|$)")
    _UNNAMED_MED = re.compile(r"(?:這個|那個|我的|家人的|媽媽的|爸爸的).{0,5}(?:藥|藥物).{0,12}(?:副作用|用途|怎麼吃|注意)")

    @classmethod
    def identify_required_facts(cls, query: str) -> list[str]:
        """只標示回答本題不可或缺、且必須由使用者提供的欄位。"""

        gaps: list[str] = []
        if cls._UNNAMED_MED.search(query) and not re.search(r"metformin|胰島素|insulin|SGLT2|藥名[：:]?\s*\S+", query, re.IGNORECASE):
            gaps.append("medicine_name")
        if cls._VAGUE_SYMPTOM.search(query):
            gaps.append("symptom_description")
        return gaps[:4]

    def decide(
        self,
        context: AgentDecisionContext,
        *,
        allow_rewrite: bool,
    ) -> AgentDecision:
        if context.identified_missing_information:
            return AskUserDecision(
                action="ASK_USER",
                reason_code="MISSING_REQUIRED_CONTEXT",
                missing_information=context.identified_missing_information[:4],
            )
        if allow_rewrite and not context.previous_attempts:
            return RewriteQueryDecision(
                action="REWRITE_QUERY",
                reason_code="QUERY_FORMULATION_NEEDS_REWRITE",
            )
        return FallbackDecision(action="FALLBACK", reason_code="RECOVERY_EXHAUSTED")

    @staticmethod
    def build_question(missing: list[str], declared_role: str) -> str | None:
        field = missing[0] if missing else None
        role = str(declared_role)
        if field in {"medicine_name", "medication_class", "drug_type", "known_medications"}:
            if role == "HEALTHCARE_PROFESSIONAL":
                return "請補充藥品學名、商品名或藥物類別。"
            if role == "CAREGIVER":
                return "這是您代家人轉述的資料嗎？請查看藥袋，補充藥名或成分；若不確定可回答「待確認」。"
            return "請查看藥袋，告訴我藥名或成分；若目前不確定，可以回答「待確認」。"
        if field in {"symptom", "symptom_description"}:
            if role == "HEALTHCARE_PROFESSIONAL":
                return "請補充主要症狀、發生時間及目前變化。"
            if role == "CAREGIVER":
                return "這是家人本人描述，還是您的觀察？請先說最主要的不舒服，以及何時開始。"
            return "請先說最主要的不舒服是什麼，以及大約何時開始；例如「今天早上開始頭暈」。"
        return None
