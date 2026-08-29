from __future__ import annotations

import random
import threading

from tfda_context_gate.d_output_gate.gate import DEFAULT_FALLBACK


IDENTITY_VARIANTS: list[str] = [
    "您好，我是糖尿病衛教小幫手（非真人，依 TFDA／國健署衛教文件回答）。能幫您：🥗 衛教 📋 看診前整理 💊 藥袋查詢。個人用藥請諮詢醫師/藥師。",
    "您好，我是糖尿病衛教小幫手（非真人，依 TFDA／國健署衛教文件回答）。能幫您：🥗 衛教 📋 看診前整理 💊 藥袋查詢。個人用藥請諮詢醫師/藥師。很高興為您服務，歡迎提問！",
    "您好，我是糖尿病衛教小幫手（非真人，依 TFDA／國健署衛教文件回答）。能幫您：🥗 衛教 📋 看診前整理 💊 藥袋查詢。個人用藥請諮詢醫師/藥師。謝謝您的詢問！",
]

WELCOME_VARIANTS: list[str] = [
    "您好！我是糖尿病衛教小幫手，可以幫您：\n1. 🥗 糖尿病飲食/運動衛教\n2. 💊 藥物資訊查詢\n3. 📋 看診前資料整理（幫您整理用藥、症狀、想問醫師的問題）\n請問需要什麼協助？",
    "又見面了～有什麼想繼續的？\n需要衛教、看診前整理或藥袋查詢都可以告訴我！可試試：為什麼會有糖尿病／飲食怎麼吃／上傳藥袋／我能幫什麼",
    "您好～歡迎回來！有什麼想繼續聊的嗎？🥗 衛教 📋 看診前整理 💊 藥袋查詢 都可以問我。",
]

# ── P5-2 Variation Pool (台灣敬語、emoji節制，醫療免責語保留) ─────────────
_QUICK_SUFFIX = "可試試：為什麼會有糖尿病／飲食怎麼吃／上傳藥袋／我能幫什麼"

O_GENERIC_VARIANTS: list[str] = [
    f"這題目前超出我能可靠回答的衛教範圍。我可以協助：🥗 糖尿病衛教 📋 看診前資料整理 💊 藥物查詢，您想試哪一個？（個人用藥請諮詢醫師/藥師）\n{_QUICK_SUFFIX}",
    f"抱歉，這個問題我手邊的衛教資料還沒涵蓋，依 TFDA／國健署文件無法給出可靠回覆。歡迎改問衛教、看診前整理或藥袋查詢，我會盡量整理給您。\n{_QUICK_SUFFIX}",
    f"目前我沒辦法針對這題給出有依據的回覆（僅依 TFDA／國健署衛教文件回答）。您可以試試衛教查詢或整理看診資料再與醫師討論。\n{_QUICK_SUFFIX}",
]

CHIT_CHAT_VARIANTS: list[str] = [
    f"這個我幫不上，不過我可以：🥗 衛教 📋 看診前資料整理 💊 藥物查詢，試試哪個？\n{_QUICK_SUFFIX}",
    f"這題不在我的衛教範圍內，但我很樂意幫您整理衛教重點、看診資料或藥袋資訊，要試哪個呢？\n{_QUICK_SUFFIX}",
    f"謝謝您的訊息～目前我主要協助衛教、看診前整理與藥袋查詢，您想從哪個開始？\n{_QUICK_SUFFIX}",
]

Q_NEED_MORE_VARIANTS: list[str] = [
    f"可以多說一點嗎？例如您想問飲食、血糖、運動或藥物哪一塊？給個關鍵字，我幫您依衛教文件整理。\n{_QUICK_SUFFIX}",
    f"想更精準地幫您，麻煩補個關鍵字（如飲食、血糖、運動、藥物），我會依文件整理重點給您參考。\n{_QUICK_SUFFIX}",
    f"請您補充一下想了解的主題（飲食／血糖／運動／藥物），我來幫您整理相關衛教資訊。\n{_QUICK_SUFFIX}",
]

B_INSUFFICIENT_VARIANTS: list[str] = [
    f"這題我手上的衛教資料不夠，建議看診時問醫師（僅供衛教參考，非診斷）。\n{_QUICK_SUFFIX}",
    f"抱歉，手邊的 TFDA／國健署衛教文件沒有涵蓋這題的可靠答案，建議與醫師討論確認。\n{_QUICK_SUFFIX}",
    f"目前找不到足夠的衛教依據來回答這題，為安全起見建議看診時請醫師評估。\n{_QUICK_SUFFIX}",
]

EMPATHY_VARIANTS: list[str] = [
    "抱歉讓您有這樣的感受，謝謝您告訴我。我的回覆是依 TFDA／國健署衛教文件整理，比較制式，還在學習更自然地表達。您可以試試：1) 為什麼會有糖尿病 2) 飲食怎麼吃 3) 上傳藥袋查詢，我會盡量說得更清楚。",
    "收到您的回饋，謝謝您提醒我。為了確保資訊正確，我目前只依衛教文件回覆，語氣可能不夠自然。您可以試試：1) 為什麼會有糖尿病 2) 飲食怎麼吃 3) 上傳藥袋，我來換個方式說明。",
    "謝謝您直說感受，對您造成的困擾很抱歉。我是依文件提供衛教的 AI 小幫手，會持續改進表達。您可以點：1) 為什麼會有糖尿病 2) 飲食怎麼吃 3) 上傳藥袋，或告訴我想怎麼改進。",
]

FALLBACK_VARIANTS: dict[str, list[str]] = {
    "O_GENERIC": O_GENERIC_VARIANTS,
    "CHIT_CHAT_OUT_OF_SCOPE": CHIT_CHAT_VARIANTS,
    "Q_NEED_MORE": Q_NEED_MORE_VARIANTS,
    "B_INSUFFICIENT": B_INSUFFICIENT_VARIANTS,
    "IDENTITY": IDENTITY_VARIANTS,
    "EMPATHY": EMPATHY_VARIANTS,
    "WELCOME": WELCOME_VARIANTS,
}

# session內 seen-set 去重（同句同 session 不重複）
_fallback_seen: dict[str, set[str]] = {}
_fallback_lock = threading.Lock()

# 共情嚴重情緒檢測（若命中則補 1925）
_SEVERE_EMOTION_RE = None
try:
    import re as _re
    _SEVERE_EMOTION_RE = _re.compile(r"想死|不想活|活不下去|自殺|輕生|結束生命", _re.IGNORECASE)
except Exception:
    _SEVERE_EMOTION_RE = None

FALLBACK_TEMPLATES = {
    "A_EMERGENCY": "偵測到可能的緊急警訊。請立即停止使用本系統，撥打 119 或前往最近的急診；若身旁有人，請請他協助。",
    "A_URGENT_HUMAN": "偵測到需要立即由真人協助的安全警訊。請立刻聯絡合格醫療專業人員；若有立即危險，請撥打 119。",
    "A_BLOCKED": "目前無法處理此請求，請改由合格醫療專業人員評估。",
    "CHIT_CHAT_OUT_OF_SCOPE": CHIT_CHAT_VARIANTS[0],
    "Q_NEED_MORE": Q_NEED_MORE_VARIANTS[0],
    "O_GENERIC": O_GENERIC_VARIANTS[0],
    "R_GUARDRAIL_BLOCKED": "這句話我沒辦法處理（可能包含系統指令）。你可以改用一般問法，例如：『糖尿病飲食怎麼吃』。",
    "R_DIAGNOSIS_BOUNDARY": "關於個人診斷／處置（如要不要調整劑量），我沒辦法直接回答。你可以試試衛教查詢或整理看診資料：🥗 衛教 📋 看診前資料整理 💊 藥物查詢。",
    "A_DEPENDENCY": "目前無法完成安全的輸入檢查，請稍後再試或改由合格醫療專業人員評估。",
    "B_INSUFFICIENT": B_INSUFFICIENT_VARIANTS[0],
    "B_UNSAFE": "目前無法確認檢索資料足以支援可靠回答，請改由合格醫療專業人員評估。",
    "C_FAILURE": "目前無法產生可驗證的回答，請改由合格醫療專業人員評估。",
    "SYSTEM_DEPENDENCY": "目前系統無法完成安全處理，請稍後再試或改由合格醫療專業人員評估。",
    "DEPENDENCY_OR_TIMEOUT": "目前系統無法完成安全處理，請稍後再試或改由合格醫療專業人員評估。",
    "FORMAL_TIMEOUT": "這題我還沒整理出可靠的回答，建議看診時直接問醫師。要我幫你把這題記到『想問醫師的問題』嗎？",
    "IDENTITY": IDENTITY_VARIANTS[0],
    "EMPATHY": EMPATHY_VARIANTS[0],
    "WELCOME": WELCOME_VARIANTS[0],
}


def fallback_response(reason: str, session_id: str | None = None, seen: set[str] | None = None) -> str:
    variants = FALLBACK_VARIANTS.get(reason)
    if variants:
        if seen is not None:
            unused = [v for v in variants if v not in seen]
            choice = random.choice(unused) if unused else random.choice(variants)
            seen.add(choice)
            return choice
        if session_id is not None:
            key = f"{session_id}:{reason}"
            with _fallback_lock:
                seen_set = _fallback_seen.get(key, set())
                unused = [v for v in variants if v not in seen_set]
                if not unused:
                    seen_set = set()
                    unused = list(variants)
                choice = random.choice(unused)
                seen_set.add(choice)
                _fallback_seen[key] = seen_set
                return choice
        key = f"global:{reason}"
        with _fallback_lock:
            seen_set = _fallback_seen.get(key, set())
            unused = [v for v in variants if v not in seen_set]
            if not unused:
                seen_set = set()
                unused = list(variants)
            choice = random.choice(unused)
            seen_set.add(choice)
            _fallback_seen[key] = seen_set
            return choice
    return FALLBACK_TEMPLATES.get(reason, DEFAULT_FALLBACK)


def empathy_response(text: str | None = None) -> str:
    base = fallback_response("EMPATHY")
    if text and _SEVERE_EMOTION_RE and _SEVERE_EMOTION_RE.search(text):
        return base + " 若您感到情緒困擾，需要有人聊聊，可撥打 1925 安心專線（24小時）。"
    return base


def clear_fallback_seen(session_id: str | None = None) -> None:
    with _fallback_lock:
        if session_id is None:
            _fallback_seen.clear()
        else:
            keys = [k for k in _fallback_seen if k.startswith(f"{session_id}:")]
            for k in keys:
                _fallback_seen.pop(k, None)
