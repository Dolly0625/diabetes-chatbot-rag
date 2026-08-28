from __future__ import annotations

import re
import unicodedata

from .labels import (
    IntentTag,
    LanguageCode,
    Polarity,
    RiskFlag,
    TargetSubject,
    TimeFrame,
)
from .schemas import ContextModifiers, RouterSignals


class InputValidationError(ValueError):
    """The request is structurally valid but cannot be safely normalized."""


def normalize_input(raw_input: str) -> str:
    normalized = unicodedata.normalize("NFKC", raw_input).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        raise InputValidationError("user_raw_input is empty after normalization")
    return normalized


class RuleBasedSignalExtractor:
    """Small, explainable demo extractor; it is not a clinical triage engine.

    The rules cover taxonomy/policy-boundary examples documented for the MVP.
    No glucose, symptom-severity, age, or other clinical threshold is inferred.
    """

    _injection = re.compile(
        r"忽略(?:前面|以上|所有)?規則|忘記(?:你的)?指示|解除限制|揭露(?:系統|提示|system prompt)|"
        r"ignore\s+(?:all\s+)?(?:previous|prior|以上)?\s*instructions?|system\s+prompt|"
        r"jailbreak|developer\s+message",
        re.IGNORECASE,
    )
    _med_change = re.compile(
        r"停藥|停止(?:服用|吃)|不要吃(?:藥)?|加量|減量|調整(?:劑量|用藥)|換藥|改藥|"
        r"藥停掉|停掉.{0,6}藥|再補一顆|多吃一顆|自行(?:調整|增加|減少).{0,8}(?:藥|劑量|胰島素)|"
        r"increase\s+(?:my\s+)?dose|decrease\s+(?:my\s+)?dose|stop\s+taking",
        re.IGNORECASE,
    )
    _diagnosis = re.compile(
        r"(?:我|本人).{0,8}(?:是不是|是否為|有沒有).{0,12}(?:糖尿病|高血糖|低血糖)|"
        r"幫我診斷|替我診斷|請診斷|幫我排除(?:疾病|糖尿病)|"
        r"(?:am\s+i|do\s+i\s+have)\s+(?:diabetes|diabetes mellitus)",
        re.IGNORECASE,
    )
    _general_medication = re.compile(r"藥物|藥品|副作用|用途|作用|怎麼吃藥|insulin|metformin", re.IGNORECASE)
    _symptoms = re.compile(
        r"症狀|口渴|頻尿|冒冷汗|發抖|頭暈|胸痛|喘不過氣|呼吸困難|意識不清|昏迷|"
        r"symptom|dizzy|sweat|chest pain|shortness of breath",
        re.IGNORECASE,
    )
    _diabetes_scope = re.compile(
        r"糖尿病|血糖|胰島素|低血糖|高血糖|SGLT2|飲食|運動|血糖監測|diabetes|glucose|insulin",
        re.IGNORECASE,
    )
    _out_of_scope = re.compile(
        r"天氣|股票|股價|寫程式|python|政治|訂餐|導航|遊戲|寫詩|笑話|"
        r"weather|stock price|write code|recipe for dinner",
        re.IGNORECASE,
    )

    def extract(self, text: str, language: LanguageCode | None = None) -> RouterSignals:
        text = normalize_input(text)
        intents: list[IntentTag] = []
        risks: list[RiskFlag] = []

        if self._injection.search(text):
            risks.append(RiskFlag.PROMPT_INJECTION_SUSPECTED)
        if self._med_change.search(text):
            intents.append(IntentTag.MEDICATION_CHANGE_REQUEST)
            risks.append(RiskFlag.PERSONALIZED_MEDICATION)
        if self._diagnosis.search(text):
            intents.append(IntentTag.DIAGNOSIS_REQUEST)
        if self._symptoms.search(text):
            intents.append(IntentTag.SYMPTOM_INFORMATION)
        if self._general_medication.search(text) and not self._med_change.search(text):
            intents.append(IntentTag.GENERAL_MEDICATION_INFORMATION)
        if self._diabetes_scope.search(text) and not self._general_medication.search(text):
            intents.append(IntentTag.GENERAL_EDUCATION)
        if self._out_of_scope.search(text) and not self._diabetes_scope.search(text):
            intents.append(IntentTag.NON_MEDICAL)

        # Explicit injection text is a security signal, not a policy override.
        # The actual route remains determined by the other observable signals.
        if not intents and self._out_of_scope.search(text):
            intents.append(IntentTag.NON_MEDICAL)

        modifiers = ContextModifiers(
            time_frame=self._time_frame(text),
            target_subject=self._target_subject(text),
            polarity=(Polarity.NEGATIVE if self._is_negative(text) else Polarity.AFFIRMATIVE),
            language=language or LanguageCode.ZH_TW,
        )
        return RouterSignals(
            intent_tags=self._unique(intents),
            risk_flags=self._unique(risks),
            context_modifiers=modifiers,
        )

    @staticmethod
    def _unique(values):
        return list(dict.fromkeys(values))

    @staticmethod
    def _time_frame(text: str) -> TimeFrame:
        if re.search(r"如果|假設|萬一|hypothetical|what if", text, re.IGNORECASE):
            return TimeFrame.HYPOTHETICAL
        if re.search(r"昨天|之前|過去|曾經|last\s+(?:week|month)|previously", text, re.IGNORECASE):
            return TimeFrame.PAST
        return TimeFrame.CURRENT

    @staticmethod
    def _target_subject(text: str) -> TargetSubject:
        if re.search(r"媽媽|爸爸|家人|長輩|照護者|患者|他人|家屬", text):
            return TargetSubject.FAMILY_OR_CAREGIVER
        if re.search(r"朋友|同事|病人|第三人", text):
            return TargetSubject.THIRD_PARTY
        return TargetSubject.SELF

    @staticmethod
    def _is_negative(text: str) -> bool:
        return bool(re.search(r"沒有|無|(?<!是)不是|並未|否認|no\s+|not\s+|without", text, re.IGNORECASE))


def merge_signals(*signal_sets: RouterSignals) -> RouterSignals:
    """Union hard and model signals; no risk flag can be removed by a model."""
    intents = []
    risks = []
    context = signal_sets[0].context_modifiers
    for signals in signal_sets:
        for item in signals.intent_tags:
            if item not in intents:
                intents.append(item)
        for item in signals.risk_flags:
            if item not in risks:
                risks.append(item)
    return RouterSignals(intent_tags=intents, risk_flags=risks, context_modifiers=context)
