"""LINE Rich Menu／Quick Reply 的穩定產品動作，不含醫療決策。

包含「看診前對談室」專用入口：LINE 留給日常衛教，八題問診在專用網頁對談室，
可暫停、最後由病患確認才分享。此模組為純呈現層，不生成 token、不推斷病患/草稿、
不以中文字串猜狀態，所有安全連結由外部（orchestrator / backend）提供。
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

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

# ── 看診前專用對談室：統一觸發與呈現契約 ──────────────────────────────
# 產品決策：LINE 留給日常衛教；不在 LINE 內開始八題問診。
# 使用者輸入「我要準備看診」時，應被導向專用網頁對談室；
# 實際開啟動作由精準文字觸發，讓 backend 產生安全連結。
PREVISIT_ROOM_TRIGGER_TEXT: str = "開啟看診前對談室"
PREVISIT_ROOM_TRIGGER_TEXTS: set[str] = {PREVISIT_ROOM_TRIGGER_TEXT}
# AltText 供不支援 Flex 的裝置與通知列使用：必須 ≤400 字元（LINE 約束）
PREVISIT_ROOM_ALT_TEXT: str = "看診前對談室入口：LINE 適合衛教，看診資料在專用對談室整理"
# 入口說明（body 呈現，清楚交代三件事）
PREVISIT_ROOM_TITLE: str = "看診前對談室"
PREVISIT_ROOM_BUBBLE_BODY_TEXTS: tuple[str, ...] = (
    "LINE 適合日常衛教；看診資料在專用對談室整理。",
    "可隨時暫停，回來繼續。",
    "最後由你確認才分享給醫護。",
    "不會在 LINE 內開始八題問診。",
)
# 按鈕 label 必須 1-20 字元（LINE QuickReply / Flex button 約束）
PREVISIT_ROOM_BUTTON_LABEL: str = PREVISIT_ROOM_TRIGGER_TEXT  # 8 字元，符合 1-20
# 無連結時的安全說明（不可點狀態，不可造假連結）
PREVISIT_ROOM_NO_URL_HINT: str = "尚未產生專用連結。請點下方按鈕，系統將產生安全連結。"
PREVISIT_ROOM_WITH_URL_HINT: str = "連結已就緒，請點下方按鈕進入專用對談室。"


def is_valid_rich_menu_url(value: str | None) -> bool:
    """Validate a shared, tokenless HTTPS URL for a patient Rich Menu."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value.strip())
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    # Rich Menus are shared by all users.  A one-user intake token must never
    # be embedded in the menu URI.
    query = parse_qs(parsed.query, keep_blank_values=True)
    return "token" not in {key.lower() for key in query}


def _is_valid_room_url(room_url: str | None) -> bool:
    """嚴格驗證外部提供的安全 room_url。

    - 必須為 https:// 開頭（LINE uri 要求 https）
    - 長度 1-2000，且不含空白
    - 有 netloc（避免假連結如 https://）
    - 不生成 token、不記錄 log（此函式不應 log URL）
    """
    if not isinstance(room_url, str):
        return False
    url = room_url.strip()
    if not url or len(url) > 2000:
        return False
    if " " in url or "\n" in url or "\t" in url:
        return False
    if not url.startswith("https://"):
        return False
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            return False
        # 拒絕 javascript:, data: 等非 https
        if parsed.scheme != "https":
            return False
        return True
    except Exception:
        return False


def is_previsit_room_trigger(text: str) -> bool:
    """精準判斷是否為統一觸發句。只做 strip 精準匹配，不做 substring/模糊。

    用戶點按時 send text 必須精準為 `開啟看診前對談室`，讓 backend 接手產生安全連結。
    """
    try:
        return (text or "").strip() == PREVISIT_ROOM_TRIGGER_TEXT
    except Exception:
        return False


def build_previsit_room_flex_contents(*, room_url: str | None = None) -> dict:
    """建立 Flex bubble contents（不含外層 type/altText）。

    - room_url 為有效 https 時：footer 為 uri 按鈕（開啟對談室）
    - 否則：footer 為 message 按鈕（觸發 `開啟看診前對談室`），且不含任何 uri 假連結
    - 永遠不生成 token、不 log URL、不以回覆中文猜狀態
    """
    has_url = _is_valid_room_url(room_url)

    # body 內容
    body_contents: list[dict] = [
        {
            "type": "text",
            "text": PREVISIT_ROOM_TITLE,
            "weight": "bold",
            "size": "lg",
            "wrap": True,
        },
        {
            "type": "text",
            "text": PREVISIT_ROOM_BUBBLE_BODY_TEXTS[0],
            "size": "sm",
            "color": "#5F746D",
            "wrap": True,
            "margin": "md",
        },
        {"type": "separator", "margin": "md"},
        {
            "type": "box",
            "layout": "vertical",
            "margin": "md",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"• {PREVISIT_ROOM_BUBBLE_BODY_TEXTS[1]}", "size": "sm", "wrap": True, "color": "#333333"},
                {"type": "text", "text": f"• {PREVISIT_ROOM_BUBBLE_BODY_TEXTS[2]}", "size": "sm", "wrap": True, "color": "#333333"},
                {"type": "text", "text": f"• {PREVISIT_ROOM_BUBBLE_BODY_TEXTS[3]}", "size": "sm", "wrap": True, "color": "#333333"},
            ],
        },
        {
            "type": "text",
            "text": PREVISIT_ROOM_WITH_URL_HINT if has_url else PREVISIT_ROOM_NO_URL_HINT,
            "size": "xs",
            "color": "#8A99A3",
            "wrap": True,
            "margin": "lg",
        },
    ]

    # footer 按鈕
    if has_url:
        # 安全 URI 動作：label 符合 1-20，uri 為外部提供的 https
        action: dict = {
            "type": "uri",
            "label": PREVISIT_ROOM_BUTTON_LABEL,
            "uri": (room_url or "").strip(),
        }
    else:
        # 不可點/安全狀態：message 動作，精準回傳觸發文字
        action = {
            "type": "message",
            "label": PREVISIT_ROOM_BUTTON_LABEL,
            "text": PREVISIT_ROOM_TRIGGER_TEXT,
        }

    button: dict = {
        "type": "button",
        "style": "primary",
        "color": "#087F67",
        "action": action,
    }

    footer: dict = {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [button],
    }

    bubble: dict = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": body_contents,
        },
        "footer": footer,
    }
    return bubble


def build_previsit_room_flex_message(*, room_url: str | None = None) -> dict:
    """建立完整 LINE Flex Message payload（可直接放入 reply/push messages）。

    回傳結構：{"type": "flex", "altText": "...", "contents": {"type": "bubble", ...}}
    - altText 長度 1-400（符合 Messaging API 約束）
    - button label 1-20
    - room_url 無效時不產生假連結，維持 message 動作
    """
    # 不在此 log room_url，避免 token 洩漏到 log
    alt = PREVISIT_ROOM_ALT_TEXT
    # 防禦：確保 altText 合法
    if not isinstance(alt, str) or not (1 <= len(alt) <= 400):
        alt = "看診前對談室入口"
    contents = build_previsit_room_flex_contents(room_url=room_url)
    return {"type": "flex", "altText": alt, "contents": contents}


def build_previsit_room_entry_messages(*, room_url: str | None = None) -> list[dict]:
    """由 app.py 消費的入口：回傳可直接 reply 的 messages 陣列（單一 Flex）。

    - 接收外部提供的安全 room_url；沒有或無效時不製造假連結
    - backend 流程：收到 `開啟看診前對談室`（is_previsit_room_trigger）→ 產生安全連結 → 以本 builder(room_url=link) 回覆
    """
    return [build_previsit_room_flex_message(room_url=room_url)]


def build_previsit_room_trigger_quick_reply() -> list[dict[str, str]]:
    """QuickReply 版本的觸發器（供 fallback 或純文字模式使用）。

    label/text 皆精準等於契約字串，符合 1-20 約束。
    """
    return [{"label": PREVISIT_ROOM_TRIGGER_TEXT, "text": PREVISIT_ROOM_TRIGGER_TEXT}]


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
    """產生病患用 Rich Menu 定義；不在啟動時修改外部狀態。

    LINE 是日常聊天／衛教入口；看診前資料整理則由這個單一入口帶往
    專用病患網頁。醫護端刻意不放進病患選單，避免把兩種角色混在一起。
    ``patient_portal_url`` 必須由部署者提供已授權的 HTTPS 網址（Demo 可用
    ``/demo/previsit``，正式 LIFF 則用病患 room URL）。
    """
    if not is_valid_rich_menu_url(patient_portal_url):
        raise ValueError("patient_portal_url must be a tokenless HTTPS URL")
    return {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "TFDA 看診前整理 Demo v0.1",
        "chatBarText": "開始看診前整理",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 2500, "height": 1686},
                "action": {
                    "type": "uri",
                    "label": "開始看診前整理",
                    "uri": patient_portal_url,
                },
            },
        ],
    }
