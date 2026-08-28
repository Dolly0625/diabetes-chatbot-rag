from __future__ import annotations

# 本模組定義全域枚舉標籤（Labels）：所有路由與政策判斷的詞彙表
# 設計要點：所有枚舉繼承 _CodeEnum（str, Enum），可直接與字串比較與序列化

from enum import Enum


class _CodeEnum(str, Enum):
    """字串枚舉基底：value 即字串本身，__str__ 回傳 value 方便日誌與序列化。"""

    def __str__(self) -> str:
        return self.value


class DeclaredRole(_CodeEnum):
    """宣告身分：使用者自稱角色，僅作語境參考，不具授權效力。"""

    PATIENT = "PATIENT"  # 病患本人
    CAREGIVER = "CAREGIVER"  # 照護者／家屬
    HEALTHCARE_PROFESSIONAL = "HEALTHCARE_PROFESSIONAL"  # 醫事人員


class LanguageCode(_CodeEnum):
    """語系代碼：標記輸入與回應語系。"""

    ZH_TW = "zh-TW"  # 繁體中文（台灣）
    ZH_CN = "zh-CN"  # 簡體中文
    EN_US = "en-US"  # 英文（美國）


class TimeFrame(_CodeEnum):
    """時間框架：描述事件發生的時間語境。"""

    CURRENT = "CURRENT"  # 當前／現在進行式
    PAST = "PAST"  # 過去曾發生
    HYPOTHETICAL = "HYPOTHETICAL"  # 假設／如果情境


class TargetSubject(_CodeEnum):
    """目標對象：問題所指涉的主體。"""

    SELF = "SELF"  # 使用者本人
    FAMILY_OR_CAREGIVER = "FAMILY_OR_CAREGIVER"  # 家人或照護對象
    THIRD_PARTY = "THIRD_PARTY"  # 第三人（朋友／同事等）


class Polarity(_CodeEnum):
    """語氣極性：肯定或否定，影響風險與意圖判讀。"""

    AFFIRMATIVE = "AFFIRMATIVE"  # 肯定語氣
    NEGATIVE = "NEGATIVE"  # 否定語氣（如「沒有」「不是」）


class IntentTag(_CodeEnum):
    """意圖標籤：使用者問題的語意分類（Layer 1 觀測）。"""

    GENERAL_EDUCATION = "GENERAL_EDUCATION"  # 一般衛教（糖尿病/血糖/飲食等通識）
    SYMPTOM_INFORMATION = "SYMPTOM_INFORMATION"  # 症狀資訊詢問
    DIAGNOSIS_REQUEST = "DIAGNOSIS_REQUEST"  # 要求診斷／排除疾病
    GENERAL_MEDICATION_INFORMATION = "GENERAL_MEDICATION_INFORMATION"  # 一般藥物通識（副作用/用途）
    MEDICATION_CHANGE_REQUEST = "MEDICATION_CHANGE_REQUEST"  # 要求調整用藥／劑量（高風險）
    NON_MEDICAL = "NON_MEDICAL"  # 非醫療範疇（天氣/股票/寫程式等）
    PRE_VISIT_INTAKE = "PRE_VISIT_INTAKE"  # 看診前整理（要看醫生/回診/準備看診等自然觸發）


class RiskFlag(_CodeEnum):
    """風險旗標：觸發政策閘門的高風險訊號。"""

    POSSIBLE_EMERGENCY = "POSSIBLE_EMERGENCY"  # 疑似急症（需緊急處理）
    MENTAL_HEALTH_CRISIS = "MENTAL_HEALTH_CRISIS"  # 心理危機／自傷風險
    PERSONALIZED_MEDICATION = "PERSONALIZED_MEDICATION"  # 個人化用藥請求
    HIGH_RISK_NOT_EXCLUDED = "HIGH_RISK_NOT_EXCLUDED"  # 高風險無法排除
    PROMPT_INJECTION_SUSPECTED = "PROMPT_INJECTION_SUSPECTED"  # 疑似提示注入攻擊（安全否決）


class RouterStatus(_CodeEnum):
    """路由狀態：A 路由器唯一輸出，8 選 1，決定下游行為。"""

    E_EMERGENCY = "E_EMERGENCY"  # 緊急：疑似急症，立即轉介急救
    U_URGENT_HUMAN = "U_URGENT_HUMAN"  # 緊急轉真人：心理危機或高風險未排除
    M_MEDICATION_REFERRAL = "M_MEDICATION_REFERRAL"  # 用藥轉介：個人化用藥需專業人員
    R_POLICY_BOUNDARY = "R_POLICY_BOUNDARY"  # 政策邊界：診斷請求或注入攻擊（安全否決）
    Q_CLARIFICATION = "Q_CLARIFICATION"  # 需釐清：資訊不足無法判斷
    G_GENERAL_EDUCATION = "G_GENERAL_EDUCATION"  # 一般衛教：唯一允許 RAG 的狀態
    O_OUT_OF_SCOPE = "O_OUT_OF_SCOPE"  # 超出範圍：非醫療問題
    F_ROUTER_DEPENDENCY = "F_ROUTER_DEPENDENCY"  # 依賴失效：LLM 超時/格式錯誤等（fail-closed）


class PolicyReasonCode(_CodeEnum):
    """政策原因碼：解釋路由決策的依據，供日誌與稽核。"""

    INQUIRY_GENERAL_EDUCATION = "INQUIRY_GENERAL_EDUCATION"  # 一般衛教詢問
    INQUIRY_DIETARY_EDUCATION = "INQUIRY_DIETARY_EDUCATION"  # 飲食衛教詢問
    INQUIRY_SYMPTOM_INFORMATION = "INQUIRY_SYMPTOM_INFORMATION"  # 症狀資訊詢問
    INQUIRY_GENERAL_MEDICATION_INFORMATION = "INQUIRY_GENERAL_MEDICATION_INFORMATION"  # 一般藥物資訊詢問
    REASON_DIAGNOSIS_OR_TREATMENT_REQUEST = "REASON_DIAGNOSIS_OR_TREATMENT_REQUEST"  # 診斷或治療請求（政策邊界）
    REASON_PERSONALIZED_MEDICATION_REQUEST = "REASON_PERSONALIZED_MEDICATION_REQUEST"  # 個人化用藥請求
    REASON_POSSIBLE_EMERGENCY = "REASON_POSSIBLE_EMERGENCY"  # 疑似急症
    REASON_MENTAL_HEALTH_CRISIS = "REASON_MENTAL_HEALTH_CRISIS"  # 心理危機
    REASON_HIGH_RISK_NOT_EXCLUDED = "REASON_HIGH_RISK_NOT_EXCLUDED"  # 高風險未排除
    REASON_PROMPT_INJECTION_SUSPECTED = "REASON_PROMPT_INJECTION_SUSPECTED"  # 疑似提示注入
    REASON_OUT_OF_SCOPE = "REASON_OUT_OF_SCOPE"  # 超出醫療範圍
    REASON_INSUFFICIENT_INFORMATION = "REASON_INSUFFICIENT_INFORMATION"  # 資訊不足需釐清
    NO_CRITICAL_SYMPTOMS_DETECTED = "NO_CRITICAL_SYMPTOMS_DETECTED"  # 未檢出危急症狀（G 路由佐證）
    MEETS_SAFE_SCOPE = "MEETS_SAFE_SCOPE"  # 符合安全範圍（G 路由佐證）
    REASON_ROUTER_TIMEOUT = "REASON_ROUTER_TIMEOUT"  # 路由超時（F 依賴失效）
    REASON_SCHEMA_VALIDATION_FAILED = "REASON_SCHEMA_VALIDATION_FAILED"  # 架構驗證失敗（F 依賴失效）
    REASON_ROUTER_DEPENDENCY_ERROR = "REASON_ROUTER_DEPENDENCY_ERROR"  # 路由依賴錯誤（F 依賴失效）
    REASON_INPUT_VALIDATION_FAILED = "REASON_INPUT_VALIDATION_FAILED"  # 輸入驗證失敗（F 依賴失效）
