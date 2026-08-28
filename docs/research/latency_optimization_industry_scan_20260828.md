# LLM Agent 鏈結構化輸出與多階段 Pipeline 延遲優化 — 業界掃描 2026-08-28

> **Scope**: 針對本專案 formal 鏈 `A 路由（LLM 分類）→ RAG（bge-m3）→ C 生成（structured output 4 段文 300–400 字）→ D gate（8 步）` 端到端 17–40s vs 裸 LLM 3s 的差距，掃描業界做法。每個做法均在「醫療安全 B/D gate 不可繞過」前提下評估。
> **方法**: websearch 英文為主（2024–2026 業界文獻、benchmark、生產架構）+ 本地 codebase 抽樣（`a_router/router.py`, `c_generator/langchain_adapter.py`, `workflow/runner.py|stream.py|formal_factory.py`, `d_output_gate/verifier.py`）。

---

## TL;DR 與整體診斷

**我們的延遲帳本（實測拆解）**：

| 階段 | 實作現狀 | 估計耗時 | 可優化性 |
|------|---------|---------|---------|
| A 路由 | `LangChainSignalExtractor` → `with_structured_output(RouterSignals, include_raw=True)` + fallback 鏈 3 種 method 串行重試 | 4–8s（小模型也慢，因 function_calling + include_raw + 二次包裝） | ★★★★★ |
| RAG | Ollama `bge-m3` + `.vector_cache/*.pkl`（已優化 24s→0.17s 快取命中），TFDA 129 + HPA 9 chunks | 0.2–1.5s（快取命中時可忽略） | ★★☆☆☆ |
| C 生成 | `ChatOpenAI(opencode/mimo-v2.5)` → `with_structured_output(EvidenceAwareV2Answer, method=function_calling, include_raw=True)` 非串流，輸出 300–400 字 4 段 | 6–18s（最大頭，token 生成 + 結構化約束 + 長輸出） | ★★★★★ |
| D gate | 8 步（結構→政策→證據歸屬→語意驗證 HeuristicMapping） | 0.1–0.5s | ★☆☆☆☆ |
| Graph 編排 | `stream_workflow` = `run_workflow` 先跑完再 `buffered_stream_after_d` 切塊（假串流） | 感知延遲 = 全量 17–40s | ★★★★☆ |

**一句話結論**：慢的不是模型能力，而是 **兩次 `with_structured_output(include_raw=True)` 的結構化冗餘 + fallback 鏈串行重試 + 長文本非串流 + 假串流**。裸 LLM 3s 證明模型本身很快；業界要把 17–40s 壓到 5s 內，靠的不是換更大模型，而是 **讓簡單請求不走 LLM、讓必要 LLM 走最短路徑、先讓用戶看到東西** 三件事。

---

## 1. LLM 分類/路由：避免完整 function calling

### 1.1 Rule-based Pre-router（規則前置分流）

**原理**：在任何 LLM 調用前，用正則 / 關鍵字 / 長度 / 上下文長度等 <1ms 規則把「可判定」的請求直接分流。業界通稱 Fast-Path / Bypass Pattern。

**業界數據**：
- LangGraph 官方論壇與 `SynaptoRoute` 提案：將 `create_react_agent` 每次都走 LLM 判定 `tool_calls` 的 1–3s 延遲，改為前置 ONNX 語意路由，命中時 <10ms（`fastembed` ONNX 約 3ms），整體 Agentic RAG 從 18s → 2.8s（[LangGraph Fast-Path Proposal #4321](https://github.com/langchain-ai/docs/issues/4321), [C-Sharp Corner Agentic RAG](https://www.c-sharpcorner.com/article/redesigning-agentic-rag-in-langgraph-to-slash-latency-and-cost/)）。
- vLLM Semantic Router 論文：heuristic signals（keyword, language, length, auth）<1ms，ML signals（embedding similarity, domain classification）10–120ms，demand-driven 僅計算被 decision 引用的 signal 可省 50–70%（[vLLM Semantic Router 2026-03](https://arxiv.org/html/2603.04444v3)）。

**適用場景**：本專案已在 `workflow/runner.py::_is_formal_eligible` 實作部分前置判定（`pre_visit_intake` / `is_red_flag` / `is_chit_chat` / 短句 `<4字` → 直接走 fast-path，不建 LLM）。但 A 內部的 `LangChainSignalExtractor.from_llm` 仍對「通過前置檢查」的請求走 LLM；可再擴大規則集。

**預期收益**：對 `runner.py` 已攔截的約 40–60% 流量（intake / chitchat / capability 詢問 / 紅旗），延遲從 4–8s → <50ms。等價於把整體 P50 壓低 40–60%（Tencent Cloud OpenClaw 報告同口徑）。

**風險（醫療安全 gate 不可繞過）**：規則不可替 agent 做「醫療分類」決策；只能做 **非醫療 / 明確 intake / 紅旗** 的快速分流。任何 `MEDICATION_CHANGE_REQUEST` / `PERSONALIZED_MEDICATION` 等風險旗標仍須走完整 B/D。規則誤判代價是把醫療問句誤判為 chitchat → 走錯誤流程，因此規則集必須是 **fail-open 到 general 而非 fail-closed 到具體醫療標籤**，且保留 `F_ROUTER_DEPENDENCY` fail-closed 兜底。

**對我們鏈路的建議**：
- **P0**：擴大 `RuleBasedSignalExtractor` 的 deterministic 快路覆蓋（見 `a_router/rules.py`），把「可枚舉的非醫療意圖」吃掉，不進 A 的 LLM。需補測試集防止誤殺醫療句。
- **與現有設計對齊**：已有的 `_is_formal_eligible` 已是正確方向，建議把規則前置從 runner 的 formal 判定上提到 `a_router` 入口，讓非 formal 路徑也受益。

---

### 1.2 小模型分流 / Model Cascading（量級分流）

**原理**：用小/快模型做第一棒分類，信心高就直接用，信心低再升級到強模型。與「每次都走強模型做分類」對比。

**業界數據**：
- `RouteLLM`（LMSYS/Berkeley）：用矩陣分解 / BERT / causal LLM 做路由器，在 MT-Bench 上用強弱模型對（GPT-4 vs Mixtral）省 50–85% 成本並維持 95% GPT-4 品質；路由器本身延遲 10–30ms（BERT 規模，Kalvium/RouteLLM 文件）。小模型分類單次約 $0.00005。
- 生產回報（LangGraph adaptive agent 2026）：Haiku 分類 + 單次升級上限，分類與 guard 節點合計 <0.5s，整體 cost 降 28%（Haiku→Sonnet 單次 escalation）。
- 反例警示：Cascade 若設計為「先跑便宜模型→評分→再跑貴模型」，對 hard case 會 **翻倍延遲**（cheap + judge + strong），在 <300ms 實時聊天場景不可用（tianpan 2025-10 評估）。

**適用場景**：A 路由本質是小分類任務（輸出 `RouterSignals` 僅含 `intent_tags`, `risk_flags`, `context_modifiers`），適合小模型。本地 `qwen3:1.7b` / `mimo-v2.5` 已有 `reasoning: none` 配置，但仍走 function_calling 結構化，沒發揮小模型快路優勢。

**預期收益**：若能讓 simple/medium 請求由 `qwen3:1.7b` 本地或 `mimo` 快速判斷（<500ms）而非遠端強模型 4–8s，A 階段 P50 可從 4–8s → 0.5–1.5s。但若 fallback 鏈設計不良（先小後大串行），hard case 反而更慢。

**風險**：小模型對 `MEDICATION_CHANGE_REQUEST` 等醫療細粒度標籤容易漏檢；需搭配政策閘的確定性規則兜底。建議小模型只負責「是否為 general education」的二分，而非全量 `risk_flags` 細粒度預測。

**對我們鏈路的建議**：
- **P1**：評估 `qwen3:1.7b` 本地直連（Ollama）做 A 的 fast tier，而非經 `opencode/mimo` 雲端。本地 1.7B 分類可在 <300ms 完成，且不受 API 限流影響。需做離線評測（accuracy vs mimo-v2.5 比對）。
- 避免 cascade 的「先跑一次小模型再判定要不要跑大模型」的雙重延遲，改用 **單一小模型 + confidence 門檻直接走 B**，fail 則走 `F_ROUTER_DEPENDENCY` 降級，不二次 LLM。

---

### 1.3 Semantic Router（aurelio-labs / vLLM Semantic Router）

**原理**：把 query embed 後與預定義的 intent exemplars 做 cosine similarity，路由到對應處理分支。無需 LLM 生成，僅一次 embedding。

**業界數據**：
- `aurelio-labs/semantic-router`：Python 庫，適合輕量場景，延遲約 20–50ms（含 embedding）。
- `vLLM Semantic Router`（Iris v0.1, 2026-01）：生產級，支援 13 種 signal、布林決策引擎、policy DSL，demand-driven 評估 + 嵌入快取，單次路由 <5ms（embedding 快取命中時）。

**適用場景**：當非醫療意圖可枚舉（chitchat / capability / climate / image 任務等）且表述多變（paraphrase）時，semantic router 比 rule-based 更魯棒，且比 LLM 分類快 10–100 倍。

**預期收益**：對可枚舉的 8–12 類非醫療意圖，路由延遲 20–50ms → 取代 4–8s 的 LLM 分類。

**風險**：embedding 模型需本地化（如 `bge-m3` 已在 RAG 使用，可復用），避免額外 API 調用。閾值（threshold）設定敏感，過低會誤路由醫療問句到非醫療分支。需以持留集（holdout）標定 threshold，類似 RouteLLM 的 threshold calibration 流程（`router-mf-0.1159` 這類標定）。

**對我們鏈路的建議**：
- **P1**：用 `bge-m3`（已部署於 Ollama）實作 Semantic Router 作為 Rule-based 與 LLM 之間的中間層：Rule 精確命中 → 直接分流；Semantic 高信心命中 → 分流；其餘再走 LLM。復用既有 embedding 基礎設施，零新增依賴。

---

### 1.4 LangGraph Fast-Path（Command + SynaptoRoute）

**原理**：在 LangGraph 節點前插入一個輕量路由節點，若高信心命中確定性 intent（例：`get_weather`, `check_calendar`），直接合成 `AIMessage(tool_calls=...)` 並 `Command(goto="tools")`，完全繞過 LLM 節點；低信心則回落到正常 agent 節點。

**業界數據**：SynaptoRoute（ONNX `fastembed`）約 3ms，整體 <10ms 完成工具路由；LangGraph 官方回應建議把 abstention（放棄判定）作為一等公民，低 margin 必須回落，且 trace 需可區分 router-generated vs model-generated tool_calls。

**適用場景**：本專案 fixed workflow A→B→C→D，非 planner `CALL_TOOL` 迴圈，但思想可借用：在 A 之前插入 `DeterministicFastPathNode`，若命中則直接設定 `RouteDecision` 而不調 LLM。

**預期收益**：與 Rule-based / Semantic Router 同級，對命中流量 <10ms。

**風險**：同 1.1/1.3，需保證醫療分支永不被 fast-path 誤命中；建議 fast-path 僅允許路由到「非醫療 / intake」分支，醫療分支永不由 fast-path 產生，需由 LLM 或 heuristic + policy 共同決定。

**綜合優先序（第 1 章）**：

| 做法 | 優先序 | 理由 |
|------|--------|------|
| Rule-based pre-router 擴大 | **P0** | 零成本、已局部實作、可立即擴大，無新增依賴，風險可控（僅分流非醫療） |
| Semantic Router（復用 bge-m3） | **P1** | 補 rule 的 paraphrase 盲區，延遲 <50ms，但需標定 threshold |
| 小模型本地分流 | **P1** | 若 Ollama 本地可用，收益大；否則雲端小模型仍受網路/限流影響 |
| LangGraph fast-path 節點化 | **P2** | 架構優雅但與 semantic router 重疊，先做 semantic 再考慮節點化 |

---

## 2. Structured Output 延遲最佳化

本章是 formal 鏈最大的延遲池。裸 LLM 3s → formal 17–40s 的大部分來自 **結構化約束 + include_raw + retry + 長輸出** 四重疊加。

### 2.1 約束解碼（Constrained Decoding）：Outlines / Guidance / XGrammar / llguidance / vLLM

**原理**：將 JSON Schema 編譯為 grammar（FSM / CFG / PDA），在每次採樣時對下一個 token 做 mask（非法 token 機率置零），使輸出「按構造」符合 schema，無需重試。

**業界 benchmark（關鍵數據）**：
- **JSONSchemaBench 10K schemas**（Guidance AI）：Guidance 在效能上優於 Llamacpp 與 Outlines；Outlines 對含 `minItems`/`maxItems`/`enum`/`Array` 的 schema 編譯可達 40s–10min 超時；Llamacpp/Guidance 編譯近零（[Generating Structured Outputs 2501.10868](https://arxiv.org/html/2501.10868v1)）。
- **SqueezeBits vLLM/SGLang 對比**（2025-09）：XGrammar 在 **重複 schema** 場景（Book-Info）吞吐最高、TPOT 最低；LLGuidance 在 **動態 schema** 場景（Github_easy/medium 每請求不同 schema）勝出，因無需預計算快取。vLLM 在 batch≥8 時因 sequential mask 無 overlap 而明顯掉吞吐，SGLang 因 overlap mask 與 GPU 推理而更平滑（[Guided Decoding Perf Blog](https://blog.squeezebits.com/guided-decoding-performance-vllm-sglang)）。
- **開銷**：多數場景 constrained decoding 僅 +1–3% 延遲，複雜 schema（20 fields/nested/large enum）可達 +10%；XGrammar ~+1%，llguidance ~+2%，Outlines ~+3%（[Chaos and Order guide](https://www.youngju.dev/blog/llm/2026-03-07-llm-structured-output-constrained-decoding-json-schema.en)）。有研究稱 constrained 可 **提速 50%**（因減少重試與無效 token）。
- **OpenAI Structured Outputs**（2024-08-06）：複雜 JSON schema 遵循率從 <40%（無約束）→ 100%（有約束），首個新 schema 需 5–60s 編譯並快取（[OpenAI 官方](https://openai.com/index/introducing-structured-outputs-in-the-api/)）。

**適用場景**：
- 本專案當前 **未自託管 vLLM**，而是走 `opencode/mimo-v2.5` 雲 API（ChatOpenAI），其內部已使用類似 constrained 的機制（OpenAI 称之為 Structured Outputs）。若未來遷到自託管（如 vLLM + Qwen），XGrammar/llguidance 可作為本地 structured output 的可選後端。
- 當前最相關的是：**不要在客戶端用正則/FSM 做客戶端約束**，而是依賴雲 API 的原生 structured。

**預期收益**：若自託管，XGrammar 可將結構化輸出的重試率從 2–15% → <0.3%，等價於省去 1–3 次重試（每次 1–3s）。在雲 API 場景，收益體現為 **選對 method**（見 2.2）。

**風險**：自託管需承擔模型運維、顯存、編譯快取。醫療場景下，constrained 保證「語法/shape」而非「語意正確」—— 仍需 Pydantic + 業務規則驗證。

---

### 2.2 直出 JSON vs Function Calling vs json_schema（strict）

**原理差異**：
- `json_mode`（`response_format: {type:"json_object"}`）：僅保證「語法上是 JSON」，不保證符合你的 schema。黑盒測試：schema violation 9–12%（GPT-4o 9.3%, Claude 11.7%），需客戶端驗證 + 重試。
- `function_calling` / `tool_use`：定義 JSON Schema 並強制模型填充，violation 0.3–2.1%（GPT-4o 1.2% → strict 0.2%），但需傳遞 tool 定義並觸發 function-calling 路徑。
- `json_schema` strict（`strict:true`）：最嚴格，語意等價於 constrained decoding，violation 0.2–0.3%。

**延遲對比（生產實測 30 天）**（[Kalvium 2026-04](https://www.kalviumlabs.ai/blog/structured-output-from-llms-json-mode-function-calling/)）：

| Method | Schema 欄位數 | 平均額外延遲 | p95 |
|--------|-------------|-------------|-----|
| json_mode | - | ~0ms | ~10ms |
| function_calling | 5–10 | +80ms | +150ms |
| function_calling | 11–20 | +120ms | +220ms |
| function_calling | 21+ | +180ms | +350ms |
| strict json_schema | 5–10 | +90ms | +160ms |

另有文獻稱 function calling 新增 15–20ms（[NeuralBase](https://theneuralbase.com/compare/openai-function-calling-vs-json-mode/)）或 80–150ms，取決於模型與 schema 複雜度。本專案 `RouterSignals` 與 `EvidenceAwareV2Answer` 屬於 5–15 欄位，符合 +80–120ms 區間——**結構化本身只貴 100ms 左右**，17–40s 的大頭不是這個，而是 **include_raw + retry + 長輸出**。

**對我們鏈路的建議**：
- **P0**：區分「語法保證」與「schema 保證」的需求。A 的 `RouterSignals` 是小 schema（3 欄位 + enums），可用 `json_mode` + 客戶端 Pydantic 驗證 + 單次 error-feedback retry 替代 `function_calling`，省 80ms 且避免 tool calling 的額外 roundtrip。若需最嚴格，改用 `json_schema(strict:true)` 而非 `function_calling`（LangChain `with_structured_output` 在 mimo 這類模型上 `function_calling` 反而更慢，見 `a_router/router.py:from_llm` 的 `is_small` 分支）。
- **P1**：C 的 `EvidenceAwareV2Answer` / `ClinicianEvidenceDraft`（4 段文 + citations）屬於 10+ 欄位長輸出，建議 **維持 `function_calling` 或 `json_schema(strict)`**，因為 violation 代價（重試 1–3s）遠高於 100ms 結構化開銷。勿為省 100ms 改為 json_mode 而引入 2–15% 重試率。

---

### 2.3 `include_raw=True` 的雙往返問題

**現狀審計**：
- `a_router/router.py:LangChainSignalExtractor` 建 chain 時 `include_raw=True`，且 `_candidates` 準備 3 條 fallback chain，`extract` 內串行試每一條，每次 `chain.invoke(messages)` 若 `parsing_error != None` 則試下一條。
- `c_generator/formal_factory.py:_build_formal_generator` 同樣 `include_raw=True`。
- LangChain 的 `with_structured_output(include_raw=True)` 語意是「回 `{raw, parsed, parsing_error}`」而非「多一次 API 調用」；但實務上有兩層額外成本：
  1. **客戶端解析與驗證開銷**：需做 Pydantic 解析並返回 `parsing_error`，且 GitHub issue [#32977](https://github.com/langchain-ai/langchain/issues/32977) 回報在 parsing 失敗後後續重試極慢（數十秒），疑似 backoff/狀態卡住。
  2. **抽象洩漏風險**：`include_raw` 若被誤透傳到 OpenAI payload（`TypeError: Completions.parse() got unexpected keyword argument 'include_raw'`，[#35041](https://github.com/langchain-ai/langchain/issues/35041)）會直接報錯。本專案未踩此坑（因走 `with_structured_output` 正確包裝），但仍需警惕版本升級回歸。
  3. **語義上的「雙往返」**：`include_raw=True` 並非多一次 HTTP，但在 **retry 語義上等價於雙往返**—— 成功路徑 1 次 LLM 調用，失敗路徑需帶 `parsing_error` 再做 error-feedback retry（見 4.1），等價於 2× LLM 調用。

**適用場景**：`include_raw=True` 的價值在於「解析失敗時仍能拿到 raw 以便修復提示」，對醫療場景有價值（可落盤審計）。但若 schema 穩定且 violation 率 <2%，可改為 `include_raw=False` + 外層 `try/except OutputParserException` 並僅在失敗時重試一次（見 4.1）。

**預期收益**：對成功率 98% 的請求，`include_raw=True` 的常態開銷很小（僅一次多返回欄位），但對 2% 失敗請求，若走「`include_raw` 攜帶 `parsing_error` → 串行 fallback 3 條 chain」的路徑，延遲會從 1× → 3×（串行 3 次 LLM 調用）。改為 **單一 method + 單次 error-feedback retry** 可把失敗路徑從 3× 壓到 2×。

**風險**：關閉 `include_raw` 後，失敗時 raw 遺失不利於審計。建議保留 trace 側的 raw 落盤（`e_observability` 已有），而非依賴 `include_raw` 的返回結構。

**對我們鏈路的建議**：
- **P0**：將 `a_router/router.py:_candidates` 的 3 條串行 fallback 改為 **單一主 method + 單次 error-feedback retry**（見 4.1），並評估 `include_raw=False` 作為預設，僅在 debug/trace 模式開啟 `include_raw`。
- **P1**：C 生成側同理，保留 `include_raw=True` 僅用於落盤審計，但不以其作為 retry 觸發器；retry 應基於 Pydantic 驗證失敗而非 `parsing_error` 是否為 None。

---

## 3. 長文生成延遲：Streaming、摘要先行、Token 預算

### 3.1 Streaming 首字優先（TTFT 導向）

**原理**：TTFT（time to first token）是用戶感知的唯一指標。串流（SSE / chunked）讓用戶在 0.5–1s 內看到首字，即使全量需 6–18s。非串流則必須等全量完成才推播，感知 = 全量。

**現狀審計**：
- `workflow/stream.py:buffered_stream_after_d` 名為 streaming，實為 **buffered-then-stream**：先 `run_workflow` 跑完整鏈（含 C 的 4 段長文）並通過 D 後，才按 `chunk_size=20` 切塊產出。`line_bot/app.py` 的 `/callback` 在 `use_formal=True` 時全程等待，無 `show_loading_animation` / `reply_message` 快發。
- `c_generator/langchain_adapter.py:LangChainCV2Generator.stream` 雖有 `llm.stream` 分支，但 `formal_factory.py` 建的 chain 仍走 `chain.invoke`（非 `stream`），且 `workflow/runner.py:stream_workflow` 僅在 `run_workflow` 之後才 `buffered_stream_after_d`，未真正利用 token 級串流。

**業界數據**：
- TTFT 定義與預算：interactive chat 目標 TTFT 500ms–1.5s p95，總回應 2–4s p95；tail ratio p99/p50 <3× 為健康（[Data AI Hub Latency Optimization](https://www.dataaihub.co/learn/latency-optimization)、[Spheron SLO Guide](https://www.spheron.network/blog/llm-inference-slo-ttft-itl-latency-budget-guide-2026/)）。
- 串流可將感知等待從 3s 壓到 ~500ms（Data AI Hub）。
- AWS AgentPerf 建議：instrument **pipeline TTFT** vs **model TTFT** 兩條指標，串流需覆蓋「tool 調用間的靜默期」以 buffer-and-resume + progress indicator（[AWS AGENTPERF02-BP04](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentperf02-bp04.html)）。
- 多 agent pipeline 串行時，TTFT = 各 upstream 全量之和；漸進式串流（upstream 中間輸出即時流入 downstream）可大幅削減（AWS 同文）。

**適用場景**：C 的 4 段文（300–400 字，約 450–600 tokens）若全量生成後才推，TTFT = C 全量 6–18s；若 token 級串流，TTFT 可 <1s。

**預期收益**：對 C 長文，TTFT 從 6–18s → 0.5–1.5s（感知），全量仍 6–18s 但用戶已在閱讀。對整體 formal 鏈，感知 P50 可從 17–40s → 5–8s（含 A+RAG 仍串行）。

**風險**：醫療衛教內容若邊生成邊推，可能 **先推了未經 D 驗證的片段**，違反安全 gate。本專案 `buffered_stream_after_d` 的「D 通過才推」是正確的安全不變式，不能為 TTFT 放棄。解法是「先 ack 後補送」而非「邊生成邊推未驗證內容」（見第 5 章）。

**對我們鏈路的建議**：
- **P0**：保留「D 通過才推」的正確語義，但將 `show_loading_animation` 的 ack 在 **1s 內先發**（見第 5 章），讓用戶感知不為 17–40s。
- **P1**：讓 C 的 `LangChainCV2Generator` 真正走 `llm.stream` + `partial-json-parser`/`jiter` 增量解析（OpenAI SDK 原生支援 streaming structured outputs 的增量 `parsed` 快照），在 D 通過後 **回放** 已生成的 tokens（類似 speculative streaming），而非重新生成後再切塊。

---

### 3.2 摘要先行 / 全文後補（Progressive Disclosure）

**原理**：先生成 1–2 句「可用摘要」（如 50–80 字）並立即推播，再生成全文 4 段（300–400 字）作為後續補送。用戶在 1–2s 內獲得可用資訊，全文在 5–10s 內補齊。

**業界數據**：Agentic AI 支援平台架構（5k RPS 250ms 目標）中，將「摘要/FAQ」與「全文/推理」分層，FAQ 快路 <80ms vector + <100ms LLM，推理慢路再補（[Amitesh Surwar 2026-08](https://amiteshsurwar.com/blog/agentic-ai-customer-support-platform-architecture-a-productionready-design-walkthrough-20260821)）。Data AI Hub 建議對長回應設 `max_tokens` 分段或截斷策略。

**適用場景**：衛教問答中，用戶常只需「結論+1 條可執行建議」即可離開；全文 4 段是合規要求而非用戶等待的必要條件。可將 C 的 prompt 拆為 `summary`（≤80 字）+ `body`（4 段），或在 `EvidenceAwareV2Answer` schema 中增加 `summary` 欄位，先推 summary，body 後補。

**預期收益**：TTFT 從 6–18s → 1–2s（summary 先達），全文仍 6–18s 但用戶已獲價值。

**風險**：摘要必須同樣通過 D gate（主張→證據支撐），否則先推的摘要若被 D 否決，需撤回/更正，損害信任。建議摘要與全文 **同一次 C 調用中共同生成**（schema 內 `summary` + `sections`），D 對二者分別驗證，summary 先推（若 D 通過），body 再推。

**建議**：**P1**，待 streaming 基礎完成後再引入 progressive disclosure。

---

### 3.3 Output Token 預算控制

**原理**：`max_output_tokens` / `max_tokens` 直接決定 decode 階段時長與成本。對 300–400 字（約 500 tokens）若未設上限，模型可能生成 800–1200 tokens（含重複/冗餘），延遲與費用同增。

**業界數據**：Tencent Cloud 建議分層模型與 `max_tokens` 分級：FAQ 200–500ms / troubleshooting 500–1200ms / complex 1–3s。 Prefill 與 decode 的 TTFT/TPOT 權衡在 vLLM 中以 `max_tokens` + `chunked prefill` 控制。

**現狀**：`formal_factory.py` 未顯式設定 `max_tokens`，依賴模型預設；`a_router` 的 `RouterSignals` 極短但仍走同樣長輸出配置。

**建議**：**P0** — 為 A 與 C 分別設定 `max_tokens` 預算（A: 128–256 tokens 足夠，C: 512–700 tokens 對應 300–400 字）。可省 20–40% 的尾部 token 生成時間，且避免 `finish_reason: length` 截斷（見 4.1 截斷 vs 解析失敗的區分）。

---

## 4. Retry 最佳化：上限、Fail-Fast、Degraded Path

### 4.1 解析重試（Parse Retry）上限

**現狀審計**：
- `a_router/router.py:extract` 的 fallback 是 **3 條 chain 串行**（`function_calling` → `json_schema strict:false` → `json_schema strict:true`），每條失敗都走 `parsing_error` 判定，最多 3 次 LLM 調用。
- LangChain 生態的典型陷阱是 `RetryWithErrorOutputParser` + `.with_retry()` 雙重重試疊加，可達 3.7× 調用量（RunGuard 2026-06）：`with_retry(stop_after_attempt=3)` 單獨 1.9×，`RetryWithErrorOutputParser(max_retries=2)` 單獨 1.6×，二者疊加 3.7×。
- 本專案目前未使用 `RetryWithErrorOutputParser`，但等價於手寫的 3 連擊。

**業界數據**：
- 2% 失敗率 × 平均 3 次重試 = 1.06× 全 fleet 成本底線；考慮 retry prompt 更長（1.5–2× input tokens）與更長輸出，實際 12–18% 算力被失敗路徑消耗（[Structured Output Retry Loop](https://tianpan.co/blog/2026-04-28-structured-output-retry-loop-hidden-compute-waste)）。
- 經驗值：attempt 1 成功 87.4%，attempt 2 補 9.1%，attempt 3 補 2.8%，剩 0.7% 徹底失敗，三次以上皆為燒 token（[Bulletproofing Structured Output](https://dev.to/velsof/bulletproofing-llm-structured-output-in-python-healing-retries-cost-caps-and-drift-detection-c89)）。
- **Token 定額 retry 預算** 比「次數上限」更有效：「單請求最多花成功路徑 2× 的 token 預算即放棄，轉 degraded」（同上文）。

**對我們鏈路的建議（P0）**：
- 將 A 的 3 條串行 fallback **壓為單一主 method（mimo 選 `function_calling`，其他選 `json_schema strict:true`）+ 單次 error-feedback retry**，總 LLM 調用從最多 3 次 → 最多 2 次。
- 區分 **截斷（`finish_reason: length` / `stop_reason: max_tokens`）** 與 **解析失敗**：前者是預算問題（調高 `max_tokens` 重跑），後者是 schema 問題（帶 `ValidationError` 重跑）。勿把截斷當解析失敗去「修復」JSON，會發明數據（[When Structured Output Breaks 2026-07](https://dreaming.press/posts/when-structured-output-breaks-repair-recovery-playbook.html)）。
- 引入 **token 預算熔斷**：單請求重試累計 input+output token 超過成功路徑 2× 即熔斷，轉 degraded。

---

### 4.2 Fail-Fast 與 Degraded Path（降級模板）

**原理**：重試無法收斂時，fail-fast 並走確定性降級，而非無限 retry。

**業界模式**：
- **3-tier fallback 鏈**：Tier1 cheap+fast（如 Haiku/gpt-4o-mini）→ Tier2 strong（Sonnet/gpt-4o）→ Tier3 放寬 schema（optional 欄位、looser types）或預置模板（[Fallback Chains 2026-05](https://vivekwisdom.com/structured-outputs-and-fallback-chains-how-to-stop-llms-from-breaking-your-parser/)）。Tier1 命中 95%，Tier2 補大多數，Tier3 兜底長尾。
- **Circuit breaker**：對 provider 級失敗（5xx 激增）開路，30s 探測後恢復，避免對已降級的 provider 持續燒延遲（[Bulletproofing](https://dev.to/velsof/bulletproofing-llm-structured-output-in-python-healing-retries-cost-caps-and-drift-detection-c89)）。
- **Healing retry vs Blind retry**：blind retry（同 prompt 重發）幾乎必重複失敗；healing retry（帶上 `ValidationError` / `parsing_error` 的結構化修復提示）一輪修復率顯著更高（Instructor 文件）。

**現狀**：本專案 `workflow/fallbacks.py:fallback_response` 已有降級路徑（`fallback_reason` 驅動的模板），且 `graph` 的 `agent_decision` 支援 `ASK_USER/REWRITE/FALLBACK` 三選一，架構正確。但 A 的 fallback 仍為 LLM 重試而非 degraded。

**對我們鏈路的建議（P1）**：
- A 失敗 1 次 healing retry 後即走 **確定性 degraded**：`F_ROUTER_DEPENDENCY` + 通用衛教前綴 + `ASK_USER` 澄清，而非試第 2、3 條 chain。
- C 失敗走 **模板化降級**：若 `EvidenceAwareV2Answer` 解析失敗，改用 `DeterministicFixtureCGenerator` 的模板（既有）或縮小版 answer（僅 `answer` + `sources`），並標記 `fallback_reason=PARSE_FALLBACK`，保證 D 仍可 gate。
- 對 `opencode/mimo` 雲 API 加 **provider circuit breaker**（閾值 3 次連續失敗，冷卻 30s，期間切 Ollama 本地或直接 degraded），避免在 provider 降級期間放大延遲。

---

### 4.3 重試可觀測性

**建議**：在 `e_observability` 中為重試鏈增加 **attempt 級 span**（含 `method`, `parsing_error.type`, `token_usage`, `latency`），並以 **accepted-object cost**（總 retry 成本 / 成功可用對象數）評估重試策略，而非僅看 parse 成功率（[LLM Structured Outputs in Production — Towards AI](https://pub.towardsai.net/llm-structured-outputs-in-production-how-to-stop-json-from-breaking-your-ai-workflow-66703754d341)）。限流與重試需共同以 trace 呈現，否則「98% 成功」掩蓋 12–18% 算力浪費。

---

## 5. 客服/衛教 Bot 業界如何在 5 秒內回應又保持安全 Gate

本章直接回答「formal 17–40s 如何在不繞過 D 的前提下讓用戶 5s 內有回應」。

### 5.1 Ack-First（先回 200，再非同步生成）

**平台約束**：
- **LINE**：webhook 要求 1s 內回 200，否則 410 Gone；reply token 單次使用、約 30s–1min 過期，未及時回覆會靜默失敗；顯示 `show_loading_animation`（`loadingSeconds` 5–60，約束 100 req/s）告知用戶「處理中」；超時後改 `push_message`（消耗配額但必達）（[Gemini Lab 2026-03](https://gemilab.net/en/articles/gemini-api/gemini-api-line-bot-python-guide)、[LINE 官方 Sending Messages](https://developers.line.biz/en/docs/messaging-api/sending-messages/)、[LavX Webhook Best Practice 2026-01](https://news.lavx.hu/article/building-resilient-line-bots-webhook-security-performance-and-operational-best-practices)）。
- **Slack**：3s 內必須 2xx，否則重試 3 次（`x-slack-retry-num`）；**Discord**：3s 內需 `DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE`（type 5），token 15min 內可 PATCH（[DEV 2026-08](https://dev.to/instawebhook/managing-slack-and-discord-bot-webhooks-without-getting-rate-limited-2m8e)）。
- **通用結論**（Telegram/Slack/LINE/Stripe/GitHub 皆同）：webhook handler 應是 **message router** 而非 processing engine；慢操作放背景執行緒/佇列（[Ack-First Webhooks 2026-06](https://drunk.support/ten-errors-one-stuck-queue/)）。

**對我們鏈路的現狀**：`line_bot/app.py` 的 `/callback` 目前 **同步等待** `run_workflow`/`stream_workflow` 完成才回 webhook，違反 1s 約束；且 `run_workflow` 內無 `show_loading_animation`。這是 17–40s 感知延遲的直接放大器（用戶看到 LINE 的 loading 超時或重試）。

**建議（P0，最高 ROI）**：
```
webhook 收到
  → 0–50ms：驗證 X-Line-Signature，寫冪等鍵（webhookEventId），入佇列，回 200
  → 50ms–1s：show_loading_animation(loading_seconds=20)
  → 背景 worker：run_workflow(use_formal=True)（17–40s）
  → 完成：若 replyToken 仍有效 → reply_message；否則 push_message（配額代價）
```
此模式不改 formal 鏈路任何 B/D 邏輯，僅改 delivery 時序，風險最低、收益最大。需補：**冪等（dedupe by webhookEventId）** + **durable queue**（非 `setImmediate`，進程重啟不丟）+ **pending_update_count 健康檢查**。

---

### 5.2 Durable Queue + Dedupe + 後補送（Push Fallback）

**業界實作**：
- Gemini LINE Bot：`threading.Thread(target=process_event, ...).start()` + `return "OK"` 立即回 200，`process_event` 內 `try: reply_message catch: push_message`（[Gemini Lab](https://gemilab.net/en/articles/gemini-api/gemini-api-line-bot-python-guide)）。
- Slack Vercel Bot：`ctx.waitUntil(handler)` 同實例非同步；需 `200 OK within 3s`（[Slack LLM Bot](https://github.com/lebedevilya/slack-llm-bot)）。
- Telegram：`res.json({ok:true})` 立即回，`queue.enqueue(() => adapter.handleIncomingMessage)` 入 durable queue，`getWebhookInfo.pending_update_count` 監控堆積（[Ack-First](https://drunk.support/ten-errors-one-stuck-queue/)）。

**對我們鏈路的建議**：
- 在 `line_bot/app.py` 引入 **BackgroundTask / Queue**（FastAPI `BackgroundTasks` 或 Redis/BullMQ），`/callback` 僅做驗證→入列→回 200。
- 背景 worker 執行 `run_workflow`，結果經 `MessagingApi` 發送；同時寫入 `e_observability`（含 `pipeline TTFT` 與 `model TTFT` 分別計量）。
- D gate 未通過的回應仍按 `fallback_response` 推播（用戶有回應），而非靜默失敗。

---

### 5.3 階段化推播：先 Ack 文案，再補正式內容

**模式**：類似「摘要先行」的 delivery 版——1s 內先推一條 **確定性 Ack**（例：「已收到您的提問，正在依據 TFDA/HPA 资料整理回覆…」），formal 完成後再推正式 4 段文。業界稱為 **placeholder → replace**（Slack 用 `chat.update` 定點更新，LINE 用兩條 push）。

**風險與合規**：Ack 文案必須為 **非醫療斷言** 的確定性文字（不含具體用藥/劑量/診斷建議），否則等價於未經 D 就推醫療內容。Ack 僅承擔「告知進度」功能，正式醫療內容仍走 A→B→C→D 完整鏈。

**預期收益**：用戶在 1s 內即有回應，formal 17–40s 的等待被轉為「已告知處理中」，放棄率顯著下降。LINE bot 實測 `show_loading_animation(20s)` + Ack 可將體感從「沉默 17s」→「1s 有回應」。

**建議（P1）**：Ack 文案走 **模板化**（`tool_contract` / `deterministic_generators`），與 formal 鏈解耦；formal 完成後推第二條消息（或編輯第一條若平台支援）。

---

### 5.4 平台原生優化（連線複用、健康檢查、限流）

- **Persistent connections**：HTTP/2 或 WebSocket 復用，避免每條消息 50–150ms 建連（Tencent Cloud）。
- **Health check 分離**：`GET /health` 不經過重型 pipeline，避免探活流量污染 RAG/LLM 佇列。
- **Worker 限流**：按 LINE 100 req/s（loading animation）、Messaging 2000 req/s 限流，避免 429 連鎖。

---

## 6. 整合優先序（跨 5 章）與風險總表

| 優先序 | 做法 | 章節 | 預期延遲收益 | 醫療風險 | 實施成本 | 建議順序 |
|--------|------|------|-------------|---------|---------|---------|
| **P0-1** | **Ack-first webhook（200 先回 + show_loading_animation + push fallback + dedupe + durable queue）** | 5.1/5.2 | 感知 17–40s → **1s 有回應**（正式內容仍 17–40s 但用戶不感知為超時） | 無（不改 B/D） | 低（改 `line_bot/app.py`） | 第 1 步 |
| **P0-2** | **壓縮 A 的 structured retry：3 條串行 fallback → 單主 method + 單次 healing retry + token 預算熔斷** | 4.1/2.3 | A 的失敗路徑 3× → 2×；整體 P50 省 1–4s（取決於失敗率） | 低（fail-fast 後走 `F_ROUTER_DEPENDENCY` 降級，已在 policy 內） | 低（改 `a_router/router.py`）| 第 2 步 |
| **P0-3** | **擴大 rule-based pre-router 覆蓋 + 為 C/A 設 `max_tokens` 預算** | 1.1/3.3 | 對 40–60% 非醫療流量省 4–8s；長文尾部省 20–40% | 低（僅分流非醫療，醫療永不被誤分） | 低 | 第 3 步 |
| P1-1 | 語意路由（復用 bge-m3）作為 rule 與 LLM 之間的中間層 | 1.3 | 補 rule 盲區，再省 20–50ms/命中 | 中（需 threshold 校準） | 中 | 第 4 步 |
| P1-2 | 真串流（`llm.stream` + partial parser）+ buffered 回放（D 通過後） | 3.1 | TTFT 6–18s → 0.5–1.5s 感知 | 中（需保證 D 通過才推） | 中 | 第 5 步 |
| P1-3 | C 的 `summary` 欄位 + progressive disclosure（摘要先行） | 3.2 | 感知 1–2s 有可用結論 | 中（摘要同樣需 D） | 中 | 第 6 步 |
| P1-4 | 小模型本地分流（qwen3:1.7b Ollama） | 1.2 | A 4–8s → 0.5–1.5s（若本地可用） | 中（需離線評測） | 中 | 評估分支 |
| P1-5 | Retry 可觀測性 + accepted-object cost 指標 + 3-tier degraded | 4.2/4.3 | 間接（防止 0.7% 長尾放大為 12–18% 算力） | 低 | 低 | 並行 |
| P2-1 | LangGraph fast-path 節點化 | 1.4 | 與語意路由重疊，邊際收益 | 低 | 中 | 後續 |
| P2-2 | 自託管 vLLM + XGrammar/llguidance（若遷自託管） | 2.1 | 結構化重試率 <0.3%，省 1–3s | 中（運維成本） | 高 | 遠期 |

> **風險紅線重申**：任何做法均不得讓「未經 B/D 的醫療斷言」抵達用戶。Ack 文案、fast-path、semantic router 的命中目標僅限「非醫療 / intake / 明確能力詢問」；醫療分支永由 LLM + policy + D 共同決定。B/D 的語意驗證（步驟 8）雖為 heuristic，但在替換為獨立 NLI/LLM verifier 前，仍需保留且不可短路。

---

## 7. 如果只能改 3 件事

按 **ROI（感知改善 / 實施成本 / 風險）** 排序，選 3 件能把 **17–40s 感知壓到 1–5s 體感** 的改動：

### ① 讓 webhook 1 秒內有回應（Ack-first + loading + push fallback + dedupe + durable queue） —— 5.1/5.2

- **改哪裡**：`line_bot/app.py` 的 `/callback`。
- **怎麼改**：驗證 `X-Line-Signature` → 寫冪等（`webhookEventId`）→ `show_loading_animation(20)` → 入背景佇列 → 立即 `return 200`；worker 跑 `run_workflow(use_formal=True)`，完成後 `try: reply_message except: push_message`。
- **收益**：零侵入 formal 鏈，單點改 delivery，感知 17–40s → **1s 有回應**，放棄率與超時重試歸零。為後續所有優化爭取時間窗口。
- **不改什麼**：不動 A/B/C/D 任何邏輯，不繞 gate。
- **驗收**：本地 `simulate_*` + 刻意 `sleep 35` 驗證 token 過期後仍經 push 到達；`getWebhookInfo.pending_update_count` 不堆積。

### ② 把 A 的 3 連擊重試砍為 1 次 healing retry + token 熔斷（並收斂 include_raw） —— 4.1/2.3

- **改哪裡**：`tfda_context_gate/a_router/router.py` 的 `LangChainSignalExtractor.extract` + `from_llm`。
- **怎麼改**：主 method 單一（mimo 選 `function_calling`，否則 `json_schema strict:true`），`include_raw=False` 為預設（trace 側另行落 raw），失敗僅一次 healing retry（帶 `ValidationError` 修復提示），累計 token 超 2× 成功路徑即熔斷轉 `F_ROUTER_DEPENDENCY` 降級。
- **收益**：A 的失敗路徑延遲 3× → 2×；常態成功路徑省去多餘 `include_raw` 解析分支；重試算力從潛在 12–18% 壓回 <6%。
- **驗收**：以 `general-medication-information` 等固定 15 個 formal 測試集量測 A 平均耗時與重試率（`e_observability` 新增 attempt span）。

### ③ 擴大確定性前置分流 + 關緊 token 預算 —— 1.1/3.3

- **改哪裡**：`tfda_context_gate/a_router/rules.py` + `workflow/runner.py::_is_formal_eligible` + `workflow/formal_factory.py`。
- **怎麼改**：
  - 前置規則擴到：已有的 `pre_visit_intake` / `red_flag` / `chitchat` / 短句，再加 `capability/climate/image` 等非醫療意圖（複用 `RuleBasedSignalExtractor.is_chit_chat_text` 族），全量走 45ms `Rule+Fixture` 快路。
  - 為 A 設 `max_tokens=256`、C 設 `max_tokens=700`（對應 300–400 字），避免無上限生成與 `finish_reason:length` 截斷誤判為解析失敗。
- **收益**：對 40–60% 流量直接省 4–8s；長文尾部省 20–40% decode 時間；`max_tokens` 明確後，截斷與解析失敗的區分（見 4.1）不再混淆。
- **驗收**：以 `declared_role` + `user_raw_input` 灰度對比 fast-path vs formal 的分流正確率（holdout 需 >95% 不誤判醫療句）。

> **做完 3 件後的延遲預期**：被前置分流的 40–60% 流量 <1s；formal 流量感知 1s（ack）→ 5–10s（D 通過後推正式 4 段文，含 C 6–10s）；全量 P50 感知從 17–40s → **3–8s**。若再疊加 P1 的真串流與語意路由，可進一步壓到 **1–3s TTFT + 5s 內全量**。

---

## 8. 測量與落地建議

1. **補兩條 TTFT 指標**：`pipeline_TTFT`（webhook→首推抵達用戶）與 `model_TTFT`（首個 LLM token），以 OpenTelemetry + CloudWatch/本地日誌呈現 p50/p95/p99 與 tail ratio。
2. **Accepted-object cost**：`總 retry token / 成功可用對象數` 作為重試策略的北極星，避免「98% 成功」掩蓋 12–18% 算力浪費。
3. **Threshold 校準**：Semantic router / rule 的閾值需以持留集 + 生產日誌回放標定（如 RouteLLM 的 `router-mf-0.1881` 校準流程），並設 `fallback_reason` 的監控告警。
4. **安全不變式回歸**：每次優化後重跑 `tfda_context_gate/tests/test_workflow_integration.py` 15 passed 與 formal 3 場景（patient-education / pre-visit intake / clinician draft）的 `mimo-v2.5 + bge-m3` PASS 集；D 的 `BLOCKED/FALLBACK` 分支必須仍有覆蓋。
5. **文獻來源追蹤**：重試與約束解碼的 benchmark 受版本影響大，建議以 `JSONSchemaBench` + 自家 500 條衛教問句的離線評測為準，而非直接引用外部數字。

---

## 參考來源（精選）

- OpenAI Structured Outputs（2024-08-06）— complex JSON schema 100% vs <40%，首 schema 編譯 5–60s。[openai.com](https://openai.com/index/introducing-structured-outputs-in-the-api/)
- JSONSchemaBench 10K — Guidance/Llamacpp/Outlines 對比，Outlines 超時 40s–10min。[arXiv 2501.10868](https://arxiv.org/html/2501.10868v1)
- Guided Decoding Perf（SqueezeBits 2025-09）— XGrammar vs LLGuidance 在重複/動態 schema 與 vLLM/SGLang 上的 TPOT/throughput 對比。[blog.squeezebits.com](https://blog.squeezebits.com/guided-decoding-performance-vllm-sglang)
- Constrained Decoding 實戰指南（Chaos & Order 2026-03）— 1–3% 開銷，複雜 schema 10%，Outlines→XGrammar 遷移。[youngju.dev](https://www.youngju.dev/blog/llm/2026-03-07-llm-structured-output-constrained-decoding-json-schema.en)
- Structured Output 延遲分層（Kalvium 2026-04）— function calling +80–350ms 分級。[kalviumlabs.ai](https://www.kalviumlabs.ai/blog/structured-output-from-llms-json-mode-function-calling/)
- LangChain `include_raw` / parsing 慢重試 issue（#32977, #35041）。[github.com/langchain-ai/langchain](https://github.com/langchain-ai/langchain/issues/32977)
- Retry 放大與 token 定額熔斷。[tianpan.co 2026-04-28](https://tianpan.co/blog/2026-04-28-structured-output-retry-loop-hidden-compute-waste) / [Bulletproofing DEV 2026-05-10](https://dev.to/velsof/bulletproofing-llm-structured-output-in-python-healing-retries-cost-caps-and-drift-detection-c89) / [RunGuard 2026-06-14](https://runguard.dev/blog/langchain-structured-output-cost-control.html)
- RouteLLM / vLLM Semantic Router / LLM Routing Production（BERT 10–30ms、threshold 校準、demand-driven 50–70% 省）。[github.com/lm-sys/RouteLLM](https://github.com/lm-sys/RouteLLM) / [arXiv 2603.04444v3](https://arxiv.org/html/2603.04444v3) / [tianpan.co 2025-10-19](https://tianpan.co/blog/2025-10-19-llm-routing-production)
- LangGraph Fast-Path + SynaptoRoute 3ms。[github.com/langchain-ai/docs #4321](https://github.com/langchain-ai/docs/issues/4321)
- LINE / Slack / Telegram Ack-first 架構（1s/3s 死線、reply→push 降級、dedupe、durable queue、loading animation 20s）。[gemilab.net](https://gemilab.net/en/articles/gemini-api/gemini-api-line-bot-python-guide) / [dev.to 2026-08](https://dev.to/instawebhook/managing-slack-and-discord-bot-webhooks-without-getting-rate-limited-2m8e) / [drunk.support 2026-06](https://drunk.support/ten-errors-one-stuck-queue/)
- TTFT/TPOT SLO 與 pipeline vs model 拆解。[AWS AGENTPERF02-BP04](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentperf02-bp04.html) / [Spheron 2026](https://www.spheron.network/blog/llm-inference-slo-ttft-itl-latency-budget-guide-2026/) / [Data AI Hub](https://www.dataaihub.co/learn/latency-optimization)
- Streaming structured outputs（partial-json-parser / jiter / OpenAI incremental parsed）。[dreaming.press 2026-07-15](https://dreaming.press/posts/when-structured-output-breaks-repair-recovery-playbook.html)

---

*報告維護：後續改動 `a_router` / `workflow` / `line_bot` 時，請同步更新第 6 章優先序表的「已落地」狀態與實測 p50/p95，並在 `e_observability` 中固化 TTFT 與 accepted-object cost 儀表板。*
