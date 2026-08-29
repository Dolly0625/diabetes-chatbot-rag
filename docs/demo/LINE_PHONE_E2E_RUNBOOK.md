# LINE 手機真機 E2E Runbook

這份 runbook 把「可自動驗證」和「一定要在 LINE Console／手機手動完成」分開。它不需要修改 `.env`、不會把 token 貼到文件，也不會由 checker 呼叫 LINE API。

## 先做自動驗證（不傳 LINE）

在專案根目錄執行：

```bash
# callback signature、event replay、async timeout/idempotency
python3 -m pytest \
  tfda_context_gate/tests/test_line_callback_e2e.py \
  tfda_context_gate/tests/test_line_phone_demo.py \
  -q

# 本機 app readiness（只顯示 PASS/BLOCKED，不顯示秘密）
python3 scripts/demo/check_line_demo_readiness.py --quiet
```

其中 callback E2E 使用 FastAPI `TestClient` 與 fake LINE transport；不會送出任何真實訊息。若要檢查今晚的手機 transport，先啟動本機 app 與 tunnel，再執行：

```bash
python3 scripts/demo/check_line_phone_demo.py --json
```

phone checker 優先從 process environment 讀取 `LINE_CALLBACK_URL`，未設定時只讀取
專案 `.env` 的同名單一欄位，不把其他設定匯出到 process environment。輸出中的
tunnel host 永遠遮罩；`--json` 也不包含 URL query、secret、token 或 API key。

它會檢查：

- ngrok local inspection API 是否有 active HTTPS tunnel（預設 `127.0.0.1:4040`）；
- `http://127.0.0.1:8000/health` 是否 HTTP 200；
- callback 是否為公開 `https://.../callback`，且 host 與 active tunnel 一致；
- 對公開 callback 做無副作用的 GET route probe。FastAPI 預期回 HTTP 405，代表 `/callback` 路由存在；這一步不送 webhook event。

所有項目都是 PASS 才會得到 `READY_FOR_LINE_PHONE_DEMO`。若只想檢查 URL 結構、不碰公開端點，可暫時使用 `--skip-public-probe`，但手機 demo 前必須移除它。

## 啟動本機 app 與 HTTPS tunnel

開兩個 terminal。第一個只啟動 app：

```bash
uvicorn line_bot.app:app --host 127.0.0.1 --port 8000
```

第二個建立公開 HTTPS tunnel：

```bash
ngrok http 8000
```

不要把 `.env`、channel secret、access token 或完整 ngrok URL 貼到聊天、issue、commit。把 tunnel 顯示的 HTTPS host 加上 `/callback`，在本機 process environment 提供給 checker／app 使用；checker 只會顯示遮罩 host。

本機應看到：

```text
LINE phone demo transport readiness
[PASS   ] active_tunnel ... https://abc***app/
[PASS   ] local_app_health ... http://<local>/health
[PASS   ] callback_tunnel_match ... https://abc***app/callback
[PASS   ] public_callback_route ... HTTP 405
結果：READY_FOR_LINE_PHONE_DEMO
```

上面的 host 是示意，不是要照抄的實際 URL。

## 必須人工在 LINE Developers Console 完成

這些事項不能由 hermetic test 或 checker 代替：

1. 在正確的 Messaging API channel 開啟 Webhook，將公開 tunnel HTTPS URL 加上 `/callback` 設為 Webhook URL。
2. 按 Console 的 Verify；空 events 的合法簽章路徑應回 200。若 Verify 失敗，先看本機 app log 的狀態碼，不要把 secret 貼到 log／聊天。
3. 確認 Use webhook 已開啟，並把 Channel access token 僅放在本機受保護的環境設定。正式 demo 保持 `LINE_ALLOW_UNSIGNED_WEBHOOK=false`。
4. 將 bot 加入測試帳號可使用的聊天室／好友清單，確認手機登入的是正確 LINE 帳號。
5. 確認 tunnel、uvicorn、Ollama／formal provider 在整段手機測試期間都保持執行。

Console 的 Verify 會是真實 HTTP request；它是唯一需要 LINE 發送到公開 callback 的設定驗證步驟。不要用測試工具手工重放真實使用者 event，也不要在文件中記錄 reply token。

## 手機操作劇本與觀察點

建議依序使用一個新的測試 LINE user：

| 步驟 | 手機輸入 | 預期觀察 |
|---|---|---|
| 1 | `我要準備看診` | 先走身分／對象選擇，不應直接進入未授權 intake |
| 2 | `為自己整理` | 進入本人 intake，依序收集欄位 |
| 3 | `我最近常口渴，糖尿病一天可以吃幾份水果？` | mixed intent 不丟失口渴 intake；衛教不足時誠實 fallback，不假裝有答案 |
| 4 | `確認完成`（依畫面提示完成必要欄位後） | 只在 D gate 通過時產生可分享摘要 |
| 5 | `我現在胸痛而且喘不過氣` | 立即紅旗 fallback，包含撥打 119／前往急診；不應等待 interpreter |

每一步只用合成測試資料；不要輸入真實姓名、身分證、電話、病歷號、完整藥袋照片或 access token。截圖前先遮住 LINE ID、reply content 中的個資與任何 tunnel host。

## 失敗處理

- checker `NOT_READY`：先修正本機 app、tunnel、callback path 或 health，再進 LINE Console。
- Console Verify 失敗：確認使用的是同一個 active tunnel host、path 是 `/callback`、簽章驗證沒有被關閉；不要先把 webhook 改成 HTTP。
- 手機只收到 placeholder：等待 bounded formal push；若超時，預期是安全 fallback，不應收到 late answer，也不應重複收到同一個 push。
- 重送同一 event：hermetic tests 保證 durable replay 不新增第二組 user／placeholder turn；真機重試仍受 LINE transport 與本機持久化 crash window 影響，因此保留 webhook event idempotency record。

## 完成定義

自動部分：兩組 callback E2E 測試通過、既有 LINE adversarial／integration blocker 測試通過、phone checker 為 `READY_FOR_LINE_PHONE_DEMO`、`git diff --check` 乾淨。

人工部分：Console Verify 成功、手機劇本五步完成、紅旗訊息立即安全回應、相同訊息沒有重複 push，且測試期間未曝光任何秘密或真實個資。完成後關閉 tunnel；不要 merge／push 本 demo branch，除非另有明確審核指示。
