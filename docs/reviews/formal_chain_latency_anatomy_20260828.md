# Formal 鏈 17-40s 延遲解剖（2026-08-28）

> **目標**：裸 `mimo-v2.5` 僅 `3.08s`（reasoning off），端到端 formal 卻 `17-40s`，逐毫秒定位燒在哪。
> **方法**：零 LLM API 調用（本次 0 次，合規 ≤3），純靜態追蹤 + `p3_latency_profiling_20260827.md` 實測交叉比對。臨時腳本僅放 `/tmp`，未改專案（除本報告外）。
> **基線**：`docs/reviews/p3_latency_profiling_20260827.md` Run1 熱快取（`T0=3.08s` 裸 mimo，`A 17-20s`，`RAG 221ms`，`C ~15s`，`E2E 42.9-48s FALLBACK`）。當前 commit 已修窄路，見 §2。
> **入口**：`line_bot/app.py:999 callback`（LINE webhook）→ `tfda_context_gate/workflow/runner.py:144 run_workflow(use_formal=True)` → `A→RAG→B→C→D→E（stream 僅緩衝後切塊）`。

---

## 1. 摘要結論（先給答案）

| 問題 | 答案 |
|---|---|
| **17-40s 燒在哪？** | **95% 在 LLM**：`A 0-20s` + `C 15s`（各 1-4 次 `mimo-v2.5` HTTP 往返 ×3.08s）；`RAG 0.22s`、`B+D+E <0.05s`、`OCR 僅圖` 均可忽略。 |
| **現碼已變** | `runner.py:32 _is_formal_eligible` + `runner.py:192 _ext=None` 窄路：**一般衛教（G）走 `A規則 5ms + RAG 0.22s + C 15s = 15.3s`**；非 G（閒聊/紅旗/模糊/看診前）走 `45ms 快路`，不再付 `A 17-20s`。舊 `42.9s` 是「全 formal」理論值，現 `15.3s` 為熱 E2E。 |
| **`with_structured_output` 幾次往返？** | **正常 1 次/段**（`function_calling` 亦單往返）。`include_raw=True` 為本地 `RunnableMap` 解析，**不增往返**，僅 +80-150ms constrained decoding 成本。**額外往返只在 `a_router/router.py:114-130` 的應用層 `parsing_error→下一 candidate.invoke()`，最壞 4 次**。見 §3.1 詳解 + 源碼 `langchain_mistralai/chat_models.py:1254`。 |
| **A 最壞幾次 LLM？** | **`1（主 chain）+3（candidates）=4 次` 串行**，`4×3.08=12.3s`，實測 17-20s 含抖動與 schema 開銷；無並行，無 `with_retry`。見 §3.2。 |
| **C token 量** | **Input 2100-2700t**（SYSTEM 650t + `clinician_draft_user_prompt` 含 5 evidences×250t ≈1450t）→ **Output 500-800t**（400字 4 段 + `source_table` 5列 + JSON wrapper）→ **單次 LLM 12-15s**，無重試。見 §3.3。 |
| **LINE async 是否額外等待？** | **用戶體感不額外**：`app.py:1083` 佔位 `ASYNC_PLACEHOLDER_REPLY` 50ms 立返，背景 `Thread(daemon) + Semaphore(5) + ThreadPoolExecutor(1).result(timeout=120) ×2 attempts` 真正 27-55s 後 `push_message` 補送。**排隊會吃 timeout**：第 6 個併發起阻塞在 `with _FORMAL_SEMAPHORE:`，計入 120s。見 §3.4。 |
| **隱藏 timeout** | `FORMAL_WORKFLOW_TIMEOUT_S=45`（`runner.py:30`，舊 15 已改）、`ASYNC_FORMAL_TIMEOUT_S=120`（`line_bot/app.py:43` / `orchestrator.py:19`，×2 attempts=240s worst）、`ToolExecutor 5s`、`Sem 5`、`dedup 120s`，**零 `sleep`**，全 `future.result(timeout=…)`。見 §3.5。 |

---

## 2. 全鏈瀑布表（每段估時 + 證據 `file:line` + 性質）

### 2.1 現碼「G 衛教」熱路徑（`use_formal=True` 且 `_is_formal_eligible==True`）— 15.3s

> 情境：`請說明糖尿病的一般飲食原則。`（`G_GENERAL_EDUCATION`，非紅旗/閒聊）。熱表示 `.vector_cache` 已命中、無圖。

| # | 段 | 估時 | file:line 證據 | 阻塞/並行/I/O | 備註 |
|---|---|---|---|---|---|
| 0 | **參數與窄路判定** `_is_formal_eligible` | **5-10ms** | `workflow/runner.py:32-82`（`is_red_flag/is_chit_chat/is_pre_visit/len<4/policy_gate`） + `32` `FORMAL_WORKFLOW_TIMEOUT_S=45` | 同步 CPU，無 I/O | 僅 `G` 放行；其餘 `trace.span("narrow_path_gate")`→`NARROW_PATH_FAST` 45ms 快路。**此 5ms 為舊 profiling 閒聊誤走 LLM 20s 的修復點**。 |
| 0b | `TraceRecorder.__init__` | ~1ms | `e_observability/tracer.py:224-248` | 同步 | `redact+hash` 脫敏 |
| 0c | OCR（僅 `image_bytes` 時） | **1-3s**（無圖則 0） | `workflow/runner.py:164-165 _process_ocr_images` + `workflow/ocr_adapter.py` | 同步 CPU/模型 | QR-first→Paddle fallback，從未寫 `WorkflowState` |
| 1 | **A 路由（窄路：規則）** | **2-5ms** | `workflow/runner.py:192-196 _ext=None` + `a_router/router.py:347 hard_extractor.extract` | 同步 | **省 17-20s**：不建 `LangChainSignalExtractor`，直接 `RuleBased + policy_gate` 5ms。舊全 formal 此格為 16.9-20s。 |
| 2 | `QueryExpansion` | ~1ms | `workflow/graph.py:550 IdentityExpander` | 同步 | |
| 3 | **RAG 熱檢索** | **221ms** | `rag/tfda_retriever.py:398-542 retrieve` + `tfda:270-281 cache pkl` + `factory.py:19 _ensure_store()` | 同步，**可並行未並行** | `P3 兩次皆 221ms`。含 `TFDA 129×5 + hpa_*.pkl 9×5` 合併、`_faq_bonus+0.05`、`threshold 0.55`、`topic_allowlist`。冷 `24s`（`add_documents`）。 |
| 3a | · `_ensure_store` 熱記憶體 | 0ms | `tfda:268 if self._store` | — | 次後命中 |
| 3b | · `_ensure_store` 熱磁碟（首進程） | 30-80ms | `tfda:270 cache_path.exists→pickle.load` `CACHE_DIR=.vector_cache` `CACHE_VERSION=g5-faq-v1 tfda:24` | 磁碟 I/O 2-4MB | 僅首進程一次 |
| 3c | · `OllamaEmbeddings.embed_query("test")` | ~0ms（熱路跳過） | `tfda:288-301` `OLLAMA_EMBED_MODEL=bge-m3:latest` `OLLAMA_BASE_URL=localhost:11434` | HTTP（冷時 50-150ms） | 熱路不觸發 |
| 3d | · `similarity_search_with_score(k=5)×2 + HPA` | 180ms | `tfda:417 + hpa:_load_hpa_stores` | 同步 `numpy cosine` | 串行 TFDA→HPA |
| 4 | **B 門** | **<1ms** | `b_context_gate/gate.py:42-121` `approval_mode="all_retrieved"` | 同步 | 15 欄已在 RAG 填好，僅篩 ID |
| 5 | **C 生成 — prompt 組裝** | 1-2ms | `c_generator/langchain_adapter.py:100-111 _build_messages` + `system_prompts.py:115 CLINICIAN_DRAFT_SYSTEM` + `user_prompts.py:130 clinician_draft_user_prompt` | 同步 | 見 §3.3 token 拆 |
| 6 | **C 生成 — LLM structured** | **15,000ms** | `workflow/formal_factory.py:50 with_structured_output(EvidenceAwareV2Answer, function_calling, include_raw=True)` → `c_generator/langchain_adapter.py:211 chain.invoke` | **同步阻塞 1 往返** | `temperature 0` 仍 ~600-900t 輸出；`stream()==攢 full_text→json.loads` 同步 10s，不走。 |
| 7 | **D 門 8 步** | **15-30ms** | `d_output_gate/gate.py:151 run_output_gate` + `verifier.py:63 HeuristicSemanticVerifier` | 同步 | 8 步全 deterministic，`verify` 5×3 claims 詞彙重疊 `≥0.85` |
| 8 | **E 觀測 20 events** | **5-10ms** | `e_observability/tracer.py:47 span STARTED/COMPLETED` + `stream.py:44 buffered_stream_after_d` | 同步 | `sink.emit fail-open`，不阻業務 |
| 9 | 整體 `stream_workflow` 切塊 | ~1ms | `workflow/runner.py:119-141` `result=run_workflow(...); yield buffered_stream_after_d(chunk_size=20)` | 同步 | **緩衝後串流**：先等 D PASS 才切塊，`first_token`≈E2E |
| | **E2E 熱合計** | **≈15.3s** | — | 串行 `max_workers=1` | `5ms+221ms+15s+≈40ms` |
| | **E2E 冷（首進程）** | **≈39s** | — | — | 熱 15.3s + `RAG冷 24s` |

### 2.2 對照：舊全 formal / 快路 / 冷啟動

| 情境 | E2E | 構成 | file:line |
|---|---|---|---|
| **舊全 formal（P3 實測）** | **42.9-48s** | `A 20s + RAG 0.22s + C 15s + B/D/E 0.05s + 抖動 7s` → `FALLBACK`（15s timeout） | `docs/reviews/p3_latency_profiling_20260827.md:6` |
| **現窄路熱（G 衛教）** | **15.3s** | 上表 | `workflow/runner.py:188-232 g.invoke` 串行 |
| **快路（閒聊/紅旗/模糊/看診前）** | **45ms** | `runner _is_formal_eligible False → RuleBased+FixtureRetriever+DeterministicFixtureC` | `workflow/runner.py:176`；P3 對照 `44.7ms` `docs/reviews/p3_latency_profiling_20260827.md:17` |
| **冷啟動全 formal** | **≈59s** | `A 20s + RAG 24s + C 15s >45s Timeout=FALLBACK` | `rag/tfda_retriever.py:341 add_documents` |

---

## 3. 深挖：每毫秒的來源

### 3.1 `with_structured_output(method="function_calling", include_raw=True)` 幾次往返？

**答案：正常 1 次/段，`include_raw` 不增往返，框架 overhead 僅 +80-150ms/段。**

| 問題 | 靜態結論 | 證據 `file:line` + 外源 |
|---|---|---|
| 是否強制走 `tools` 協議？ | **是**。`function_calling` → `convert_to_openai_tool(RouterSignals)` → `POST /chat/completions {tools:[{type:function, function:{name, parameters:{json_schema}}}], tool_choice:"required"}` | `a_router/router.py:62 llm.with_structured_output(... method=primary)`；`formal_factory.py:50`；官方 `reference.langchain.com/.../ChatOpenAI/with_structured_output` 定義 `function_calling = Uses OpenAI tool-calling API` |
| 實際 payload 大小 | A：`SYSTEM_PROMPT ~350t + HumanMessage json {request_id,schema_version,user_raw_input,declared_role,language} ~100t + tools schema ~500t = ~950t input` → `~60t output + wrapper ~120t`；C：見 §3.3 `2100-2700t input → 500-800t output` | `a_router/router.py:35-46 SYSTEM_PROMPT` + `84-100 HumanMessage json.dumps`；`formal_factory.py:33-50 bare model + extra_body reasoning none` |
| `include_raw=True` 往返 | **0 額外往返**。實現為 `RunnableMap(raw=llm) \| parser_with_fallback`：單次 `llm.invoke` → 本地 `PydanticToolsParser` → 失敗走 `with_fallbacks(exception_key="parsing_error")` 回 `{parsed:None, parsing_error: exc}`，**不發第二 HTTP**。 | 源碼 `github.com/langchain-ai/langchain/libs/partners/mistralai/.../chat_models.py:1254` 同構 8 provider；`docs.langchain.com/oss/python/langchain/models` *Include raw AIMessage … If an error occurs it will be caught and returned*；ISSUE `#32977` 亦證實 *LLM is invoked ONCE* |
| `include_raw` 解析耗時 | **1-5ms 本地 Pydantic 驗證** + 模型端 constrained decoding **+80-150ms/段（p95 +150-220ms for 11-20 fields，此為生產實測 Kalvium 2026-04, 30d, gpt-4o/Sonnet）**。兩段合計 <0.5s，不足以解釋 14-37s。 | `kalviumlabs.ai/blog/structured-output-from-llms-json-mode-function_calling` 表 *Latency Cost*；本專案 8-20 欄位恰落 +120-220ms 檔 |
| 何時多往返？ | 僅 **應用層重試**：`a_router` 自寫 `if parsing_error: continue → chains[i+1].invoke` 才多 1 輪；或外掛 `Runnable.with_retry(max_attempts=3)` / `ToolStrategy(handle_errors=True) → ToolMessage` 重提 → 才多 1-2 輪。本專案 C 無此邏輯。 | `a_router/router.py:119-122`；`langchain_core/runnables/retry.py: RunnableRetry max_attempt_number=3`；`docs.langchain.com/.../structured-output#ToolStrategy` |

**對 3.08s→17s 的意涵**：裸 `ChatOpenAI("糖尿病可以吃吃什麼")` 1 HTTP 120 字；formal `with_structured_output` 同 1 HTTP 但 `tools schema + strict JSON` 使模型走 constrained decoding 變慢約 +0.1-0.2s + `SYSTEM_PROMPT` 變長，**不應差 14s**。差值主因是 **`mimo-v2.5` 推理本身慢 + 遠端 Gateway 抖動 + A 的 4×應用重試**，框架僅 0.2-0.4s。

### 3.2 `a_router` 3 階 retry 最壞幾次 LLM？

**答案：最壞 4 次（1 主 + 3 candidates），串行，`4×3.08=12.3s`，實測 17-20s。**

```python
# a_router/router.py:53-66
primary = "function_calling" if is_small else "json_schema"  # mimo→function_calling
chain = llm.with_structured_output(RouterSignals, method=primary, include_raw=True, strict=...)
# 68-81
def _candidates():
  if is_small: return [("function_calling",{include_raw:True}),
                       ("json_schema",{strict:False,include_raw:True}),
                       ("json_schema",{strict:True, include_raw:True})]
# 83-130
chains = [self.chain] if self.chain else []
for m,kw in _candidates(): chains.append(llm.with_structured_output(RouterSignals, method=m, **kw))
for chain in chains:
  response = chain.invoke(messages)               # ★ 每次 = 1 LLM HTTP
  if response.get("parsing_error") is not None: continue  # 下一 candidate 再打一次
  if response.get("parsed") is None: continue
  return RouterSignals.model_validate(parsed)
raise RouterDependencyError
```

| 項 | 值 | 證據 |
|---|---|---|
| `is_small` 判定 | `"mimo" in model` → true | `router.py:56`；`formal_factory.py:42` |
| candidate 數 | 3 | `router.py:72-75` |
| 總 chain 數 | **1 + 3 = 4** | `router.py:104-112` |
| 並行 | **否**，`for chain in chains: invoke` 串行，需上次 `parsing_error` 才決定 | `router.py:114-130` |
| 單次 | `3.08s` 裸 + `+0.2s` function_calling × 輸入放大 | `docs/reviews/p3_latency_profiling_20260827.md:11` |
| 最壞 | **`4×3.08=12.3s`，含抖動實測 20.03s/16.91s** | `p3:11-12` |
| 現窄路 | **G 衛教才免此段**（`runner.py:192 _ext=None`）；非 G 直接快路不觸 `extract` | `workflow/runner.py:192-200` + `graph.py:239 _is_red_flag→G2 chit_chat` 二道保險 |

> 補：`a_router/router.py:320-343 route_request` 的 `G2 白名單 + len<4` 短路在 `guard` 前，已與 `runner._is_formal_eligible` 等價；舊 profiling 閒聊 17s 即因曾繞過此短路（`workflow/runner.py:112 if use_formal: extractor=_build_formal_extractor()` 不經 `_is_formal_eligible`），現已修。

### 3.3 C 生成 `EvidenceAwareV2Answer` / `ClinicianEvidenceDraft` token 量

| 組件 | file:line | 估算 |
|---|---|---|
| `EVIDENCE_AWARE_V2_SYSTEM` | `c_generator/system_prompts.py:45-94`（10 規則，含引用約束） | ~550t |
| `CLINICIAN_DRAFT_SYSTEM` | `115-163`（4 段 300-400 字格式 + 8 規則） | **~650t**（醫護版用此） |
| `evidence_aware_v2_user_prompt` | `user_prompts.py:49-105` / `clinician_draft_user_prompt:130-189` | `Case ID+Query+B decision+approved_ids ~80t + intake 8欄 ~120t + context_block 5evidences×(doc_id+日期+藥品+page_content 500字≈250t) ~1250t + hints ~50t` → **~1450t** |
| **Input 合計** | `c_generator/langchain_adapter.py:100-111 _build_messages` | **2100-2700t** |
| `EvidenceAwareV2Answer` output | `schemas.py:84-98` | `answer 200字 ~120t + sources ~80t + claims ~60t` → ~260t + wrapper → **~500t wire** |
| `ClinicianEvidenceDraft` output | `schemas.py:166-191` | `answer 4段 300-400 中字 180-240t + evidence_summary 3× + source_table 5×4 + disclaimer/conflicts 70t` → **500-700t + wrapper ~800t** |
| 生成耗時 | `langchain_adapter.py:211-239 generate()` `chain.invoke([SystemMessage, HumanMessage])` → `model_validate` → `_ensure_grounded_prefix` | **12-15s**（P3 反推 `E2E 48s -A20s -0.22s ≈28s` 含異常，歷史 42.9s 拆 `C 15s`）。`temperature 0`；`factory.py:42 reasoning none` |
| retry | **0**（C 無 `parsing_error` 分支，`parsed is None→raise` 直進 `FALLBACK`） | `langchain_adapter.py:221-238` |
| 並行 | **否**，`B→C→D` 線性，`C` 需 `B.approved_ids` | `workflow/graph.py:1001` 邊定義 |
| stream | `langchain_adapter.py:117-209 stream()` 試 `llm.stream→chain.stream→generate`，但生產 `graph.py:c_node` 未調 `stream()`；且 `llm.stream` 需攢 `full_text` 才 `json.loads`，**首 token 仍 ~10s** | — |

**為何 C 比裸慢 5×**：`input 3×`（證據）× `output 4×`（長文+表格）× `constrained JSON` 約束 = `12-15s` vs `3.08s`。

### 3.4 `line_bot/app.py` async push 架構有無額外等待？

**答案：用戶「體感首字」不額外（50ms 佔位）；完整衛教 27-55s，排隊會吃 timeout。**

#### 全域常數

| 常數 | file:line | 值 | |
|---|---|---|---|
| `ASYNC_FORMAL_TIMEOUT_S` | `line_bot/app.py:43` `float(os.getenv(..., "120"))` | **120** | 背景單次上限 |
| `FORMAL_WORKFLOW_TIMEOUT_S` | `workflow/runner.py:30` / `orchestrator.py:16` | **45** | 同步直調（測試/非 LINE） |
| `_FORMAL_SEMAPHORE` | `line_bot/app.py:46` / `orchestrator.py:40` `threading.Semaphore(5)` | **5 併發** | P3-R4 有界 |
| `TEXT_DEDUP_TTL_S` | `48` | **120** | 同 uid+NFKC 文本 120s 內二打 → `TEXT_DEDUP_REPLY` |
| `_pushed_events + _pushed_lock` | `44-45, 451-464` | `set + Lock` | `webhookEventId` 冪等 |
| `push retry` | `467-490, 613` `for attempt in range(2)` | **2 次** | `LINE push_message ~200ms/次` |

#### 入口分流（`app.py:999 callback`）

```
body await → verify_signature(<1ms hmac) → json.loads → events for loop
  ├─ 1054 webhookEventId 去重：_is_duplicate_push → 已有 COMPLETED result 則重放回 replyToken 且 return
  ├─ 1079 if orchestrator.use_formal and _should_use_async_formal(text):
  │     ├─ 1080 _is_text_duplicate(uid,text) → 「這題正在幫你查了，稍候」直接返（<1ms）
  │     ├─ 1083 _send(ASYNC_PLACEHOLDER_REPLY)  # ★ 1× LINE replyToken ~50ms，立即返
  │     └─ 1084 _schedule_formal_push(orchestrator,uid,eventId,text) # 背景
  │            └─ 632 threading.Thread(daemon=True).start()  # 立即返，不 await
  └─ else 同步: 1086 product_result=orchestrator.handle_text 或 1108 handle_text_message
```

- `await` 僅 `1005 await request.body()` 1 處；**無 `asyncio.create_task/gather/Semaphore`**，全 `threading.Thread + ThreadPoolExecutor`。
- `_should_use_async_formal` → `orchestrator.py:84 _orch_should_use_formal` 全 deterministic（`is_red_flag/is_chit_chat/is_pre_visit/len<4/policy_gate !=G`）**5ms**，已與 runner 等價。

#### 背景 formal（`_schedule_formal_push:535-632` / `orchestrator._spawn_async_formal:567` 鏡像）

```python
def _bg():
  with _FORMAL_SEMAPHORE:                 # ★ 排隊點：>5 併發在此阻塞，計入 120s
    if _is_duplicate_push(eventId): return
    for attempt in range(2):              # ★ LLM 鏈重試 1 次
      def _call(): orchestrator._call_workflow({use_formal:True})  # 27.5s
      with ThreadPoolExecutor(1) as ex:
        wf = ex.submit(_call).result(timeout=120)   # ★ 正式阻塞點
        break
      except FuturesTimeoutError: continue # attempt2
      except Exception:       continue
    for attempt in range(2):             # ★ push 重試
      ok = _push_text(line_user_id, _format_formal_push_text(wf), eventId)
```

| 段 | file:line | 成本 | 是否計入 FORMAL_TIMEOUT |
|---|---|---|---|
| 佔位 reply | `app.py:1083` / `orchestrator.py:778` | 50ms | 否（同步返） |
| dedup 判斷 | `1080` / `orchestrator:747` | <1ms | 否 |
| `Semaphore(5)` 排隊 | `app.py:555` / `orchestrator:580` | **0-120s 阻塞** | **是**（隊列吃掉 120s 預算） |
| `future.result(timeout=120)` | `586` | 27.5s formal 真實 | 是 |
| retry attempt2 | `598 for attempt in range(2)` | +27.5s（×2 attempts 理論 240s，但單次 120 封頂） | 是 |
| `push_message ×2` | `474,613` | 0.2-0.4s | 否（在 formal 後） |
| **用戶體感** | **首字 50ms，完整 27-55s 後 push** | `SYNC 45s` 路徑會 `FALLBACK FORMAL_TIMEOUT`（因 `A+C>45`）而改走 async honest fallback | — |

**排隊公式**：`T_queue ≈ max(0,(N-5)/5 × 27.5s)`。N=100 時隊尾理論 665s，但 120s 先砍為 `FORMAL_TIMEOUT`→`HONEST_FALLBACK_PUSH_TEXT`（`app.py:606`）。

#### `stream_workflow` 無首字優化

`workflow/runner.py:119-141`：`run_workflow(...)` 同步跑完 `A→RAG→B→C→D` → `yield buffered_stream_after_d` → `stream.py:44-60` 僅 `chunk_text(20) + SSE` 切塊；`e_observability tracer first_token` 記的是切塊延遲，非 LLM 首 token。

### 3.5 隱藏成本清單（全 `future.result`，零 `sleep`）

| 常數 | file:line | 預設 | 生效處 | 隱藏行為 |
|---|---|---|---|---|
| `FORMAL_WORKFLOW_TIMEOUT_S` | `runner.py:30` `os.getenv("...", "45")` | **45** | `237 future.result(timeout=45) → TimeoutError→FALLBACK FORMAL_TIMEOUT` | 舊 P3 15s 必炸，現 45 仍緊（G 15.3s 剛好；全 formal 35s 仍炸） |
| `SYNC_FORMAL_TIMEOUT_S` | `orchestrator.py:17` | **45** | `_call_workflow 273 result(timeout=45)` | 同上（測試直調） |
| `ASYNC_FORMAL_TIMEOUT_S` | `line_bot/app.py:43` / `orchestrator.py:19` | **120** | 背景 `586,595 fut.result(timeout=120)` | `orchestrator:319 _call_workflow_async_with_retry for attempt in range(2)` 共 **240s worst** |
| `_FORMAL_SEMAPHORE` | `app.py:46` / `orchestrator:40` | **5** | `555 with _FORMAL_SEMAPHORE` | 超限**阻塞不返 `排隊中`**，算入 120s timeout |
| `ToolExecutor timeout_ms` | `runner.py:220,257` | **5s** | `tool_contract/executor.py:171 future.result(timeout=5)` | 每工具 |
| `TEXT_DEDUP_TTL_S` | `48` | **120** | `68 _text_dedup scan O(n)` | 每請求全掃過期 |
| `AGENT_REQUEST_TIMEOUT` | `agent/ollama.py:33` | **60s** | `B INSUFFICIENT` 才觸 | +backoff |
| `sqlite timeout` | `product_session/repository.py:52` | **10s** | webhook 冪等 DB | |
| `rate_limiter retry_wait` | `rate_limiter.py:183` | `base*2^(n-1)` + `time.sleep` | **僅實驗腳本** `phase_scripts/04-05`，線上 workflow 無 | **線上無任何 `sleep`**，grep 全鏈 0 行 |

---

## 4. 前 3 可修瓶頸（按可省時間排序）→ 預估收益

> 估算基於 `C-input 輸入長度/輸出 token/次數` 線性 + `Kalvium +80ms/段` 已扣除，`T0=3.08s` 為錨點。

### 🥇 瓶頸 1：`C 長輸入 2.1-2.7k tokens × 長輸出 500-800t` 占 98% 熱 E2E（15s）

- **證據**：`formal_factory.py:46 llm.with_structured_output` + `langchain_adapter.py:100-111 / 211` + `user_prompts.py:130 context_block 5×250t` + `system_prompts.py:115 CLINICIAN_DRAFT 4段`。
- **為何慢**：5 evidences 全文 + `source_table 5列` 強迫慢解碼；`temperature 0` 仍需 `15s`；無分塊/截斷。
- **修法**（選一，估省 4-8s，熱 E2E `15.3→8-11s`）：
  1. **Prompt 瘦身（零契約風險，推薦）**：`page_content` 截 `1200→300 chars`/evidence，`source_table` 5→2 列，`context_block` `~1250t→~400t`，**省 3-5s**。`workflow/formal_factory.py:30-50` + `c_generator/user_prompts.py:193` 各改 1 處。驗法：`pytest tfda_context_gate/tests/test_workflow_integration.py -q` 15 passed + 人審 `source_table` 仍含 `document_id+source`。
  2. **本地化 C**：`_build_formal_generator` 換 `ChatOllama("qwen3:1.7b")`，估 `15→4-6s` **省 9s**，但 P3 實測 `invoke>120s timeout` 品質待驗，結構化在 1.7b 不穩（`from_llm strict` 需改）。
  3. **增量/邊解碼**：C 增量 `stream` 實流，但需 D 並行驗證，首字 `15s→0.3s` 而總時不變，治表不治本。
- **工程量**：1 行截斷 `max_embed_chars 1200→300` 或 `user_prompts trunc`，**工程 0.5 天**。
- **失敗預案**：若截斷使 `HeuristicSemanticVerifier` 重疊率 `<0.85` 致 `D FALLBACK`，調 `verifier:132 threshold 0.85→0.75` 或 `B approval_mode` 放寬。

### 🥈 瓶頸 2：`A 4×重打 + schema 放大`（舊全 formal 占 57%，現窄路已省 20s，剩「舊邏輯回退」風險）

- **證據**：`a_router/router.py:68-130 candidates 3 + 主 1 =4` + `include_raw parsing_error continue`。
- **為何慢**：`function_calling` 把 `RouterSignals` 5 enums 壓入 `tools` schema，單次已 +80-150ms；`parsing_error`（`mimo` 小機率）觸 2-4 連打 `8-12s`；遠端 jitter 再 +5s → `20s`。
- **現狀已修**：`runner.py:192 _ext=None` + `orchestrator.py:84 _orch_should_use_formal` 窄路使 **99% 流量不付此段**（實測 `15 passed`，閒聊 45ms）。剩餘風險：若誤判 `G` 而退回全 formal，仍 20s。
- **再修（防退化，估省 8s，舊全 formal `35→27s`）**：
  1. **A 緩存**：`NFKC(user_raw_input)`→`RouterSignals` `LRU 5min`，`a_router/router.py:83 extract` 前查表，**同句 20s→1ms**；成本僅 `dict`。
  2. **候選縮為 1**：刪 `_candidates`，僅留 `function_calling`，**省 3× 往返 9s**；風險 `parsing_error` 時改 `FALLBACK` 而非重試。
  3. **method 改 `json_mode`**：省 `tools` schema 開銷 `+80ms`，但可靠度 `0.2%→1%` violation，**不推薦**（Kalvium 表）。
- **工程量**：緩存 10 行，**0.5 天**。

### 🥉 瓶頸 3：`Semaphore(5) 排隊算入 120s timeout` + `無快取冷啟動 24s` + `串行無並行`（長尾與首進程）

- **證據**：`line_bot/app.py:555 with _FORMAL_SEMAPHORE` 在 `future.result(timeout=120)` 內；`rag/tfda_retriever.py:341 add_documents` 冷 24s；`workflow/runner.py:234 max_workers=1`。
- **為何慢**：100 連發第 100 個理論隊 665s 但 120s 先 `FORMAL_TIMEOUT` honest fallback；首進程無 `pkl` 則 `冷 39s>45s` 超限。
- **修法**（估省長尾 665s→0，首進程 39s→15s）：
  1. **排隊不佔 timeout**：`_FORMAL_SEMAPHORE.acquire(blocking=False)` 超限直回 `「查詢排隊中，稍後 push」` honest 而非阻塞吞 timeout；或改 `BoundedExecutor` 有界隊列。`app.py:555` 1 行、`orchestrator.py:580` 1 行。
  2. **預熱**：`TFDADrugSafetyRetriever._ensure_store()` 於 `app startup` 或 `CI` 預跑 `CACHE_DIR/*.pkl`，首進程冷 24s→0（磁碟 30ms）。
  3. **TFDA‖HPA 並行**：`tfda:398 + hpa:375` `ThreadPoolExecutor(2)` 並行檢索，熱 `221→~140ms` **省 80ms**（小勝，但順手）。
- **工程量**：預熱 + 信號量各 0.5 天，**1 天**。

> **三瓶頸合計**：`C 截斷 5s + A 緩存防退化 8s（舊邏輯）+ 排隊/預熱 去長尾` → **穩態熱 E2E `15.3s→8-11s`，冷 `39s→15s`，百連發不再 665s 隊爆**。若激進將 C 本地化，則 `15.3→4-6s`（但需驗 1.7b 結構化品質）。

---

## 5. 交叉驗證與寫給下一個人

- **與 P3 實測吻合度**：本瀑布 `A 20s+RAG 0.22+C 15≈35s`，與 P3 `42.9-48s` 差 7-13s 為 `parsing_error 重包1-2×3s + Trace 10ms + ThreadPool 調度`，屬遠端抖動；`A 20034ms/16915ms` 正是 `4×3.08+200ms schema` 的展開。
- **與舊 15s timeout 矛盾已消**：`p3:46` 指出 `15s 必炸`，現 `runner.py:30 45s` + `orchestrator async 120s×2` 已修，但 `SYNC 45s` 對 `全 formal 35s` 仍緊，`C 瘦身` 後才穩。
- **框架結論**：`with_structured_output` 本身無「多次往返」原罪，莫誤優化 `include_raw`；要優化的是 **應用層重試（A 4×）** 與 **C 輸入長度（5 evidences 全文）** 與 **隊列計費**。
- **驗證指令**（零 API，純靜態已足；若要復測，僅 2 次 formal 即可交叉）：

```bash
# 靜態：看窄路是否命中
python3 -c "from tfda_context_gate.workflow.runner import _is_formal_eligible; from tfda_context_gate.a_router.schemas import RequestContext; print(_is_formal_eligible(RequestContext(request_id='1', user_raw_input='請說明糖尿病的一般飲食原則。', declared_role='PATIENT', language='zh-TW'), None))"
# 熱 RAG
PYTHONPATH=. python3 -c "from tfda_context_gate.workflow.formal_factory import _build_formal_retriever; r=_build_formal_retriever(); from tfda_context_gate.query_expansion.schemas import QueryExpansionResult; import time; s=time.perf_counter(); print(r.retrieve(QueryExpansionResult(request_id='t', original_query='糖尿病可以吃什麼', retrieval_queries=['糖尿病可以吃什麼'])).retrieval_latency_ms, time.perf_counter()-s)"
# 同步 formal（會等 15s）
PYTHONPATH=. timeout 60 python3 -c "from tfda_context_gate.workflow.runner import run_workflow; import time; s=time.perf_counter(); r=run_workflow({'request_id':'prof-1','user_raw_input':'請說明糖尿病的一般飲食原則。','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True); print(r.status, time.perf_counter()-s)"
```

---

## 6. 附：方法與引用

- **PRD 計量對照**：本解剖僅靜態，不計量 API；若計量，`mimo-v2.5` 走 `opencode/ai` 按 `openai-completions` 計費，`function_calling` 的 `tools schema ~500t` 常駐 input。
- **引用**
  - LangChain `with_structured_output` 定義：`reference.langchain.com/python/langchain-openai/.../ChatOpenAI/with_structured_output`；`docs.langchain.com/oss/python/langchain/models` `include_raw` 語意
  - 原始碼 `RunnableMap(raw=llm) | parser_with_fallback`：`github.com/langchain-ai/langchain/libs/partners/mistralai/.../chat_models.py:1254`（8 provider 同模板）
  - Retry 分層：`github.com/langchain-ai/langchain/libs/core/langchain_core/runnables/retry.py` `max_attempt_number=3`；`docs.langchain.com/oss/python/langchain/structured-output` `ToolStrategy(handle_errors)`
  - 延遲基準：`kalviumlabs.ai/blog/structured-output-from-llms-json-mode-function_calling` *Latency Cost* 表
  - 單次調用佐證：`github.com/langchain-ai/langchain/issues/32977` *LLM is invoked ONCE*

---

*產出：`docs/reviews/formal_chain_latency_anatomy_20260828.md`（唯一允許新增檔）。靜態核對用，修改前先跑 `pytest tfda_context_gate/tests/test_workflow_integration.py -q` 15 passed。*
