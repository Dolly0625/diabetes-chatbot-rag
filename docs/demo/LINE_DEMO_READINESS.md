# LINE 真機 Demo 上線前檢查（Preflight）

> 位置：`scripts/demo/check_line_demo_readiness.py`  
> 執行：`python3 -m scripts.demo.check_line_demo_readiness` 或 `python3 -m scripts.demo.check_line_demo_readiness --json`  
> 限制：僅新增 `scripts/demo/`、`docs/demo/`，不修改 `line_bot/app.py`、orchestrator、workflow、權限或分享邏輯；不呼叫 LINE API 建立／刪除外部資源；不印出秘密值。

## 目的

在不接觸外部服務的前提下，告訴開發者「本機 Demo」與「LINE 真機 Demo」還缺什麼，並給出可執行的修復提示。所有輸出僅含 PASS / WARN / BLOCKED 與 hint，絕不包含 `LINE_CHANNEL_SECRET` / `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_IDENTITY_HASH_KEY` / `OPENCODE_API_KEY` 等原文。

## 快速使用

```bash
# 文字報告（預設）
python3 -m scripts.demo.check_line_demo_readiness

# JSON（不含秘密，可給 CI 解析）
python3 -m scripts.demo.check_line_demo_readiness --json

# 僅結果行（供腳本判斷）
python3 -m scripts.demo.check_line_demo_readiness --quiet
```

Exit code：

- `0` → `READY_FOR_LINE_DEVICE_DEMO`（真機可上線，仍建議處理 WARN）
- `1` → `READY_FOR_LOCAL_DEMO`（僅本地可 Demo，真機尚缺 LINE 設定）
- `2` → `NOT_READY`（本地核心亦未就緒，需處理 BLOCKED）

## 檢查項目（16 項）

| # | id | 名稱 | 來源 env | PASS | WARN | BLOCKED |
|---|----|------|----------|------|------|---------|
| 1 | `line_channel_secret` | LINE_CHANNEL_SECRET 是否存在 | `LINE_CHANNEL_SECRET` | 已設定 | — | 未設定 |
| 2 | `line_channel_access_token` | LINE_CHANNEL_ACCESS_TOKEN 是否存在 | `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_ACCESS_TOKEN` / `LINE_CHANNEL_TOKEN` | 已設定且長度 ≥20 | 長度過短 | 未設定 |
| 3 | `line_identity_hash_key` | LINE_IDENTITY_HASH_KEY 長度 | `LINE_IDENTITY_HASH_KEY` | ≥16 字元 | 未設定（有 fallback 派生） | <16 字元 |
| 4 | `line_session_db_path` | LINE_SESSION_DB_PATH 可寫入 | `LINE_SESSION_DB_PATH`（預設 `data/processed/line_sessions.sqlite3`） | 目錄可寫 | — | 不可寫 |
| 5 | `line_use_formal` | LINE_USE_FORMAL 設定 | `LINE_USE_FORMAL` / `USE_FORMAL` / `FORMAL_ENABLED` / `CONVERSATION_USE_FORMAL` | 已設定 | 未設定 | — |
| 6 | `conversation_llm_model` | CONVERSATION_LLM_MODEL / ROUTER_LLM_MODEL | `CONVERSATION_LLM_MODEL` / `ROUTER_LLM_MODEL` | 任一已設定 | — | 皆未設定 |
| 7 | `provider_config` | OPENCODE / OPENAI provider 完整性 | `OPENCODE_API_KEY` / `OPENCODE_BASE_URL` / `OPENAI_API_KEY` | 依模型 provider 完整 | 未設定可 fallback | 缺 Key |
| 8 | `ollama_base_url` | OLLAMA_BASE_URL 可連線 | `OLLAMA_BASE_URL`（預設 `http://localhost:11434`） | 可連線 | 無法連線 | — |
| 9 | `bge_m3` | bge-m3 是否存在 | `OLLAMA_BASE_URL` + `data/processed/.vector_cache/*.pkl` | 快取或 Ollama 有 | 未發現 | — |
| 10 | `portal_urls` | patient / clinician portal URL | `PATIENT_PORTAL_URL` / `CLINICIAN_PORTAL_URL` / `APP_BASE_URL` 等 | 已設定 | 未設定 | — |
| 11 | `liff_config` | LIFF channel / client ID | `LINE_LOGIN_CHANNEL_ID` / `LINE_LIFF_ID` | 兩者皆有 | 部分或無 | — |
| 12 | `callback_https` | callback 需公開 HTTPS | `LINE_CALLBACK_URL` / `APP_BASE_URL` / `PATIENT_PORTAL_URL` 等 | `https://` | `localhost` 或未設定 | `http://` 非本地 |
| 13 | `webhook_signature_verification` | webhook 簽章驗證是否開啟 | `LINE_CHANNEL_SECRET` + `LINE_ALLOW_UNSIGNED_WEBHOOK` | 已開啟 | — | 未設定或允許未簽章 |
| 14 | `async_push` | async push Messaging API 權限 | `LINE_CHANNEL_ACCESS_TOKEN` 等 | 已設定 token | — | 未設定 |
| 15 | `rich_menu` | Rich Menu 提醒 | —（不呼叫 API） | — | 需人工確認 | — |
| 16 | `demo_clinician_allowlist` | Demo clinician allowlist | `LINE_DEMO_MODE` + `DEMO_CLINICIAN_IDS` | Demo 模式且有 allowlist | 未設定或未啟用 | Demo 模式但空 allowlist |

## Readiness 判定

```python
local_blocked = any(c.status == BLOCKED and c.category == "local" for c in checks)
if local_blocked:
    return "NOT_READY"
if any(c.status == BLOCKED for c in checks):
    return "READY_FOR_LOCAL_DEMO"
return "READY_FOR_LINE_DEVICE_DEMO"
```

- `NOT_READY`：`local` 類別（LLM 模型、provider、session DB）有 BLOCKED，連本地 Demo 都不可。
- `READY_FOR_LOCAL_DEMO`：本地核心通過，但 `line` / `security` 等有 BLOCKED，真機不可。
- `READY_FOR_LINE_DEVICE_DEMO`：無 BLOCKED，僅 WARN 提醒（Rich Menu、LIFF 等需人工確認）。

## 輸出範例

### 文字

```
============================================================
LINE 真機 Demo 上線前檢查（preflight）
============================================================
[PASS   ] ✓ LINE_CHANNEL_SECRET 是否存在
         已設定（長度合理，不顯示原文）
         → 若需輪替，請同時更新 LINE Console 與 .env，並重啟服務
[BLOCKED] ✗ CONVERSATION_LLM_MODEL 或 ROUTER_LLM_MODEL
         未設定
         → 請在 .env 設定 CONVERSATION_LLM_MODEL 或 ROUTER_LLM_MODEL（例：opencode/mimo-v2.5）
...
------------------------------------------------------------
統計：PASS 5 / WARN 7 / BLOCKED 4
結果：READY_FOR_LOCAL_DEMO
⚠️ 僅可本地 Demo，真機 Demo 尚缺設定（請處理 BLOCKED）
============================================================
```

### JSON

```json
{
  "readiness": "READY_FOR_LINE_DEVICE_DEMO",
  "summary": { "PASS": 14, "WARN": 2, "BLOCKED": 0 },
  "checks": [
    {
      "id": "line_channel_secret",
      "name": "LINE_CHANNEL_SECRET 是否存在",
      "status": "PASS",
      "message": "已設定（長度合理，不顯示原文）",
      "hint": "若需輪替，請同時更新 LINE Console 與 .env，並重啟服務",
      "category": "line"
    }
  ],
  "note": "no secrets included"
}
```

JSON 絕不包含秘密值，僅告知是否設定與修復提示。

## 修復指引（常見）

- **本地 Demo 缺項**：`CONVERSATION_LLM_MODEL` / `ROUTER_LLM_MODEL` 未設 → 在 `.env` 加 `ROUTER_LLM_MODEL=opencode/mimo-v2.5`；`OPENCODE_API_KEY` 需同步設定。
- **LINE 真機缺項**：`LINE_CHANNEL_SECRET` / `LINE_CHANNEL_ACCESS_TOKEN` 未設 → 從 LINE Developers Console 複製；`LINE_IDENTITY_HASH_KEY` 建議 ≥16 隨機字串；`LINE_SESSION_DB_PATH` 確保 `data/processed` 可寫。
- **callback**：真機需公開 `https://`（ngrok / Cloud Run），`localhost` 僅本地可用。
- **簽章驗證**：`LINE_ALLOW_UNSIGNED_WEBHOOK` 正式環境必須 `false`。
- **Rich Menu**：本工具不呼叫 LINE API，請手動預覽 `GET /api/line/rich-menu?patient_portal_url=https://...` 後再建立。
- **Demo clinician**：`LINE_DEMO_MODE=true` 時必須同時設定 `DEMO_CLINICIAN_IDS`。

## 安全保證

- 不讀出或顯示任何 secret / token 原文（輸出與 JSON 皆僅「是否設定」與「長度區間」）。
- 不呼叫 LINE API 建立／刪除資源（Rich Menu 僅給提醒與預覽 URL）。
- 缺少 LINE / GCP 設定時不修改外部服務（僅本地檢查與提示）。
- `git diff --check` 乾淨、無尾隨空白。

## 測試建議（供參考）

```python
def test_no_secret_in_output(monkeypatch, capsys):
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "my_secret_123")
    main([])
    out = capsys.readouterr().out
    assert "my_secret_123" not in out

def test_json_no_secret(monkeypatch, capsys):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-abc")
    main(["--json"])
    j = json.loads(capsys.readouterr().out)
    assert "sk-abc" not in json.dumps(j)

def test_missing_required_exits_nonzero(monkeypatch):
    monkeypatch.delenv("CONVERSATION_LLM_MODEL", raising=False)
    monkeypatch.delenv("ROUTER_LLM_MODEL", raising=False)
    assert main([]) != 0
```

完整 pytest 不得退步（21 tests 仍綠）。

## 限制與後續

- 本工具不檢查 GCP / 雲端部署參數（若未來有再擴充）。
- `bge-m3` 檢查優先看本地快取 `data/processed/.vector_cache/*.pkl`，其次嘗試 `OLLAMA_BASE_URL/api/tags`，離線有快取即可。
- 若需擴充 strict 模式，可在 CI 對 `readiness != READY_FOR_LINE_DEVICE_DEMO` 直接 fail。
