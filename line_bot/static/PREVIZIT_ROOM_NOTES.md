# 看診前 AI 對談室 — 前端交付說明

## 檔案
- `line_bot/static/previsit_room.html` — 單檔原生 HTML/CSS/JS，無框架，無打包。僅此檔 + 本文件為本次新增資產。
- 契約測試：`tfda_context_gate/tests/test_previsit_room_frontend_contract.py`（13 項，`pytest -q` 全綠，無 Node）

## 固定契約（後端尚未合併，純前端約定）
- 頁面：`GET /patient/previsit-room?token=<opaque>` → 回本檔（待後端在 `app.py` 加 `FileResponse`）
- 載入：`GET /api/patient/previsit-room`（`token` 由 `?token=` 或 `Authorization: Bearer <opaque>` 沿用，記憶體變數 `opaqueToken`，不寫 DOM/localStorage/log）
- 發言：`POST /api/patient/previsit-room/chat {message, version, client_message_id}` → `{reply,status,intake_stage,version,intake_snapshot}`

> 已用 `buildUrl()` 同步把 `token` 補到 query，`authHeaders()` 同步帶 `Authorization`，相容任一後端實作。

## UX 對應
1. **入口**：預設遮罩 `entryOverlay` 顯示「這是看診前整理室，與 LINE 衛教分開，絕不會在你未選擇的情況下悄悄帶入舊資料」。`GET /api/patient/previsit-room` 探測草稿：有內容/版本>0/訊息>0 → 同時顯示 **繼續上次整理 / 開始新的整理 / 取消整理**；無草稿只顯示 **開始新的整理**。選後才渲染歷史泡泡。
2. **對談**：`#chat[role=log]` 中 `bubble ai/user`，一次一題；底部固定 `input#input + button#send`，`Enter` 送出，上方 `quick[role=toolbar]` 橫向 chips（後端 `quick_replies` 驅動）。
3. **固定可見**：`action-bar` 內 **暫停並離開 / 結束並清除** 全程可見；暫停僅提醒不會自動分享，結束需 `confirm()` 二次確認才清畫面並送 `結束並清除`。
4. **摘要**：`shouldShowSummary()` 僅在 `COMPLETED / submitted / AWAITING_CONFIRMATION / review 完成` 才掛 `summaryWrap`（**確認完成 / 修改資料**）；`red_flag / RED_FLAG` 時強制隱藏摘要，不被成功卡覆蓋。
5. **390px / 鍵盤 / safe-area**：`viewport-fit=cover + interactive-widget=resizes-content`，`height:100dvh` + `env(safe-area-inset-*)` + `var(--keyboard-h)` + `env(keyboard-inset-height)`，`visualViewport` 監聽自適應；`max-width:760px` 置中，`@media (max-width:375px)` 泡泡 86%，無橫捲。
6. **錯誤人話**：`401 身分驗證失敗請重開、403 無權限、409 版本不一致已重新同步、0 網路失敗資料未儲存、5xx 伺服忙碌`，保留輸入值、透明度提示未送達，絕不假存。`client_message_id = crypto.randomUUID()` 同 id 重送不重複寫入。

## 安全
- `opaqueToken` 僅閉包變數，不進 `dataset / localStorage / innerHTML / console`。
- 已 `grep` 驗證：`data-token` 0、`localStorage` 0、`console.log.*token` 0、`\\n` 0。
- `white-space:pre-wrap` 呈現換行，不依賴 literal `\\n`。

## 本機驗證
```bash
python3 -m pytest tfda_context_gate/tests/test_previsit_room_frontend_contract.py -v  # 13 passed
python3 -m pytest line_bot/tests/test_intake_entry_controls.py -q  # 5 passed
```
`HTMLParser` 解析通過。

## 手動測試清單（交給 QA / PM 逐項勾）
- [ ] **390px**：iPhone 14 / Chrome 390 寬無橫向捲動，`inputBar` 不被鍵盤遮擋（`visualViewport` 生效），`safe-area` 在有瀏海機型不貼邊
- [ ] **入口**：無草稿僅見「開始新的整理」；有草稿同時見「繼續上次整理 / 開始新的整理」，未選擇前 `chat` 為空
- [ ] **繼續**：點「繼續」後舊泡泡按序出現，不重問已完成欄位
- [ ] **開始新**：點「開始新的整理」清畫面並送出首題，進度歸 0
- [ ] **一次一題**：`chat` 中 `ai/user` 交替，`quick` chips 點選即填入並送出
- [ ] **固定列**：捲動時 `暫停並離開/結束並清除` 與 `inputBar` 皆固定可見
- [ ] **暫停**：點「暫停並離開」顯示「已為你暫停…繼續上次整理即可繼續」，不自動分享
- [ ] **結束**：點「結束並清除」彈 `confirm`，確認後清畫面與進度，`stage` 回「準備中」
- [ ] **未完成不見摘要**：`stage1/2/3` 時 `summaryWrap` 隱藏
- [ ] **完成才見摘要**：`review/COMPLETED/submitted` 才見「結構化摘要 + 確認完成/修改資料」
- [ ] **紅旗不被蓋**：回覆含 `RED_FLAG/119/急診` 時紅底泡泡置頂，摘要保持隱藏
- [ ] **斷網**：關網路送出 → 顯示「網路連線失敗，資料未儲存，請檢查網路後重試」且輸入值保留
- [ ] **401/403**：用錯 `token` → 人話「身分驗證失敗/沒有權限」
- [ ] **409**：版號衝突 → 「版本不一致，已為你重新同步」並自動 `GET` 重載，輸入值保留可重送
- [ ] **重送**：同 `client_message_id` 連點送出不重複寫入
- [ ] **View Source**：搜 `token` 不見明文於 DOM，`Console` 無 `token` log，`Application/LocalStorage` 無 `token` key
- [ ] **無 \\n**：`grep -F '\\n' previsit_room.html` 0 命中

## 後端待辦（不屬本次變更）
- 在 `line_bot/app.py` 新增 `GET /patient/previsit-room` 與 `GET/POST /api/patient/previsit-room` 對應 `ConversationOrchestrator` + `ProductSession` + `client_message_id` 去重，成功後請補 E2E `playwright` 截圖。
