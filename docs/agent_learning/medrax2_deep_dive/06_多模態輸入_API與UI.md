# 06｜多模態輸入、API 與 UI

Agent graph 只吃 messages；真正把使用者的檔案、文字與 thread 轉成 messages 的，是產品 adapter。

## 1. 為什麼影像要傳兩次

FastAPI 收到 upload 後先寫暫存檔，再建立兩個 message representations：

```text
filesystem path → 給本地 Python tools
base64 image URL → 給 multimodal reasoning LLM
```

這解決了兩種 consumer 的需求：

- Torch/PIL/pydicom 類工具通常需要 path 或 bytes；
- Chat model API 通常需要 image content block。

若只給 base64，本地工具需要自行解碼；若只給 path，遠端 LLM 看不到檔案內容。

## 2. HTTP 邊界做了哪些事

[`api.py:150-286`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/api.py#L150-L286)：

```text
validate at least one image
→ validate MIME allowlist
→ generate/reuse thread_id
→ save temporary files
→ encode base64
→ build messages
→ stream workflow
→ aggregate final text and tool markers
→ delete temporary files
→ return response
```

它處理的是 transport validation，不是醫療 input policy。例如 MIME type 合法，不代表影像內容真的是胸腔 X 光，也不代表使用者有權上傳該病患資料。

## 3. 暫時修改 shared Agent prompt 的 concurrency 風險

API 允許 request 傳入 custom system prompt：

```python
original_prompt = self.agent.system_prompt
self.agent.system_prompt = system_prompt
try:
    ... run workflow ...
finally:
    self.agent.system_prompt = original_prompt
```

如果同一 Agent instance 同時服務多個 request，這是 shared mutable state。兩個 request 交錯時可能互相看到 prompt。較安全的方法是：

- per-request runtime config；
- immutable Agent configuration；
- 每 request 建立輕量 Agent wrapper；
- 或在 graph state 中保存已授權 prompt profile ID，而不是任意 prompt text。

## 4. UI 如何重建 tool call card

Agent stream 會依 node 產生 updates。Gradio UI 對 AIMessage 做兩件事：

```python
if msg.content:
    # 顯示文字

if msg.tool_calls:
    for tool_call in msg.tool_calls:
        pending_tool_calls[tool_call["id"]] = {
            "name": tool_call["name"],
            "args": tool_call["args"],
        }
```

收到 ToolMessage 時：

```python
pending_call = pending_tool_calls.pop(msg.tool_call_id)
tool_name = pending_call["name"]
tool_args = pending_call["args"]
result = parse(msg.content)
render_tool_card(tool_name, tool_args, result)
```

因此 tool trace 不是靠解析自然語言答案，而是利用 tool call ID 的結構化關聯。

## 5. `stream_mode="updates"` 的含義

```python
for chunk in agent.workflow.stream(
    {"messages": messages},
    {"configurable": {"thread_id": current_thread_id}},
    stream_mode="updates",
):
    for node_name, node_output in chunk.items():
        ...
```

這裡主要取得 node 完成後的 state update，不應自動等同 token-level model streaming。UI 同時處理 `AIMessageChunk`，但實際是否收到 chunk 還取決於 graph/model 的 streaming 實作與呼叫方式。

## 6. UI state、graph state、database state

| State | 例子 | 生命週期 |
| --- | --- | --- |
| UI state | `pending_tool_calls`、顯示影像 path | browser/process interaction |
| Graph state | `messages` | thread/checkpointer |
| Database state | patient、chat、scan、tool execution | product retention policy |

三者不能互相取代。UI 掛掉不應使 durable run 無法恢復；graph checkpoint 也不應成為唯一的病患資料庫。

## 7. 舊版 API 的 trace 能力有限

固定快照中，FastAPI 收到 ToolMessage 後回傳：

```python
yield {"type": "tool", "tool_name": "tool_executed"}
```

所以 `QueryResponse.tools_used` 去重後通常只能知道「有工具執行」，不能知道真實工具名稱、args、狀態或 latency。

相較之下，Gradio UI 因為保存 pending tool calls，能顯示更完整資訊。這提醒我們：同一核心 Agent 的不同 interface，observability contract 可能不一致。

## 8. 暫存檔與 checkpoint 的生命週期衝突

API 結束後刪除 image files，但 message history 可能仍保存 path：

```text
thread state: image_path: temp_api/abc.png
filesystem:   abc.png 已刪除
```

後續同 thread 若模型嘗試重跑工具，舊 path 已不可用。要支援真正 durable multimodal thread，需要：

- durable artifact store；
- artifact ID 而非裸 path；
- tenant authorization；
- retention/expiration；
- checkpoint 與 artifact lifecycle 協調。

## 9. 對 TFDA 主題的轉譯

TFDA 系統未必需要影像，但同一 pattern 可用於文件與資料：

```text
使用者上傳仿單 PDF
→ artifact store ID
→ parser tool 讀 artifact
→ extracted candidate evidence
→ B approval
```

不要把本機暫存 path 直接當成長期可引用的 evidence provenance。

