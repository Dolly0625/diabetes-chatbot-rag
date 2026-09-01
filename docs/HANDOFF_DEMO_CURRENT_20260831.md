# Demo 交接手冊（2026-08-31）

## 先讀這段：目前產品怎麼分工

這個專題的 Demo 不要再把所有事塞進 LINE。

| 使用場景 | 放在哪裡 | 目前定位 |
| --- | --- | --- |
| 日常聊天、糖尿病衛教 | LINE | 主流程，已可用 |
| 看診前資料整理 | 病患網站 | 主流程，與 AI 一題一題對談 |
| 查看已確認摘要 | 醫護網站 | Demo 必須補齊清楚入口 |

一句話說明產品：**LINE 負責衛教；病患在網站完成看診前對談；醫護在網站讀取病患確認後的摘要。**

## 目前工作位置與狀態

- 工作目錄：`/Users/dolly/Documents/code/tfda-diabetes-agent-previsit-room-sse-integration`
- 分支：`previsit-room-sse-integration`
- 最新已提交基底：`8a91af7 feat(previsit-room): implement SSE streaming frontend with fallback`
- 本輪修改尚未 commit、未 merge、未 push。不要用 `git reset --hard`、`git checkout --` 或刪除未追蹤檔案。
- `data/processed/line_sessions.sqlite3-shm` 是 SQLite 執行中的暫存檔，**不可 commit**。

本輪完成後的完整測試：`695 passed, 5 warnings`。

## 已完成、可以展示的功能

### 1. LINE 衛教

LINE 上的糖尿病衛教問答仍是主功能，例如詢問糖尿病成因、飲食原則。不要在 LINE 內硬跑八題看診問卷。

### 2. 病患網站的看診前對談

展示入口：`/demo/previsit`。

- 開啟時會建立新的匿名展示 session，再導向專屬對談室。
- 不需要 LINE、LIFF 或登入。
- 一進頁面直接顯示第一題；不再顯示 `stage1`、版本、草稿三選一或「開始新的整理」等工程用語。
- 首題的「沒有吃藥」已實測回覆：

  > 了解，我先記為目前沒有固定用藥。接下來想確認：有沒有藥物或食物過敏？沒有、不確定都可以直接說。

- 內部值如 `none`／`無` 不會顯示給病患。
- 暫停與清除收在「需要先離開嗎？」低調選單。
- 紅旗、權限錯誤、版本衝突、未確認資料等安全行為仍留在後端；UI 只把錯誤翻成人話。

### 3. 安全與資料處理仍保留

- 網頁只是呈現層，資料寫入仍經 `ConversationOrchestrator`、`PendingAction`、版本鎖與紅旗判斷。
- 網頁回覆自然化只作用於專用的 web pre-visit API，不改 LINE 衛教，也不改核心狀態機。
- `/demo/previsit` 必須同時有 `LINE_DEMO_MODE=true` 與 `DEMO_WEB_ENABLED=true` 才會啟用；每次新開都產生獨立展示資料。不可輸入真實病患資料。

## 尚未完成：Demo 前真正要補的項目

優先順序如下，**不要再擴充其他功能**。

1. **醫護端入口與固定摘要案例**
   - LINE 卡片應有兩個清楚入口：
     - 病患：開始看診前整理
     - 醫護：查看病患摘要
   - 醫護端應展示一份病患「已確認」的結構化摘要；不要讓現場 Demo 必須從零填完所有題目才看得到。
   - 正式醫院版再接院方帳號／SSO；Demo 使用 allowlist 或預備資料即可。

2. **一份固定 Demo 劇本與重置方式**
   - 提前準備一個固定病患案例與一份已確認摘要。
   - Demo 現場不要測 LINE 跳轉、草稿恢復、手機登入或任意口語變體。

3. **完整對談的語氣微調**
   - 首題「沒有吃藥」已修；其他欄位仍可能保留舊有的「你提到／我記為／對嗎」模板。
   - 這是體驗改善，不應阻塞醫護摘要頁與固定 Demo 劇本。

4. **真正 token-by-token streaming（非 Demo blocker）**
   - 現在頁面會先顯示「正在理解…」，完成後顯示整段回覆。
   - SSE 端點與 loading 已存在，但不是模型 token 逐字串流。要做展示可說「處理中」，不要宣稱已逐字串流。

## 不要做的事

- 不要把看診前八題重新塞回 LINE。
- 不要為了解決 UI 問題而取消樂觀鎖、紅旗、確認才分享或權限檢查。
- 不要把 Demo 入口當成正式病患登入，也不要輸入真實個資。
- 不要在現場臨時試不同用語；使用固定腳本。
- 舊文件若仍說「草稿必須三選一」，那是較早的設計；**目前 Demo UX 已改為直接接著下一題，不顯示阻塞彈窗。**

## Demo 當天的最短劇本

### 畫面一：LINE 衛教（約 30 秒）

1. 打開 LINE 官方帳號。
2. 輸入：`糖尿病可以吃什麼？`
3. 說明：LINE 作為日常衛教與聊天入口。

### 畫面二：病患網站（約 60 秒）

1. 用瀏覽器開啟 `<公開 HTTPS 網址>/demo/previsit`。
2. 顯示「看診前資料整理」與第一題。
3. 點「沒有吃藥」。
4. 等待「正在理解…」結束，展示自然的下一題（過敏）。
5. 說明：網站適合長對談、整理進度與最後確認，因此看診前蒐集放在這裡。

### 畫面三：醫護摘要（約 30 秒）

1. 開啟事先準備的「已確認病患摘要」。
2. 說明：醫護只讀到病患確認後的結構化內容，例如用藥、過敏、病史、症狀與想問醫師的問題。
3. 不要在現場從頭填滿所有欄位再跳到醫護頁。

Demo 的口頭總結：

> LINE 負責日常衛教；需要整理看診資料時，病患改到網站和 AI 對談；確認後，醫護能快速看到結構化摘要。

## 啟動方式（本機 + ngrok）

先確認 `.env` 已有既有的 LINE、LLM 與資料庫設定；不可把 `.env` commit。

```bash
cd /Users/dolly/Documents/code/tfda-diabetes-agent-previsit-room-sse-integration
set -a; . /Users/dolly/Documents/code/tfda-diabetes-agent/.env; set +a
export DEMO_INTAKE_TOKEN_ENABLED=true
export DEMO_WEB_ENABLED=true
/Users/dolly/Documents/code/tfda-diabetes-agent/.venv/bin/python -m uvicorn line_bot.app:app --host 0.0.0.0 --port 8000 --log-level info
```

另一個終端（若 ngrok 尚未啟動）：

```bash
ngrok http 8000
```

取得目前公開網址：

```bash
curl -fsS http://127.0.0.1:4040/api/tunnels | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(t["public_url"] for t in d["tunnels"] if t.get("proto")=="https"))'
```

瀏覽器開：`<上一步 HTTPS 網址>/demo/previsit`。

## 驗收指令

```bash
cd /Users/dolly/Documents/code/tfda-diabetes-agent-previsit-room-sse-integration

# 目前這輪的關鍵網頁契約
python3 -m pytest \
  line_bot/tests/test_previsit_room_api.py \
  tfda_context_gate/tests/test_previsit_room_frontend_contract.py \
  tfda_context_gate/tests/test_previsit_room_sse_contract.py -q

# 前端語法／雙檔同步
perl -0777 -ne 'print $1 if /<script>(.*)<\/script>/s' line_bot/static/previsit-room.html | node --check
diff -q line_bot/static/previsit-room.html line_bot/static/previsit_room.html

# 完整回歸（最近結果：695 passed）
python3 -m pytest -q
```

## 重要檔案

- `line_bot/app.py`：FastAPI、LINE callback、專用 web pre-visit API、Demo 入口、web 回覆自然化。
- `line_bot/static/previsit-room.html`：病患展示聊天頁的正式檔案。
- `line_bot/static/previsit_room.html`：相容檔，內容必須與上檔完全相同。
- `line_bot/tests/test_previsit_room_api.py`：token、API、紅旗、meta 文字、展示入口、回覆自然化測試。
- `tfda_context_gate/tests/test_previsit_room_frontend_contract.py`：病患端 UX／靜態契約。
- `docs/demo/LINE_PREVISIT_ROOM_ENTRY.md`：較早的 LINE → 看診前對談室設計參考；以本手冊的產品分工為最新決策。

