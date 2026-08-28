# 架構設計與 MedRAX2 對照

## 1. 設計目的

這個實驗不是逐檔複製 MedRAX2，而是回答更適合本專案的問題：

> 如何保留 MedRAX2 的動態 tool-calling 能力，又不讓醫療資訊 agent 可以自行跳過必要的安全與證據檢查？

因此它使用「固定外骨架 + 動態內迴圈」：

- 外骨架由程式碼持有控制權：A input policy、B evidence、D output。
- 內迴圈由模型決定下一個 read-only tool 或完成草稿。
- 工具執行器持有限額、allowlist、schema validation、快取與錯誤邊界。

## 2. State 是執行契約，不只是聊天紀錄

`AgentState` 分成五類：

| 類別 | 欄位 | 用途 |
|---|---|---|
| 對話 | `messages` | user、assistant、tool messages；使用 reducer 累加 |
| 執行識別 | `run_id`, `thread_id` | 分離一次 run 與跨 run thread |
| 控制 | `status`, `termination_reason`, `agent_steps`, `tool_call_counts` | 決定是否繼續與為何結束 |
| 工具與證據 | `pending_tool_calls`, `tool_results`, `candidate_evidence`, `approved_evidence_ids` | 區分候選資料與已核准證據 |
| 輸出 | `draft_response`, `final_response` | 明確區分模型草稿與系統可交付答案 |

最重要的是 `candidate_evidence != approved_evidence_ids`。Tool output 進入 state 時仍只是一個候選；只有 B gate 可以升級它的使用資格。

## 3. Graph 的控制流

核心圖位於 `agent_lab/graph.py::_build_graph`：

```python
graph.add_edge(START, "input_gate")
graph.add_conditional_edges(
    "input_gate", self._input_route,
    {"AGENT": "agent", "END": END},
)
graph.add_conditional_edges(
    "agent", self._agent_route,
    {"TOOLS": "tools", "EVIDENCE": "evidence_gate", "END": END},
)
graph.add_edge("tools", "agent")
graph.add_conditional_edges(
    "evidence_gate", self._evidence_route,
    {"OUTPUT": "output_gate", "END": END},
)
graph.add_edge("output_gate", END)
```

三個關鍵性質：

1. 模型回傳 tool calls 時只能去 `tools`，不能直接執行 Python function。
2. 模型沒有 tool call 時也不能直接 END，必須先經 B、D。
3. 任一安全閘門都可以 fail closed，留下結構化 reason code。

## 4. 一次正常執行的時序

```text
User             A gate          Model          Tool executor       B gate       D gate
 │                  │              │                  │                │            │
 ├─ query ─────────►│              │                  │                │            │
 │                  ├─ PASS ──────►│                  │                │            │
 │                  │              ├─ search + lookup►│                │            │
 │                  │              │   (parallel)     │                │            │
 │                  │              │◄─ ToolResult ×2 ─┤                │            │
 │                  │              ├─ inspect ───────►│                │            │
 │                  │              │◄─ ToolResult ────┤                │            │
 │                  │              ├─ cited draft ────────────────────►│            │
 │                  │              │                  │                ├─ PASS ─────►│
 │◄────────────────────────────────────────────────────────────────────── final/pass ┤
```

離線模型固定走這條路，是為了讓測試可重現。換成 LLM 後，內迴圈的工具次序可以改變，但所有外層 gate 與限額不變。

## 5. 工具層的契約

每個工具繼承 `ExperimentTool`，必須宣告：

- `name`
- `description`
- `input_model`
- `max_calls_per_run`
- `risk_level`
- `execute()`

基類負責共同邊界：

```python
validated = self.input_model.model_validate(call.arguments)
value = self.execute(validated)
return ToolResult(
    call_id=call.call_id,
    tool_name=self.name,
    status="OK",
    payload=value.payload,
    candidate_evidence=value.candidate_evidence,
    latency_ms=(time.perf_counter() - started) * 1000,
)
```

輸入驗證錯誤變成 `INVALID_ARGUMENTS`，相依服務例外變成 `TOOL_EXECUTION_FAILED`。工具例外不會直接炸掉整個 graph，也不會把任意 exception message 暴露給模型或使用者。

目前三個工具都是 `READ_ONLY`：

| 工具 | 目的 | 每 run 上限 |
|---|---|---:|
| `search_tfda_risk_communications` | 依問題搜尋 TFDA 風險溝通資料 | 2 |
| `lookup_tfda_ingredient_risks` | 依藥品成分或藥物類別查詢 | 2 |
| `inspect_tfda_evidence_set` | 依 evidence ID 重新取得確定資料 | 2 |

## 6. Selective initialization 與 capability allowlist

MedRAX2 由設定選擇要初始化哪些 tools。此實驗保留同一思想，但 registry 更早 fail：

```python
unknown = sorted(set(selected) - set(available_by_name))
if unknown:
    raise ValueError("unknown tools: %s" % ", ".join(unknown))
```

工具清單同時決定：

1. graph executor 實際允許呼叫什麼；
2. LLM adapter 綁定哪些 JSON schema。

這避免出現「prompt 說不能用，但模型其實仍持有工具」的虛假安全邊界。

## 7. 平行執行、順序還原與快取

同一個 assistant turn 內的 calls 以 `ThreadPoolExecutor` 平行執行。future 完成順序不固定，所以完成後依原始 `call_id` 排回模型提出的順序：

```python
by_call_id = {item.call_id: item for item in immediate + executed}
ordered = [by_call_id[call.call_id] for call in calls if call.call_id in by_call_id]
```

快取 key 是 `tool name + canonical JSON arguments` 的 SHA-256。只快取 `OK`，不快取失敗結果；命中時保留新的 `call_id` 並標示 `cache_hit=True`。

這裡的 cache 是 process-local，目的是展示 harness responsibility，不等同 production distributed cache。

## 8. Bounded autonomy

`AgentLimits` 預設值：

```python
class AgentLimits(StrictModel):
    max_agent_steps: int = 4
    max_total_tool_calls: int = 6
    deadline_seconds: float = 15.0
```

此外每個 tool 還有自己的 `max_calls_per_run`。因此模型即使不停要求工具，也會因下列 reason 結束：

- `MAX_AGENT_STEPS_EXCEEDED`
- `MAX_TOOL_CALLS_EXCEEDED`
- `PER_TOOL_LIMIT_EXCEEDED`
- `DEADLINE_EXCEEDED`

注意：deadline 在 node 邊界檢查，是 cooperative timeout。若要做到硬中斷，需要為每個外部 client 設定 connect/read timeout，或把執行移至可取消的 worker。

## 9. 三道強制閘門

### A：輸入政策

目前用小型規則展示三種分流：prompt injection、緊急醫療情境、個人化停換藥或劑量要求。它不是正式分類器；重點在於 A 是 graph 的必經節點，不是 system prompt 中的一句提醒。

### B：證據核准

B 目前只核對：

- evidence ID 不重複；
- `source == TFDA 藥品安全資訊風險溝通資料`；
- content 非空；
- retrieval score 大於 0。

它刻意不宣稱能判定醫療語義、時效性或內容是否足以回答特定問題。production 版應加入 query-evidence entailment、日期/版本規則、必要欄位、衝突證據與人工升級。

### D：輸出核准

D 要求：

- 至少一個 `[tfda-risk-NNNN]` citation；
- 所有引用都在 B 的 allowlist；
- 至少引用一筆核准證據；
- 沒有直接個人化停藥、換藥、加減藥或診斷句型；
- 存在明確範圍聲明。

因此 `draft_response` 只是模型產物；只有 D PASS 後才成為 `final_response`。

## 10. Memory、run 與 thread

graph 使用 `MemorySaver`，同一 `thread_id` 可保留累積 messages。但一次 `run()` 都生成新的 `run_id`，回傳結果時只取出該 run 的 messages 與 trace：

```python
current_trace = [x for x in state["trace_events"] if x.run_id == run_id]
current_messages = [x for x in state["messages"] if x.run_id == run_id]
```

這解決一個常見問題：需要 thread history，不代表本次 API response 應混入之前 run 的工具結果。

## 11. Trace 的隱私邊界

Trace 保留 stage、event、status、時間與控制資料，例如 tool name、reason code、cache hit；刻意不放完整 user query、完整 evidence content 或完整 final answer。

這是「最少必要可觀測性」的示範。正式環境仍需定義 retention、access control、redaction、correlation ID 與稽核政策。

## 12. 與 MedRAX2 的能力對照

| 面向 | MedRAX2 | 本隔離實驗 | 判斷 |
|---|---|---|---|
| orchestration | LangGraph `agent ↔ tools` | LangGraph `agent ↔ tools` | 核心概念對齊 |
| selective tools | 設定驅動初始化 | registry allowlist + fail-fast | 對齊並加嚴 |
| state/messages | message state | typed state + typed messages | 對齊並擴充控制欄位 |
| checkpoint | checkpointer | `MemorySaver` + thread/run 分離 | 對齊實驗層級 |
| model provider | 多 provider factory | protocol + LangChain adapter | 只完成可插拔邊界 |
| tool breadth | 多模態與醫療工具群 | 3 個 TFDA read-only tools | 尚未對齊廣度 |
| RAG | 專案 RAG pipeline | lexical baseline over existing corpus | 尚未達 production retrieval |
| parallel calls | 由工具執行抽象處理 | 明確 thread pool + reorder | 已示範 |
| structured errors | 各工具自行處理 | 統一 `ToolResult` | 本實驗較明確 |
| mandatory policy | 主要靠 prompt/應用邏輯 | A/B/D graph nodes | 針對 TFDA 主題刻意加嚴 |
| API/UI/benchmark | 都有 | 尚未實作 | 下一階段工作 |

所以「匹配 MedRAX2 等級」在這一輪的精確含義是：先匹配 agent harness 的重要工程能力與可測試性，不是假稱已匹配工具數量、臨床能力、產品介面與 benchmark 規模。

## 13. 下一階段的合理順序

1. 把 lexical baseline 換成 BM25 + vector + metadata filter，建立 retrieval gold set。
2. 建立真正的 TFDA tool adapter，而不是複製 production code。
3. 將 B gate 擴充成 evidence contract 與 query entailment tests。
4. 接 tool-calling LLM，使用錄製 fixture 做 deterministic regression。
5. 加入 FastAPI streaming、request ID、auth、rate limit 與持久化 trace。
6. 建立 benchmark：答案正確性、citation validity、tool selection、policy bypass、延遲與成本。
7. 通過評測後，再決定哪些元件值得移植回原專案；不要整包合併。
