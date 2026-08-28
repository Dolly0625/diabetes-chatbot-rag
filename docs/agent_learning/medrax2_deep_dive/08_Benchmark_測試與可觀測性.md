# 08｜Benchmark、測試與可觀測性

MedRAX2 有完整 benchmark infrastructure，但 benchmark、runtime regression test 和 production observability 是三種不同證據。

## 1. BenchmarkResult 記錄什麼

[`benchmarking/runner.py`](https://github.com/bowang-lab/MedRAX2/blob/dcd6b852f3f9557640159e200fab5f0acdea39ff/benchmarking/runner.py#L17-L31)：

```python
@dataclass
class BenchmarkResult:
    data_point_id: str
    question: str
    model_answer: str
    correct_answer: str
    is_correct: bool
    duration: float
    usage: dict | None = None
    error: str | None = None
    chunk_history: object | None = None
    tool_execution_trace: list[dict] | None = None
    metadata: dict | None = None
```

這比只存 final accuracy 好，因為保留了 latency、usage、error、chunk history 和 tool trace，可做 trajectory analysis。

## 2. Benchmark provider 是 adapter

benchmark runner 不直接依賴單一模型，而是透過 provider interface 將 request 轉給 OpenAI、Google、MedGemma 或 MedRAX provider。

```text
BenchmarkDataPoint
→ LLMRequest
→ provider.generate()
→ LLMResponse
→ answer extraction
→ correctness evaluation
→ BenchmarkResult
```

這讓同一資料集可以比較 standalone model 與 full Agent system。

## 3. Final accuracy 看不到的問題

兩條 trajectory 可能同樣答對：

```text
A：1 次必要工具 → 正確答案
B：8 次重複工具 + 1 次失敗 + 高成本 → 正確答案
```

只看 `is_correct` 會把 A、B 視為相同。Agent evaluation 還需要：

- tool selection precision/recall；
- argument validity；
- redundant call rate；
- tool failure recovery；
- conflict resolution；
- citation/grounding；
- step、latency、token、API/GPU cost；
- safe fallback appropriateness。

## 4. 固定快照缺少一般 runtime test suite

在固定 commit 中沒有找到一般 `tests/test_*.py` regression suite。Repository 內有 benchmark、experiment scripts，以及某些手動測試腳本，但不等於以下 contract 已被自動驗證：

- 有 tool call 一定走 tools node；
- 無 tool call 一定停止；
- 多 tool calls 的順序/平行行為；
- tool exception/error payload 行為；
- thread 隔離；
- custom prompt concurrency；
- unknown registry key；
- step limit；
- checkpoint restore。

這不是說 benchmark 沒價值，而是證據種類不同。

## 5. 三層測試金字塔

```text
        End-to-end benchmark
      final task quality / cost
               ▲
       Integration trajectory tests
  model stub + real graph + fake tools
               ▲
         Unit/contract tests
 schema / adapter / route / cache / gate
```

對 Agent 系統，integration trajectory tests 特別重要。它們可以使用 scripted model，不花 API token，精確驗證：

```text
LLM call 1 → tools A+B
tool A success / B error
LLM call 2 → tool C
LLM call 3 → final
```

## 6. UI trace 與 evaluation trace 不同

Gradio 的 tool card 適合人看；benchmark 的 `tool_execution_trace` 適合分析；production trace 還需要：

- trace/run/thread ID；
- node start/end；
- normalized tool name/args digest；
- execution/domain status；
- policy decision；
- cache hit；
- latency/cost；
- sensitive data redaction；
- final termination reason。

不可直接把 UI 顯示紀錄當成合規 audit log。

## 7. 建議的 MedRAX2 contract tests

若要補測試，優先順序如下：

1. `AgentState.messages` append reducer。
2. direct answer 不執行工具。
3. single tool call 後回 agent synthesis。
4. multiple tool calls 的結果關聯。
5. tool error 的呈現方式。
6. thread A/B 隔離。
7. unknown selected tool 顯式失敗。
8. max steps/timeout/cost termination。
9. artifact 刪除後的 resume 行為。
10. output verifier 必經。

## 8. 對 TFDA 的評估重點

你的 TFDA 系統不應只比較 final answer accuracy，還應記錄：

```text
A route correctness
retrieval recall / ranking
B evidence approval precision
planner action appropriateness
rewrite meaning preservation
C claim-evidence mapping
D block/pass correctness
end-to-end safe completion rate
```

MedRAX2 的 trajectory/benchmark 思維值得借鏡；你們既有的 E structured trace 則比 UI tool card 更接近可稽核基礎。

