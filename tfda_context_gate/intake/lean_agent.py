"""Lean Intake Agent — 輕量、口語化、基於 3 階段主題式（AMIE 雙軌架構）的看診前 AI 助理。

特色：
1. 【3 階段主題式對話】：用藥病史 → 本次症狀與程度 → 醫病提問（2~3 輪即可完成，告別 8 題逐題拷問）。
2. 【機會主義式萃取 (Opportunistic Slot Filling)】：病患一句話講多項，一次全部填入，只追問缺漏項。
3. 【醫療同理心承接 (Empathetic Bridging)】：真實 LLM 動態生成有溫度的回饋，告別機器人口吻，且不使用表情符號。
4. 【動態情境式快捷標籤 (Contextual Smart Chips)】：根據目前缺少的欄位，動態提供最精確的建議按鈕。
5. 【紅旗急症即時攔截】：胸痛、呼吸困難秒級安全警示。
6. 【離線確定性 Fallback】：無網路/測試環境 100% 穩定可用。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from tfda_context_gate.clinical_safety import RiskSignalPolicy
from tfda_context_gate.intake.schemas import PreVisitIntake
from tfda_context_gate.product_session.schemas import ProductSession
from tfda_context_gate.run_config import env_value

logger = logging.getLogger(__name__)

# 紅旗急症提示文案
EMERGENCY_REPLY = (
    "系統偵測到可能屬於緊急醫療狀況（如呼吸困難、意識不清或劇烈胸痛）。"
    "請勿耽擱，建議立即撥打 119 或前往最近的急診室就醫！本系統不做診斷，已為你保留目前進度。"
)

# 3 大主題的自然口語提問
STAGE_TOPIC_QUESTIONS = {
    "stage1": "你好！我是看診前資料整理小幫手。\n\n想先跟您確認：平常有沒有藥物或食物過敏、或是過去有高血壓等其他慢性病、家人有糖尿病史呢？（如果知道請直接告訴我，沒有請回「無」）",
    "stage2": "那這次想看診主要是哪裡不舒服呢？大概從什麼時候開始、如果用 1 到 10 分來評估，嚴重程度大概是幾分呢？",
    "stage3": "好的，我都幫你記下來了。這次看診有什麼特別想請教醫師或討論的問題嗎？（例如飲食原則、藥物副作用，沒有也可以說「沒有」）",
}

REVIEW_QUICK_REPLIES = [
    {"label": "確認完成", "text": "確認完成"},
    {"label": "修改資料", "text": "修改看診資料"},
]


def deduplicate_medications(meds: list[str] | None) -> list[str]:
    """Clean, normalize and deduplicate medication name list."""
    if not meds:
        return []
    cleaned: list[str] = []
    for m in meds:
        if not m:
            continue
        s = str(m).strip()
        s = s.replace("（", "(").replace("）", ")").replace("【", "[").replace("】", "]")
        s = re.sub(r"[\uff01-\uff5e]", lambda c: chr(ord(c.group(0)) - 0xfee0), s)  # fullwidth to halfwidth
        s = re.sub(r"\s+", " ", s).strip()
        if s and s not in cleaned:
            cleaned.append(s)

    # Substring / variant deduplication: if A is completely contained in B, keep B
    result: list[str] = []
    for m in cleaned:
        is_sub = False
        for other in cleaned:
            if m != other and m.lower() in other.lower():
                is_sub = True
                break
        if not is_sub:
            result.append(m)

    # Ingredient root clustering
    root_groups = [
        {"carbamazepine", "tegretol", "癲通", "卡巴氮平"},
        {"metformin", "二甲雙胍", "glucophage", "庫魯化"},
        {"glipizide", "格列匹特", "minidiab"},
        {"gliclazide", "格列齊特", "diamicron", "岱蜜克龍"},
        {"sitagliptin", "西格列汀", "januvia", "佳糖維"},
        {"linagliptin", "利格列汀", "trajenta", "糖漸平"},
        {"empagliflozin", "恩格列淨", "jardiance", "恩排糖"},
        {"dapagliflozin", "達格列淨", "forxiga", "福適佳"},
    ]

    final: list[str] = []
    seen_groups: set[int] = set()
    for m in result:
        low = m.lower()
        matched_group_idx = None
        for g_idx, group in enumerate(root_groups):
            if any(k in low for k in group):
                matched_group_idx = g_idx
                break
        if matched_group_idx is None:
            final.append(m)
        else:
            if matched_group_idx not in seen_groups:
                seen_groups.add(matched_group_idx)
                final.append(m)
            else:
                for idx, existing in enumerate(final):
                    if any(k in existing.lower() for k in root_groups[matched_group_idx]):
                        if len(m) > len(existing):
                            final[idx] = m

    return final


class LLMIntakeTurnOutput(BaseModel):
    """LLM 雙軌輸出結構（結構化數據 + 醫療同理回饋）"""
    known_medications: list[str] | None = Field(default=None, description="目前用藥清單，如 ['美獲明']，無則 ['無']")
    allergies: list[str] | None = Field(default=None, description="過敏清單，如 ['花生']，無則 ['無']")
    chronic_conditions: list[str] | None = Field(default=None, description="慢性病清單，如 ['高血壓']，無則 ['無']")
    family_history: list[str] | None = Field(default=None, description="家族病史清單，如 ['父母有糖尿病']，無則 ['無']")
    symptom_onset: str | None = Field(default=None, description="症狀開始時間，如 '三天前'")
    symptom_description: str | None = Field(default=None, description="症狀具體描述，如 '頭暈想睡覺'")
    symptom_severity: str | None = Field(default=None, description="嚴重程度，標準化為 '輕度'、'中度'、'重度'")
    questions_for_doctor: list[str] | None = Field(default=None, description="想問醫師的問題清單，無則 ['無']")
    empathetic_ack: str | None = Field(
        default=None,
        description="用 1 句溫暖、口語、有同理心且不包含任何表情符號的話回應病患剛才說的內容（例如：聽到您頭暈到 8 分真的很不舒服，辛苦了，我已經幫您記下了！）"
    )


def _clean_field_value(raw: str) -> str:
    s = raw.strip().strip("，。、；;！!？?")
    s = re.sub(r"^(我有吃|我吃|在吃|服用|每天吃|我有|我有過|有|會對|對)\s*", "", s)
    s = re.sub(r"過敏$", "", s).strip()
    # 排除單字、問候語與代名詞
    if s in ("你", "我", "他", "您", "好", "在", "哈囉", "嗨", "安安", "早安", "午安", "晚安", "你好", "您好", "在嗎", "hello", "hi"):
        return ""
    return s


class LeanIntakeAgent:
    """輕量化、極致口語之看診前 AI 助理"""

    def __init__(self, llm: Any | None = None):
        self.risk_policy = RiskSignalPolicy()
        self.llm = llm

    @classmethod
    def from_env(cls) -> LeanIntakeAgent:
        """從環境變數建立具備 LLM 能力的 Agent"""
        model = env_value("ROUTER_LLM_MODEL", "") or ""
        base_url = env_value("OPENCODE_BASE_URL") or env_value("OPENAI_BASE_URL")
        api_key = env_value("OPENCODE_API_KEY") or env_value("OPENAI_API_KEY")
        if not api_key:
            return cls(llm=None)
        try:
            from langchain_openai import ChatOpenAI
            bare_model = model.split("/", 1)[-1] if "/" in model else (model or "mimo-v2.5")
            kwargs: dict[str, Any] = {"model": bare_model, "temperature": 0.2, "api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            if "mimo" in bare_model.lower():
                kwargs["extra_body"] = {"reasoning": {"effort": "none"}}
                kwargs["reasoning_effort"] = "none"
            kwargs["timeout"] = 15.0
            llm = ChatOpenAI(**kwargs)
            return cls(llm=llm)
        except Exception as exc:
            logger.warning("Could not initialize LLM for LeanIntakeAgent: %s", exc)
            return cls(llm=None)

    def is_red_flag(self, text: str) -> bool:
        """檢查是否包含急症關鍵字"""
        try:
            risk = self.risk_policy.classify(text)
            return risk.level == "RED_FLAG"
        except Exception:
            return bool(re.search(r"昏倒|昏迷|失去意識|劇烈胸痛|心絞痛|吐血|咳血|呼吸困難|喘不過氣|叫救護車|叫119", text))

    @staticmethod
    def standardize_severity(text: str) -> str | None:
        """將 1-10 分、分數、文字標準化為 輕度 / 中度 / 重度"""
        s = text.strip()
        m_frac = re.search(r"(\d+)\s*/\s*10", s)
        if m_frac:
            try:
                n = int(m_frac.group(1))
                if 1 <= n <= 3:
                    return "輕度"
                if 4 <= n <= 6:
                    return "中度"
                if 7 <= n <= 10:
                    return "重度"
            except Exception:
                pass

        m_num = re.search(r"(?:大概|大約|約|差不多)?\s*(10|[1-9])\s*(?:分|左右)?", s)
        if m_num:
            try:
                n = int(m_num.group(1))
                if 1 <= n <= 3:
                    return "輕度"
                if 4 <= n <= 6:
                    return "中度"
                if 7 <= n <= 10:
                    return "重度"
            except Exception:
                pass

        if any(tok in s for tok in ("輕度", "輕微", "不嚴重", "不太嚴重", "還好")):
            return "輕度"
        if any(tok in s for tok in ("中度", "普通", "中等", "還行")):
            return "中度"
        if any(tok in s for tok in ("重度", "嚴重", "很嚴重", "非常嚴重")):
            return "重度"

        return None

    @staticmethod
    def format_summary_card(intake: PreVisitIntake) -> str:
        """產生清楚易讀的看診前整理摘要卡片"""
        meds = "、".join(intake.known_medications) if intake.known_medications else "無"
        allergies = "、".join(intake.allergies) if intake.allergies else "無"
        chronic = "、".join(intake.chronic_conditions) if intake.chronic_conditions else "無"
        family = "、".join(intake.family_history) if intake.family_history else "無"
        onset = intake.symptom_onset or "未特別說明"
        desc = intake.symptom_description or "未特別說明"
        sev = intake.symptom_severity or "未特別說明"
        questions = "；".join(intake.questions_for_doctor) if intake.questions_for_doctor else "無特別提問"

        summary = (
            "【看診前資料整理摘要】\n"
            "──────────────────\n"
            f"1. 目前用藥：{meds}\n"
            f"2. 過敏史：{allergies}\n"
            f"3. 慢性病史：{chronic}\n"
            f"4. 家族病史：{family}\n"
            f"5. 症狀發生時間：{onset}\n"
            f"6. 症狀描述：{desc}\n"
            f"7. 嚴重程度：{sev}\n"
            f"8. 想問醫師的問題：{questions}\n"
            "──────────────────\n"
            "我已經幫你把看診資料整理好了！請核對以上內容是否正確？\n"
            "沒問題請點「確認完成」，想調整也可以點「修改資料」或直接告訴我。"
        )
        return summary

    def _determine_stage(self, intake: PreVisitIntake) -> str:
        """根據目前欄位填寫狀況推算所屬 3 大階段"""
        # Stage 1: 用藥與基礎病史（用藥、過敏、慢性病、家族史）
        s1_filled = bool(
            intake.known_medications
            and intake.allergies
            and intake.chronic_conditions
            and intake.family_history
        )
        if not s1_filled:
            return "stage1"

        # Stage 2: 本次症狀（時間、描述、嚴重度）
        s2_filled = bool(intake.symptom_onset and intake.symptom_description and intake.symptom_severity)
        if not s2_filled:
            return "stage2"

        # Stage 3: 醫病提問
        s3_filled = bool(intake.questions_for_doctor is not None and len(intake.questions_for_doctor) > 0)
        if not s3_filled:
            return "stage3"

        return "review"

    def _generate_next_question(self, stage: str, intake: PreVisitIntake) -> tuple[str, str | None]:
        """根據目前階段與缺漏項，產生自然流暢的下一個引導問題"""
        if stage == "stage1":
            missing_health = []
            if not intake.allergies:
                missing_health.append("allergies")
            if not intake.chronic_conditions:
                missing_health.append("chronic_conditions")
            if not intake.family_history:
                missing_health.append("family_history")

            # 1. 如果完全沒有填過過敏與病史
            if len(missing_health) == 3:
                pending_field = "allergies" if not intake.allergies else "chronic_conditions"
                if intake.known_medications:
                    med_list = deduplicate_medications(intake.known_medications)
                    med_str = "、".join(med_list)
                    return (
                        f"嗨！我是看診前資料整理助理。系統已自動為您帶入剛才辨識的藥袋用藥【{med_str}】。\n\n"
                        f"接下來想請問您的健康史：\n"
                        f"1. 請問您有任何藥物或食物過敏史嗎？\n"
                        f"2. 除了糖尿病之外，平時有高血壓、心臟病等其他慢性病史嗎？\n"
                        f"3. 家人（如父母）有糖尿病或相關家族病史嗎？"
                    ), pending_field
                else:
                    return STAGE_TOPIC_QUESTIONS["stage1"], "known_medications"

            # 2. 如果只缺部分病史（例如已填過敏，缺慢性病或家族史）
            if missing_health:
                if "chronic_conditions" in missing_health and "family_history" in missing_health:
                    return "已為您記錄過敏史。請問平時有高血壓、心臟病等其他慢性病史，或是家人有糖尿病家族史嗎？", "chronic_conditions"
                elif "chronic_conditions" in missing_health:
                    return "請問除了糖尿病之外，平時有高血壓、心臟病等其他慢性病史嗎？（沒有請回答無）", "chronic_conditions"
                elif "family_history" in missing_health:
                    return "請問家人（如父母、兄弟姊妹）有糖尿病或相關家族病史嗎？（沒有請回答無）", "family_history"
                elif "allergies" in missing_health:
                    return "請問您有任何藥物或食物過敏史嗎？（沒有請回答無）", "allergies"

            # 3. 若病史都齊全，但尚未填用藥
            if not intake.known_medications:
                return (
                    "收到，健康史已為您記錄。\n\n"
                    "另外想跟您確認：目前平時有固定吃降血糖藥、降血壓藥或打胰島素嗎？（如果知道請直接告訴我，沒有請回「無」）"
                ), "known_medications"

        if stage == "stage2":
            missing = []
            if not intake.symptom_onset:
                missing.append("開始時間")
            if not intake.symptom_description:
                missing.append("具體症狀")
            if not intake.symptom_severity:
                missing.append("嚴重程度(1-10分)")
            pending_field = "symptom_onset" if not intake.symptom_onset else ("symptom_description" if not intake.symptom_description else "symptom_severity")
            if len(missing) == 3:
                return STAGE_TOPIC_QUESTIONS["stage2"], pending_field
            if "嚴重程度(1-10分)" in missing and len(missing) == 1:
                return "這個不舒服的程度，如果用 1 到 10 分評估大概是幾分呢？", "symptom_severity"
            miss_str = "與".join(missing)
            return f"那這個狀況大概的{miss_str}可以跟我說一下嗎？", pending_field

        if stage == "stage3":
            return STAGE_TOPIC_QUESTIONS["stage3"], "questions_for_doctor"

        return "請確認以上看診前整理資料是否正確？", None

    def _generate_quick_replies(self, stage: str, intake: PreVisitIntake) -> list[dict[str, str]]:
        """根據當前缺漏欄位，動態產生高精確度的情境式快捷回答按鈕（Smart Chips，乾淨無表情符號）"""
        if stage == "stage1":
            missing_health = []
            if not intake.allergies:
                missing_health.append("allergies")
            if not intake.chronic_conditions:
                missing_health.append("chronic_conditions")
            if not intake.family_history:
                missing_health.append("family_history")

            # 初始輪（缺全部病史）
            if len(missing_health) == 3:
                return [
                    {"label": "皆無（無過敏/慢性病/家族史）", "text": "沒有過敏，沒有其他慢性病，也沒有家族史"},
                    {"label": "無過敏，我有高血壓", "text": "無過敏，我有高血壓，無家族病史"},
                    {"label": "無過敏，父母有糖尿病", "text": "無過敏無慢性病，父母有糖尿病史"},
                    {"label": "對海鮮/花生過敏", "text": "我對海鮮和花生過敏，無其他病史"},
                ]

            # 缺慢性病 + 家族史
            if "chronic_conditions" in missing_health and "family_history" in missing_health:
                return [
                    {"label": "皆無（無慢性病/家族史）", "text": "沒有其他慢性病，也沒有家族病史"},
                    {"label": "有高血壓，無家族史", "text": "我有高血壓，沒有家族病史"},
                    {"label": "無慢性病，父母有糖尿病", "text": "無其他慢性病，父母有糖尿病史"},
                ]

            # 僅缺慢性病
            if "chronic_conditions" in missing_health:
                return [
                    {"label": "無其他慢性病", "text": "沒有其他慢性病"},
                    {"label": "我有高血壓", "text": "我有高血壓"},
                    {"label": "有高血壓與高血脂", "text": "我有高血壓和高血脂"},
                ]

            # 僅缺家族史
            if "family_history" in missing_health:
                return [
                    {"label": "無家族病史", "text": "沒有相關家族病史"},
                    {"label": "父母有糖尿病", "text": "父母有糖尿病史"},
                    {"label": "手足有糖尿病", "text": "兄弟姊妹有糖尿病史"},
                ]

            # 僅缺用藥
            if not intake.known_medications:
                return [
                    {"label": "吃降血糖與降血壓藥", "text": "我有吃降血糖與降血壓藥"},
                    {"label": "有固定打胰島素", "text": "平時有固定施打胰島素"},
                    {"label": "目前無任何固定用藥", "text": "目前沒有吃任何固定用藥或打胰島素"},
                ]

        if stage == "stage2":
            missing = []
            if not intake.symptom_onset:
                missing.append("onset")
            if not intake.symptom_description:
                missing.append("description")
            if not intake.symptom_severity:
                missing.append("severity")

            # 初始完整 Stage 2
            if len(missing) == 3:
                return [
                    {"label": "3天前口渴頻尿(輕度3分)", "text": "三天前開始容易口渴頻尿，大概3分輕度"},
                    {"label": "1週前頭暈疲倦(中度5分)", "text": "一週前開始容易頭暈想睡覺，大約5分中度"},
                    {"label": "最近很不舒服(重度8分)", "text": "最近幾天非常不舒服，大概8分很嚴重"},
                ]

            # 若僅缺嚴重程度
            if missing == ["severity"]:
                return [
                    {"label": "輕度 (1-3分，生活正常)", "text": "大概 2-3 分輕度，不影響平常生活"},
                    {"label": "中度 (4-6分，有點困擾)", "text": "大約 5 分中度，有些困擾想改善"},
                    {"label": "重度 (7-10分，很不舒服)", "text": "大概 8 分重度，非常不舒服"},
                ]

            # 若僅缺時間
            if missing == ["onset"]:
                return [
                    {"label": "最近 2-3 天開始", "text": "大概兩三天前開始的"},
                    {"label": "最近一週左右", "text": "大約最近一週開始"},
                    {"label": "持續一個月以上", "text": "已經持續一個多月了"},
                ]

            # 若僅缺症狀描述
            if missing == ["description"]:
                return [
                    {"label": "常常口渴、晚上頻尿", "text": "常常覺得很口渴、晚上一直爬起來尿尿"},
                    {"label": "吃飽容易頭暈想睡", "text": "每次吃飽飯後都覺得很想睡覺、頭暈"},
                    {"label": "容易飢餓、手抖心悸", "text": "容易突然很餓、手會發抖"},
                ]

            # 缺時間 + 程度
            if "onset" in missing and "severity" in missing:
                return [
                    {"label": "三天前開始，輕度(3分)", "text": "大概三天前開始，程度大約3分輕度"},
                    {"label": "一週前開始，中度(5分)", "text": "大約一週前開始，程度約5分中度"},
                    {"label": "最近幾天，重度(8分)", "text": "最近幾天開始，程度大概8分很嚴重"},
                ]

            return [
                {"label": "最近幾天開始，輕度(3分)", "text": "最近幾天開始，大約3分輕度"},
                {"label": "大約一週左右，中度(5分)", "text": "大約一週左右，約5分中度"},
                {"label": "持續一陣子，重度(8分)", "text": "已經持續一陣子，大概8分重度"},
            ]

        if stage == "stage3":
            return [
                {"label": "想問飲食與可否吃炸雞/澱粉", "text": "想請教醫師平常飲食有何禁忌，例如可以吃炸雞或甜食嗎？"},
                {"label": "想了解目前藥物副作用", "text": "想了解目前服用的藥物有沒有副作用或需要注意的地方"},
                {"label": "想詢問運動與血糖控制目標", "text": "想請教適合的運動方式與平常血糖應該控制在多少"},
                {"label": "目前沒有特別想問的", "text": "目前沒有特別想問的問題，謝謝！"},
            ]

        if stage == "review":
            return REVIEW_QUICK_REPLIES

        return []

    def extract_with_llm(self, text: str, stage: str, current_intake: PreVisitIntake) -> tuple[dict[str, Any], str | None]:
        """透過 LLM 進行雙軌萃取（結構化欄位 + 溫暖同理回饋）"""
        if self.llm is None:
            return {}, None
        try:
            prompt = (
                "你是一位專業、溫暖、親切的看診前醫療衛教助理。\n"
                "你的任務是協助病患在看診前整理資訊（用藥病史、本次症狀、想問醫師的問題），讓看診更有效率。\n\n"
                f"【目前已記錄的資料】: {current_intake.model_dump_json()}\n"
                f"【當前進行階段】: {stage}\n"
                f"【病患最新輸入】: {text}\n\n"
                "⚠️ 請嚴格遵守規則：\n"
                "1. 結構化欄位抽取：病患這句話中『明確提到』的項目請萃取填入；『沒提到的欄位一律保持 null』，絕對不要擅自幫未提及的欄位填無！\n"
                "2. 嚴重程度標準化：1-3分為輕度、4-6分為中度、7-10分為重度。\n"
                "3. 同理心口語承接 (empathetic_ack)：根據病患提到的症狀或心情，給予 1 句簡短、溫暖的醫療同理回應（例如：'頭暈到8分真的很不舒服，辛苦了，我已經幫你記下來！'；若只是單純回答無或病史，則簡短親切確認即可）。\n"
                "4. 嚴格禁止使用任何表情符號（Emoji），保持專業俐落、乾淨溫暖的文字口吻。"
            )
            structured_llm = self.llm.with_structured_output(LLMIntakeTurnOutput)
            res: LLMIntakeTurnOutput = structured_llm.invoke(prompt)
            raw_updates = {k: v for k, v in res.model_dump().items() if v is not None and v != [] and v != "" and k != "empathetic_ack"}
            
            # 安全防禦：過濾未提及欄位與代名詞/無效字
            updates: dict[str, Any] = {}
            for k, v in raw_updates.items():
                if isinstance(v, list):
                    v = [_clean_field_value(item) if isinstance(item, str) else item for item in v]
                    v = [item for item in v if item]
                    if not v:
                        continue
                if k == "symptom_severity" and (self.standardize_severity(text) is not None or any(w in text for w in ("分", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "輕", "中", "重", "嚴重"))):
                    updates[k] = self.standardize_severity(text) or v
                elif k == "allergies" and ("過敏" in text or "沒有" in text or "無" in text):
                    updates[k] = v
                elif k == "chronic_conditions" and any(w in text for w in ("高血壓", "高血脂", "心臟病", "腎臟病", "糖尿病", "三高", "都有", "病", "無", "沒有")):
                    updates[k] = v
                elif k == "family_history" and any(w in text for w in ("家人", "父母", "爸", "媽", "遺傳", "長輩", "家裡", "無", "沒有")):
                    updates[k] = v
                elif k == "symptom_onset" and any(w in text for w in ("天", "週", "月", "年", "前", "昨天", "最近", "開始", "禮拜")):
                    updates[k] = v
                elif k == "symptom_description" and any(w in text for w in ("渴", "尿", "暈", "累", "痛", "抖", "癢", "高", "低", "不舒服", "症狀", "睡")):
                    updates[k] = v
                elif k == "questions_for_doctor" and any(w in text for w in ("問", "請教", "可以", "能", "注意", "問題", "吃")):
                    updates[k] = v
                elif any(str(val).lower() in text.lower() for val in (v if isinstance(v, list) else [v])) and not any(w in text for w in ("你好", "您好", "哈囉", "嗨", "安安", "早安", "晚安")):
                    updates[k] = v

            # 清除回覆中可能殘留的 emoji
            ack = res.empathetic_ack
            if ack:
                # 移除 Unicode emoji 字符
                ack = re.sub(r"[\U00010000-\U0010ffff\u2600-\u26ff\u2700-\u27bf]", "", ack).strip()

            return updates, ack
        except Exception as exc:
            logger.warning("LLM extraction failed, falling back to deterministic: %s", exc)
            return {}, None

    def extract_with_deterministic_rules(
        self, text: str, pending_field: str | None, current_intake: PreVisitIntake
    ) -> dict[str, Any]:
        """確定性規則抽取（離線 fallback，保證 100% 穩定且不依賴外部網路）"""
        updates: dict[str, Any] = {}
        s = text.strip()

        # 1. 症狀嚴重程度
        sev = self.standardize_severity(s)
        if sev and (pending_field == "symptom_severity" or "分" in s or "程度" in s or "嚴重" in s or re.match(r"^\d+$", s)):
            updates["symptom_severity"] = sev

        # 2. 跨欄位多項抽取（Multi-clause Extraction）
        if "過敏" in s:
            if "沒有過敏" in s or "無過敏" in s or "不過敏" in s:
                updates["allergies"] = ["無"]
            else:
                m_alg = re.search(r"(?:對)?([^\s，,。、]+)過敏", s)
                if m_alg:
                    updates["allergies"] = [_clean_field_value(m_alg.group(1))]

        if any(w in s for w in ("高血壓", "高血脂", "心臟病", "腎臟病")):
            found = [w for w in ("高血壓", "高血脂", "心臟病", "腎臟病") if w in s]
            if found:
                updates["chronic_conditions"] = found

        if any(w in s for w in ("不帶入此藥袋", "取消用藥", "移除用藥", "移除藥品", "沒吃這個藥", "不要帶入藥袋", "清空用藥", "修改用藥")):
            updates["known_medications"] = ["無"]
        elif any(w in s for w in ("metformin", "美獲明", "伯基", "胰島素", "降血糖藥", "降血壓藥")):
            m_med = re.search(r"(metformin|美獲明|伯基|胰島素|降血糖藥|降血壓藥|[a-zA-Z0-9\-]+)", s, re.IGNORECASE)
            if m_med:
                updates["known_medications"] = [m_med.group(1)]

        if any(w in s for w in ("口渴", "頻尿", "夜尿", "頭暈", "手抖", "血糖高", "吃不飽", "想睡覺")) and "symptom_description" not in updates:
            updates["symptom_description"] = s

        if any(w in s for w in ("天前", "週前", "月前", "昨天", "最近一週", "幾天前", "禮拜前", "三天前")) and "symptom_onset" not in updates:
            updates["symptom_onset"] = s

        # 3. 否定回答與特殊短語
        is_neg = bool(re.search(r"^(沒有|無|沒有吃|沒吃|沒吃藥|沒有過敏|無過敏|無其他|無家族史|沒有家族史|沒有家族病史|無家族病史|沒有慢性病|沒有其他慢性病|無其他慢性病|無慢性病|沒有問題|沒有別的了|都好了|無特別|沒有想問的|目前只有固定吃這款藥袋的藥，沒有其他用藥|只有這款藥|沒有其他用藥|無其他用藥)$", s))

        if any(w in s for w in ("只有這款藥", "沒有其他用藥", "無其他用藥", "只有藥袋的藥")):
            if pending_field in ("chronic_conditions", "family_history") and "chronic_conditions" not in updates:
                updates["chronic_conditions"] = ["無"]

        if pending_field == "known_medications" and "known_medications" not in updates:
            if is_neg:
                updates["known_medications"] = ["無"]
            elif re.search(r"不確定|不知道|忘了|忘記", s):
                updates["known_medications"] = ["不清楚（待看診確認）"]
            else:
                cleaned = _clean_field_value(s)
                if cleaned:
                    updates["known_medications"] = [cleaned]

        elif pending_field == "allergies" and "allergies" not in updates:
            if is_neg:
                updates["allergies"] = ["無"]
            else:
                cleaned = _clean_field_value(s)
                if cleaned:
                    updates["allergies"] = [cleaned]

        elif pending_field == "chronic_conditions" and "chronic_conditions" not in updates:
            if is_neg or "沒有其他慢性病" in s or "無慢性病" in s or "沒有慢性病" in s or "沒有其他用藥" in s:
                updates["chronic_conditions"] = ["無"]
            else:
                cleaned = _clean_field_value(s)
                if cleaned:
                    updates["chronic_conditions"] = [cleaned]

        elif pending_field == "family_history" and "family_history" not in updates:
            if is_neg or "沒有家族" in s or "無家族" in s:
                updates["family_history"] = ["無"]
            else:
                cleaned = _clean_field_value(s)
                if cleaned:
                    updates["family_history"] = [cleaned]

        elif pending_field == "symptom_onset" and "symptom_onset" not in updates:
            if s:
                updates["symptom_onset"] = s

        elif pending_field == "symptom_description" and "symptom_description" not in updates:
            if s:
                updates["symptom_description"] = s

        elif pending_field == "questions_for_doctor" and "questions_for_doctor" not in updates:
            if is_neg or re.search(r"沒有想問|沒有問題|無|沒了", s):
                updates["questions_for_doctor"] = ["無"]
            else:
                if s:
                    updates["questions_for_doctor"] = [s]

        return updates

    def process_turn(self, session: ProductSession, text: str) -> tuple[ProductSession, dict[str, Any]]:
        """處理單一對話回合並回傳更新後的 Session 與回應 Payload"""
        raw_text = text.strip()

        # ── 1. 紅旗急症檢查（即時攔截） ──
        if self.is_red_flag(raw_text):
            red_payload = {"level": "RED_FLAG", "signals": [raw_text]}
            updated_session = session.model_copy(
                update={"system_risk_classification": red_payload, "status": "ACTIVE"}, deep=True
            )
            return updated_session, {
                "reply": EMERGENCY_REPLY,
                "status": "FALLBACK",
                "intake_stage": updated_session.intake_stage,
                "version": updated_session.version,
                "intake_snapshot": updated_session.intake_snapshot.model_dump(mode="json"),
                "quick_replies": [],
            }

        # ── 2. 重新整理 / 初始化 ──
        if raw_text in ("開始新的整理", "開始整理", "重新整理"):
            existing_meds = (
                list(session.intake_snapshot.known_medications or [])
                if (raw_text != "清除全部資料" and session.intake_snapshot)
                else []
            )
            intake = PreVisitIntake(known_medications=list(existing_meds))
            first_q, first_field = self._generate_next_question("stage1", intake)
            updated_session = session.model_copy(
                update={
                    "status": "ACTIVE",
                    "intake_stage": "stage1",
                    "pending_field": first_field or "known_medications",
                    "pending_question": first_q,
                    "intake_snapshot": intake,
                },
                deep=True,
            )
            return updated_session, {
                "reply": first_q,
                "status": "ACTIVE",
                "intake_stage": "stage1",
                "version": updated_session.version,
                "intake_snapshot": intake.model_dump(mode="json"),
                "quick_replies": self._generate_quick_replies("stage1", intake),
            }

        # ── 3. Review 階段確認完成（交卷） ──
        is_confirm_cmd = bool(
            raw_text in ("確認完成", "完成對話", "完成", "結束對話", "結束整理", "結束", "確認", "送出")
            or raw_text.startswith("確認")
        )
        if (session.intake_stage == "review" or session.status == "AWAITING_CONFIRMATION") and is_confirm_cmd:
            updated_session = session.model_copy(
                update={
                    "status": "SUBMITTED",
                    "intake_stage": "submitted",
                    "pending_field": None,
                    "pending_question": None,
                },
                deep=True,
            )
            return updated_session, {
                "reply": "已為你完成看診前資料整理！你可以點擊下方按鈕或出示 QR Code 分享給醫護人員。",
                "status": "SUBMITTED",
                "intake_stage": "submitted",
                "version": updated_session.version,
                "intake_snapshot": updated_session.intake_snapshot.model_dump(mode="json"),
                "quick_replies": [],
                "review": {
                    "summary_text": self.format_summary_card(updated_session.intake_snapshot),
                    "is_submitted": True,
                },
            }

        # ── 4. 雙軌抽取（結構化 + 同理回饋） ──
        current_intake = session.intake_snapshot.model_copy(deep=True)
        current_stage = session.intake_stage or self._determine_stage(current_intake)

        # 優先嘗試 LLM 雙軌抽取
        updates, empathetic_ack = self.extract_with_llm(raw_text, current_stage, current_intake)
        if not updates:
            updates = self.extract_with_deterministic_rules(raw_text, session.pending_field, current_intake)

        # 處理口語指名修改（例如：「過敏要改成盤尼西林」）
        m_mod = re.search(r"(?:要改成|改成|更正為|更正成|其實是|修正為|是)\s*([^\s，,。；;]+)", raw_text)
        if m_mod:
            val = _clean_field_value(m_mod.group(1).strip())
            if "過敏" in raw_text:
                updates["allergies"] = [val]
            elif "藥" in raw_text:
                updates["known_medications"] = [val]
            elif "慢性" in raw_text or "病" in raw_text:
                updates["chronic_conditions"] = [val]
            elif "家族" in raw_text:
                updates["family_history"] = [val]
            elif "時間" in raw_text or "天" in raw_text:
                updates["symptom_onset"] = val
            elif "症狀" in raw_text:
                updates["symptom_description"] = val
            elif "程度" in raw_text or "分" in raw_text:
                updates["symptom_severity"] = self.standardize_severity(val) or val
            elif "問題" in raw_text or "問" in raw_text:
                updates["questions_for_doctor"] = [val]

        # 套用 updates 到 snapshot
        for k, v in updates.items():
            if hasattr(current_intake, k) and v is not None:
                setattr(current_intake, k, v)

        # 檢查更新後的所屬階段
        new_stage = self._determine_stage(current_intake)

        # ── 5. 若已全數填完，進入 Review 確認階段 ──
        if new_stage == "review":
            summary_card = self.format_summary_card(current_intake)
            lead_ack = empathetic_ack or "好的，我都幫你記下來了！"
            full_reply = f"{lead_ack}\n\n{summary_card}"
            updated_session = session.model_copy(
                update={
                    "status": "AWAITING_CONFIRMATION",
                    "intake_stage": "review",
                    "pending_field": None,
                    "pending_question": "請確認以上資料是否正確？",
                    "intake_snapshot": current_intake,
                },
                deep=True,
            )
            return updated_session, {
                "reply": full_reply,
                "status": "AWAITING_CONFIRMATION",
                "intake_stage": "review",
                "version": updated_session.version,
                "intake_snapshot": current_intake.model_dump(mode="json"),
                "quick_replies": REVIEW_QUICK_REPLIES,
                "review": {
                    "summary_text": summary_card,
                    "is_submitted": False,
                },
            }

        # ── 6. 產生下一個自然引導問題與動態情境按鈕 ──
        next_q, pending_field = self._generate_next_question(new_stage, current_intake)
        quick_replies = self._generate_quick_replies(new_stage, current_intake)

        updated_session = session.model_copy(
            update={
                "status": "ACTIVE",
                "intake_stage": new_stage,
                "pending_field": pending_field,
                "pending_question": next_q,
                "intake_snapshot": current_intake,
            },
            deep=True,
        )

        # 組合溫暖同理回饋 + 下一題
        if empathetic_ack:
            reply_text = f"{empathetic_ack}\n\n{next_q}"
        else:
            ack_prefix = "了解，已幫你記下。"
            if updates.get("symptom_severity"):
                ack_prefix = f"收到，嚴重度已記為「{updates['symptom_severity']}」。"
            elif updates.get("known_medications"):
                ack_prefix = "收到目前用藥資訊。"
            reply_text = f"{ack_prefix}\n\n{next_q}"

        return updated_session, {
            "reply": reply_text,
            "status": "ACTIVE",
            "intake_stage": new_stage,
            "version": updated_session.version,
            "intake_snapshot": current_intake.model_dump(mode="json"),
            "quick_replies": quick_replies,
        }
