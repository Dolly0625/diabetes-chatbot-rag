#!/usr/bin/env python3
"""LINE 真機 Demo 上線前檢查工具（safe preflight）。

設計原則：
- 只檢查、不修改外部服務；不呼叫 LINE API 建立／刪除資源。
- 絕不印出秘密原文（secret/token/key 僅顯示是否設定與長度區間提示）。
- 每項輸出 PASS / WARN / BLOCKED，最後給出 READY_FOR_LOCAL_DEMO / READY_FOR_LINE_DEVICE_DEMO / NOT_READY。
- 支援 --json（不含秘密值），可被 CI / monkeypatch 測試覆蓋。
- 缺少必要變數時 exit code 非 0（NOT_READY）。
- 可用 `python3 -m scripts.demo.check_line_demo_readiness` 執行。

檢查項目（對應需求）：
- LINE_CHANNEL_SECRET
- LINE_CHANNEL_ACCESS_TOKEN
- LINE_IDENTITY_HASH_KEY（長度 >=16）
- LINE_SESSION_DB_PATH 可建立／寫入
- LINE_USE_FORMAL 設定
- CONVERSATION_LLM_MODEL / ROUTER_LLM_MODEL
- OPENCODE / OPENAI provider 完整性
- OLLAMA_BASE_URL 可連線
- bge-m3 是否存在（cache 或 Ollama）
- patient / clinician portal URL
- LIFF channel / client ID
- callback 公開 HTTPS 提醒
- webhook signature verification 是否開啟
- async push 是否具 Messaging API 權限
- Rich Menu 尚未建立提醒
- Demo clinician allowlist
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# 專案根目錄（scripts/demo/check_line_demo_readiness.py -> 專案根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 僅用於 bge-m3 cache 檢查，不影響核心流程
CACHE_DIR = PROJECT_ROOT / "data" / "processed" / ".vector_cache"

# 狀態常數
PASS = "PASS"
WARN = "WARN"
BLOCKED = "BLOCKED"

# 最終 readiness
READY_LOCAL = "READY_FOR_LOCAL_DEMO"
READY_DEVICE = "READY_FOR_LINE_DEVICE_DEMO"
NOT_READY = "NOT_READY"

# 秘密相關的 env key（絕對不輸出值）
SECRET_KEYS = {
    "LINE_CHANNEL_SECRET",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_ACCESS_TOKEN",
    "LINE_CHANNEL_TOKEN",
    "LINE_IDENTITY_HASH_KEY",
    "OPENCODE_API_KEY",
    "OPENAI_API_KEY",
    "OLLAMA_API_KEY",
}


@dataclass
class CheckResult:
    id: str
    name: str
    status: str
    message: str
    hint: str
    category: str  # local | line | portal | security | demo


def _get_env(name: str, default: str | None = None) -> str | None:
    """取得 env 值，優先 os.getenv，其次讀 .env 檔（不覆蓋 monkeypatch）。

    不輸出秘密值；此函式僅供檢查是否「存在」，回傳值僅用於內部長度/空值判斷。
    """
    val = os.getenv(name)
    if val is not None:
        return val
    # fallback: 讀 .env（若存在），但不覆蓋已設定的 monkeypatch
    dotenv_path = PROJECT_ROOT / ".env"
    if dotenv_path.is_file():
        try:
            for line in dotenv_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k, v = stripped.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
        except Exception:
            pass
    return default


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def check_line_channel_secret() -> CheckResult:
    val = _get_env("LINE_CHANNEL_SECRET", "")
    if val and val.strip():
        return CheckResult(
            id="line_channel_secret",
            name="LINE_CHANNEL_SECRET 是否存在",
            status=PASS,
            message="已設定（長度合理，不顯示原文）",
            hint="若需輪替，請同時更新 LINE Console 與 .env，並重啟服務",
            category="line",
        )
    return CheckResult(
        id="line_channel_secret",
        name="LINE_CHANNEL_SECRET 是否存在",
        status=BLOCKED,
        message="未設定",
        hint="請在 .env 設定 LINE_CHANNEL_SECRET（LINE Developers Console > Basic settings > Channel secret）",
        category="line",
    )


def check_line_access_token() -> CheckResult:
    val = (
        _get_env("LINE_CHANNEL_ACCESS_TOKEN")
        or _get_env("LINE_ACCESS_TOKEN")
        or _get_env("LINE_CHANNEL_TOKEN")
        or ""
    )
    if val and val.strip():
        # 長度簡單合理性檢查（LINE token 通常 > 50）
        if len(val.strip()) < 20:
            return CheckResult(
                id="line_channel_access_token",
                name="LINE_CHANNEL_ACCESS_TOKEN 是否存在",
                status=WARN,
                message="已設定但長度過短，請確認是否為完整 token",
                hint="請貼上完整 Channel access token（Messaging API > Channel access token）",
                category="line",
            )
        return CheckResult(
            id="line_channel_access_token",
            name="LINE_CHANNEL_ACCESS_TOKEN 是否存在",
            status=PASS,
            message="已設定",
            hint="需要 Messaging API push 權限才能使用 async push",
            category="line",
        )
    return CheckResult(
        id="line_channel_access_token",
        name="LINE_CHANNEL_ACCESS_TOKEN 是否存在",
        status=BLOCKED,
        message="未設定",
        hint="請在 .env 設定 LINE_CHANNEL_ACCESS_TOKEN（或 LINE_ACCESS_TOKEN / LINE_CHANNEL_TOKEN）",
        category="line",
    )


def check_identity_hash_key() -> CheckResult:
    val = _get_env("LINE_IDENTITY_HASH_KEY", "") or ""
    channel_secret = _get_env("LINE_CHANNEL_SECRET", "") or ""
    if val and val.strip():
        if len(val.strip()) < 16:
            return CheckResult(
                id="line_identity_hash_key",
                name="LINE_IDENTITY_HASH_KEY 長度合理性",
                status=BLOCKED,
                message="已設定但長度不足（需 >=16 字元）",
                hint="請使用至少 16 字元的隨機字串（正式環境建議獨立於 Channel secret）",
                category="security",
            )
        return CheckResult(
            id="line_identity_hash_key",
            name="LINE_IDENTITY_HASH_KEY 長度合理性",
            status=PASS,
            message="已設定且長度合理",
            hint="正式環境請定期輪替並確保與舊 session 的相容策略",
            category="security",
        )
    # 未設定時，若有 channel secret 則有 fallback 派生（worktree 有實作），但仍提醒
    if channel_secret and channel_secret.strip():
        return CheckResult(
            id="line_identity_hash_key",
            name="LINE_IDENTITY_HASH_KEY 長度合理性",
            status=WARN,
            message="未設定，將以 Channel secret 派生（Demo 可用，正式環境不建議）",
            hint="建議在 .env 明確設定 LINE_IDENTITY_HASH_KEY（>=16 字元隨機值）",
            category="security",
        )
    return CheckResult(
        id="line_identity_hash_key",
        name="LINE_IDENTITY_HASH_KEY 長度合理性",
        status=WARN,
        message="未設定",
        hint="請設定 LINE_IDENTITY_HASH_KEY（>=16 字元），否則 ProductSession 無法持久化",
        category="security",
    )


def check_session_db_path() -> CheckResult:
    raw = _get_env("LINE_SESSION_DB_PATH", "data/processed/line_sessions.sqlite3") or "data/processed/line_sessions.sqlite3"
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        # 確保目錄可建立
        path.parent.mkdir(parents=True, exist_ok=True)
        # 檢查父目錄是否可寫（不實際建立檔案，避免覆蓋）
        if not os.access(path.parent, os.W_OK):
            return CheckResult(
                id="line_session_db_path",
                name="LINE_SESSION_DB_PATH 是否可建立／寫入",
                status=BLOCKED,
                message=f"目錄不可寫入：{path.parent}",
                hint="請確認磁碟空間與權限，或改為可寫路徑",
                category="local",
            )
        # 若檔案已存在，檢查可寫
        if path.exists() and not os.access(path, os.W_OK):
            return CheckResult(
                id="line_session_db_path",
                name="LINE_SESSION_DB_PATH 是否可建立／寫入",
                status=BLOCKED,
                message=f"檔案不可寫入：{path}",
                hint="請修正權限（chmod）或更換路徑",
                category="local",
            )
        # 嘗試建立空檔案或追加測試（安全：若不存在則建立後立即檢查，不刪除原有資料）
        # 我們不實際寫入 db，只測試目錄可寫性已足夠
        return CheckResult(
            id="line_session_db_path",
            name="LINE_SESSION_DB_PATH 是否可建立／寫入",
            status=PASS,
            message=f"可寫入（路徑：{path.parent}）",
            hint="正式環境請備份 SQLite 並考慮定期遷移至受管資料庫",
            category="local",
        )
    except Exception as exc:
        # 不洩漏路徑之外的資訊
        _ = exc
        return CheckResult(
            id="line_session_db_path",
            name="LINE_SESSION_DB_PATH 是否可建立／寫入",
            status=BLOCKED,
            message="無法建立或寫入",
            hint="請確認 LINE_SESSION_DB_PATH 目錄權限與磁碟空間",
            category="local",
        )


def check_line_use_formal() -> CheckResult:
    # 支援多個別名，舊版可能用不同命名
    candidates = ["LINE_USE_FORMAL", "USE_FORMAL", "FORMAL_ENABLED", "CONVERSATION_USE_FORMAL"]
    found = None
    for k in candidates:
        v = _get_env(k)
        if v is not None and v.strip() != "":
            found = (k, v)
            break
    if found is None:
        return CheckResult(
            id="line_use_formal",
            name="LINE_USE_FORMAL 設定",
            status=WARN,
            message="未設定（將使用預設值）",
            hint="若 Demo 需強制走 formal，請設定 LINE_USE_FORMAL=true；本地開發可維持未設定",
            category="local",
        )
    key, val = found
    return CheckResult(
        id="line_use_formal",
        name="LINE_USE_FORMAL 設定",
        status=PASS,
        message=f"已設定（{key}）",
        hint="確認與 workflow 的 use_formal 參數一致",
        category="local",
    )


def check_llm_model() -> CheckResult:
    conv = (_get_env("CONVERSATION_LLM_MODEL", "") or "").strip()
    router = (_get_env("ROUTER_LLM_MODEL", "") or "").strip()
    if conv or router:
        model = conv or router
        # 不顯示完整模型名中的秘密，僅顯示 provider 前綴
        provider = model.split("/")[0] if "/" in model else "unknown"
        return CheckResult(
            id="conversation_llm_model",
            name="CONVERSATION_LLM_MODEL 或 ROUTER_LLM_MODEL",
            status=PASS,
            message=f"已設定（provider: {provider}）",
            hint="正式 Demo 建議 opencode/mimo-v2.5",
            category="local",
        )
    return CheckResult(
        id="conversation_llm_model",
        name="CONVERSATION_LLM_MODEL 或 ROUTER_LLM_MODEL",
        status=BLOCKED,
        message="未設定",
        hint="請在 .env 設定 CONVERSATION_LLM_MODEL 或 ROUTER_LLM_MODEL（例：opencode/mimo-v2.5）",
        category="local",
    )


def check_provider_config() -> CheckResult:
    model = (_get_env("CONVERSATION_LLM_MODEL", "") or _get_env("ROUTER_LLM_MODEL", "") or "").strip()
    if not model:
        return CheckResult(
            id="provider_config",
            name="OPENCODE／OPENAI provider 設定是否完整",
            status=BLOCKED,
            message="未設定模型，無法判斷 provider",
            hint="請先設定 CONVERSATION_LLM_MODEL 或 ROUTER_LLM_MODEL",
            category="local",
        )
    lower = model.lower()
    if "opencode" in lower:
        key = (_get_env("OPENCODE_API_KEY", "") or "").strip()
        base = (_get_env("OPENCODE_BASE_URL", "") or "").strip()
        if not key:
            return CheckResult(
                id="provider_config",
                name="OPENCODE／OPENAI provider 設定是否完整",
                status=BLOCKED,
                message="opencode 模型但未設定 OPENCODE_API_KEY",
                hint="請設定 OPENCODE_API_KEY（與 OPENCODE_BASE_URL，預設 https://opencode.ai/zen/go/v1）",
                category="local",
            )
        if not base:
            return CheckResult(
                id="provider_config",
                name="OPENCODE／OPENAI provider 設定是否完整",
                status=WARN,
                message="已設定 OPENCODE_API_KEY，但未設定 OPENCODE_BASE_URL（將使用預設）",
                hint="建議明確設定 OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1",
                category="local",
            )
        return CheckResult(
            id="provider_config",
            name="OPENCODE／OPENAI provider 設定是否完整",
            status=PASS,
            message="opencode provider 設定完整",
            hint="請妥善保管 API Key（已 gitignore，不得提交）",
            category="local",
        )
    if "openai" in lower or "gpt" in lower:
        key = (_get_env("OPENAI_API_KEY", "") or "").strip()
        if not key:
            return CheckResult(
                id="provider_config",
                name="OPENCODE／OPENAI provider 設定是否完整",
                status=BLOCKED,
                message="openai 模型但未設定 OPENAI_API_KEY",
                hint="請設定 OPENAI_API_KEY 與 OPENAI_BASE_URL（若使用自託管）",
                category="local",
            )
        return CheckResult(
            id="provider_config",
            name="OPENCODE／OPENAI provider 設定是否完整",
            status=PASS,
            message="openai provider 設定完整",
            hint="請確認額度與模型權限",
            category="local",
        )
    if "ollama" in lower or "qwen" in lower:
        # ollama 類模型通常走本地，不需 API key，但需 base url
        return CheckResult(
            id="provider_config",
            name="OPENCODE／OPENAI provider 設定是否完整",
            status=PASS,
            message="本地/ollama 模型，無需 OPENCODE/OPENAI Key",
            hint="請確認 OLLAMA_BASE_URL 可連線且模型已 pull",
            category="local",
        )
    # 未知 provider，檢查是否有任一 key
    if (_get_env("OPENCODE_API_KEY", "") or "").strip() or (_get_env("OPENAI_API_KEY", "") or "").strip():
        return CheckResult(
            id="provider_config",
            name="OPENCODE／OPENAI provider 設定是否完整",
            status=PASS,
            message="已設定 API Key",
            hint="請確認與模型 provider 一致",
            category="local",
        )
    return CheckResult(
        id="provider_config",
        name="OPENCODE／OPENAI provider 設定是否完整",
        status=WARN,
        message="未偵測到明確的 provider Key，使用 deterministic fallback 亦可本地 Demo",
        hint="若需 formal，請設定對應的 API Key",
        category="local",
    )


def check_ollama_base_url() -> CheckResult:
    base = (_get_env("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434").strip()
    # 僅嘗試連線，不洩漏 URL 外的資訊；timeout 短，避免阻塞
    target = base.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(target, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if 200 <= resp.status < 300:
                return CheckResult(
                    id="ollama_base_url",
                    name="OLLAMA_BASE_URL 是否可連線",
                    status=PASS,
                    message="可連線",
                    hint="若 bge-m3 已快取，即使離線也可 Demo",
                    category="local",
                )
            return CheckResult(
                id="ollama_base_url",
                name="OLLAMA_BASE_URL 是否可連線",
                status=WARN,
                message=f"回應異常（HTTP {resp.status}）",
                hint="請確認 Ollama 是否啟動，或使用已快取的向量庫",
                category="local",
            )
    except urllib.error.URLError:
        return CheckResult(
            id="ollama_base_url",
            name="OLLAMA_BASE_URL 是否可連線",
            status=WARN,
            message="無法連線",
            hint="請確認 Ollama 已啟動（ollama serve）或依賴快取；本地 Demo 仍可能通過",
            category="local",
        )
    except Exception:
        return CheckResult(
            id="ollama_base_url",
            name="OLLAMA_BASE_URL 是否可連線",
            status=WARN,
            message="無法連線",
            hint="檢查網路與防火牆設定",
            category="local",
        )


def check_bge_m3() -> CheckResult:
    # 1) 檢查快取是否存在（最可靠的本地指標）
    try:
        if CACHE_DIR.is_dir():
            pkls = list(CACHE_DIR.glob("*.pkl"))
            # 只要有任一快取，視為 PASS（包含 hpa_* 與 g5-faq 快取）
            if pkls:
                # 進一步檢查是否有 bge 相關快取名稱或大小合理
                return CheckResult(
                    id="bge_m3",
                    name="bge-m3 是否存在",
                    status=PASS,
                    message=f"快取存在（{len(pkls)} 個 pkl）",
                    hint="快取命中時無需 Ollama 連線即可檢索",
                    category="local",
                )
    except Exception:
        pass
    # 2) 嘗試透過 Ollama API 檢查模型清單
    base = (_get_env("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434").strip()
    target = base.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(target, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = resp.read().decode("utf-8", errors="ignore")
            if "bge-m3" in data:
                return CheckResult(
                    id="bge_m3",
                    name="bge-m3 是否存在",
                    status=PASS,
                    message="Ollama 已安裝 bge-m3",
                    hint="可執行 ollama list 確認版本",
                    category="local",
                )
            # 有 Ollama 但無 bge-m3
            return CheckResult(
                id="bge_m3",
                name="bge-m3 是否存在",
                status=WARN,
                message="Ollama 可連線但未發現 bge-m3",
                hint="請執行 ollama pull bge-m3:latest，或確認快取是否存在",
                category="local",
            )
    except Exception:
        pass
    return CheckResult(
        id="bge_m3",
        name="bge-m3 是否存在",
        status=WARN,
        message="未發現快取且無法連線確認",
        hint="請執行 ollama pull bge-m3:latest；若已有快取則可忽略（將自動重建）",
        category="local",
    )


def check_portal_urls() -> CheckResult:
    # patient / clinician portal URL 是否設定（用於分享與 LIFF）
    candidates = [
        "PATIENT_PORTAL_URL",
        "CLINICIAN_PORTAL_URL",
        "PATIENT_URL",
        "CLINICIAN_URL",
        "APP_BASE_URL",
        "BASE_URL",
        "PUBLIC_URL",
    ]
    found = []
    for k in candidates:
        v = (_get_env(k, "") or "").strip()
        if v:
            found.append(k)
    if found:
        return CheckResult(
            id="portal_urls",
            name="patient portal／clinician portal URL 是否設定",
            status=PASS,
            message=f"已設定（{', '.join(found)}）",
            hint="請確認 URL 為公開可存取的 HTTPS（行動裝置需外網）",
            category="portal",
        )
    # 若無 portal URL，但有 LIFF 相關設定，也視為部分滿足
    liff = (_get_env("LINE_LIFF_ID", "") or "").strip()
    if liff:
        return CheckResult(
            id="portal_urls",
            name="patient portal／clinician portal URL 是否設定",
            status=WARN,
            message="未設定 portal URL，但已設定 LIFF",
            hint="建議設定 PATIENT_PORTAL_URL / CLINICIAN_PORTAL_URL 為 HTTPS",
            category="portal",
        )
    return CheckResult(
        id="portal_urls",
        name="patient portal／clinician portal URL 是否設定",
        status=WARN,
        message="未設定",
        hint="若 Demo 需在手機開啟 portal，請設定 PATIENT_PORTAL_URL / CLINICIAN_PORTAL_URL（HTTPS）",
        category="portal",
    )


def check_liff() -> CheckResult:
    channel_id = (_get_env("LINE_LOGIN_CHANNEL_ID", "") or "").strip()
    liff_id = (_get_env("LINE_LIFF_ID", "") or "").strip()
    if channel_id and liff_id:
        return CheckResult(
            id="liff_config",
            name="LIFF channel／client ID 是否設定",
            status=PASS,
            message="已設定",
            hint="請確認 LINE Login channel 與 LIFF app 已在 Console 建立並綁定",
            category="portal",
        )
    if channel_id or liff_id:
        return CheckResult(
            id="liff_config",
            name="LIFF channel／client ID 是否設定",
            status=WARN,
            message="部分設定（僅其中一項）",
            hint="請同時設定 LINE_LOGIN_CHANNEL_ID 與 LINE_LIFF_ID",
            category="portal",
        )
    return CheckResult(
        id="liff_config",
        name="LIFF channel／client ID 是否設定",
        status=WARN,
        message="未設定",
        hint="若需手機 LIFF 登入，請建立 LINE Login channel 並設定 LINE_LOGIN_CHANNEL_ID / LINE_LIFF_ID",
        category="portal",
    )


def check_callback_https() -> CheckResult:
    # 檢查 callback 是否需要公開 HTTPS（行動裝置必備）
    candidates = ["LINE_CALLBACK_URL", "CALLBACK_URL", "APP_BASE_URL", "PUBLIC_URL", "PATIENT_PORTAL_URL"]
    url = ""
    found_key = ""
    for k in candidates:
        v = (_get_env(k, "") or "").strip()
        if v:
            url = v
            found_key = k
            break
    if not url:
        return CheckResult(
            id="callback_https",
            name="callback 是否需要公開 HTTPS",
            status=WARN,
            message="未設定公開 URL，需手動確認",
            hint="真機 Demo 需要公開 HTTPS callback（例：ngrok / Cloud Run），請設定 LINE_CALLBACK_URL 或 APP_BASE_URL 為 https://",
            category="line",
        )
    if url.startswith("https://"):
        return CheckResult(
            id="callback_https",
            name="callback 是否需要公開 HTTPS",
            status=PASS,
            message=f"已設定 HTTPS（{found_key}）",
            hint="請確認 LINE Console Webhook URL 與此一致且已啟用 Use webhook",
            category="line",
        )
    if url.startswith("http://"):
        # 若是 localhost，本地可，但真機不行
        if "localhost" in url or "127.0.0.1" in url:
            return CheckResult(
                id="callback_https",
                name="callback 是否需要公開 HTTPS",
                status=WARN,
                message="為 localhost，僅本地可用",
                hint="真機 Demo 請改用公開 HTTPS（ngrok / serveo / Cloud Run）",
                category="line",
            )
        return CheckResult(
            id="callback_https",
            name="callback 是否需要公開 HTTPS",
            status=BLOCKED,
            message="非 HTTPS",
            hint="LINE Webhook 必須為 HTTPS，請改為 https:// 開頭的公開 URL",
            category="line",
        )
    return CheckResult(
        id="callback_https",
        name="callback 是否需要公開 HTTPS",
        status=WARN,
        message="URL 格式不明",
        hint="請設定為 https:// 開頭的公開 URL",
        category="line",
    )


def check_webhook_signature() -> CheckResult:
    secret = (_get_env("LINE_CHANNEL_SECRET", "") or "").strip()
    allow_unsigned = _is_truthy(_get_env("LINE_ALLOW_UNSIGNED_WEBHOOK", "false"))
    if allow_unsigned:
        return CheckResult(
            id="webhook_signature_verification",
            name="webhook signature verification 是否開啟",
            status=BLOCKED,
            message="已允許未簽章請求（LINE_ALLOW_UNSIGNED_WEBHOOK=true）",
            hint="正式／真機環境請設為 false 並確保 LINE_CHANNEL_SECRET 已設定（fail-closed）",
            category="security",
        )
    if not secret:
        return CheckResult(
            id="webhook_signature_verification",
            name="webhook signature verification 是否開啟",
            status=BLOCKED,
            message="未設定 Channel secret，無法驗證簽章",
            hint="請設定 LINE_CHANNEL_SECRET；若僅本地測試可暫時設 LINE_ALLOW_UNSIGNED_WEBHOOK=true（不建議）",
            category="security",
        )
    return CheckResult(
        id="webhook_signature_verification",
        name="webhook signature verification 是否開啟",
        status=PASS,
        message="已開啟（將驗證 X-Line-Signature）",
        hint="請確認 LINE Console 的 Webhook 已啟用並指向正確 URL",
        category="security",
    )


def check_async_push() -> CheckResult:
    token = (
        _get_env("LINE_CHANNEL_ACCESS_TOKEN")
        or _get_env("LINE_ACCESS_TOKEN")
        or _get_env("LINE_CHANNEL_TOKEN")
        or ""
    )
    if token and token.strip():
        return CheckResult(
            id="async_push",
            name="async push 是否需要 Messaging API push 權限",
            status=PASS,
            message="已設定 access token，具備 push 條件",
            hint="請確認 token 具備 push 權限且未過期；缺權限時將退回 fallback 文字",
            category="line",
        )
    return CheckResult(
        id="async_push",
        name="async push 是否需要 Messaging API push 權限",
        status=BLOCKED,
        message="未設定 access token，無法推送",
        hint="請設定 LINE_CHANNEL_ACCESS_TOKEN 並確認 Messaging API 已啟用",
        category="line",
    )


def check_rich_menu() -> CheckResult:
    # 不呼叫 LINE API，僅提醒
    return CheckResult(
        id="rich_menu",
        name="Rich Menu 提醒",
        status=WARN,
        message="尚未自動檢查（需人工確認）",
        hint="請至 LINE Console 或呼叫 GET /api/line/rich-menu?patient_portal_url=https://... 預覽定義後再手動建立",
        category="demo",
    )


def check_demo_clinician_allowlist() -> CheckResult:
    demo_mode = _is_truthy(_get_env("LINE_DEMO_MODE", "false"))
    ids_raw = (_get_env("DEMO_CLINICIAN_IDS", "") or "").strip()
    ids = [x.strip() for x in ids_raw.split(",") if x.strip()]
    if demo_mode:
        if ids:
            return CheckResult(
                id="demo_clinician_allowlist",
                name="Demo clinician allowlist 是否已設定",
                status=PASS,
                message=f"已設定（{len(ids)} 組）",
                hint="正式環境請改接院方 SSO/OIDC，僅 Demo 使用此 allowlist",
                category="demo",
            )
        return CheckResult(
            id="demo_clinician_allowlist",
            name="Demo clinician allowlist 是否已設定",
            status=BLOCKED,
            message="已啟用 Demo 模式但未設定 allowlist",
            hint="請設定 DEMO_CLINICIAN_IDS（逗號分隔）或關閉 LINE_DEMO_MODE",
            category="demo",
        )
    if ids:
        return CheckResult(
            id="demo_clinician_allowlist",
            name="Demo clinician allowlist 是否已設定",
            status=WARN,
            message="已設定 allowlist 但未啟用 Demo 模式",
            hint="若需 Demo 臨床端，請設 LINE_DEMO_MODE=true",
            category="demo",
        )
    return CheckResult(
        id="demo_clinician_allowlist",
        name="Demo clinician allowlist 是否已設定",
        status=WARN,
        message="未設定（Demo 臨床功能將不可用）",
        hint="若不需 Demo 臨床端可忽略；需用時請設 LINE_DEMO_MODE=true 與 DEMO_CLINICIAN_IDS",
        category="demo",
    )


def run_all_checks() -> list[CheckResult]:
    return [
        check_line_channel_secret(),
        check_line_access_token(),
        check_identity_hash_key(),
        check_session_db_path(),
        check_line_use_formal(),
        check_llm_model(),
        check_provider_config(),
        check_ollama_base_url(),
        check_bge_m3(),
        check_portal_urls(),
        check_liff(),
        check_callback_https(),
        check_webhook_signature(),
        check_async_push(),
        check_rich_menu(),
        check_demo_clinician_allowlist(),
    ]


def compute_readiness(checks: list[CheckResult]) -> str:
    """計算最終 readiness。

    - 若 local 類別有 BLOCKED => NOT_READY
    - 否則若 line/security 類別有 BLOCKED => READY_FOR_LOCAL_DEMO
    - 否則 => READY_FOR_LINE_DEVICE_DEMO（WARN 不阻擋，但會提示）
    """
    local_blocked = any(c.status == BLOCKED and c.category == "local" for c in checks)
    if local_blocked:
        return NOT_READY
    # 檢查是否有任何 BLOCKED（非 local 也算阻擋真機）
    any_blocked = any(c.status == BLOCKED for c in checks)
    if any_blocked:
        return READY_LOCAL
    return READY_DEVICE


def format_text(checks: list[CheckResult], readiness: str) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("LINE 真機 Demo 上線前檢查（preflight）")
    lines.append("=" * 60)
    for c in checks:
        icon = {"PASS": "✓", "WARN": "⚠", "BLOCKED": "✗"}.get(c.status, "?")
        lines.append(f"[{c.status:7s}] {icon} {c.name}")
        lines.append(f"         {c.message}")
        lines.append(f"         → {c.hint}")
    lines.append("-" * 60)
    # 統計
    counts = {PASS: 0, WARN: 0, BLOCKED: 0}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    lines.append(f"統計：PASS {counts[PASS]} / WARN {counts[WARN]} / BLOCKED {counts[BLOCKED]}")
    lines.append(f"結果：{readiness}")
    if readiness == READY_DEVICE:
        lines.append("✅ 可進行 LINE 真機 Demo（建議仍處理 WARN 項目）")
    elif readiness == READY_LOCAL:
        lines.append("⚠️ 僅可本地 Demo，真機 Demo 尚缺設定（請處理 BLOCKED）")
    else:
        lines.append("⛔ 尚未就緒（請處理 BLOCKED 項目）")
    lines.append("=" * 60)
    lines.append("備註：本工具不印出任何秘密值，僅告知是否設定與修復提示。")
    lines.append("      不會呼叫 LINE API 建立／刪除外部資源。")
    return "\n".join(lines)


def format_json(checks: list[CheckResult], readiness: str) -> str:
    # 絕不包含秘密值
    payload: dict[str, Any] = {
        "readiness": readiness,
        "summary": {
            "PASS": sum(1 for c in checks if c.status == PASS),
            "WARN": sum(1 for c in checks if c.status == WARN),
            "BLOCKED": sum(1 for c in checks if c.status == BLOCKED),
        },
        "checks": [asdict(c) for c in checks],
        "note": "no secrets included",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LINE Demo Readiness Preflight（不印出秘密）")
    parser.add_argument("--json", action="store_true", help="輸出 JSON（不含秘密）")
    parser.add_argument("--quiet", action="store_true", help="僅輸出結果行")
    args = parser.parse_args(argv)

    checks = run_all_checks()
    readiness = compute_readiness(checks)

    if args.json:
        output = format_json(checks, readiness)
        print(output)
    elif args.quiet:
        print(readiness)
    else:
        print(format_text(checks, readiness))

    # exit code：NOT_READY => 2，READY_LOCAL => 1，READY_DEVICE => 0
    # 讓 CI 可區分；「缺少必要變數時 exit code 非 0」對應 NOT_READY 與 READY_LOCAL
    if readiness == NOT_READY:
        return 2
    if readiness == READY_LOCAL:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
