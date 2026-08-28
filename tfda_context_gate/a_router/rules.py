from __future__ import annotations

import re
import unicodedata

from tfda_context_gate.clinical_safety import RiskSignalPolicy

from .labels import (
    IntentTag,
    LanguageCode,
    Polarity,
    RiskFlag,
    TargetSubject,
    TimeFrame,
)
from .schemas import ContextModifiers, RouterSignals


# 本模組提供輸入正規化與規則式訊號萃取（管線第 2、4 步）
# 關鍵原則：僅做可解釋的關鍵字匹配，不做臨床閾值推斷；風險旗標不可被模型移除

class InputValidationError(ValueError):
    """輸入驗證錯誤：結構合法但正規化後為空，無法安全處理。
    The request is structurally valid but cannot be safely normalized."""

    # 觸發時機：normalize_input 後為空字串 → 上游應回 F_ROUTER_DEPENDENCY


def normalize_input(raw_input: str) -> str:
    """正規化使用者輸入；輸入：原始字串，輸出：NFKC 正規化並壓縮空白後的字串；若為空則拋 InputValidationError。"""
    normalized = unicodedata.normalize("NFKC", raw_input).strip()  # NFKC 正規化（全形轉半形等）並去頭尾空白
    normalized = re.sub(r"\s+", " ", normalized)  # 連續空白壓為單一空格
    if not normalized:
        raise InputValidationError("user_raw_input is empty after normalization")  # 空輸入視為驗證失敗
    return normalized


class RuleBasedSignalExtractor:
    """規則式訊號萃取器：小而可解釋的 demo 實作，非臨床檢傷引擎。
    僅覆蓋 MVP 文件中的分類／政策邊界範例，不推斷血糖、症狀嚴重度、年齡等臨床閾值。
    Small, explainable demo extractor; it is not a clinical triage engine.

    The rules cover taxonomy/policy-boundary examples documented for the MVP.
    No glucose, symptom-severity, age, or other clinical threshold is inferred.
    """

    _injection = re.compile(
        r"忽略(?:前面|以上|所有)?規則|忘記(?:你的)?指示|解除限制|揭露(?:系統|提示|system prompt)|"
        r"ignore\s+(?:all\s+)?(?:previous|prior|以上)?\s*instructions?|system\s+prompt|"
        r"jailbreak|developer\s+message",
        re.IGNORECASE,
    )  # 注入攻擊正則：中英文越獄關鍵字（忽略規則/忘記指示/揭露系統提示/jailbreak）
    _med_change = re.compile(
        r"停藥|停止(?:服用|吃)|不要吃(?:藥)?|加量|減量|調整(?:劑量|用藥)|換藥|改藥|"
        r"藥停掉|停掉.{0,6}藥|再補一顆|多吃一顆|自行(?:調整|增加|減少).{0,8}(?:藥|劑量|胰島素)|"
        r"increase\s+(?:my\s+)?dose|decrease\s+(?:my\s+)?dose|stop\s+taking",
        re.IGNORECASE,
    )  # 用藥變更正則：停藥/加減量/換藥/自行調整劑量等（觸發 PERSONALIZED_MEDICATION）
    _diagnosis = re.compile(
        r"(?:我|本人).{0,8}(?:是不是|是否為|有沒有).{0,12}(?:糖尿病|高血糖|低血糖)|"
        r"幫我診斷|替我診斷|請診斷|幫我排除(?:疾病|糖尿病)|"
        r"(?:am\s+i|do\s+i\s+have)\s+(?:diabetes|diabetes mellitus)",
        re.IGNORECASE,
    )  # 診斷請求正則：自稱病患並詢問是否罹病／要求診斷
    _general_medication = re.compile(r"藥物|藥品|副作用|用途|作用|怎麼吃藥|insulin|metformin", re.IGNORECASE)  # 一般藥物資訊正則：副作用/用途等通識
    _symptoms = re.compile(
        r"症狀|口渴|頻尿|冒冷汗|發抖|頭暈|胸痛|喘不過氣|呼吸困難|意識不清|昏迷|"
        r"symptom|dizzy|sweat|chest pain|shortness of breath",
        re.IGNORECASE,
    )  # 症狀正則：常見糖尿病相關與急症症狀關鍵字
    _diabetes_scope = re.compile(
        r"糖尿病|血糖|胰島素|低血糖|高血糖|SGLT2|飲食|運動|血糖監測|diabetes|glucose|insulin",
        re.IGNORECASE,
    )  # 糖尿病範疇正則：判斷是否屬本專案醫療範圍
    _out_of_scope = re.compile(
        r"天氣|股票|股價|寫程式|python|政治|訂餐|導航|遊戲|寫詩|笑話|"
        r"weather|stock price|write code|recipe for dinner",
        re.IGNORECASE,
    )  # 超範圍正則：天氣/股票/程式/政治等非醫療主題
    # G2 chit-chat whitelist: benign short sentences → O_OUT_OF_SCOPE (not RISK_FLAG)
    # 僅含無害閒聊，優先於 _out_of_scope 判斷；Keep Traditional Chinese
    _chit_chat = re.compile(
        r"想睡覺|想睡了|無聊|你好|哈囉|晚安|你好嗎|嗨|"
        r"你可以跟我說什麼|你可以說什麼|你能做什麼|你能幫什麼|"
        r"可以跟我說什麼|能做什麼|能幫我做什麼|功能介紹|我能問什麼|你會做什麼|系統能做什麼",
        re.IGNORECASE,
    )
    # MENTAL_HEALTH_CRISIS 窄範圍：僅明確自傷/自殺字眼，絕不含「想睡覺」「想睡」「休息」等睡眠詞
    _mental_health_crisis = re.compile(
        r"想自殺|自殺|不想活|活不下去|想死|輕生|結束生命|自殘|割腕|自傷",
        re.IGNORECASE,
    )
    _emergency = re.compile(
        r"胸痛|胸悶|喘不過氣|呼吸困難|呼吸急促|意識不清|昏迷|昏厥|"
        r"冒冷汗.*胸|大量出血|持續嘔吐|高燒不退|"
        r"chest pain|shortness of breath|unconscious|emergency|severe chest",
        re.IGNORECASE,
    )
    _intake = re.compile(
        r"準備看診|看診前|整理.*資料|已知用藥|症狀.*整理|想問醫師|pre.?visit|intake"
        r"|要看醫生|回診|下週.*看醫生|下週.*看診|下週.*回診|回診.*整理|下週.*看",
        re.IGNORECASE,
    )
    # Natural pre-visit trigger (proactive, without button) — must be medical-context aware
    # Covers: 要看醫生 / 回診 / 準備看診 / 下週看醫生 / 回診整理 etc.
    # Guard: broad "下週.*看" only triggers when medical context present (醫生/診/回診/血糖/藥)
    _pre_visit_natural = re.compile(
        r"要看醫生|回診|準備看診|回診.*整理|下週.*看醫生|下週.*看診|下週.*回診",
        re.IGNORECASE,
    )
    _broad_next_week = re.compile(r"下週.*看", re.IGNORECASE)
    _medical_context = re.compile(r"醫生|診|回診|血糖|藥|症狀|看診", re.IGNORECASE)

    RED_FLAG_PATTERN = _emergency

    @staticmethod
    def _is_pre_visit_intake(text: str) -> bool:
        if RuleBasedSignalExtractor._pre_visit_natural.search(text):
            return True
        if RuleBasedSignalExtractor._broad_next_week.search(text) and RuleBasedSignalExtractor._medical_context.search(text):
            return True
        if RuleBasedSignalExtractor._intake.search(text):
            if RuleBasedSignalExtractor._broad_next_week.search(text) and not RuleBasedSignalExtractor._medical_context.search(text):
                return False
            return True
        return False

    @staticmethod
    def is_pre_visit_intake_text(text: str) -> bool:
        try:
            normalized = normalize_input(text)
        except InputValidationError:
            return False
        return RuleBasedSignalExtractor._is_pre_visit_intake(normalized)

    @staticmethod
    def is_chit_chat_text(text: str) -> bool:
        try:
            normalized = normalize_input(text)
        except InputValidationError:
            return False
        return bool(RuleBasedSignalExtractor._chit_chat.search(normalized))

    def extract(self, text: str, language: LanguageCode | None = None) -> RouterSignals:
        """萃取訊號；輸入：原始文字與語系，輸出：RouterSignals（意圖+風險+語境）；流程：正規化→逐正則匹配→去重→組裝。"""
        text = normalize_input(text)  # 第 2 步：先正規化（NFKC+空白壓縮）
        intents: list[IntentTag] = []
        risks: list[RiskFlag] = []

        if self._injection.search(text):
            risks.append(RiskFlag.PROMPT_INJECTION_SUSPECTED)
        if RiskSignalPolicy().classify(text).level == "RED_FLAG":
            risks.append(RiskFlag.POSSIBLE_EMERGENCY)
        if self._mental_health_crisis.search(text):
            risks.append(RiskFlag.MENTAL_HEALTH_CRISIS)
        if self._chit_chat.search(text):
            intents.append(IntentTag.NON_MEDICAL)
        is_intake = self._is_pre_visit_intake(text)
        if is_intake:
            intents.append(IntentTag.PRE_VISIT_INTAKE)
            intents.append(IntentTag.GENERAL_EDUCATION)
            intents.append(IntentTag.SYMPTOM_INFORMATION)
        if self._med_change.search(text):
            intents.append(IntentTag.MEDICATION_CHANGE_REQUEST)
            risks.append(RiskFlag.PERSONALIZED_MEDICATION)
        if self._diagnosis.search(text):
            intents.append(IntentTag.DIAGNOSIS_REQUEST)
        if self._symptoms.search(text):
            intents.append(IntentTag.SYMPTOM_INFORMATION)
        if self._general_medication.search(text) and not self._med_change.search(text) and not is_intake:
            intents.append(IntentTag.GENERAL_MEDICATION_INFORMATION)
        if self._diabetes_scope.search(text) and not self._general_medication.search(text):
            intents.append(IntentTag.GENERAL_EDUCATION)
        if is_intake and IntentTag.GENERAL_EDUCATION not in intents:
            intents.append(IntentTag.GENERAL_EDUCATION)
        if self._out_of_scope.search(text) and not self._diabetes_scope.search(text):
            intents.append(IntentTag.NON_MEDICAL)  # 超範圍（僅當非糖尿病範疇時）

        # Explicit injection text is a security signal, not a policy override.
        # The actual route remains determined by the other observable signals.
        # 注入文字僅作安全訊號，不直接覆蓋政策路由；最終路由由 policy_gate 決定
        if not intents and self._out_of_scope.search(text):
            intents.append(IntentTag.NON_MEDICAL)  # 兜底：若仍無意圖但含超範圍詞，補 NON_MEDICAL

        modifiers = ContextModifiers(
            time_frame=self._time_frame(text),  # 萃取時間框架
            target_subject=self._target_subject(text),  # 萃取目標對象
            polarity=(Polarity.NEGATIVE if self._is_negative(text) else Polarity.AFFIRMATIVE),  # 萃取語氣極性
            language=language or LanguageCode.ZH_TW,  # 語系沿用或預設繁中
        )
        return RouterSignals(
            intent_tags=self._unique(intents),  # 去重後意圖
            risk_flags=self._unique(risks),  # 去重後風險
            context_modifiers=modifiers,
        )

    @staticmethod
    def _unique(values):
        """去重保序：利用 dict.fromkeys 保持首次出現順序。"""
        return list(dict.fromkeys(values))

    @staticmethod
    def _time_frame(text: str) -> TimeFrame:
        """判斷時間框架；輸入：文字，輸出：HYPOTHETICAL/PAST/CURRENT（依序匹配）。"""
        if re.search(r"如果|假設|萬一|hypothetical|what if", text, re.IGNORECASE):
            return TimeFrame.HYPOTHETICAL  # 含假設詞 → 假設情境
        if re.search(r"昨天|之前|過去|曾經|last\s+(?:week|month)|previously", text, re.IGNORECASE):
            return TimeFrame.PAST  # 含過去詞 → 過去
        return TimeFrame.CURRENT  # 預設當前

    @staticmethod
    def _target_subject(text: str) -> TargetSubject:
        """判斷目標對象；輸入：文字，輸出：FAMILY_OR_CAREGIVER/THIRD_PARTY/SELF。"""
        if re.search(r"媽媽|爸爸|家人|長輩|照護者|患者|他人|家屬", text):
            return TargetSubject.FAMILY_OR_CAREGIVER  # 家人/照護相關詞
        if re.search(r"朋友|同事|病人|第三人", text):
            return TargetSubject.THIRD_PARTY  # 第三人相關詞
        return TargetSubject.SELF  # 預設本人

    @staticmethod
    def _is_negative(text: str) -> bool:
        """判斷是否為否定語氣；輸入：文字，輸出：是否含否定詞（沒有/不是/not 等）。"""
        return bool(re.search(r"沒有|無|(?<!是)不是|並未|否認|no\s+|not\s+|without", text, re.IGNORECASE))


def is_red_flag(text: str) -> bool:
    try:
        normalized = normalize_input(text)
    except InputValidationError:
        return False
    return RiskSignalPolicy().classify(normalized).level == "RED_FLAG"


def merge_signals(*signal_sets: RouterSignals) -> RouterSignals:
    """合併多組訊號（聯集）；輸入：多個 RouterSignals，輸出：合併後 RouterSignals；關鍵：風險旗標只能聯集不可移除（模型不可洗掉硬規則風險）。
    Union hard and model signals; no risk flag can be removed by a model."""
    intents = []  # 合併後意圖（保序去重）
    risks = []  # 合併後風險（保序去重，模型不可移除硬規則風險）
    context = signal_sets[0].context_modifiers  # 語境以第一組為準（通常為模型訊號）
    for signals in signal_sets:
        for item in signals.intent_tags:
            if item not in intents:
                intents.append(item)
        for item in signals.risk_flags:
            if item not in risks:
                risks.append(item)
    return RouterSignals(intent_tags=intents, risk_flags=risks, context_modifiers=context)
