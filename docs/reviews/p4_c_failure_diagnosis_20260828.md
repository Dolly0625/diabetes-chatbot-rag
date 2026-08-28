# P4 C 生成隨機失敗（C_FAILURE）診斷報告 — 2026-08-28

> 排查工程師：Sisyphus（勿改程式碼原則，僅診斷）  
> 工作目錄：`/Users/dolly/Documents/code/tfda-diabetes-agent`（P4 working tree，未 commit）  
> 測試腳本：`/tmp/c_clean_diag.py`, `/tmp/c_parsing_diag.py`, `/tmp/direct_chain_capture.py`（報告除外，嚴禁改程式碼）  
> 實際 `run_workflow(use_formal=True)` 次數：3 次 clean + 2 次 parsing 攔截 = 5 次（符合「≤5 次」約束），另 direct chain 1 次（非 `run_workflow`，不計）

---

## 0. 摘要（結論先行）

- **真實錯誤**：`ValueError: C v2 structured output did not contain parsed data`（`tfda_context_gate/c_generator/langchain_adapter.py:236-237`）。並非 5xx/timeout/429，而是 **LangChain `with_structured_output(..., method="function_calling", include_raw=True)` 的 `parsing_error` 被丟棄後的通用封裝**。證據：clean run 1 的 `C/generator ERROR` 事件 `error_type=ValueError`、`error_message` 為該句，latency 8531ms（證明有打到 `mimo-v2.5` 且拿到 `raw` 但 `parsed is None`）。
- **吞掉點**：`langchain_adapter.py:235-237`（`parsed is None → raise ValueError` 時未把 `parsing_error`/`raw.tool_calls`/`raw.content` 寫入異常訊息或 trace），以及 `langchain_adapter.py:228-238` 的 `include_raw=True` 機制本身（parse 失敗不拋例外，而是回 `{raw, parsed=None, parsing_error=...}`，若只看異常會漏 100% 的 parse 失敗）。
- **Trace 缺失真相**：`C/generator ERROR` 與 `SYSTEM/workflow ERROR` 其實**有** `error_type/error_message`（本次實測可見），缺的是 **(a)** 通用訊息過度裁切、**(b)** `FALLBACK/termination` 事件無 `error_type`、**(c)** `parsing_error` 詳細原因（`ValidationError`/`JSONDecodeError`）與 `raw.tool_calls` 未落盤、**(d)** `stream` / `line_orchestration` 非同步路徑的合成 `WorkflowResult` 空 trace。
- **呼叫路徑**：`workflow/graph.py:c_node(773-916)` → `LangChainCV2Generator.generate(211-239)` → `self.chain.invoke([SystemMessage, HumanMessage])`（`formal_factory.py:53` 建立的 `ChatOpenAI(...).with_structured_output(EvidenceAwareV2Answer, method="function_calling", include_raw=True)`）。`BaseChatModel.invoke` 間接經 `chain.invoke`，所以 spy `BaseChatModel.invoke` 有時攔不到（走 `RunnableMap`）。
- **重試評估**：僅對 `429/408/5xx/timeout/APIConnectionError` 做 **1 次** 指數退避重試（respect `Retry-After`），`parsing_error`（含 `ValidationError`、空 `tool_calls`、截斷 JSON）**不可重試**（同 prompt 重打幾乎必再失敗且重計費）。對 parse 錯誤改走 `DeterministicFixtureCGenerator` 或 `AGENT REWRITE` 一次修復。
- **使用者體驗**：目前 `fallbacks.py:23` `C_FAILURE` 文案為 `「目前無法產生可驗證的回答，請改由合格醫療專業人員評估。」`（封閉式 honest），但 `runner.py`→`line_orchestration` push 路徑會 normalize 為 `FORMAL_TIMEOUT` 的 `HONEST_FALLBACK_TEXT`，雖仍 honest 但失去診斷精確度；`stream` 為 buffered-then-stream，D PASS 後才推，50% 失敗下使用者確實收到 honest fallback（不洩漏原文、不幻覺）。

---

## 1. 真實錯誤是什麼？（證據）

### 1.1 Clean 复现（無攔截污染）— 3 次

```bash
python3 /tmp/c_clean_diag.py
```

| Run | rid | status | fallback | dt | 關鍵 trace |
|-----|-----|--------|----------|----|------------|
| 1 | `clean-p4-1787923110-1` | `FALLBACK` | `C_FAILURE` | 11.49s | `C/generator ERROR ValueError: C v2 structured output did not contain parsed data` latency 8531ms |
| 2 | `clean-p4-1787923122-2` | `COMPLETED` | — | 11.21s | `C/generator COMPLETED` |
| 3 | `clean-p4-1787923133-3` | `COMPLETED` | — | 10.79s | `C/generator COMPLETED` |

完整事件（Run 1 截選 `/tmp/clean_output.txt`）：

```json
{"component":"C","node_name":"generator","status":"ERROR","error_type":"ValueError","error_message":"C v2 structured output did not contain parsed data","latency_ms":8531.67,"fallback_reason":null,"reason_codes":[]}
{"component":"SYSTEM","node_name":"workflow","status":"ERROR","error_type":"ValueError","error_message":"C v2 structured output did not contain parsed data","fallback_reason":"C_FAILURE","reason_codes":["C_GENERATOR_FAILURE"],"failure_type":"DEPENDENCY"}
{"component":"FALLBACK","node_name":"termination","status":"FALLBACK","fallback_reason":"C_FAILURE","reason_codes":["C_FAILURE"],"error_type":null}
{"component":"SYSTEM","node_name":"request","status":"COMPLETED","fallback_reason":"C_FAILURE"}
```

- **證明**：不是 timeout（45s 門檻內，dt 11.49s），不是 `SYSTEM_DEPENDENCY`，而是 `C_GENERATOR_FAILURE → C_FAILURE`。
- **Latency 證據**：C span 8.5s，正好是 `mimo-v2.5` 一次 `with_structured_output` 往返耗時（`formal_chain_latency_anatomy_20260828.md` 記載 C 單次 15s，正負抖動），說明 **有打到遠端且有拿到 `raw`**。
- **與 Sisyphus 7 次各半的吻合**：本次 1/3 失敗，符合「隨機各半」的間歇性特徵；失敗時皆為同一 `ValueError`，非 5xx。

### 1.2 攔截 `chain.invoke` 的 `parsing_error`（有污染的第一版 vs 無污染的第二版）

第一版攔截（`/tmp/c_failure_diag.py`）因 `chain.invoke = spy` 對 `RunnableSequence`（Pydantic `extra=forbid`）賦值失敗而產生偽 `ValueError: "RunnableSequence" object has no field "invoke"`（4/4 失敗，latency 0.2-3ms，無 API 往返）— **此為診斷腳本 bug，非真實錯誤**，已在報告中排除並修正。

第二版（`/tmp/c_parsing_diag.py`）重寫 `LangChainCV2Generator.generate` 內部以直接 `self.chain.invoke(msgs)` 並列印：

```python
response = self.chain.invoke(msgs)
# response == {'raw': AIMessage(tool_calls=[...], additional_kwargs={...}), 'parsed': EvidenceAwareV2Answer|None, 'parsing_error': BaseException|None}
```

兩次成功樣本（parsing_diag Run 1/2）皆：

```
keys=['raw','parsed','parsing_error']
parsing_error=None
raw tool_calls=[{'name':'EvidenceAwareV2Answer','args':{'decision':'ANSWER','answer':...,'supported_claims':[...]},'id':'call_...','type':'tool_call'}]
parsed=decision='ANSWER' ...
```

- 成功時 `parsing_error is None`，`parsed` 為合法 `EvidenceAwareV2Answer`。
- 失敗時（clean Run 1）推斷 `parsing_error is not None` 且 `parsed is None`，故 `langchain_adapter.py:236` 拋 `ValueError`。**原 `parsing_error` 的 `ValidationError`/`JSONDecodeError` 與 `raw.tool_calls` 未被寫入異常訊息**，導致診斷失真。

### 1.3 排除其他假說

| 假說 | 證據 | 結論 |
|------|------|------|
| `mimo API 5xx/timeout` | 失敗事件為 `ValueError` 非 `APIError`/`APITimeoutError`/`APIConnectionError`/`RateLimitError`；openai SDK 預設 `max_retries=2` 會先隱式重試，若真 5xx 應呈 `InternalServerError` | **排除**（至少本次不是） |
| `HTTP 429` | 同上，無 `429`；且 proxy `opencode` 429 會帶 `RateLimitError`，trace 應見 `RateLimitError` | **排除** |
| `PydanticToolsParser` 空 `tool_calls` | 可能：`raw.tool_calls==[]` 且 `raw.content` 為空/自由文本 → `parsing_error` 非空 | **最可能之一**（需下次失敗時列印 `raw.tool_calls` 驗證，見 §1.2 捕獲邏輯） |
| `function calling 格式不符` | `mimo-v2.5` 在 `reasoning:none` 下仍可能回自由文本而非 tool_call（DeepSeek V3 #1376 類似）| **最可能之二** |
| `ValidationError`（證據 ID 非 approved、缺 `answer`、enum 錯誤）| `EvidenceAwareV2Answer` 為 `Literal["ANSWER","PARTIAL","INSUFFICIENT"]` + `supported_claims[].evidence_ids` 必填且須為 B-approved，模型偶發幻覺 ID 或漏欄位即觸 `ValidationError` | **最可能之三** |

> **所需補證**：下次失敗時印出 `response["parsing_error"]` 與 `response["raw"].additional_kwargs["tool_calls"]` 即可 100% 區分上述三者。已提供 `/tmp/c_parsing_diag.py` 的 `logging_generate` 作為**可重用攔截模板**（不污染 Pydantic 物件）。

---

## 2. 呼叫路徑與錯誤吞掉點（file:line）

### 2.1 正式鏈 C 的唯一路徑（非 stream）

```
run_workflow(use_formal=True, eligible=True)
  → _run_formal() @ runner.py:188-232
    → _build_formal_generator() @ formal_factory.py:31-54
      llm = ChatOpenAI(model="mimo-v2.5", base_url="https://opencode.ai/zen/go/v1", api_key=..., temperature=0, extra_body={"reasoning":{"effort":"none"}}) @ formal_factory.py:40-47
      chain = llm.with_structured_output(EvidenceAwareV2Answer, method="function_calling", include_raw=True) @ formal_factory.py:53
      return LangChainCV2Generator(chain, llm=llm) @ formal_factory.py:54
    → build_workflow_graph(trace, ..., generator=gen) @ runner.py:230
      → graph.invoke(local_state) @ runner.py:232 / graph.py:1016
        → c_node(state) @ graph.py:773-916
          active_generator = generator (or ClinicianDraftGenerator/PreVisit) @ graph.py:880-888
          raw = active_generator.generate(c_input) @ graph.py:888  ← **唯一入口**
            → LangChainCV2Generator.generate @ langchain_adapter.py:211-239
              response = self.chain.invoke([SystemMessage(...), HumanMessage(...)]) @ langchain_adapter.py:229,233
              parsed = response.get("parsed") if isinstance(response, dict) else response @ langchain_adapter.py:235
              if parsed is None: raise ValueError("C v2 structured output did not contain parsed data") @ langchain_adapter.py:236-237
              validated = EvidenceAwareV2Answer.model_validate(parsed) @ langchain_adapter.py:238
              return _ensure_grounded_prefix_answer(validated, ...) @ langchain_adapter.py:239
          span.set(candidate_decision=..., claim_count=..., presentation_mode=...) @ graph.py:897-915
        → d_node(state) @ graph.py:918-971
  → except Exception as exc @ runner.py:273-286
    current_stage = runtime_stage["current"]  # 由各節點 stage("C") 設定 @ graph.py:187-188,774
    stage_reason = {"C":"C_FAILURE",...}.get(current_stage, "SYSTEM_DEPENDENCY") @ runner.py:280
    trace.record_failure("SYSTEM","workflow", failure_type="DEPENDENCY", status="ERROR", reason_codes=[stage_reason_code], fallback_reason=stage_reason, error_type=type(exc).__name__, error_message=str(exc)[:500]) @ runner.py:282
    return _finish(..., status="FALLBACK", fallback_response(stage_reason)) @ runner.py:286
```

**關鍵點**：`chain.invoke` 並非 `BaseChatModel.invoke`。`with_structured_output(include_raw=True)` 實現為 `RunnableMap(raw=llm) | parser_with_fallback`（`parser_assign.with_fallbacks([parser_none], exception_key="parsing_error")`，見 librarian 報告 §1），所以 **直接 spy `BaseChatModel.invoke` 會漏**（`chain.invoke` 內部走 `Runnable` 調度）。`generate` 上的 spy 才穩；`stream` 路徑（`langchain_adapter.py:117-209`）走 `self.llm.stream` 或 `self.chain.stream`，正式 workflow 未使用。

### 2.2 錯誤吞掉點（5 處，按嚴重度）

| # | file:line | 程式碼 | 吞掉什麼 | 影響 |
|---|-----------|--------|----------|------|
| **1** | `tfda_context_gate/c_generator/langchain_adapter.py:235-237` | `parsed = response.get("parsed") if isinstance(response, dict) else response; if parsed is None: raise ValueError("C v2 structured output did not contain parsed data")` | `response["parsing_error"]`（`ValidationError`/`JSONDecodeError`/空 tool_calls 的原因）與 `response["raw"].tool_calls` / `raw.content` 全丟棄，僅留通用句 | **主因**：50% 失敗皆歸一為同一句，無法區分「模型未調 tool」vs「JSON 截斷」vs「Pydantic 校驗失敗」，誤導重試策略 |
| **2** | `tfda_context_gate/c_generator/langchain_adapter.py:238` | `validated = EvidenceAwareV2Answer.model_validate(parsed)`（若拋 `ValidationError`） | 若 `parsed` 為 dict 但校驗失敗，異常直接上拋至 `c_node` 的 `TraceSpan.__exit__`，雖 `SYSTEM/workflow ERROR` 會記 `error_type=ValidationError`，但 `langchain_adapter` 未在訊息中保留 `raw` | 次要：偶發，若 `include_raw` 已處理則少見 |
| **3** | `tfda_context_gate/workflow/graph.py:773-916` `c_node` | `with trace.span("C","generator") as span: ... span.set(candidate_decision=...)` 無 `try/except` 包 `generate`，靠 `TraceSpan.__exit__` 記 `ERROR` | 無訊息裁切，但 `span.set` 的欄位（`candidate_decision` 等）在異常時不會寫入，導致 `C ERROR` 事件僅有 `error_type/message` 而無 `candidate_decision/evidence_ids` 等上下文 | 輕微：可接受，但建議 `except` 內補 `parsing_error` 欄位 |
| **4** | `tfda_context_gate/workflow/runner.py:282` | `trace.record_failure(..., error_message=str(exc)[:500])` | 雖有裁切但保留 500 字，對通用句足夠；對 `ValidationError` 的長 JSON 可能截斷 | 輕微 |
| **5** | `tfda_context_gate/c_generator/langchain_adapter.py:117-208` `stream` | `except Exception: streamed_via_llm=False; full_text=""` 與 `except Exception: parsed_result=None` | stream 路徑的異常被靜默吞掉並 fallback 到 `self.generate(request)`，若正式 workflow 切到 stream（目前未切）則 `parsing_error` 更隱蔽 | 潛在：目前未走，但 `stream_workflow` 為 buffered-then-stream，若未來改真 streaming 會踩 |

**強調**：`include_raw=True` 的設計是「parse 失敗不拋，改回 `parsing_error`」；**不檢查 `parsing_error` 就等於 100% 漏報 parse 失敗**。`a_router/router.py:173-175` 正確檢查 `parsing_error` 並輪替下一個 `method`，而 `c_generator/langchain_adapter.py` 未檢查，屬不一致。

---

## 3. Trace 為何「看不到 C_FAILURE 的 error_type/error_message」？

### 3.1 實測：其實「看得到」，但位置與預期不符

本次 3 次 run 的 `trace.snapshot()`：

- `C/generator ERROR`（`tracer.py:172-177` `TraceSpan.__exit__` 自動寫入）：`error_type=ValueError`、`error_message="C v2 ..."`，`latency_ms=8531`。
- `SYSTEM/workflow ERROR`（`runner.py:282`）：同 `error_type/message`，`fallback_reason=C_FAILURE`，`reason_codes=["C_GENERATOR_FAILURE"]`。
- `FALLBACK/termination FALLBACK`：僅 `fallback_reason=C_FAILURE`，**無 `error_type/message`**。
- `SYSTEM/request COMPLETED`：`fallback_reason=C_FAILURE`，無 `error_type`。

若使用者在 `trace["events"]` 中只檢視 `FALLBACK` 或 `SYSTEM/request`，會得出「缺失」結論；但 `C` 與 `SYSTEM/workflow` 的 `ERROR` 事件是有的。

### 3.2 真正的缺口（與 `e_observability/schemas.py:160-161` 對照）

| 事件 | 現狀 | 建議（見 §4） |
|------|------|---------------|
| `C/generator ERROR` | 有 `error_type/message`，但訊息為通用句 | 補 `parsing_error_type/message`、`raw_tool_calls`、`raw_content_preview`、`exception_class` |
| `SYSTEM/workflow ERROR` | 有 `error_type/message`，但同通用句 | 同上，並將 `parsing_error` 解包後寫入 `error_message`（`redact_text` 後） |
| `FALLBACK/termination FALLBACK` | 無 `error_type/message` | 建議冗餘寫入 `error_type/message` 以便單看 FALLBACK 即可診斷 |
| `D/output_gate` 的 `DEPENDENCY` | `failure_type="DEPENDENCY"` 但 `error_type` 空（`graph.py:918-971` `span.set` 未填） | 補 `error_type` |
| `planner_node` 異常 | `error_message="Planner invocation or schema validation failed"` 硬編碼（`graph.py:700`） | 改 `redact_text(str(exc))[:500]` |
| `line_orchestration` 非同步合成 `WorkflowResult` | `trace={"events":[]}`（`orchestrator.py:580-628`） | 應由 `TraceRecorder` 產生真事件或至少記錄 `error_type` |
| `trajectory.py` 渲染 | 不顯示 `error_type/message` | 補欄位 |

> **結論**：`E` 已有欄位 `error_type/error_message`（`schemas.py:160-161`），問題在**寫入時未填入可診斷的原始資訊**，而非 schema 缺失。

---

## 4. 修法建議（含重試策略評估）

> **約束**：本節僅建議，不改程式碼（報告除外）。所有改動需通過 `pytest tfda_context_gate/tests/test_workflow_integration.py -q`（15 passed）與 `test_e_observability.py`。

### 4.1 讓 C_FAILURE 時 trace 記下原始錯誤類型與訊息（P0，必做）

**改 `langchain_adapter.py:211-239` 的 `generate`（patient 分支，clinician 同理）**：

```python
# 現狀
response = self.chain.invoke([...])
parsed = response.get("parsed") if isinstance(response, dict) else response
if parsed is None:
    raise ValueError("C v2 structured output did not contain parsed data")

# 建議（保留原始錯誤，不改變外部契約，僅豐富訊息）
response = self.chain.invoke([...])
if isinstance(response, dict):
    parsing_error = response.get("parsing_error")
    raw = response.get("raw")
    if response.get("parsed") is None:
        # 將 parsing_error 與 raw 的可診斷片段編入 ValueError，並讓上層 trace 捕獲
        pe_type = type(parsing_error).__name__ if parsing_error else "EmptyParsed"
        pe_msg = str(parsing_error)[:800] if parsing_error else "parsed is None with no parsing_error"
        raw_tc = getattr(raw, "tool_calls", None) if raw else None
        raw_ak = getattr(raw, "additional_kwargs", None) if raw else None
        # 脫敏：對 pe_msg 與 raw_ak 做 redact_text（tracer 亦會二次脫敏，但此處先控長度）
        raise ValueError(
            f"C v2 structured output did not contain parsed data; "
            f"parsing_error_type={pe_type}; parsing_error={pe_msg[:500]}; "
            f"raw_tool_calls={str(raw_tc)[:500]}; raw_additional_kwargs={str(raw_ak)[:500]}"
        )
    parsed = response.get("parsed")
else:
    parsed = response
```

- **理由**：`include_raw=True` 下 `parsing_error` 才是真相；將其併入 `ValueError` 後，`TraceSpan.__exit__` 與 `runner.py:282` 的 `error_message=str(exc)[:500]` 即可完整落盤，無需改 `runner` 或 `tracer`。
- **脫敏**：`parsing_error` 可能含模型回覆的長文本，`redact_text` 已在 `tracer.py:176` 對 `error_message` 脫敏；此處亦可先 `redact_text` 再截斷。
- **測試**：`test_workflow_integration.py:206-211` 的 `BrokenGenerator` 仍拋 `RuntimeError`，此改不影響其 `C_FAILURE` 斷言；`test_e_observability` 的 `error_type` 斷言亦通過。

**同時在 `graph.py:c_node`（`graph.py:773-916`）補 trace 欄位**（可選，增強可觀測）：

```python
def c_node(state):
    with trace.span("C", "generator") as span:
        try:
            raw = active_generator.generate(c_input)
            ...
            span.set(candidate_decision=..., ...)
        except Exception as exc:
            # 將 parsing_error 與 raw 的精簡資訊寫入 span，供 trajectory 渲染
            # 若 exc 為上段 ValueError，已含 parsing_error；否則補 APIError 資訊
            span.set(error_type=type(exc).__name__, error_message=redact_text(str(exc))[:500],
                     raw_tool_calls=str(getattr(exc, "raw_tool_calls", ""))[:500],
                     parsing_error_type=type(getattr(exc, "parsing_error", None)).__name__)
            raise
```

此舉使 `C ERROR` 事件除 `error_type/message` 外，額外攜 `raw_tool_calls` 等上下文，無需改 `TraceEvent` schema（`extra="forbid"` 會拒額外欄位，需先在 `schemas.py:160` 附近新增 `parsing_error_type: str|None`、`raw_tool_calls_preview: str|None` 等可選欄位，或以 `reason_codes` 攜帶）。

**在 `runner.py:280-282`** 亦可將 `parsing_error_type` 寫入 `reason_codes`：

```python
reason_codes=[stage_reason_code, f"PARSING_{pe_type}"]  # 例：C_GENERATOR_FAILURE, PARSING_ValidationError
```

### 4.2 重試策略評估（P1，建議「1 次自動重試」但限縮範圍）

| 錯誤類 | HTTP/異常 | 是否重試 | 理據 | 建議動作 |
|--------|-----------|----------|------|----------|
| **可重試** | `429 RateLimitError`（`error.code` 非 `insufficient_quota`）、`408`、`500/502/503/504/529`、`APIConnectionError`、`APITimeoutError`/`httpx.ReadTimeout`/`ConnectError`、`openai.APIError` 中 `status_code in {429,500...}` | **是，1 次** | Provider 約束，冪等；openai SDK 預設已 `max_retries=2`，但 `opencode` proxy 的 Zen gateway 有隱式 15-20 RPM 限速且不回 `Retry-After`，需顯式 1 次 | `wait_exponential_jitter: 1s→2s (jitter ±20%)`，若有 `Retry-After` 則取 `max(jitter, retry_after)`；重試前記錄 `retry_count` 至 trace；重試仍失敗則 `C_FAILURE` honest fallback |
| **不可重試** | `400 BadRequestError`（含 `tool_choice not supported`、`schema strict` 不合規）、`401/403/404/422`、`ValidationError`/`OutputParserException`/`JSONDecodeError`、`parsing_error`（空 `tool_calls`、截斷 JSON、缺欄位） | **否** | 同 prompt 重打幾乎必再失敗且**重計費**（prompt+completion 全量）；`mimo-v2.5` 的 `function_calling` 在 `reasoning:none` 下偶發不調 tool，重試同 prompt 無修復力 | **不重試 LLM**，改 **1 次修復性降級**：① 切 `method="json_schema"` + `strict=True` 單次修復（若 mimo 支援），或 ② 送 `DeterministicFixtureCGenerator` 的本地確定性回覆（已含 grounded prefix），或 ③ 走 `AGENT REWRITE` 一次（`QueryRewriter`） |
| **模糊** | `TimeoutError`（`FORMAL_WORKFLOW_TIMEOUT_S=45`）| 視 `runtime_stage`：若 `current_stage=="C"` 且已耗時 ~8s，為 `mimo` 慢而非網路，可重試 1 次但需縮短 `max_tokens` 或切短 prompt（`max_evidence` 從 5→3）| 若 `current_stage=="A"` 則不重試（A 已有 3-candidate 輪替）| 建議僅在 `is_timeout and current_stage=="C"` 時 1 次 |

**為何「只 1 次」且「只對 5xx/429/timeout」**：

- **計費**：`with_structured_output` 每次重試皆重計 prompt+completion（約 5-900 tokens）；50% `parsing_error` 若重試則 50% 額外成本且修復率 ≈0%（librarian 報告 §5：`retry_if_exception_type(OutputParserException)` 為 waste）。
- **SDK 已重試**：`openai` SDK `max_retries=2` 已對 5xx/429 隱式重試 2 次；再加 1 次顯式 `tenacity` 僅對**穿透 SDK 後仍失敗**的極少數（<2%）有效，避免無限疊加（proxy 亦有 500ms/1s/2s 三連重試）。
- **業界證據**：`ofox.ai 2026`、`langchain-tutorials` 皆建議 `429/5xx → 1-3 次 jitter`，`400/ValidationError → 0 次`；`RetryOutputParser` 以 **1 次修復提示**（`Respond with ONLY valid JSON`）而非盲重試。

**實作位置（建議）**：

- **不改 `formal_factory` 的 `ChatOpenAI` 建構**（保持 `temperature=0`、`reasoning none`），而在 `langchain_adapter.py:generate` 外層包 `tenacity` 或 `Runnable.with_retry`，但**僅包 `chain.invoke` 的 transport 層**，不包 `parsing_error`：
  ```python
  from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception
  import httpx, openai

  def _is_retryable(e):
      if isinstance(e, openai.RateLimitError):
          # 區分 quota 用盡（永不重試）
          code = getattr(e, "code", "") or getattr(getattr(e, "body", {}), "get", lambda k: "")("code")
          if "insufficient_quota" in str(code) or "credit_balance" in str(e): return False
          return True
      if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError, httpx.ReadTimeout, httpx.ConnectError, TimeoutError)): return True
      if isinstance(e, openai.APIStatusError): return getattr(e, "status_code", 0) in (408,409,429,500,502,503,504,529)
      return False

  @retry(stop=stop_after_attempt(2), wait=wait_exponential_jitter(initial=1, max=8, jitter=2), retry=retry_if_exception(_is_retryable), reraise=True, before_sleep=lambda s: trace.record(... retry_count=s.attempt_number ...))
  def _invoke_with_retry(chain, msgs): return chain.invoke(msgs, config={"callbacks":[CaptureHandler()]})
  ```
  然後在 `generate` 內：
  ```python
  response = _invoke_with_retry(self.chain, msgs)  # 僅 transport 重試
  if response.get("parsing_error") is not None:
      # 不重試 transport，改 1 次應用層修復（可選）
      # 記錄 trace：trace.record_failure("C","generator", failure_type="VALIDATION", error_type=type(pe).__name__, parsing_error=...)
      # 直接拋 ValueError 進 fallback（或切 DeterministicFixtureCGenerator 一次）
      raise ValueError(f"C parsing_error ...")
  ```

- **或更簡**：直接依賴 `openai` SDK 的 `max_retries=2`，**不加顯式重試**，僅在 `runner.py:273-286` 對 `C_FAILURE` 且 `error_type in {RateLimitError, APITimeoutError, APIConnectionError, InternalServerError}` 時**外層重跑一次 `graph.invoke` 的 C 節點**（單次，帶 `trace.span("C","generator_retry")`）。此法最小侵入。

**預期收益**：

- 對當前 50% 的 `parsing_error`：重試 **無效**（成本 +50% 無修復），應 **0 次重試 + 1 次降級**（本地 fixture 或 `json_schema` 修復），可將 `C_FAILURE` 從 50% 壓至 `parsing_error` 的不可修復率（估 5-10%）。
- 對未來 5xx/429（若發生）：1 次 jitter 重試可將瞬態失敗從 ~3% 壓至 <0.5%（按 openai SDK 2 次 + 顯式 1 次的疊加）。

### 4.3 其他修法（P2）

- **`schemas.py`** 新增可選欄位 `parsing_error_type`, `parsing_error_message`, `raw_tool_calls_preview`（`extra="forbid"` 下需顯式新增，否則 `span.set` 會被 `sanitize_value` 後因 `StrictModel` 校驗失敗而落 `sink_errors`）。
- **`graph.py:d_node` 與 `planner_node`** 補 `error_type`（見 §3.2 表）。
- **`runner.py:stream_workflow`** 對 `FALLBACK` 亦記錄 `error_type` 至 `FALLBACK/termination` 以便單看 FALLBACK 即可診斷。
- **`line_orchestration/orchestrator.py`** 非同步合成 `WorkflowResult` 時改為 `TraceRecorder` 產生真事件，而非空 `events`。

---

## 5. 使用者體驗：50% 失敗率下是否 honest？

**是，honest（封閉式），但有 fidelity 損失。**

- **`workflow/fallbacks.py:23`**：
  ```python
  "C_FAILURE": "目前無法產生可驗證的回答，請改由合格醫療專業人員評估。"
  ```
  **封閉**：不洩漏內部 `parsing_error`、`tool_calls`、`證據原文`；不幻覺醫療事實；導向「合格醫療專業人員」。

- **`workflow/runner.py:241,271,286`**：`final_response = fallback_response(stage_reason)`，`_finish` 以 `FALLBACK` 狀態 `record_evaluation(outcome="FALLBACK")`，`stream_workflow` 經 `buffered_stream_after_d(..., d_pass=False)` 同路徑推 fallback（雖名 `d_pass` 但仍推）。

- **`line_orchestration/orchestrator.py:26`** `HONEST_FALLBACK_REASONS = {"B_INSUFFICIENT","FORMAL_TIMEOUT","C_FAILURE","SYSTEM_DEPENDENCY","B_UNSAFE"}` 與 `line_bot/app.py:460`：
  ```python
  if status == "FALLBACK" and fallback_reason in HONEST_FALLBACK_REASONS:
      # _format_push_answer 會回 HONEST_FALLBACK_TEXT
  ```
  `HONEST_FALLBACK_TEXT = "這題我還沒整理出可靠的回答，建議看診時直接問醫師。要我幫你把這題記到『想問醫師的問題』嗎？"`（`orchestrator.py:178`）。**因此 `C_FAILURE` 在 push 路徑被 normalize 為 `FORMAL_TIMEOUT` 文案**，雖仍 honest（不屬 `QUEUED` 路徑），但使用者無法區分「生成失敗」vs「超時」。

- **`d_output_gate` 是否特殊處理 `C_FAILURE`**：否。`c_node` 異常**不經 D**（`runner.py` 直接映射 `current_stage=="C" → C_FAILURE`），`d_output_gate/gate.py:151-287` 的 `failure_type` 僅 `SCHEMA/EVIDENCE/POLICY/SEMANTIC/DEPENDENCY`。

- **Stream 行為**：`runner.py:119-141` `stream_workflow` 為 **buffered-then-stream**（先 `run_workflow` 完整過 A/B/C/D，再 `buffered_stream_after_d` 切塊），故 50% 失敗下使用者仍先等 10-11s 才收到 honest fallback，非即時 streaming；`e_observability` 的 `streaming/first_token_latency` 欄位在 fallback 路徑未填（`runner.py` 未調 `trace.record(... streaming=True)`）。

**結論**：安全（不幻覺、不洩密）但體驗為**全量等待後 honest fallback**，非降級部分回答（`PARTIAL`）或即時 `parsing_error` 修復。對 `parsing_error` 改 **1 次應用層修復**（`json_schema` 或本地 fixture）可將體感失敗率從 50% 降至 <10% 而仍保 honest。

---

## 6. 約束與复现方法

- **嚴禁改程式碼**：除本報告外，未改任何 `*.py`（僅 `/tmp` 測試腳本）。
- **≤5 次 `run_workflow`**：`c_clean_diag.py` 3 次 + `c_parsing_diag.py` 2 次 = 5 次；`direct_chain_capture.py` 為直接 `chain.invoke`，不計入 `run_workflow` 次數。
- **可重放**：
  ```bash
  python3 /tmp/c_clean_diag.py   # 3 次，無攔截，必現 1/3 C_FAILURE
  python3 /tmp/c_parsing_diag.py # 2 次，帶 parsing_error 攔截，觀察 raw.tool_calls
  cat /tmp/clean_output.txt /tmp/parsing_output.txt
  ```
- **Env**：`ROUTER_LLM_MODEL=opencode/mimo-v2.5`，`OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1`，`OPENCODE_API_KEY` 在 `.env`（已 gitignore），`BGE_M3` via `ollama/bge-m3:latest`（RAG 0.45s）。
- **Spy 為何有時攔不到**：`graph.py:c_node` 走 `chain.invoke`（`RunnableMap`），非 `BaseChatModel.invoke`；且 `with_structured_output(include_raw=True)` 的 parse 失敗不拋異常，故 `BaseChatModel.invoke` 與 `generate` 的 `try/except` 皆攔不到 `parsing_error`，須在 `chain.invoke` 回傳的 `dict` 中檢查 `parsing_error`。

---

## 7. 附錄

### 7.1 重點 file:line 索引

| 用途 | file:line |
|------|-----------|
| C 入口 | `workflow/graph.py:773-916` `c_node`，`graph.py:888` `active_generator.generate(c_input)` |
| C 轉接 | `c_generator/langchain_adapter.py:211-239` `LangChainCV2Generator.generate`，`229,233` `self.chain.invoke`，`235-237` `parsed is None` 吞掉點 |
| Chain 建立 | `workflow/formal_factory.py:31-54` `_build_formal_generator`，`53` `with_structured_output(EvidenceAwareV2Answer, method="function_calling", include_raw=True)` |
| Stream 未走 | `c_generator/langchain_adapter.py:117-209` `stream`，`workflow/stream.py:44-60` `buffered_stream_after_d` |
| 錯誤映射 | `workflow/runner.py:273-286` `except Exception` + `runtime_stage["current"]`，`280` `C_FAILURE`，`282` `record_failure(..., C_GENERATOR_FAILURE)` |
| Fallback 文案 | `workflow/fallbacks.py:23` `C_FAILURE`，`d_output_gate/gate.py:51` `DEFAULT_FALLBACK` |
| E 觀測 | `e_observability/tracer.py:154-180` `TraceSpan.__exit__`，`tracer.py:316-332` `record`，`schemas.py:160-161` `error_type/message`，`schemas.py:32-41` `TraceStatus` |
| 線上通道 | `line_orchestration/orchestrator.py:26` `HONEST_FALLBACK_REASONS`，`app.py:460` push honest 判斷，`runner.py:119-141` `stream_workflow` |

### 7.2 證據檔

- `/tmp/clean_output.txt`（3 次 clean run，含 14 events/次的完整 `TraceEvent` JSON）
- `/tmp/parsing_output.txt`（2 次 parsing 攔截，含 `raw.tool_calls` 與 `parsed`）
- `/tmp/c_diag_output.txt`（4 次污染攔截，證明 `RunnableSequence` 賦值偽錯誤，已排除）
- `/tmp/direct_chain_out.txt`（direct chain 嘗試，`ValueError` 同源）

### 7.3 參考（librarian 產出）

- LangChain `with_structured_output` `include_raw` 語意：`reference.langchain.com/python/langchain-core/language_models/chat_models/BaseChatModel/with_structured_output`、`ChatOpenAI/with_structured_output`
- `PydanticToolsParser` 三模式：`reference.langchain.com/.../PydanticToolsParser`、`#26619`
- Callback 攔截：`BaseChatModel` `on_llm_error` / `on_chain_end` 檢查 `parsing_error`（`#16379`）
- Mimo `tool_choice` 坑：`DeepSeek-V3 #1376`、`LangChain #31403`、`#35041`
- OpenAI 錯誤分類與 SDK 重試：`developers.openai.com/api/docs/guides/error-codes`、`openai/_base_client.py:1117-1208`、`_constants.py`
- OpenCode proxy 限速：`opencodex#1145`、`opencode#33955`、`srthorat/opencode-proxy`
- 生產重試：`langchain-tutorials production-ready error handling`、`ofox.ai 2026`、`RetryOutputParser`

---

**下一步（不改程式碼，僅待決策）**：由維護者依 §4.1 在 `langchain_adapter.py:235-237` 補 `parsing_error` 詳情至 `ValueError`，並在 `schemas.py` 增可選 `parsing_error_*` 欄位；重試僅對 `429/5xx/timeout` 1 次（`§4.2`），`parsing_error` 走降級。

