# Demo 最終交接手冊（2026-09-01）

> 這份是目前 Demo 的最新決策；較早的 `HANDOFF.md` 與
> `HANDOFF_DEMO_CURRENT_20260831.md` 可留作歷程，但不要用它們把看診前問卷塞回 LINE。

## 一句話產品流程

**LINE 負責日常聊天與糖尿病衛教；看診前資料由病患在專用網頁和 AI 對談；病患確認後產生 QR 分享碼，醫護在另一個網頁掃碼或貼碼查看唯讀摘要。**

## 目前可展示的網址

目前 ngrok 公開網址（ngrok 重啟後會改，請重新查）：

- 病患看診前對談室：`https://heroism-unkempt-pediatric.ngrok-free.dev/demo/previsit`
- 醫護摘要頁：`https://heroism-unkempt-pediatric.ngrok-free.dev/clinician`
- 健康檢查：`https://heroism-unkempt-pediatric.ngrok-free.dev/health`

Demo 醫護 ID：`doctor-demo`。

## 已完成的入口替換（最新）

LINE 現有常駐選單會送出文字 `開始看診前整理`。後端已改為：

```text
Rich Menu 送出「開始看診前整理」
  → LINE 回傳「看診前對談室」卡片
  → 按「開啟看診前對談室」
  → /demo/previsit
  → 產生新的匿名 Demo session
  → /patient/previsit-room?token=...（瀏覽器內部轉址）
```

- 新的卡片不再連回舊 LINE ProductSession，因此不會顯示以前卡住的八題、草稿或版本衝突。
- `/demo/previsit` 每次都建立新的 Demo session，適合現場重跑。
- **先前已經收過的舊卡片無法被後端改寫**；測試時必須重新從 Rich Menu 點一次，取得新卡片。
- 一般衛教回答不再自動附上「如果要看醫生需要幫你整理嗎？」；使用者要整理時由 Rich Menu 進網頁。

## 病患與醫護的 Demo 操作

### 病患端

1. LINE 中做衛教聊天，例如：`糖尿病飲食可以怎麼吃`。
2. 要整理看診資料時，點 Rich Menu 的「開始看診前整理」。
3. 在新卡片按「開啟看診前對談室」，確認網址先是 `/demo/previsit`。
4. 網頁內一次回答一題；完成後按「確認完成」。
5. 按「分享給醫護」，頁面會產生一次性 QR code／分享碼。

### 醫護端

1. 開啟 `/clinician`。
2. 輸入 Demo 醫護 ID：`doctor-demo`。
3. 用相機掃病患畫面的 QR code，或貼上分享碼。
4. 只能讀取病患已確認的結構化摘要，不能修改病患資料。

## 啟動服務（目前正在跑的設定）

工作目錄：

```text
/Users/dolly/Documents/code/tfda-diabetes-agent-previsit-room-sse-integration
```

服務目前 PID 是 `43162`。若要重啟，先停止舊 process，再執行：

```bash
cd /Users/dolly/Documents/code/tfda-diabetes-agent-previsit-room-sse-integration
set -a; . /Users/dolly/Documents/code/tfda-diabetes-agent/.env; set +a
export DEMO_INTAKE_TOKEN_ENABLED=true
export DEMO_WEB_ENABLED=true
export LINE_DEMO_MODE=true
export DEMO_CLINICIAN_IDS=doctor-demo
/Users/dolly/Documents/code/tfda-diabetes-agent/.venv/bin/python -m uvicorn line_bot.app:app --host 0.0.0.0 --port 8000 --log-level info
```

ngrok 若已啟動，不需要重啟。查目前公開 URL：

```bash
curl -fsS http://127.0.0.1:4040/api/tunnels | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(t["public_url"] for t in d["tunnels"] if t.get("proto")=="https"))'
```

`.env` 在主工作目錄：

```text
/Users/dolly/Documents/code/tfda-diabetes-agent/.env
```

其中已有使用者填寫的 `GEMINI_API_KEY`；絕不顯示、提交或複製其中的 secret。

## RAG 整合現況

- `diabetes-rag` 已加入這個 integration worktree 作 submodule，並以 editable install 安裝到既有 `.venv`。
- `RAG_BACKEND` 預設會走 RAG 組的 `EvidenceRetrievalTool`；主專案仍會經 A/B/C/D 安全流程。
- Gemini key 已可讓 RAG 同時回傳 vector + graph 證據。
- 已知 RAG 品質問題：`為什麼會有糖尿病` 有時被 D gate fallback；`糖尿病怎麼得到的` 可能檢到不夠貼近的證據。這是 RAG 語料／檢索品質議題，整理觀察交給 RAG 組優化，不應為了 Demo 繞過 B/D gate。

## 已知限制（Demo 時請避開）

1. 網頁 intake 仍沿用較早的 8 欄結構；回覆自然度不完全一致，部分情境仍可能出現「你提到／我記為／對嗎」模板。
2. 模型不是逐 token streaming：頁面會顯示處理中，完成後才顯示回答。
3. Demo 網頁是匿名短期 session，不是正式醫院登入；正式版須改用 LIFF + 院方 SSO/OIDC。
4. QR 分享碼是一次性、短效、只讀；病患一定要先「確認完成」才能產生。
5. 不要在現場試任意口語變體，依下方劇本展示即可。

## 最短 Demo 劇本

1. **LINE 衛教**：輸入 `糖尿病飲食可以怎麼吃`，展示衛教回答。
2. **進入病患網頁**：從 Rich Menu 重新點「開始看診前整理」，點新卡片進 `/demo/previsit`。
3. **病患對談**：使用固定回答完成幾題，展示確認摘要與 QR code。
4. **醫護端**：進 `/clinician`，輸入 `doctor-demo`，掃 QR code，展示唯讀結構化摘要。

口頭總結：

> LINE 保持簡單，負責日常衛教；需要整理看診資訊時才去網站和 AI 對談；病患確認後，醫護可以快速查看摘要。

## 驗證紀錄與接手檢查

本輪入口替換後已跑：

```bash
/Users/dolly/Documents/code/tfda-diabetes-agent/.venv/bin/python -m pytest -q \
  tfda_context_gate/tests/test_line_callback_session.py \
  line_bot/tests/test_line_entry_boundary.py \
  tfda_context_gate/tests/test_p1_1_controls_and_generality.py
# 29 passed, 2 warnings

curl -sS http://127.0.0.1:8000/health
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  https://heroism-unkempt-pediatric.ngrok-free.dev/demo/previsit
# 預期 303，導向 /patient/previsit-room?token=...；token 不可貼到文件或 git。
```

較早一次完整 pytest 的結果為 `706 passed, 2 warnings`，但在後續入口小改後尚未再完整重跑；若要交付／合併，請先跑：

```bash
cd /Users/dolly/Documents/code/tfda-diabetes-agent-previsit-room-sse-integration
/Users/dolly/Documents/code/tfda-diabetes-agent/.venv/bin/python -m pytest -q
git diff --check
git status --short
```

## Git 與安全注意事項

- 分支：`previsit-room-sse-integration`。
- 工作樹有很多**尚未 commit** 的功能變更與 untracked 檔，包含 RAG adapter、網頁、QR 分享、入口測試；不可用 `git reset --hard`、`git clean -fd`、`git checkout --`。
- `data/processed/line_sessions.sqlite3-shm` 和 `-wal` 是執行期 SQLite 暫存，不能提交。
- 目前尚未 merge、push 或 commit 本輪整合結果。
- 改動涉及既有使用者檔案時，先看 `git diff`，只 stage 明確屬於本輪的檔案，絕對不要 `git add -A`。

## 重要檔案

- `line_bot/app.py`：LINE callback、`/demo/previsit`、病患對談 API、QR 分享 API。
- `line_bot/static/previsit-room.html`：病患對談室。
- `line_bot/static/clinician.html`：醫護端 QR 掃描／分享碼輸入／唯讀摘要。
- `line_bot/ui.py`：LINE Flex 卡片與 Rich Menu payload。
- `tfda_context_gate/workflow/intake_router.py`：已關掉舊衛教尾端 intake invitation。
- `tfda_context_gate/rag/diabetes_rag_retriever.py`：主專案到 RAG 組的 in-process adapter。
- `tfda_context_gate/workflow/formal_factory.py`：正式 RAG backend 選擇。
