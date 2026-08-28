"""E 觀測層隱私與脫敏（Privacy）— 寫入 sink 前的最後防線。

本模組確保 E 層「只觀測、不洩密」：
- redact_text：以 3 條正則覆蓋常見憑證外洩形態，寫入前即時脫敏
- hash_text：對原始查詢做 SHA256 不可逆雜湊，保留關聯能力但不存明文
- sanitize_value：遞迴清洗任意 payload，key 命中敏感詞或 value 命中正則皆脫敏

與 tracer.py 的對應：
- TraceRecorder.__init__ 對 original_query 同時做 redact_text + hash_text
- record() / record_evaluation() 對所有 fields / metadata 做 sanitize_value
- _emit() 對例外訊息亦做 redact_text，避免錯誤訊息洩漏憑證
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any


# 【正則 1】賦值型憑證：api_key / password / secret / authorization / token 等
# 形態如 "api_key: sk-xxx" / "password=123" / "secret token value"
# 透過命名分組保留 name 與 separator，僅將 value 替換為 [REDACTED]
_ASSIGNED_SECRET = re.compile(
    r"(?P<name>\b(?:api[_ -]?key|password|passwd|secret|authorization|access[_ -]?token|token)\b)"
    r"(?P<separator>\s*[:=]\s*|\s+)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
# 【正則 2】Bearer Token：常見於 Authorization header，形態如 "Bearer eyJ..."
_BEARER = re.compile(r"(\bBearer\s+)[^\s,;]+", re.IGNORECASE)
# 【正則 3】常見 API Key 前綴：sk- / rk- / pk- 開頭且長度 >=8 的字串
_COMMON_API_KEY = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b")
# 用於 sanitize_value 的 key 層級判斷：若 key 含敏感詞，整值直接脫敏
_SENSITIVE_KEY = re.compile(
    r"(?:api[_ -]?key|password|passwd|secret|authorization|access[_ -]?token|token)",
    re.IGNORECASE,
)
# 健康 PII 敏感 key：question/query/medication/summary/planner_context/meds 等需雜湊/截斷，避免明文落盤
_HEALTH_PII_KEY = re.compile(
    r"(?:question|query|medication|summary|planner_context|meds|known_medications|original_query|current_query|rewritten_query|retrieval_query|questions_for_doctor|symptom|allergies|chronic_conditions|family_history)",
    re.IGNORECASE,
)


def redact_text(value: str) -> str:
    """脫敏常見憑證形態，同時保留可讀的 log 上下文。

    處理順序（3 條正則依序套用）：
    1. _ASSIGNED_SECRET：將「key: value」中的 value 替換為 [REDACTED]
    2. _BEARER：將「Bearer <token>」中的 token 替換為 [REDACTED]
    3. _COMMON_API_KEY：將裸露的 sk-/rk-/pk- key 直接替換為 [REDACTED]

    參數:
        value: 待脫敏的原始字串（任意 to-string 皆可）

    回傳:
        脫敏後的字串，憑證部分以 [REDACTED] 取代
    """

    text = str(value)
    # 步驟 1：處理賦值型憑證，保留 key 與分隔符，僅遮蔽 value
    text = _ASSIGNED_SECRET.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}[REDACTED]",
        text,
    )
    # 步驟 2：處理 Bearer token，保留 "Bearer " 前綴
    text = _BEARER.sub(r"\1[REDACTED]", text)
    # 步驟 3：處理裸露的常見 API key 形態
    return _COMMON_API_KEY.sub("[REDACTED]", text)


def hash_text(value: str | None) -> str | None:
    """對可選的原始文本做不可逆雜湊，用於關聯而不存明文。

    - 若 value 為 None 則回傳 None（保持可選語意）
    - 否則以 SHA256 對 UTF-8 編碼後的字串做雜湊，回傳 hexdigest

    參數:
        value: 原始文本（可能為 None）

    回傳:
        SHA256 hex 字串或 None
    """

    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


_STREAMING_ALLOWLIST = {
    "first_token_latency_ms",
    "first_chunk_latency_ms",
    "total_stream_latency_ms",
    "stream_token_count",
    "stream_char_count",
    "stream_chunk_count",
    "token_usage",
}


def sanitize_value(value: Any, *, key: str | None = None) -> Any:
    """遞迴清洗事件 payload，確保寫入 sink 前無敏感資料。

    清洗策略：
    - 若 key 命中 _SENSITIVE_KEY（例如 "api_key"、"password"），直接回傳 "[REDACTED]"
    - 若 value 為 str，對其做 redact_text（套用 3 條正則）
    - 若 value 為 Mapping，遞迴清洗每個 value 並以 key 判斷敏感性
    - 若 value 為 list/tuple，遞迴清洗每個元素
    - 其他型別原樣回傳

    參數:
        value: 待清洗的任意值
        key: 當前欄位名（用於 key 層級敏感判斷）

    回傳:
        清洗後的值（結構保持不變，敏感部分以 [REDACTED] 取代）
    """

    if key and key in _STREAMING_ALLOWLIST:
        if isinstance(value, Mapping):
            return {str(item_key): sanitize_value(item, key=str(item_key)) for item_key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [sanitize_value(item) for item in value]
        return value
    if key and _SENSITIVE_KEY.search(key):  # key 命中敏感詞 → 整值脫敏
        return "[REDACTED]"
    if key and _HEALTH_PII_KEY.search(key):
        if isinstance(value, str):
            h = hash_text(value)
            return f"[HASHED:{h[:16]}]" if h else "[HASHED]"
        if isinstance(value, Mapping):
            return {str(item_key): sanitize_value(item, key=str(item_key)) for item_key, item in value.items()}
        if isinstance(value, (list, tuple)):
            if key and key.lower() in ("meds", "known_medications", "medication", "medications"):
                out: list[Any] = []
                for elem in value:
                    if isinstance(elem, str):
                        h = hash_text(elem)
                        out.append(f"[HASHED:{h[:16]}]" if h else "[HASHED]")
                    else:
                        out.append(sanitize_value(elem))
                return out
            return [sanitize_value(item, key=key) for item in value]
        h = hash_text(str(value))
        return f"[HASHED:{h[:16]}]" if h else "[HASHED]"
    if isinstance(value, str):
        return redact_text(value)  # 字串值 → 套用 3 正則脫敏
    if isinstance(value, Mapping):
        return {str(item_key): sanitize_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    return value
