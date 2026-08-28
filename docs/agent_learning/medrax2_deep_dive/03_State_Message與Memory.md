# 03｜State、Message 與 Memory

「Agent 有 memory」這句話太模糊。MedRAX2 至少需要分清楚四種東西：

```text
Graph state       當前 thread 的 messages
Checkpoint        state snapshot 的保存機制
Product database  chat/patient/scan 等業務資料
Model memory      影像模型內部的 feature/memory，與對話記憶不同
```

本章只討論前三種。

## 1. AgentState 是 message-centric state

```python
class AgentState(TypedDict):
    messages: Annotated[List[AnyMessage], operator.add]
```

優點：

- 符合大多數 chat model API；
- tool call 與 tool result 自然串接；
- 容易支援多輪對話；
- 新增工具通常不需擴充 state schema。

代價：

- domain facts 隱藏在自由文字與 tool payload 裡；
- 很難直接查詢「目前批准了哪些 evidence」；
- 計數、budget、risk state 沒有 typed field；
- 長對話可能快速膨脹；
- 要依賴 LLM 從歷史自行辨認舊結果與衝突。

## 2. 一次影像 request 會產生多個 user messages

[`api.py:201-239`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/api.py#L201-L239) 的輸入不是把所有內容塞進單一 message，而是：

```python
messages.append({"role": "user", "content": f"image_path: {temp_path}"})

messages.append({
    "role": "user",
    "content": [{
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
    }],
})

messages.append({
    "role": "user",
    "content": [{"type": "text", "text": question}],
})
```

同一張影像有兩種 representation：

| Representation | 消費者 | 目的 |
| --- | --- | --- |
| `image_path: ...` | 本地 tool | 讓分類、分割、DICOM 等工具找到檔案 |
| base64 `image_url` | multimodal LLM | 讓 reasoning model 直接看影像 |

這是很實用的 adapter pattern，但也帶來同步問題：路徑檔案若被清理，checkpoint 中仍可能留下已失效的 path message。

## 3. Tool call 與 ToolMessage 的配對

一輪典型 message history：

```text
HumanMessage(question + image)
AIMessage(
  content="",
  tool_calls=[{id: "call_1", name: "torchxrayvision_classifier", args: {...}}]
)
ToolMessage(
  tool_call_id="call_1",
  content="...tool output..."
)
AIMessage(content="final synthesis")
```

`tool_call_id` 是關聯鍵。Gradio UI 會先記住 AIMessage 裡的 pending call：

```python
self.pending_tool_calls[tool_call["id"]] = {
    "name": tool_call["name"],
    "args": tool_call["args"],
}
```

收到 ToolMessage 後，再用 `tool_call_id` 取回 tool name 與 args，顯示完整工具卡。這是 UI state，不是 Agent graph state。

## 4. `MemorySaver` 的作用

[`main.py:194-212`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/main.py#L194-L212)：

```python
checkpointer = MemorySaver()

agent = Agent(
    llm,
    tools=list(tools_dict.values()),
    system_prompt=prompt,
    checkpointer=checkpointer,
)
```

呼叫端再提供 `thread_id`：

```python
agent.workflow.stream(
    {"messages": messages},
    {"configurable": {"thread_id": thread_id}},
    stream_mode="updates",
)
```

關係是：

```text
thread_id → checkpointer 找到該 thread 的 graph state snapshots
```

沿用 thread ID，新的 user messages 會接在先前 state 上；換新的 thread ID，就像開新對話。

## 5. `MemorySaver` 不等於 durable production memory

固定快照使用的是 in-process memory checkpointer。典型限制包括：

- process 重啟後消失；
- 多 worker 不會天然共享；
- 不等於資料庫交易；
- 沒有在核心 graph 表達 retention/deletion policy；
- `thread_id` 本身不自動提供 tenant authorization。

因此正確說法是：

> MedRAX2 核心 demo 支援 thread-scoped checkpoint conversation state。

不應直接說：

> MedRAX2 已完成企業級長期記憶與病患資料治理。

## 6. Checkpoint memory 與產品資料庫不同

較新的 `web_platform/backend` 額外定義 chat、message、patient、scan、tool execution 等資料模型。這代表產品層需要可查詢、可管理的業務資料；不能把所有東西都藏在 LangGraph messages 裡。

```text
Checkpoint state：為了恢復 Agent execution
Product database：為了產品查詢、權限、歷史、管理與稽核
```

兩者可以互相關聯，但責任不能混同。

## 7. Memory 不等於 cache

ToolMessage 留在 history，表示 LLM「有機會看到」舊結果；它不代表 runtime 會依 `(tool_name, normalized_args)` 自動命中 cache。

真正的 tool cache 至少需要：

```text
cache key
result version
TTL
invalidation policy
scope / tenant
artifact availability
error caching policy
```

若高成本影像模型被重複呼叫，只靠 prompt 說「不要重複」不足以保證成本與延遲。

## 8. 對 TFDA 的啟示

你的 TFDA 系統現在使用 typed business state：

- `original_query`；
- `current_query`；
- `b_result`；
- `approved_evidence_ids`；
- `agent_steps`；
- `rewrite_count`；
- `termination_reason`。

這比純 messages 更適合 safety-critical workflow。若未來加入對話 memory，比較安全的方式是雙層 state：

```text
Conversation layer
└─ user/assistant messages、thread metadata

Medical workflow layer
└─ typed request、policy result、candidate/approved evidence、limits、trace
```

不要讓舊對話中的自由文字直接升格成當次 request 的 approved evidence。

## 第三章檢核

1. 同一張影像為什麼同時需要 path 與 base64？
2. ToolMessage 如何和原始 tool call 配對？
3. `thread_id` 是 memory 本身嗎？
4. 為什麼 `MemorySaver` 不等於 durable storage？
5. 為什麼 message history 不能取代 tool output cache？

