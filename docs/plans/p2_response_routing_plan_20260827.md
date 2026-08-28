# P2 計劃：回應分流與去機器腔（2026-08-27）

> 依據：docs/research/out_of_scope_response_research_20260827.md（D1-D6 改法已標行號）
> 基準 commit：f9d4a5d
> 動機：使用者真實對話（2026-08-27 13:36-13:37 LINE transcript）暴露三類失敗，見下方「驗收語錄」。

## 使用者真實語錄（= P2 驗收案例，全部必須通過）

| # | 使用者說 | 現在的反應 | 期望 |
|---|---|---|---|
| T1 | 「我想睡覺」 | ❌ BLOCKED 嚇人模板 | 友善接話＋能力選單 Quick Reply |
| T2 | 「你可以跟我說什麼」 | ❌ BLOCKED 嚇人模板 | 介紹三大功能＋Quick Reply |
| T3 | 「為什麼會有糖尿病」 | ❌ 答非所問（回飲食）＋機器腔 | 病因相關衛教內容；若檢索不到→誠實說沒資料並建議問醫師，不可拿飲食充數 |
| T4 | 「糖尿病可以吃什麼」 | ⚠️ 時好時壞（路由不穩） | 穩定回飲食衛教，去機器腔開頭 |
| T5 | 「胸痛冒冷汗」（紅旗回歸） | ✅ abort | **不得回退，仍必須 abort** |

## 必做 5 項（對應研究 D1-D6）

### G1. fallback 模板細分（fallbacks.py:11-20 + graph.py:244-254）

A_BLOCKED 一把抓拆成：
- `CHIT_CHAT_OUT_OF_SCOPE`：閒聊/benign 短句 →「這個我幫不上，不過我可以：🥗 衛教 📋 看診前資料整理 💊 藥物查詢，試試哪個？」＋ Quick Reply 三鈕
- `Q_NEED_MORE`：模糊問題 →「可以多說一點嗎？」＋建議選項
- `E_EMERGENCY / U_URGENT`：維持現有嚴肅模板，一字不改

### G2. 閒聊白名單（a_router/rules.py / labels.py）

「想睡覺」「無聊」「你好」等 benign 短句 → O_OUT_OF_SCOPE（走 G1 的 CHIT_CHAT 模板）；MENTAL_HEALTH_CRISIS 只認明確字眼（自我傷害類），不得把「想睡覺」當心理危機。

### G3. grounded 回應去機器腔（deterministic_generators.py:81）

- 刪「根據提供的資料：」前綴，改「幫你整理了衛教重點（依 TFDA／國健署）：」
- 規則式選句：按 。；切句，選 1-2 句最相關核心（關鍵詞重疊度），不整段串接
- 保留 source citation 與免責句（D gate 302-400 字規則確認不受影響）

### G4. 檢索精準度（tfda_retriever.py / hpa_retriever.py）

- chunk 按主題小標切分（病因/遺傳/飲食/睡眠），加 metadata.topic
- 相似度硬門檻 + topic 一致性檢查：問病因不得命中飲食文件
- 低於門檻 → 誠實回：「這題我手上的衛教資料不夠，建議看診時問醫師。」＋引導整理看診問題

### G5. HPA FAQ 補充內容

新增 4 主題 FAQ chunks：成因總覽／遺傳／睡眠與血糖／本助手能做什麼（供 T1/T2/T3 命中）。走既有 vector cache 建置流程，記得 invalidate `.vector_cache` 相關快取。

## 明確不做

- 不讓 LLM 自由生成醫療內容（全部規則式/模板式改寫）
- 不動 E/U 緊急模板文字、B/D gates、紅旗路徑
- 不動 intake 8 欄位與 P0/P1 修復成果

## 開發者驗收條件

- [ ] T1-T5 逐條重放（orchestrator + run_workflow 雙路），貼原始輸出
- [ ] pytest 全綠且新增 G1-G5 各自測試（≥180）
- [ ] T5 紅旗 + P0 的 5 條紅旗回歸全部仍 abort
- [ ] T3 在「檢索不到病因內容」與「已補 FAQ」兩種情況下分別測：前者誠實拒答、後者正確回答
- [ ] 機器腔檢查：所有非緊急回覆不含「根據提供的資料」
