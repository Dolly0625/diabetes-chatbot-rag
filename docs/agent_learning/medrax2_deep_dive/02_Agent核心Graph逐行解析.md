# 02｜Agent 核心 Graph 逐行解析

本章只分析 [`medrax/agent/agent.py`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/medrax/agent/agent.py)。讀完後應能不用 LangGraph 術語，自己描述每一輪發生什麼事。

## 1. State reducer：更新不是覆蓋，而是 append

原始碼第 15–25 行：

```python
class AgentState(TypedDict):
    messages: Annotated[List[AnyMessage], operator.add]
```

`Annotated[..., operator.add]` 告訴 LangGraph：node 回傳新的 `messages` 時，使用 list addition 合併到既有 state。

```text
舊 state.messages = [HumanMessage]
agent node return = {"messages": [AIMessage]}

reducer 後：
[HumanMessage, AIMessage]
```

如果沒有 reducer 而採預設覆蓋，第二輪只會剩下最新 AIMessage，模型看不到前文與工具結果。

## 2. ToolNode：把「執行工具」抽成一個 node

原始碼第 61–73 行：

```python
self.tool_node = tool_node if tool_node is not None else ToolNode(tools)

workflow = StateGraph(AgentState)
workflow.add_node("agent", self.process_request)
workflow.add_node("tools", self.tool_node)
workflow.add_conditional_edges(
    "agent", self.has_tool_calls, {True: "tools", False: END}
)
workflow.add_edge("tools", "agent")
workflow.set_entry_point("agent")

self.workflow = workflow.compile(checkpointer=checkpointer)
self.model = model.bind_tools(tools)
```

這段有兩個常被混在一起的動作：

- `ToolNode(tools)`：runtime 真的可以依 tool call 找到並執行工具。
- `model.bind_tools(tools)`：把 tool name、description、schema 告訴模型，使模型能產生合法 tool call。

只做前者，模型不知道有哪些工具；只做後者，模型雖能要求工具，但 graph 沒有 executor。

## 3. Graph 的實際控制流

```text
ENTRY
  ↓
agent/process_request
  ↓
has_tool_calls(response)
  ├─ False → END
  └─ True  → tools/ToolNode
                  ↓
               agent
```

固定的是 loop 骨架；動態的是：

- 叫不叫工具；
- 一次叫幾個工具；
- 叫哪些工具；
- 工具參數；
- 看到結果後再叫什麼；
- 何時輸出沒有 tool call 的文字。

這些動態決策主要由 LLM response 擁有。

## 4. `process_request()` 的三個階段

### 4.1 取得歷史

```python
messages = state["messages"]
```

Agent 沒有另外建立 domain state，例如 `diagnosis_candidates`、`approved_evidence_ids` 或 `tool_budget`。所有可供模型使用的進度，都必須存在 messages 中。

### 4.2 在 invocation view 前面補 system prompt

```python
if self.system_prompt and (
    len(messages) == 0 or not isinstance(messages[0], SystemMessage)
):
    messages = [SystemMessage(content=self.system_prompt)] + messages
```

重要細節：這裡改的是區域變數 `messages`，node 最後只回傳 model response。

因此 system prompt 通常會送入 model，但不會因這段程式自動寫回 `AgentState.messages`。註解所說的「避免每次重複加入」比較像 invocation-level 判斷，不等於 checkpointer 中一定持久化了一個 SystemMessage。

可以分成兩個視角：

```text
persisted state view:   user / assistant / tool messages
model invocation view: system prompt + persisted messages + optional synthesis prompt
```

### 4.3 工具後補 synthesis instruction

```python
has_tool_results = len(messages) > 0 and isinstance(messages[-1], ToolMessage)

if has_tool_results:
    synthesis_prompt = HumanMessage(
        content="Review the tool results above. If you need more information, "
                "you can call additional tools. Otherwise, provide your complete "
                "final answer synthesizing all the information."
    )
    messages = messages + [synthesis_prompt]
```

作者特別用 `HumanMessage`，註解說是為了跨模型相容性，並避免 Gemini 看完 tool result 就停止。

這段同時施加一個行為提示：

```text
資訊仍不足 → 可以繼續叫工具
資訊已足夠 → 輸出完整答案
```

但它仍是 prompt，不是程式化的 evidence sufficiency 判斷。

### 4.4 呼叫模型並只 append response

```python
response = self.model.invoke(messages)
return {"messages": [response]}
```

node 不解析自然語言內容，也不驗證醫療聲明。它只把 AIMessage 交回 state reducer。

## 5. 終止條件其實只有一個

```python
def has_tool_calls(self, state: AgentState) -> bool:
    response = state["messages"][-1]
    return len(response.tool_calls) > 0
```

在這個核心 graph 中：

```text
有 tool_calls = 繼續
沒有 tool_calls = 結束
```

所以以下三種文字對 graph 完全等價：

- 完整醫療回答；
- 向使用者追問；
- 一句錯誤或拒答。

只要沒有 tool call，它們都會走向 `END`。

## 6. 平行工具執行的真正含義

原始碼把預設 executor 寫成 `ToolNode(tools)`，class docstring 稱其有 parallel tool execution capabilities。這代表若模型在同一個 AIMessage 產生多個彼此獨立的 tool calls，ToolNode 可在同一工具階段處理它們。

但「可平行」不代表「任何工具都應平行」：

```text
適合：同一張影像分別跑 classifier 與 segmentation
不適合：先把 DICOM 轉 PNG，再把產生的 PNG 交給 classifier
```

第二種具有資料相依性，模型必須先叫 DICOM tool，看到輸出路徑後在下一輪叫 classifier。

## 7. 核心 graph 沒有明示的 production limits

在固定快照的 `agent.py` 裡沒有看到：

- `max_agent_steps` state；
- wall-clock deadline；
- per-tool call limit；
- token/cost budget；
- duplicate tool-call cache key；
- mandatory human approval node；
- final output verifier node。

執行仍可能受到外部 HTTP timeout、模型 SDK、LangGraph recursion limit 或呼叫端取消影響，但這些不等於 application-owned policy。

## 8. 用純 Python 重寫心智模型

以下不是 MedRAX2 原始碼，而是等價概念：

```python
messages = load_thread(thread_id) + new_user_messages

while True:
    response = llm.invoke(system_prompt + messages, tools=tools)
    messages.append(response)

    if not response.tool_calls:
        save_thread(thread_id, messages)
        return response.content

    results = execute_tool_calls(response.tool_calls)
    messages.extend(results)
```

LangGraph 在這裡主要提供 state reducer、node routing、tool node、checkpoint integration 與 stream interface。

## 第二章檢核

1. `bind_tools()` 和 `ToolNode()` 缺一個會怎樣？
2. system prompt 是否一定存在 checkpoint messages 裡？
3. 追問使用者為什麼不需要獨立 node 也能發生？
4. MedRAX2 核心是誰決定停止？
5. 為什麼不能把框架 recursion limit 當成產品的 cost policy？

