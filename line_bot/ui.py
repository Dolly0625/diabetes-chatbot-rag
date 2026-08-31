"""LINE Rich Menu／Quick Reply 的穩定產品動作，不含醫療決策。"""

from line_bot.intake_entry import (
    INTAKE_ENTRY_EXPLANATION,
    RESUME_CHOICE_ACTIONS,
    RESUME_CONTRACT_TEXT_CANCEL,
    RESUME_CONTRACT_TEXT_RESUME,
    RESUME_CONTRACT_TEXT_RESTART,
    build_intake_entry_message,
    build_resume_choice_actions,
)

INTAKE_ENTRY_ACTIONS = RESUME_CHOICE_ACTIONS


PATIENT_FAMILY_ACTIONS = [
    {"label": "健康諮詢", "text": "開始健康諮詢"},
    {"label": "準備看診", "text": "我要準備看診"},
    {"label": "上傳藥袋", "text": "我要上傳藥袋"},
    {"label": "看診摘要", "text": "查看看診摘要"},
    {"label": "分享給醫護", "text": "分享給醫護"},
    {"label": "說明與緊急協助", "text": "使用說明與緊急協助"},
]

SUBJECT_SELECTION_ACTIONS = [
    {"label": "為自己整理", "text": "為自己整理"},
    {"label": "代家人整理", "text": "代家人整理"},
]

PROXY_SOURCE_ACTIONS = [
    {"label": "家人本人描述", "text": "家人本人描述"},
    {"label": "我的觀察", "text": "我的觀察"},
]

REVIEW_ACTIONS = [
    {"label": "確認完成", "text": "確認完成"},
    {"label": "修改資料", "text": "修改看診資料"},
]

CLINICIAN_ACTIONS = [
    {"label": "讀取病患分享", "uri": "/clinician"},
    {"label": "存取紀錄", "uri": "/clinician#audit"},
    {"label": "使用與安全界線", "uri": "/clinician#policy"},
]


def build_rich_menu_payload(*, patient_portal_url: str) -> dict:
    """產生可交給 LINE Rich Menu API 的 v0.1 定義；不在啟動時修改外部狀態。"""
    return {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "TFDA 糖尿病照護 Demo v0.1",
        "chatBarText": "開啟功能選單",
        "areas": [
            {"bounds": {"x": 0, "y": 0, "width": 833, "height": 843}, "action": {"type": "message", "label": "健康諮詢", "text": "開始健康諮詢"}},
            {"bounds": {"x": 833, "y": 0, "width": 834, "height": 843}, "action": {"type": "message", "label": "準備看診", "text": "我要準備看診"}},
            {"bounds": {"x": 1667, "y": 0, "width": 833, "height": 843}, "action": {"type": "message", "label": "上傳藥袋", "text": "我要上傳藥袋"}},
            {"bounds": {"x": 0, "y": 843, "width": 833, "height": 843}, "action": {"type": "message", "label": "看診摘要", "text": "查看看診摘要"}},
            {"bounds": {"x": 833, "y": 843, "width": 834, "height": 843}, "action": {"type": "uri", "label": "分享給醫護", "uri": patient_portal_url}},
            {"bounds": {"x": 1667, "y": 843, "width": 833, "height": 843}, "action": {"type": "message", "label": "緊急協助", "text": "使用說明與緊急協助"}},
        ],
    }
