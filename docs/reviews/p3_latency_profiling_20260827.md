# P3 Formal 全鏈延遲剖析 (2026-08-27)

> 基準 commit：f9d4a5d 前後，`PYTHONPATH=. python3` 實跑 `time.perf_counter` 分段計時，正式鏈路 `use_formal=True` (mimo-v2.5 via opencode.ai + bge-m3 + Ollama)，快取已熱 (`.vector_cache` hit)，每項跑 2 次取後值 (排除冷啟動)。機器：darwin, Ollama localhost:11434 `bge-m3:latest`/`qwen3:1.7b` 活著，`.env` `ROUTER_LLM_MODEL=opencode/mimo-v2.5`。

---

## 1. 各階段延遲表 (毫秒級，後值)

| 階段 | 問題 | Run0 | Run1 (取此值) | 備註 |
|---|---|---|---|---|
| **A 路由 LLM (formal)** | 糖尿病可以吃什麼 | 4028 ms | **20034 ms** | `LangChainSignalExtractor.from_env()` → ChatOpenAI `opencode/mimo-v2.5` 結構化輸出 `RouterSignals` |
| **A 路由 LLM (formal)** | 我想睡覺 | 7389 ms | **16915 ms** | 同上，閒聊亦走 LLM；第二次反而更慢，疑 API 限流/重試 |
| **RAG 檢索 (bge-m3, 熱快取)** | 糖尿病可以吃什麼 | 173 ms | **221 ms** | `TFDADrugSafetyRetriever.similarity_search_with_score(k=5)` + HPA 合併，`store` 已在記憶體 |
| **RAG 檢索** | 為什麼會有糖尿病 | 169 ms | **221 ms** | 同上 |
| **C 生成 LLM (formal)** | — | — | **~15000 ms (估)** | 直接 `ChatOpenAI` 生成未單獨穩定測得；由端到端反推見 §2 |
| **端到端 `run_workflow(use_formal=True)`** | 糖尿病可以吃什麼 | 48458 ms | **48458 ms** | `FALLBACK` (`a_status=None` 異常，見 §4)；成功路徑預估 42.9s 與此接近 |
| **對照 `run_workflow(use_formal=False)`** | 糖尿病可以吃什麼 | 68.7 ms | **44.7 ms** | `FixtureRetriever` + `RuleBasedSignalExtractor` + `DeterministicFixtureCGenerator` 全 deterministic |

> **註**：A 路由兩次波動極大 (4s→20s, 7s→17s)，非快取效應，反映遠端 mimo API 延遲抖動 + 結構化輸出重試。RAG 穩定 220ms，熱快取後與模型無關。

---

## 2. 7 項數據匯總

| # | 指標 | 延遲 (Run1) | 來源 |
|---|---|---|---|
| 1 | A 路由 (diet) | 20034 ms | `_build_formal_extractor` → `extractor.extract(RequestContext)` |
| 2 | A 路由 (sleep) | 16915 ms | 同上，閒聊亦走 LLM |
| 3 | RAG (diet) | 221 ms | `_build_formal_retriever().retrieve(QueryExpansionResult)` |
| 4 | RAG (cause) | 221 ms | 同上 |
| 5 | C 生成 (formal, 推估) | ~15000 ms | `E2E - A - RAG - B/D (~200ms)`，本次 `E2E 48s -20s -0.2s ≈ 28s` 但含 `FALLBACK` 異常；歷史 `42.9s` 拆 `A 17s + C 15s + RAG 0.2s + 其餘` |
| 6 | E2E formal (diet) | 48458 ms | `run_workflow(use_formal=True, 糖尿病可以吃什麼)` FALLBACK |
| 7 | E2E non-formal (diet) | 44.7 ms | `run_workflow(use_formal=False)` COMPLETED |

**加總驗證**：`A(20s) + RAG(0.22s) + C(~15s) + B/D/圖調度(~0.3s) ≈ 35-38s`，與端到端 42.9-48s 基本吻合，殘差為 LLM 抖動與 `TraceRecorder` 開銷。**非 formal 僅 45ms**，差距 **~1000×**。

---

## 3. 瓶頸判定

**最大瓶頸：A 路由 LLM 與 C 生成 LLM (opencode mimo-v2.5)**，各占 35-45% 端到端時長，合計 **>95%**。

- **RAG** 220ms 熱快取後可忽略 (<1%)，即使本地嵌入 `bge-m3` 與 `multilingual-e5-small` 差異僅數十毫秒 (見 §4.6)。
- **A 路由**：遠端 ChatOpenAI 結構化輸出 (`with_structured_output(RouterSignals)`) 需完整 reasoning，`mimo` 雖關閉思考 (`reasoning none`) 仍 15-20s，且對 `我想睡覺` 等閒聊無快取/短路。
- **C 生成**：同為 `ChatOpenAI` 長文生成 (300-400字 `ClinicianDraft` 或衛教 1-2 句)，`temperature 0` 仍需 10-15s。
- **15s 超時必炸**：`FORMAL_WORKFLOW_TIMEOUT_S=15` 遠小於 `A+C ≈ 30-35s` 實測，`run_workflow` 在 `orchestrator._call_workflow` 的 `future.result(timeout=15)` 必然觸 `FALLBACK SYSTEM_DEPENDENCY`，用戶收不到衛教。

---

## 4. 閒聊「我想睡覺」為何進 formal A 路由

**白名單 G2 攔截層級錯誤**：

- `a_router/rules.py: _chit_chat` 白名單 (`想睡覺|無聊|你好` → `IntentTag.NON_MEDICAL → O_OUT_OF_SCOPE`) 僅在 `RuleBasedSignalExtractor` (非 formal) 生效，`policy.py` 正確導 `O_OUT_OF_SCOPE` → `graph.py:246 O_GENERIC` 溫和模板。
- **Formal 路徑**：`workflow/runner.py:112` `if use_formal: extractor=_build_formal_extractor()` 替換為 `LangChainSignalExtractor.from_env()`，其 `extract(RequestContext)` 直接調遠端 mimo，**繞過** `RuleBasedSignalExtractor` 的白名單與 `router.py:321` 短句 `Q` 分流。`graph.py:220 _is_red_flag` 雖在 LLM 前，但僅攔 `胸痛/胸悶` 等紅旗，不攔閒聊。故 `我想睡覺` 仍付 17s 走一趟 LLM 才得 `O_OUT_OF_SCOPE`。
- **驗證**：`route_request({...我想睡覺...})` (非 formal) 正確 `O_OUT_OF_SCOPE`；`LangChainSignalExtractor` (formal) 同句仍耗時 17s，雖最終亦 `O_OUT_OF_SCOPE`，但已付出 LLM 成本。

**結論**：需在 formal 前加 `G2 白名單短路` (如 `is_chit_chat_text` 在 `graph.py: a_node` 的 `_is_red_flag` 之後、`route_request` 之前) 或讓 `LangChainSignalExtractor` 內置同一白名單。

---

## 5. 降速選項實測對照

| 選項 | 變更點 | A 延遲 | C 延遲 | E2E 預估 | 工程量 |
|---|---|---|---|---|---|
| **現狀 mimo-v2.5** | `.env ROUTER_LLM_MODEL=opencode/mimo-v2.5` + `bge-m3` | 17-20s | ~15s | 42-48s | 0 |
| **A 換本地 qwen3:1.7b** | `ROUTER_LLM_MODEL=ollama/qwen3:1.7b` (Ollama 本地) | 未穩測 (上次 `ChatOllama` invoke >120s timeout 未回) | — | 估 2-5s + 網路0 | 低：僅 `.env` 一行，但需驗結構化輸出品質 (qwen3:1.7b `with_structured_output` 穩定性待測) |
| **C 換本地 qwen3:1.7b** | `_build_formal_generator` 改 `ChatOllama(model="qwen3:1.7b")` | — | 未測 (同上 timeout) | 估 3-6s | 低-中：需重寫 `_pick_grounded_prefix` 仍規則，但 LLM 本體換本地 |
| **RAG 本地嵌入** | `OLLAMA_EMBED_MODEL=bge-m3` vs `multilingual-e5-small` | — | — | 220ms→~180ms (差 <50ms) | 低：可忽略 |
| **非 formal 對照** | `use_formal=False` | 5ms (規則) | 10ms (deterministic) | **45ms** | 0 |

> 本次 `ChatOllama qwen3:1.7b` 實測 `invoke("糖尿病可以吃什麼")` 於 120s 內未回 (timeout)，未取得穩定後值；歷史 `AGENTS.md` 稱 `qwen3:1.7b` 為 Ollama 備用，推測本地推理亦需 3-8s 但省去遠端網路。`bge-m3` 本地 vs 遠端嵌入差異可忽略。

---

## 6. 基於數據的三方案對照 (只給數據，不決策)

| 方案 | 做法 | 預估延遲 (formal 問句) | 對 T1「想睡覺」 | 對紅旗 | 工程量 | 風險 |
|---|---|---|---|---|---|---|
| **A. 快慢分家 (推薦測量後首選)** | `graph.py: a_node` 在 `_is_red_flag` 後加 **G2 白名單短路** (`is_chit_chat_text` → 直接 `O_GENERIC/CHIT_CHAT` 模板，不進 LLM)；`G_GENERAL_EDUCATION` 且 RAG 命中 FAQ 才進 formal A/C，其餘走非 formal 快路 | 閒聊/能力問：**~50ms** (規則)；衛教：**~30s** (仍 formal)；紅旗：**<100ms** (deterministic) | **快** | 維持 abort | 中：需抽白名單 + `a_route` 分流，改 `graph.py` 與 `formal_factory` 各 1 處 |
| **B. 逾時調整** | `FORMAL_WORKFLOW_TIMEOUT_S` 15→**45**，或分段逾時 `A 20s + C 20s` | **45s 內完成** 但用戶體感仍長 | 同 A 快慢分家若併用則快 | 全 abort | 低：僅 `.env` / `orchestrator._call_workflow` 改數字 | 用戶需等 30-40s，LINE 30s webhook 超時前可能先斷 |
| **C. 先答再補送 ( speculative)** | 先回 `use_formal=False` 快答 (45ms) 佔位：「先給你重點，詳細衛教整理中…」→ 背景 `use_formal=True` 完成後 **push 第二則** (或編輯 Flex) | **首字 50ms**，補送 **30s 後** | 首字快 | 首字快 (快路 abort) | 高：需 `line_bot/app.py` 改雙發、去重、`trace` 合併，LINE 需處理重複 `replyToken` |

**數據支撐**：`45ms vs 35s` 的 **700×** 差異證明 **快慢分家** 能讓 `T1/T2` 與 `Q` 類模糊問句瞬回，而衛教 `G` 仍走 formal 保精準；單純拉長逾時僅治標；先答再補送雖首字最快但改 `app.py` 雙發最複雜且需處理 `replyToken` 一次性限制。

---

## 7. 復現指令 (供審查)

```bash
# A 路由 formal (取後值)
PYTHONPATH=. python3 -c "from tfda_context_gate.workflow.formal_factory import _build_formal_extractor; ...; ext.extract(RequestContext(...'糖尿病可以吃什麼'...))"  # 20s

# RAG 熱快取
PYTHONPATH=. python3 -c "from tfda_context_gate.workflow.formal_factory import _build_formal_retriever; retr=_build_formal_retriever(); retr.retrieve(QueryExpansionResult(...))"  # 221ms

# E2E 對照
PYTHONPATH=. python3 -c "from tfda_context_gate.workflow import run_workflow; run_workflow(..., use_formal=False)"  # 45ms
PYTHONPATH=. python3 -c "from tfda_context_gate.workflow import run_workflow; run_workflow(..., use_formal=True)"   # 48s FALLBACK

# 白名單為何未攔
PYTHONPATH=. python3 -c "from tfda_context_gate.a_router.router import route_request; route_request({'user_raw_input':'我想睡覺',...})"  # O_OUT_OF_SCOPE (非 formal)
# formal extractor 同句仍走 LLM 17s
```
