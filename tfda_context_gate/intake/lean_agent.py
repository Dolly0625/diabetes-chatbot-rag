"""Lean Intake Agent — 輕量、純粹、基於 LLM 結構化輸出的看診前資料蒐集引擎。

特色：
1. 專為網頁看診前對談室（Pre-Visit Room）設計，與 LINE 衛教 RAG 完全解耦。
2. 結構化欄位萃取（8 欄位：用藥、過敏、慢性病、家族史、發作時間、症狀描述、嚴重度、提問）。
3. 醫療紅旗急症（Red-Flag）即時攔截。
4. 具備高精確度的確定性離線 Fallback（無 API Key 或測試環境下 100% 穩定可用）。
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
    "⚠️ 系統偵測到可能屬於緊急醫療狀況（如呼吸困難、意識不清或劇烈胸痛）。"
    "請勿耽擱，建議立即撥打 119 或前往最近的急診室就醫！本系統不做診斷，已為你保留目前進度。"
)

# 8 個欄位的標準引導問題
STANDARD_STAGE_QUESTIONS = {
    "known_medications": "目前有固定吃什麼藥物或打胰島素嗎？如果知道藥名請直接告訴我；如果不確定也可以說「不確定」。",
    "allergies": "平時吃藥或吃東西，有對什麼藥物或食物過敏過嗎？（如果沒有請回「無」）",
    "chronic_conditions": "過去是否有其他慢性病或過去病史呢？（例如高血壓、高血脂，沒有請回「無」）",
    "family_history": "家裡的長輩或家人（如父母、兄弟姊妹）有糖尿病或其他慢性病史嗎？（沒有請回「無」）",
    "symptom_onset": "這次想看診的症狀大概是從什麼時候開始的呢？（例如：三天前、最近一週）",
    "symptom_description": "具體有哪些不舒服的症狀呢？（例如：常常口渴、晚上一直爬起來尿尿、容易疲倦）",
    "symptom_severity": "如果用 1 到 10 分來評估，或者用「輕度、中度、重度」來描述，這個不舒服的程度大概是幾分呢？",
    "questions_for_doctor": "這次看診有什麼特別想請教醫師或討論的問題嗎？（例如飲食原則、藥物副作用，若沒有可回「沒有」）",
}

# 快捷回答預設
DEFAULT_QUICK_REPLIES = {
    "known_medications": [{"label": "沒有吃藥", "text": "沒有吃藥"}, {"label": "不確定藥名", "text": "不確定藥名"}],
    "allergies": [{"label": "無過敏史", "text": "無"}, {"label": "對抗生素過敏", "text": "對抗生素過敏"}],
    "chronic_conditions": [{"label": "無其他疾病", "text": "無"}, {"label": "有高血壓", "text": "有高血壓"}],
    "family_history": [{"label": "無家族史", "text": "無"}, {"label": "父母有糖尿病", "text": "父母有糖尿病"}],
    "symptom_onset": [{"label": "三天前開始", "text": "三天前開始"}, {"label": "最近一週", "text": "最近一週"}, {"label": "持續一個月以上", "text": "持續一個月以上"}],
    "symptom_description": [{"label": "容易口渴、頻尿", "text": "容易口渴、頻尿"}, {"label": "早晨血糖偏高", "text": "早晨血糖偏高"}, {"label": "容易頭暈疲倦", "text": "容易頭暈疲倦"}],
    "symptom_severity": [{"label": "輕度 (1-3分)", "text": "輕度"}, {"label": "中度 (4-6分)", "text": "中度"}, {"label": "重度 (7-10分)", "text": "重度"}],
    "questions_for_doctor": [{"label": "想詢問飲食與運動原則", "text": "想詢問飲食與運動原則"}, {"label": "想了解藥物副作用", "text": "想了解藥物副作用"}, {"label": "沒有其他問題", "text": "沒有其他問題"}],
}

REVIEW_QUICK_REPLIES = [
    {"label": "確認完成", "text": "確認完成"},
    {"label": "修改資料", "text": "修改看診資料"},
]


class LLMIntakeExtraction(BaseModel):
    """LLM 萃取結構定義"""
    known_medications: list[str] | None = Field(default=None, description="目前用藥清單，無則填 ['無']")
    allergies: list[str] | None = Field(default=None, description="過敏清單，無則填 ['無']")
    chronic_conditions: list[str] | None = Field(default=None, description="慢性病清單，無則填 ['無']")
    family_history: list[str] | None = Field(default=None, description="家族病史清單，無則填 ['無']")
    symptom_onset: str | None = Field(default=None, description="症狀開始時間，如 '三天前'")
    symptom_description: str | None = Field(default=None, description="症狀具體描述，如 '口渴、頻尿'")
    symptom_severity: str | None = Field(default=None, description="症狀嚴重程度，標準化為 '輕度'、'中度'、'重度'")
    questions_for_doctor: list[str] | None = Field(default=None, description="想問醫師的問題清單，無則填 ['無']")


def _clean_field_value(raw: str) -> str:
    s = raw.strip().strip("，。、；;！!？?")
    s = re.sub(r"^(我有吃|我吃|在吃|服用|每天吃|我有|我有過|有|會對|對)\s*", "", s)
    s = re.sub(r"過敏$", "", s)
    return s.strip()


class LeanIntakeAgent:
    """輕量化看診前問卷代理人"""

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
            kwargs: dict[str, Any] = {"model": bare_model, "temperature": 0, "api_key": api_key}
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
            "📋 【看診前資料整理摘要】\n"
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
            "請核對以上資料是否正確？如果都沒問題，請點選「確認完成」；如果需要調整，請點選「修改資料」或直接告訴我哪裡要改。"
        )
        return summary

    def _next_missing_field(self, intake: PreVisitIntake) -> str | None:
        """依序檢查 8 欄位中下一個未填寫的欄位"""
        # Stage 1: 基礎病史
        if not intake.known_medications:
            return "known_medications"
        if not intake.allergies:
            return "allergies"
        if not intake.chronic_conditions:
            return "chronic_conditions"
        if not intake.family_history:
            return "family_history"

        # Stage 2: 本次症狀
        if not intake.symptom_onset:
            return "symptom_onset"
        if not intake.symptom_description:
            return "symptom_description"
        if not intake.symptom_severity:
            return "symptom_severity"

        # Stage 3: 提問
        if intake.questions_for_doctor is None or len(intake.questions_for_doctor) == 0:
            return "questions_for_doctor"

        return None

    def _determine_stage(self, intake: PreVisitIntake) -> str:
        """根據目前欄位填寫狀況推算所屬 stage"""
        if (
            not intake.known_medications
            or not intake.allergies
            or not intake.chronic_conditions
            or not intake.family_history
        ):
            return "stage1"
        if (
            not intake.symptom_onset
            or not intake.symptom_description
            or not intake.symptom_severity
        ):
            return "stage2"
        if intake.questions_for_doctor is None or len(intake.questions_for_doctor) == 0:
            return "stage3"
        return "review"

    def extract_with_llm(self, text: str, pending_field: str | None, current_intake: PreVisitIntake) -> dict[str, Any] | None:
        """透過 LLM 進行結構化萃取"""
        if self.llm is None:
            return None
        try:
            prompt = (
                "你是一位專業的醫療問卷助理。請從病患最新的一句話中，萃取出對應的結構化欄位。\n"
                f"當前正在詢問的欄位是：{pending_field}\n"
                f"目前已填寫的資料：{current_intake.model_dump_json()}\n"
                f"病患最新輸入：{text}\n"
                "規則：\n"
                "1. 嚴重程度：1-3分為輕度、4-6分為中度、7-10分為重度。\n"
                "2. 若病患回答「沒有/無」，請填 ['無']。\n"
                "3. 若病患表示不確定藥名，請填 ['不清楚（待看診確認）']。\n"
                "4. 僅回傳有提到的欄位更新。"
            )
            structured_llm = self.llm.with_structured_output(LLMIntakeExtraction)
            res: LLMIntakeExtraction = structured_llm.invoke(prompt)
            updates = {k: v for k, v in res.model_dump().items() if v is not None}
            return updates if updates else None
        except Exception as exc:
            logger.warning("LLM extraction failed, falling back to deterministic: %s", exc)
            return None

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

        if any(w in s for w in ("metformin", "美獲明", "伯基", "胰島素", "降血糖藥")):
            m_med = re.search(r"(metformin|美獲明|伯基|胰島素|降血糖藥|[a-zA-Z0-9\-]+)", s, re.IGNORECASE)
            if m_med:
                updates["known_medications"] = [m_med.group(1)]

        if any(w in s for w in ("口渴", "頻尿", "夜尿", "頭暈", "手抖", "血糖高", "吃不飽")) and "symptom_description" not in updates:
            updates["symptom_description"] = s

        if any(w in s for w in ("天前", "週前", "月前", "昨天", "最近一週", "幾天前")) and "symptom_onset" not in updates:
            updates["symptom_onset"] = s

        # 3. 否定回答（沒有、無、沒過敏、沒吃藥...）
        is_neg = bool(re.search(r"^(沒有|無|沒有吃|沒吃|沒吃藥|沒有過敏|無過敏|無其他|無家族史|沒有慢性病|沒有問題|沒有別的了|都好了|無特別|沒有想問的)$", s))

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
            if is_neg:
                updates["chronic_conditions"] = ["無"]
            else:
                cleaned = _clean_field_value(s)
                if cleaned:
                    updates["chronic_conditions"] = [cleaned]

        elif pending_field == "family_history" and "family_history" not in updates:
            if is_neg:
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
            }

        # ── 2. 重新整理 / 初始化 ──
        if raw_text in ("開始新的整理", "開始整理", "重新整理"):
            intake = PreVisitIntake()
            first_q = STANDARD_STAGE_QUESTIONS["known_medications"]
            updated_session = session.model_copy(
                update={
                    "status": "ACTIVE",
                    "intake_stage": "stage1",
                    "pending_field": "known_medications",
                    "pending_question": first_q,
                    "intake_snapshot": intake,
                },
                deep=True,
            )
            return updated_session, {
                "reply": f"你好！我是看診前整理小幫手，協助你在看診前將用藥與症狀整理成清晰的摘要。\n\n{first_q}",
                "status": "ACTIVE",
                "intake_stage": "stage1",
                "version": updated_session.version,
                "intake_snapshot": intake.model_dump(mode="json"),
                "quick_replies": DEFAULT_QUICK_REPLIES["known_medications"],
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

        # ── 4. 欄位抽取與狀態推進 ──
        current_intake = session.intake_snapshot.model_copy(deep=True)
        pending_field = session.pending_field or self._next_missing_field(current_intake)

        # 優先嘗試 LLM 萃取，若無則降級為確定性規則
        updates = self.extract_with_llm(raw_text, pending_field, current_intake)
        if not updates:
            updates = self.extract_with_deterministic_rules(raw_text, pending_field, current_intake)

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

        # 檢查下一個待詢問欄位
        next_field = self._next_missing_field(current_intake)
        new_stage = self._determine_stage(current_intake)

        # ── 5. 若已全數填完，進入 Review 確認階段 ──
        if next_field is None or new_stage == "review":
            summary_card = self.format_summary_card(current_intake)
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
                "reply": summary_card,
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

        # ── 6. 繼續下一題 ──
        next_q = STANDARD_STAGE_QUESTIONS.get(next_field, "請繼續補充其他看診前資訊。")
        quick_replies = DEFAULT_QUICK_REPLIES.get(next_field, [])

        updated_session = session.model_copy(
            update={
                "status": "ACTIVE",
                "intake_stage": new_stage,
                "pending_field": next_field,
                "pending_question": next_q,
                "intake_snapshot": current_intake,
            },
            deep=True,
        )

        # 產生簡短溫暖的承接回應
        ack_prefix = "收到，已幫你記下。"
        if updates.get("symptom_severity"):
            ack_prefix = f"收到，嚴重度已記為「{updates['symptom_severity']}」。"
        elif updates.get("known_medications"):
            ack_prefix = "收到目前用藥資訊。"

        reply_text = f"{ack_prefix}\n\n下一項：\n{next_q}"

        return updated_session, {
            "reply": reply_text,
            "status": "ACTIVE",
            "intake_stage": new_stage,
            "version": updated_session.version,
            "intake_snapshot": current_intake.model_dump(mode="json"),
            "quick_replies": quick_replies,
        }
