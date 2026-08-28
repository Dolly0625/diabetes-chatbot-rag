# 06｜真實元件與 Fixture

理解這個專案時，必須分開「contract 真的存在」和「每個 semantic component 都已真實實作」。

## 元件矩陣

| 元件 | 真實／正式路徑 | 測試／demo 替身 | 注意事項 |
| --- | --- | --- | --- |
| Planner | `LangChainAgentPlanner` + OpenRouter/Ollama | `ScriptedAgentPlanner` | Scripted 決策是預先指定，不代表模型能力 |
| Rewriter | `LangChainQueryRewriter` | `DeterministicQueryRewriter` | Fixture 依 mapping 回傳固定改寫 |
| Retriever | `TFDADrugSafetyRetriever` | `FixtureRetriever`、`StaticRetriever`、`LocalCaseRetriever` | 真實 retriever 使用 129 筆 TFDA corpus |
| B Gate | canonical interface 存在 | `DeterministicContextGate`、`DemoContextGate`、test `SequenceGate` | Demo PASS 不是臨床 semantic validation |
| C | 可注入 LangChain C v2 | 預設 `DeterministicFixtureCGenerator` | Agent 導讀不應把 fixture answer 當模型成果 |
| D | 正式 mandatory boundary | demo semantic verifier 可被注入 | 結構／evidence/policy gate 與臨床正確性不同 |
| Trace | `TraceRecorder` + JSONL sink | in-memory sink | Trace 是實際執行記錄，不等於每個元件都是真實模型 |

## run_workflow 的預設行為

如果只呼叫：

```python
run_workflow(request)
```

預設會使用：

- Identity Query Expander
- Fixture Retriever
- Deterministic Context Gate
- Deterministic Fixture C Generator
- 沒有 Agent Planner

因此這是 deterministic baseline，不是 Cloud Agent demo。

## 啟用 Agent 的必要條件

至少需要傳入 `agent_planner`。若 Planner 可能選 `REWRITE_QUERY`，也必須傳入 `query_rewriter`，否則執行 rewrite node 時會安全失敗。

Cloud demo 組裝順序在 `agent/demo.py`：

```text
build_agent_openrouter_llm()
→ LangChainAgentPlanner.from_llm(llm)
→ LangChainQueryRewriter.from_llm(llm)
→ run_workflow(...)
```

同一個 LLM 物件可被包成 Planner 與 Rewriter，但它們使用不同 prompt、不同 schema 和不同 chain。

## 最終三案例的正確解讀

最終 JSONL 可支持：

- Cloud LLM 確實回傳了結構化 Agent action。
- Query Rewriter 確實產生了改寫。
- 真實 TFDA corpus retrieval 確實執行。
- Graph 的 action routing、重試與 trace 確實執行。

它不能單獨支持：

- B 已達到臨床可用的 context sufficiency 判定。
- C/D 已達到 production medical safety。
- 三個案例足以衡量整體 Agent 準確率。
- Fixture 測試通過等於真實 LLM 一定穩定。

## demo.py 裡容易混淆的類別

- `DemoContextGate`：依案例與 evidence ID 做 deterministic match。
- `LocalCaseRetriever`：模擬文件排名變化。
- `_fixture_planner()`：依 expected action 建立 Scripted Planner。
- `_fixture_rewriter()`：依案例資料建立固定 mapping。
- `_real_components()`：才是 OpenRouter／Ollama LLM path。

閱讀 demo 結果時先查看 CLI 選項：

- `--planner fixture|llm`
- `--provider ollama|openrouter`
- `--retriever fixture|real`

「planner=llm」和「retriever=real」是兩個互相獨立的選擇。
