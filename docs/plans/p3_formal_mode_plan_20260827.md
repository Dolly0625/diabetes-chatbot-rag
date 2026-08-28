# P3 計劃：LINE 啟用正式模式（真 RAG + LLM 路由）（2026-08-27）

> 依據：使用者和 Sisyphus 共同確認 LINE bot 從未走真 RAG（use_formal 從未傳入）
> 基準 commit：98d2393。前置已完成：bge-m3 索引已建（tfda_context_gate/data/processed/.vector_cache/）、套件已裝、Ollama 活著。

## 目標

LINE 進入點（`line_bot/app.py` 與 `tfda_context_gate/line_orchestration/orchestrator.py` 內所有觸發 run_workflow 的呼叫點）接上 `use_formal=True`，讓衛教查詢走真實 A（LLM 路由）→ RAG（TFDA/HPA 向量檢索）→ C（生成），同時保持 P0/P1/P2 全部成果與安全紅線。

## 實作要求

### H1. 接線
- 找出所有 `run_workflow(...)` / `handle_text_message(...)` 呼叫點（app.py 相容模式 + orchestrator 內部），統一加 `use_formal=True`。
- 模型/端點一律從 `.env` 讀（ROUTER_LLM_MODEL、OLLAMA_BASE_URL、OPENCODE_API_KEY），不得 hardcode。
- 紅旗 deterministic pre-check 與 `revalidate_via_a` 路徑**必須在 formal 路徑上依然先生效**（LLM 之前）。

### H2. 防呆
- formal 失敗（LLM 超時/Ollama 掛掉/API key 無效）→ fallback 到現有安全回覆（嚴肅模板），絕不可掛掉不回訊息；記 trace。
- 回覆延遲：衛教查詢預期 +2~5s；若 >15s 要有處理（逾時訊息）。

### H3. 不回退清單
- T1（我想睡覺→友善選單）、T2、T4（去機器腔開頭）、紅旗 5 條（直接/間接/洗白/否定/轉折）、P0 欄位路由、P1 複述與無題號——全部維持。
- G3 去機器腔的前綴規則需同樣套用到 formal 生成的路徑（C generator formal 輸出也要套「幫你整理了衛教重點（依 TFDA／國健署）：」開頭規則），若 formal C 輸出格式不同需對齊。

## 開發者驗收條件

- [ ] pytest 全綠（含新增接線測試：mock LLM/檢索下驗證 use_formal 傳遞）
- [ ] 實跑（非 mock）：`run_workflow(..., use_formal=True)` 與 orchestrator 路徑各重放 T1-T5 + 「糖尿病可以吃什麼」「為什麼會有糖尿病」，貼原始輸出（含 trace 的 router_status 與 evidence 來源）
- [ ] T3 呈現誠實行為：若檢索無病因內容→「資料不夠」話術；不得再拿飲食句充數（formal 檢索門檻生效）
- [ ] 紅旗 5 條雙路（orchestrator + run_workflow formal）100% abort
- [ ] Ollama 停掉模擬災難：bot 回覆安全 fallback 而非掛掉（可單元模擬）
- [ ] 全程 grep 確認無 hardcode 模型名

## 已知影響（要向使用者揭露）

- 衛教回答延遲 +2~5s
- 回答內容隨真實文件變化
