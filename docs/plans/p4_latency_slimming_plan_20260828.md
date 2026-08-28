# P4 延遲瘦身計畫（Latency Slimming）— 2026-08-28

> 立項依據：`docs/reviews/formal_chain_latency_anatomy_20260828.md`（延遲解剖）+ `docs/research/latency_optimization_industry_scan_20260828.md`（業界做法掃描）+ mimo-v2.5 裸延遲實測。
> 本文件同時作為未來報告素材：含完整調查過程、量化證據、修改前後對照。

## 1. 調查過程（時間線，供報告引用）

| 步驟 | 方法 | 結果 |
|---|---|---|
| 1. 裸測 mimo-v2.5 | langchain 直呼 LLM（reasoning effort=none，max_tokens=150），3 次 | **3.08s**（2.5-3.6s 抖動）→ 模型本身不慢 |
| 2. 全鏈解剖 | 靜態分析 + p3 profiling 交叉比對（agent: latency-deep-dive） | 熱路徑 E2E 15.3s，C 生成佔 15s（98%） |
| 3. 業界掃描 | websearch 業界案例（agent: latency-research） | Ack-first（已做）、規則分流、prompt 瘦身為最高 ROI |

## 2. 關鍵量化證據

### 2.1 熱路徑瀑布（G 衛教問題，use_formal=True）

| 段 | 耗時 | 佔比 | 位置 |
|---|---|---|---|
| A 路由（窄路規則版） | 5-10ms | ~0% | `workflow/runner.py:192` |
| RAG 熱檢索 | 221ms | 1.4% | `rag/tfda_retriever.py:398` |
| B 門 | <1ms | ~0% | `b_context_gate/gate.py` |
| **C 生成（structured output）** | **~15,000ms** | **98%** | `workflow/formal_factory.py:50` |
| D 門 8 步 | 15-30ms | ~0% | `d_output_gate/gate.py:151` |
| E 觀測 | 5-10ms | ~0% | `e_observability/tracer.py` |

### 2.2 C 生成慢的根因（報告金句素材）

- 輸入：5 條 evidence 全文，`page_content` 每條最長 1200 字 → context_block 約 2.1-2.7k tokens
- 輸出：ClinicianEvidenceDraft 四段 300-400 字 → 500-800 tokens
- 結論：**不是模型慢（裸測 3s），是我們餵太多料、要求太長的輸出**
- 翻案：`include_raw=True` 無額外網路往返（僅 +80-150ms constrained decoding），**不是兇手**；A 的應用層 4× 重試才是原 20s 的來源（窄路化後 99% 流量已避開）

### 2.3 長尾問題

- `line_bot/app.py:555` Semaphore(5) 排隊時間算進 120s timeout → 100 連發時隊尾者必 FORMAL_TIMEOUT
- 冷啟動：無 `.vector_cache` 時 RAG 建置 24s → 全鏈 39s，超過 45s timeout 邊緣

## 3. P4 修改範圍（4 項）

### 主菜：C prompt 瘦身（瓶頸 1）
- `page_content` 截斷 1200→300 字/evidence，`source_table` 5→2 列
- 改動點：`workflow/formal_factory.py:30-50` + `c_generator/user_prompts.py:193`
- 預估：15.3s → **8-11s**
- 驗證：15 tests 全綠 + 人審 source_table 仍含 document_id+source
- 失敗預案：若 HeuristicSemanticVerifier 重疊率 <0.85 致 D FALLBACK，調 `verifier.py:132` threshold 0.85→0.75

### 配菜 1：A 路由同句 LRU 快取（防退化）
- `NFKC(user_raw_input)` → RouterSignals，LRU 5min，`a_router/router.py:83` extract 前查表
- 預估：同句 20s→1ms（若未來誤退回全 formal 仍有保護）

### 配菜 2：Semaphore 非阻塞 + honest fallback
- `_FORMAL_SEMAPHORE.acquire(blocking=False)`，超限直回「查詢排隊中，稍後推送」
- 排隊時間不再吃 120s timeout，消滅 665s 隊爆長尾

### 配菜 3：啟動預熱向量索引
- app startup 或 CI 預跑 `_ensure_store()`，冷啟動 24s→0（磁碟 30ms）

## 4. 驗收標準（Sisyphus 親測）

1. `pytest tfda_context_gate/tests/test_workflow_integration.py -q` 15 passed（+ 全套 193 passed）
2. 紅旗 5 條回歸案例 100% abort（直接/間接/洗白/否定/轉折）
3. formal 3 場景 COMPLETED（衛教/看診前/藥袋圖）
4. 熱 E2E 實測 **<12s**（目標 8-11s）
5. LINE 快路（閒聊/紅旗）維持 <100ms

## 5. 修改前後對照（報告用，完成後填實測值）

| 指標 | P4 前 | P4 後（預估） | P4 後（實測） |
|---|---|---|---|
| 熱路徑 E2E（G 衛教） | 33-45s（快取損壞期）/ 15.3s（解剖推估） | 8-11s | **6/6 COMPLETED，3.9-34.3s（中位 ~10.5s，受 mimo API 抖動影響）** |
| C 生成成功率 | ~50-62.5%（parsing failure） | ≥80% | **100%（6/6，limitations str→list 矯正 + tool_choice=required 生效）** |
| 向量快取命中 | 永久 miss（22.6s 重嵌每次） | 命中 <1s | **✅ 命中（pkl 482MB→2.7MB，原子寫入）** |
| SEMANTIC FALLBACK | 2/3 次 | 0 | **0** |
| C_FAILURE | 50% | <20% | **0%** |
| 紅旗 5 條回歸 | 5/5 | 5/5 | **5/5** |
| pytest | 193 passed | 193+ | **205 passed** |
| 百連發隊尾等待 | 665s→timeout | <120s | 設計修復（非阻塞+背景重試），未壓測 |
| 裸 LLM 對照基準 | 3.08s | — | — |

## 5.5 收尾過程補記（2026-08-28 晚）

P4 主體完成後驗收發現三個連鎖問題，經四輪 agent 分工排查修復：

1. **向量快取永久 miss（22.6s/次重嵌）**：embedding_model 預設值分叉（intfloat vs ollama/bge-m3）+ 非原子寫入留下損壞 pkl。修：固化預設、mtime_ns、損壞自清、store_dict 原子寫入（482MB→2.7MB）。報告：`docs/reviews/p4_vector_cache_investigation_20260828.md`
2. **SEMANTIC FALLBACK**：截斷 300 字攔腰砍句致 verifier 重疊不足。修：句邊界智慧截斷 + verifier 0.85→0.78。報告：同上
3. **C_FAILURE ~50%**：mimo 偶發 limitations 回字串而非 list（ValidationError）。A/B 實測證實思考開啟更糟（16.7% 成功率、34.9s），維持 effort=none。修：field_validator 自動矯正 + prompt 提示強化 + tool_choice=required + parsing_error 透明化 + 網路類錯誤單次重試。報告：`docs/reviews/p4_thinking_mode_ab_test_20260828.md`、`docs/reviews/p4_c_failure_diagnosis_20260828.md`

對抗審查（`docs/reviews/p4_adversarial_review_20260828.md`）曾判 FAIL（P0 指控 greedy regex 破壞紅旗），經 Sisyphus 用舊版 pattern 對照實測駁回因果（該洞為 pre-existing，builder 改動反而新增攔截），但其抓出的 LRU 污染、double-push、dead code 均屬實並已修。

## 6. 不變式（任何改動不可違反）

- B/D gates 不可繞過；D 門 8 步與 HeuristicSemanticVerifier 照跑
- 紅旗 deterministic pre-check 仍在 LLM 之前、同步攔截
- 不存 raw image、hash PII、FHIR unknown、8 欄位 intake 結構不變
- 無 hardcode 模型，一律讀 `.env`

## 7. 計畫外改動說明（P4 審查後追加）

- `tfda_context_gate/clinical_safety/risk_policy.py` CHEST_PAIN 擴充：原 `胸痛|胸悶|胸口.*悶|悶悶` 補 `胸口.*痛`（使「胸口痛」直接句正確 RED_FLAG），並於 FIX-4 將 greedy `胸口.*悶|胸口.*痛` 改為有界 `胸口.{0,3}?悶|胸口.{0,3}?痛`。
- 理由：greedy `.*` 會從「胸口」一路吞到句尾「悶」，使「沒有胸口痛但胸悶」這類否定+無標點轉折被整段包進否定前綴匹配，誤判為 NO_DEFINED_SIGNAL 漏報。有界 `{0,3}?`（非貪婪、最多 3 字）限制「胸口」與「悶/痛」距離，保留「胸口悶悶的」「胸口好痛」等口語，同時使轉折後肯定能被獨立檢出。回歸案例「沒有胸口痛但胸悶」「沒有胸痛，但會胸悶」「胸口悶悶的」「胸口好痛」「以前胸口痛現在不會了」皆 RED_FLAG。
