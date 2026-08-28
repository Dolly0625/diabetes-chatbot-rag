from __future__ import annotations

# ── 封閉式降級回覆（繁中註解）──────────────────────────────────────────────
# 設計原則：所有降級皆為「封閉式」——不洩漏內部細節、不猜測醫療事實，
# 僅導向「請改由合格醫療專業人員評估」或「稍後再試」。
# Agent 不可自訂降級文案，僅能透過 reason 選擇模板；未知 reason 回退到 DEFAULT_FALLBACK。

from tfda_context_gate.d_output_gate.gate import DEFAULT_FALLBACK


FALLBACK_TEMPLATES = {
    "A_EMERGENCY": "偵測到可能的緊急警訊。請立即停止使用本系統，撥打 119 或前往最近的急診；若身旁有人，請請他協助。",
    "A_URGENT_HUMAN": "偵測到需要立即由真人協助的安全警訊。請立刻聯絡合格醫療專業人員；若有立即危險，請撥打 119。",
    "A_BLOCKED": "目前無法處理此請求，請改由合格醫療專業人員評估。",  # 保留相容：舊 A 政策阻擋（新分流請用 O/Q/CHIT_CHAT）
    "CHIT_CHAT_OUT_OF_SCOPE": "這個我幫不上，不過我可以：🥗 衛教 📋 看診前資料整理 💊 藥物查詢，試試哪個？",
    "Q_NEED_MORE": "可以多說一點嗎？例如你想問飲食、血糖、運動或藥物哪一塊？給個關鍵字，我幫你依衛教文件整理。",
    "O_GENERIC": "這題目前超出我能可靠回答的衛教範圍。我可以協助：🥗 糖尿病衛教 📋 看診前資料整理 💊 藥物查詢，你想試哪一個？",
    "R_GUARDRAIL_BLOCKED": "這句話我沒辦法處理（可能包含系統指令）。你可以改用一般問法，例如：『糖尿病飲食怎麼吃』。",
    "R_DIAGNOSIS_BOUNDARY": "關於個人診斷／處置（如要不要調整劑量），我沒辦法直接回答。你可以試試衛教查詢或整理看診資料：🥗 衛教 📋 看診前資料整理 💊 藥物查詢。",
    "A_DEPENDENCY": "目前無法完成安全的輸入檢查，請稍後再試或改由合格醫療專業人員評估。",  # A 依賴異常
    "B_INSUFFICIENT": "這題我手上的衛教資料不夠，建議看診時問醫師。",  # G4 honest gap for knowledge gap (B INSUFFICIENT)
    "B_UNSAFE": "目前無法確認檢索資料足以支援可靠回答，請改由合格醫療專業人員評估。",  # B 安全疑慮
    "C_FAILURE": "目前無法產生可驗證的回答，請改由合格醫療專業人員評估。",  # C 生成失敗
    "SYSTEM_DEPENDENCY": "目前系統無法完成安全處理，請稍後再試或改由合格醫療專業人員評估。",  # 系統層異常
    "FORMAL_TIMEOUT": "這題我還沒整理出可靠的回答，建議看診時直接問醫師。要我幫你把這題記到『想問醫師的問題』嗎？",
}


def fallback_response(reason: str) -> str:
    # 依 reason 選模板；未知 reason 回退到 D 閘門的 DEFAULT_FALLBACK，確保永遠有封閉式回覆
    return FALLBACK_TEMPLATES.get(reason, DEFAULT_FALLBACK)
