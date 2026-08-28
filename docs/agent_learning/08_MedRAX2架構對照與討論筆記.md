# 08｜MedRAX2 架構對照與討論筆記

這份筆記用來準備 Agent 架構討論，回答三件事：

1. 我們目前有哪些模組，request 如何流動？
2. MedRAX2 的 Agent、工具與記憶如何合作？
3. 哪些設計可以借鏡，哪些不能直接搬進醫療用藥資訊流程？

## 研究快照與判讀原則

- 檢視日期：2026-08-23。
- MedRAX2 原始碼快照：`main` commit `dcd6b852f3f9557640159e200fab5f0acdea39ff`，commit date 2026-04-03。
- 論文：MedRAX arXiv v2，2025-05-29。
- 我們的系統：以 `tfda_context_gate/` 可執行原始碼與 tests 為準，`docs/agent_learning/` 為導讀。

判讀時要分開三層：

1. 論文描述的是方法設計與理想演算法。
2. README 描述的是產品能力與安裝方式。
3. 目前 commit 的可執行原始碼才代表實際控制流程。

## 一句話結論

兩個系統都使用 LangGraph，但不是同一類 Agent：

- **我們**：固定 A–E 醫療安全 workflow 中的 bounded recovery planner。LLM 只在 B 證據不足後選 `ASK_USER`、`REWRITE_QUERY` 或 `FALLBACK`，不能回答、選工具或批准 evidence。
- **MedRAX2**：以多模態 LLM 為中心的 ReAct tool-using agent。LLM 可以直接回答，也可以動態選一個或多個影像／檢索工具，反覆觀察工具結果後再決定下一步。

最關鍵差異不是「有沒有 LangGraph」，而是：**誰擁有控制權、誰能產生最終醫療內容，以及每一層有沒有獨立的 safety gate。**

## 我們目前的模組

正常路徑：

```text
User
  → A Input Router
  → Query Expansion
  → RAG Retriever
  → B Context Gate
  → C Evidence-aware Generator
  → D Output Gate
  → Answer

E Observability 橫跨整條路徑，只記錄、不改變決策。
```

| 模組 | 目前責任 | 主要控制邊界 |
| --- | --- | --- |
| A Input Router | request schema、prompt injection guard、意圖／風險／角色與 policy routing | 只有允許的 general education request 可進 RAG；Agent 不能推翻 A |
| Query Expansion | 把 A 的輸出轉成 retrieval queries | v0.1 預設是 identity expansion；保留 original query |
| RAG | 從 TFDA corpus 找候選 evidence | 只負責找候選，不代表候選已獲批准 |
| B Context Gate | 判斷 evidence 是 `PASS`、`INSUFFICIENT` 或其他不可繼續狀態 | 只有 B-approved evidence 能進 C |
| Agent Planner | B=`INSUFFICIENT` 時選有限恢復 action | 只能選三個 action，不能回答、選 graph node、選工具或批准 evidence |
| Query Rewriter | 產生 meaning-preserving retrieval query | 改寫後必須再跑 Expansion → RAG → B |
| C Generator v2 | 依 approved evidence 產生 `ANSWER`／`PARTIAL`／`INSUFFICIENT` 與 claim-evidence mapping | 不能使用 B 未批准的 evidence |
| D Output Gate | policy、schema、evidence ID、red-line 與 semantic verification | mandatory final gate，只有 `PASS` 才回傳 candidate answer |
| E Observability | trace、failure、latency、trajectory、evaluation record | observational、fail-open，不參與業務決策 |
| Workflow | LangGraph node、edge、計數器、fallback 與 dependency error boundary | 實際路由與上限由程式碼擁有 |

### 我們的 Agent 恢復流程

```text
                           ┌─ PASS ───────────────→ C → D
A → Expansion → RAG → B ──┤
                           └─ INSUFFICIENT ───────→ Planner
                                                    ├─ ASK_USER → 本次結束
                                                    ├─ REWRITE_QUERY
                                                    │    → Expansion → RAG → B
                                                    └─ FALLBACK → 安全結束
```

關鍵不變條件：

- `agent_planner` 沒有注入時，維持 deterministic baseline；B insufficient 直接 fallback。
- Planner 只看到投影後的 `AgentDecisionContext`，不是完整 `WorkflowState`。
- Planner decision 經 structured output 與 Pydantic 再驗證。
- graph 擁有 `max_agent_steps=2`、`max_rewrites=1`、`max_clarifications=1`。
- Planner requested action 即使合法，仍可被 graph 因超限改成 fallback。
- `ASK_USER` 結束本次 workflow；補充資料以新 request 從 A 重新進入。
- rewrite 只更新 `current_query`，`original_query` 永遠保留。
- Planner／Rewriter／dependency 失敗都 fail closed。

## MedRAX2 的核心模組

論文把 MedRAX 拆成四個互相連接的元件：

| 元件 | 功能 | 目前程式碼對應 |
| --- | --- | --- |
| Core Reasoning Engine | 多模態 LLM 分析 query、image、history，決定直接回答或呼叫工具 | `Agent.process_request()` 與 `model.bind_tools(tools)` |
| Specialized Toolbox | 以標準 tool wrapper 封裝分類、分割、VQA、grounding、report、RAG 等能力 | `medrax/tools/` 下的 `BaseTool` subclasses |
| Workflow Orchestrator | 在 LLM reasoning 與 tool execution 之間循環 | 兩節點 LangGraph：`agent` ↔ `tools` |
| Agent Memory | 保存 user、assistant、tool messages，支援同一 thread 多輪互動 | `AgentState.messages`、`MemorySaver`、`thread_id` |

### 真正的 graph 很小

MedRAX2 並沒有替每個醫療工具建立一個 graph node。核心 graph 是：

```text
START
  → agent: LLM 讀取 messages 並產生 AIMessage
      ├─ 沒有 tool_calls → END，文字就是回答或追問
      └─ 有 tool_calls ─→ tools: ToolNode 執行呼叫
                            → 把 ToolMessage 加回 messages
                            → agent 再推理
                            → 可再叫工具或產生最終文字
```

這是一個通用 ReAct loop：固定的只有 `agent → tools → agent`；真正的 task decomposition、tool selection、是否停止，主要由 LLM 每一輪輸出的 tool calls 決定。

### State 與 memory

核心 `AgentState` 只有一個欄位：

```text
messages: append-only list[AnyMessage]
```

其內容包含：

- system prompt。
- 使用者文字。
- image path 與 base64 image message。
- LLM response／tool call。
- ToolMessage result。

`MemorySaver` 以 `thread_id` checkpoint 同一段對話，所以 UI/API 只要沿用 thread ID，就能延續先前 messages。這比我們目前 request-level re-entry 更接近真正的多輪 conversation state。

工具結果回來後，`process_request()` 還會加入一個 synthesis prompt，要求模型檢查工具結果，資訊不足就繼續叫工具，否則整合成完整回答。

### Tool integration 方法

每個工具以 LangChain `BaseTool` 封裝，最重要的介面是：

- `name`：LLM tool call 使用的唯一名稱。
- `description`：告訴 LLM 能力、適用時機與回傳內容；同時是一種 implicit prompt。
- input schema：驗證 LLM 傳入參數。
- `_run()`／execution logic：載入模型、前處理、推論、後處理與回傳結果。

初始化時先建立 tool registry，再依 `tools_to_use` 選擇實際載入的工具，最後將選中的 instances 綁到 LLM 和 `ToolNode`。這讓不同 deployment 可以控制 GPU／API 成本，也能讓不同 Agent 使用不同 tool subset。

目前 repository 可見的能力包括：

- CXR classification：TorchXRayVision、ArcPlus。
- segmentation：ChestXRaySegmentation、MedSAM2。
- VQA：CheXagent、LLaVA-Med、MedGemma API client。
- grounding：MAIRA-2 phrase grounding。
- report generation。
- CXR generation。
- DICOM processing、image visualization。
- Medical RAG。
- Google／DuckDuckGo web search。
- stateful Python sandbox。

### RAG 在 MedRAX2 裡的位置

MedRAX2 把 RAG 當成 LLM 可自由選用的一個 tool，而不是所有 request 必經的 pipeline stage：

```text
Agent LLM
  → medical_knowledge_rag(query)
      → Pinecone vector retrieval
      → Cohere reranking
      → Cohere RetrievalQA 直接生成 answer
      → 回傳 answer + source_documents
  → Agent LLM 再整合
```

這和我們的設計有本質差異：

- 我們把 retrieval、evidence approval、answer generation、output verification 拆成 RAG → B → C → D。
- MedRAX2 的 RAG tool 內部已經 retrieval + answer generation，結果再交回 orchestrator LLM；核心 graph 沒有獨立的 evidence approval gate。

## 兩個架構的直接比較

| 比較面向 | 我們目前的 TFDA Agent | MedRAX2 |
| --- | --- | --- |
| Agent 類型 | bounded recovery planner | multimodal ReAct tool-using agent |
| Agent 啟動時機 | 只在 B=`INSUFFICIENT` | 每個 query 都先進 Agent LLM |
| LLM 的輸出權 | 三種 structured action | 文字回答、追問、一個或多個 tool calls |
| 最終回答者 | C 產生，D 驗證後回傳 | orchestrator LLM 綜合自己的視覺推理與工具結果 |
| 路由控制 | graph 固定 node/edge，LLM 只提供 action | graph 固定 ReAct 骨架，LLM 動態選 tool 與停止時機 |
| RAG | 必經候選 evidence stage | 可選 tool，tool 內含 retrieval + generation |
| Evidence approval | B 顯式批准 evidence IDs | 核心 graph 無對等的獨立 approval gate |
| Output gate | D mandatory | 主要依 system prompt 與 LLM synthesis；無對等 D node |
| Tool 生態 | Agent 不能選工具，目前 graph 注入固定 components | 大量 `BaseTool`，LLM 可動態、平行或連續呼叫 |
| State | typed business state + recovery counters | append-only message history |
| 多輪記憶 | ASK_USER 後新 request，自 A 重跑 | `MemorySaver` + `thread_id` 延續同一 conversation |
| Loop bound | graph 明確擁有 step/rewrite/clarification limits | 論文定義 `t_max`；檢視的核心程式碼沒有同等顯式 timeout/step field |
| 失敗策略 | schema／dependency failure 進固定 safe fallback | tool error 進 messages，由 LLM 再決定重試、換工具或說明限制 |
| 可觀測性 | 結構化 E trace，含 requested vs executed action | UI/API 顯示 node update、tool call 和 tool result；另有 benchmark logging |
| 主要優勢 | 可稽核、fail-closed、醫療控制邊界清楚 | 彈性高，能組合異質影像工具解多步任務 |
| 主要風險 | 能力窄、沒有 durable conversation/tool ecosystem | LLM 控制面大、工具衝突／成本／不確定性與最終驗證較難治理 |

## 論文方法與目前程式碼要分開看

以下不是否定論文，而是討論實作成熟度時必須說清楚的差異：

1. **Timeout**：論文演算法明確有 `t_max` 與 timeout response；目前 `medrax/agent/agent.py` 的 graph 沒有同等的時間欄位或顯式 termination node。執行時仍可能受 LangGraph recursion limit 或各 dependency timeout 影響，但這不等於論文所寫的 application-owned `t_max`。
2. **Tool output cache**：論文說 memory 會 cache tool outputs、避免重複計算；目前核心程式碼確實保留 ToolMessages，但沒有看到以 tool name + arguments 做 lookup／dedup 的顯式 memoization。LLM 可以從 history 看見舊結果，不代表系統強制禁止重跑相同工具。
3. **ASK_USER**：論文把 `RequiresUserInput` 寫成演算法條件；目前 graph 沒有獨立 ASK_USER node，實際上是 LLM 回傳一段沒有 tool call 的自然語言文字後結束本輪。
4. **Safety/output verification**：system prompt 要求不取代醫師、說明不確定性並批判工具結果，但核心 graph 沒有和我們 B/D 對等的獨立 verifier。
5. **Tool naming drift**：目前 `main.py` 的預設清單包含 `XRayVQATool`，但 registry key 是 `CheXagentXRayVQATool`；未知名稱會被略過。因此 demo 預期有 VQA，不代表該 instance 一定真的載入。討論或重現前應先列印並核對 `tools_dict`。
6. **Tests 與 benchmark**：repository 有完整 benchmarking／experiment scripts，但在檢視的 commit 中沒有找到一般 `tests/test_*.py`。benchmark 成績和 runtime contract regression tests 是兩種不同證據。

## 我們可以借鏡什麼

### 適合直接借鏡

- **標準 Tool contract**：統一 name、description、input schema、execution result、error envelope。
- **Selective initialization**：依 deployment／resource budget 載入不同 component，避免所有大型模型常駐 GPU。
- **Tool result normalization**：把 specialized model 的輸出統一成可 trace、可驗證的結構。
- **多模態 state representation**：區分 image reference、實際 image payload、使用者文字與 tool result。
- **平行工具執行**：只有在工具彼此獨立、輸入固定、結果仍會被 verifier 檢查時採用。
- **衝突可見性**：明確保留不同工具的各自結論，不在 adapter 層偷偷合併。

### 不宜直接照搬

- 不應讓單一 LLM 同時擁有 policy routing、工具選擇、evidence approval、最終回答與停止權。
- 不應把 tool description／system prompt 當作醫療安全的唯一強制機制。
- 不應讓 RAG tool 內生成的 answer 直接繞過 B evidence approval 和 D output gate。
- 不應只靠 conversation history 避免重複執行高成本工具；需要程式化 cache key、TTL 與 invalidation policy。
- 不應讓無上限的 ReAct loop 進入 production；時間、步數、工具次數和成本都應由 graph／runtime 強制。

### 較適合我們的混合方向

保留現有 A、B、C、D、E 邊界，只在被批准的窄範圍增加 tool orchestration：

```text
A policy gate
  → deterministic retrieval / B
  → 若 B insufficient：bounded recovery planner
       ├─ ASK_USER
       ├─ REWRITE_QUERY
       ├─ CALL_APPROVED_RETRIEVAL_TOOL（未來可新增，需 typed schema）
       └─ FALLBACK
  → 每個 tool result 正規化為 candidate evidence
  → 一律重新經 B approval
  → C generation
  → D mandatory verification
  → E 記錄 requested action、executed action、tool input digest、結果與成本
```

重點是借用 MedRAX2 的「工具模組化與組合能力」，但不放棄我們現有的「policy、evidence 與 output 三道獨立信任邊界」。

## 討論時可以直接問的問題

### 架構定位

1. 我們要解的是「資料不足時如何恢復」，還是「讓 LLM 自由拆解多步任務」？
2. Agent 是 control-plane decision maker，還是 final medical answer generator？
3. 哪些 node 必經，哪些 tool 可選？這要由 LLM 還是程式碼決定？

### 安全與控制權

4. Tool result 只是 observation，還是可直接成為 approved evidence？
5. 工具互相矛盾時，誰仲裁：另一個 LLM、規則、專門 verifier，還是人工 review？
6. 最大 agent steps、每工具次數、wall-clock timeout、token／GPU／API budget 誰擁有？
7. 非法 tool call、schema mismatch、tool crash 和 partial result 分別如何 fail closed？

### Memory 與隱私

8. 要保存完整 message history，還是只保存縮減後的 medical state？
9. `thread_id` 的生命週期、隔離、刪除與 PHI retention policy 是什麼？
10. ASK_USER 補充後，哪些安全 gate 必須重新執行？

### 評估

11. 除了 final accuracy，是否分別量 tool selection accuracy、tool argument validity、grounding、conflict resolution、fallback appropriateness、cost 和 latency？
12. Fixture、真實 retriever、真實 gate、真實 generator 的結果是否在報告中清楚分開？
13. 是否有 adversarial trace 測試 Agent 不能繞過 policy／evidence／output boundaries？

## 30 秒會議口頭版

> 我們和 MedRAX2 都用 LangGraph，但控制模型不同。MedRAX2 是典型 ReAct：LLM 每輪可以直接回答或動態呼叫多個影像工具，ToolNode 執行後把結果放回 message memory，再讓 LLM 決定是否繼續。它強在多工具組合、模組化與多輪互動。現在我們的 Agent 則只在 B 判定 evidence insufficient 後啟動，而且只能 ASK_USER、REWRITE_QUERY 或 FALLBACK；真正路由、重試上限、evidence approval、generation 和 final verification 都由 graph、B、C、D 控制。若要借鏡，我建議借它的 tool contract、selective loading、parallel execution 和 message/tool trace，但保留我們 A/B/D 的強制邊界，不把最終醫療控制權全部交給同一個 LLM。

## 來源

- [MedRAX2 repository README](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/README.md)
- [MedRAX2 Agent graph implementation](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/medrax/agent/agent.py)
- [MedRAX2 initialization and tool registry](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/main.py)
- [MedRAX2 Medical RAG tool](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/medrax/tools/rag.py)
- [MedRAX2 multiple-choice system prompt](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/medrax/docs/MedRAX2_system_prompt_v2.txt)
- [MedRAX paper, arXiv v2](https://arxiv.org/html/2502.02673v2)
- 我們的 source of truth：[CURRENT_ARCHITECTURE.md](../../tfda_context_gate/CURRENT_ARCHITECTURE.md)
- 我們的 graph：[workflow/graph.py](../../tfda_context_gate/workflow/graph.py)
- 我們的 runner：[workflow/runner.py](../../tfda_context_gate/workflow/runner.py)
- 我們的 Agent contracts：[agent/schemas.py](../../tfda_context_gate/agent/schemas.py)
