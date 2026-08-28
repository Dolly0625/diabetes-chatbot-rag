# P0 修復計劃：欄位錯位與輸入衛生（2026-08-27）

> 依據：docs/reviews/p1_simulated_user_testing_20260827.md 異常總表之高/中嚴重度項
> 基準 commit：86824fa。Phase 1 文案成果（複述/待確認/無題號）不得回退。

## Root Cause（已由 Sisyphus 親自確認）

`orchestrator.py:557` `_normalize_intake_answer()`：取 `session.pending_field` 後**把整句 user text 直接歸屬該欄**（`early_extracted` 雖呼叫 `extract_fields_from_utterance`，但 fallback 仍寫入 `field`）。使用者提前說症狀、家屬說時間 → 全部塞進當前 pending 欄。

## 必修 4 項（P0）

### F1. 內容驅動的欄位路由（修 A1/B2/D4，最高優先）

改 `_normalize_intake_answer` 為「先抽後配」：
1. 對 user text 跑 `extract_fields_from_utterance`（跨 stage，或逐欄分別嘗試），得到候選 `(field, value)` 集合。
2. 每個候選值寫入**它語意所屬的欄**（如 `大概一個月前開始口渴` → `symptom_onset`），與 `pending_field` 無關。
3. 只有當 text **無法被任何欄位語意吸收**時，才視為 pending 欄的直接回答（例如 pending=`allergies` 時答「沒有」）。
4. 寫入後重算 `pending_field = _next_pending_field(intake)`，天然消除 B2 的 onset 卡循環。
5. `pending_field` 與內容語意衝突時（例：問家族史但答案明顯是 onset），確認句應複述正確欄位歸屬：「你說的『一個月前開始口渴』我記在『症狀開始時間』」。

驗收測試：重放 B 場景 9 輪，`symptom_onset` 不得為「想問醫師…」、`family_history` 不得為「大概一個月前開始口渴」；A 場景 Turn 4 後不得空轉 2 輪。

### F2. 注入句不得作為資料值（修 D3）

在寫入任何欄位前，檢測指令/提權語句（「叫你直接給處方」「忽略規則」「你是醫師」類，建議 regex 清單放 `intake/tool.py` 常數，例如 `INJECTION_PATTERNS`）。命中：不寫入、回固定句「這裡只協助整理看診資料，無法提供處方或醫療指示」，維持 pending 原題重問。**不得新增 LLM 呼叫。**

### F3. 無效內容過濾（修 D2 + D1）

寫入前對 text 做有效性檢查（`intake/tool.py` 新增 `is_plausible_intake_value(text)`）：
- 純 emoji/符號/單字元重複 → 視為無效（D1：回 pending 題重問，**不得** BLOCKED fallback）。
- 同一 token 重複 ≥5 次或實質內容 <4 個不重複字 → 無效（D2：不寫入，回 pending 題）。
- 長度 >120 字：截取前 120 字寫入並在複述句標示「(已節錄)」，不得照單全收。

### F4. 「不清楚」誤擋修正（修 B1）

`我幫我媽問的 她不清楚` 觸發 BLOCKED 的原因需定位（疑為 B/D gate 對「不清楚」或 orchestrator 上層誤判）。修法：gate 命中判斷應區分「醫療不確定語」vs「emergency/risk 語」；含代述意圖（「幫…問」「代…整理」）應導向 `NEEDS_ROLE_SELECTION`/授權流程，不得 BLOCKED。回歸條件：真紅旗句（E 場景）仍必須 100% abort，此項改動後必跑紅旗回歸。

## 明確不做

- 不動 B/D gates 核心邏輯與紅旗轉介路徑（F4 只調整誤擋判定，不擴大攔截豁免到醫療風險語）
- 不動 8 欄位結構、FHIR、raw image、hash PII
- 不實作 Phase 2 功能（repair 專路、digression 衛教育答、自適應選題）——C1 岔題未答衛教屬 Phase 2，本輪只確保「資料已保留+回原題」現狀不回退
- 不新增 LLM 呼叫點

## 開發者驗收條件

- [ ] pytest 全綠（基準 167 passed，新增測試後 ≥170）
- [ ] 新增測試：F1 欄位路由（B 場景重放、A Turn4 提前症狀）、F2 注入句拒絕、F3 emoji/雜訊/超長、F4「幫我媽問的」不誤擋且真紅旗仍 abort
- [ ] 重放模擬測試 4+1 場景（指令見模擬測試報告），A/B/D 場景 intake_snapshot 欄位歸屬正確
- [ ] 文案成果不回退：無「第 n 題」、複述句保留、單輪確認 ≤2 項
