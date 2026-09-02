from __future__ import annotations

"""Intake router — 收斂 welcome/red-flag/a_route 判斷，純搬運自 graph.py/runner.py。

- is_welcome_trigger / _is_red_flag 為確定性正則/正規化判斷
- a_route intake 判斷：整合 task_type / intake / intent_tags / 原始文本正則
- 保持與原 graph.py 邏輯一致，僅抽模組以瘦身 graph.py
"""

import re
import unicodedata
from typing import Any

from tfda_context_gate.clinical_safety import RiskSignalPolicy

WELCOME_MESSAGE = (
    "您好！我是糖尿病衛教小幫手。今天想先做衛教、查藥袋，還是整理看診資料？"
    "直接告訴我就好。"
)

WELCOME_QUICK_REPLIES = ["我要準備看診", "飲食衛教", "藥物查詢"]

POST_ANSWER_INVITATION = "如果要看醫生需要幫你整理嗎？"

_RED_FLAG_RE = re.compile(
    r"胸痛|胸悶|喘不過氣|呼吸困難|呼吸急促|意識不清|昏迷|昏厥|"
    r"冒冷汗.*胸|大量出血|持續嘔吐|高燒不退|"
    r"chest pain|shortness of breath|unconscious|emergency|severe chest",
    re.IGNORECASE,
)

_INTAKE_RE = re.compile(r"準備看診|看診前|整理.*資料|pre.?visit|intake|要看醫生|回診|下週.*看|回診.*整理", re.IGNORECASE)


def is_welcome_trigger(text: str) -> bool:
    try:
        normalized = unicodedata.normalize("NFKC", text).strip()
        if not normalized:
            return True
        if len(normalized) < 6 and normalized in ("你好", "您好", "hi", "hello", "嗨", "哈囉", "早安", "午安", "晚安", "開始", "help", "？", "?", "…"):
            return True
        return False
    except Exception:
        return False


def _is_red_flag(text: str) -> bool:
    try:
        normalized = unicodedata.normalize("NFKC", text).strip()
        normalized = re.sub(r"\s+", " ", normalized)
        if not normalized:
            return False
        return RiskSignalPolicy().classify(normalized).level == "RED_FLAG"
    except Exception:
        return False


def is_red_flag(text: str) -> bool:
    return _is_red_flag(text)


def should_append_post_answer_invitation(a_result: Any, has_intake: bool) -> bool:
    return False


def append_post_answer_invitation(response: str, has_intake: bool = False) -> str:
    return response


def is_intake_query(
    *,
    task_type: str | None = None,
    intake: Any | None = None,
    intake_data: Any | None = None,
    a_result: Any | None = None,
    original_query: str | None = None,
) -> bool:
    """判斷是否為 intake 流程（收斂 graph.py a_route 的多條件判斷）。"""
    if task_type == "pre_visit_intake":
        return True
    if intake is not None or intake_data is not None:
        # 需區分空 dict 與有值；但原邏輯 intake 非 None 即視為 intake
        # 保持原行為：intake/intake_data 非 None 即 True
        # 但空 dict 在 graph 中會被視為 falsy？原 a_route 用 state.get("intake") or state.get("intake_data")
        # 若兩者皆空 dict 則 falsy，不進 intake。為保持一致，檢查 truthiness
        if intake or intake_data:
            return True
        # 若明確傳入空 dict 但 task_type 已判斷，此處不額外 True
    if a_result is not None:
        if getattr(a_result, "task_type", None) == "pre_visit_intake":
            return True
        try:
            tags = getattr(a_result, "intent_tags", []) or []
            if any(str(t) == "PRE_VISIT_INTAKE" for t in tags):
                return True
        except Exception:
            pass
    if original_query and _INTAKE_RE.search(original_query):
        try:
            from tfda_context_gate.a_router.rules import RuleBasedSignalExtractor

            if RuleBasedSignalExtractor.is_pre_visit_intake_text(original_query):
                return True
        except Exception:
            return True
    return False


def a_route_target(
    *,
    task_type: str | None = None,
    intake: Any | None = None,
    intake_data: Any | None = None,
    a_result: Any | None = None,
    original_query: str | None = None,
    termination_reason: str | None = None,
    rag_allowed: bool | None = None,
) -> str:
    """對應 graph.py a_route 的路由決策，返回 INTAKE_CHECK / QUERY_EXPANSION / END。"""
    if termination_reason == "WELCOME_MESSAGE":
        return "END"
    if rag_allowed is False:
        return "END"
    if a_result is not None and not getattr(a_result, "rag_allowed", True):
        return "END"
    if is_intake_query(task_type=task_type, intake=intake, intake_data=intake_data, a_result=a_result, original_query=original_query):
        return "INTAKE_CHECK"
    return "QUERY_EXPANSION"


__all__ = [
    "WELCOME_MESSAGE",
    "WELCOME_QUICK_REPLIES",
    "POST_ANSWER_INVITATION",
    "is_welcome_trigger",
    "_is_red_flag",
    "is_red_flag",
    "should_append_post_answer_invitation",
    "append_post_answer_invitation",
    "is_intake_query",
    "a_route_target",
]
