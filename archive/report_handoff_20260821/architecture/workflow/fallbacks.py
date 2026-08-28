from __future__ import annotations

from tfda_context_gate.d_output_gate.gate import DEFAULT_FALLBACK


FALLBACK_TEMPLATES = {
    "A_BLOCKED": "目前無法處理此請求，請改由合格醫療專業人員評估。",
    "A_DEPENDENCY": "目前無法完成安全的輸入檢查，請稍後再試或改由合格醫療專業人員評估。",
    "B_INSUFFICIENT": "目前提供的資料不足以可靠回答這個問題，請改由合格醫療專業人員評估。",
    "B_UNSAFE": "目前無法確認檢索資料足以支援可靠回答，請改由合格醫療專業人員評估。",
    "C_FAILURE": "目前無法產生可驗證的回答，請改由合格醫療專業人員評估。",
    "SYSTEM_DEPENDENCY": "目前系統無法完成安全處理，請稍後再試或改由合格醫療專業人員評估。",
}


def fallback_response(reason: str) -> str:
    return FALLBACK_TEMPLATES.get(reason, DEFAULT_FALLBACK)

