# P3 對抗審查報告 — 非同步推送架構（2026-08-27）

> 基準 commit：`d7aa5f0`（P3 正式實作）  
> 對照：`git diff 98d2393..d7aa5f0`（14 files, +1788 / -70）  
> 審查者：Sisyphus（對抗模式）  
> 架構：衛教問句（`_orch_should_use_formal==True` → 僅 `G_GENERAL_EDUCATION`）→ 1 秒內回 `ASYNC_PLACEHOLDER_REPLY` → 背景 `Thread(daemon=True)` 跑 formal（`ASYNC_FORMAL_TIMEOUT_S=120` + 重試 1 次）→ `push_message` 補送；快路（intake / 閒聊 / 紅旗）同步 <1s 不變  
> 方法：`PYTHONPATH=. python3 -m pytest -q` 193 passed；親跑 `ConversationOrchestrator(SQLiteProductSessionRepository(tempfile), workflow_runner=fake)` + `run_workflow` 雙路打擊，`/tmp/p3_adv_harness*.py` 實測錄證

---

## 總判定：**CONDITIONAL PASS（2 項需加固，建議放行前補強）**

| 項 | 判定 | 说明 |
|---|---|---|
| 1 競態與重複 | **部分 PASS** | 同 `event_id` 重送已防重；同文本不同 `event_id` 20 連發各推一次，無文本級去重，雙擊會重 push |
| 2 服務重啟 | **PASS（文件已揭露「不補」）** | `daemon=True` 無殭屍，in-memory 丟失符合說明 |
| 3 push 失敗 | **PASS** | 例外被捕獲不炸 worker，retry 上限 2 |
| 4 資源洩漏 | **部分 FAIL** | 無上限線程池，20 連發同時 40 線程，100+ 有耗盡風險 |
| 5 安全回歸 | **PASS** | 紅旗 5 條同步 <15ms 且非同步化，不經延遲 |
| 6 不變量 | **PASS** | 無 hardcode 模型，LINE_USE_FORMAL/快路/D-gate 皆守 |

> 若接受「雙擊重推可容忍」「20 併發內不控管」則 **PASS 放行**；若需嚴格去重與限流則 **BLOCKED** 需補 `P3-R1/R4`。

---

## 1. 競態與重複

### 1a. 連問兩次衛教問題（不同 `event_id`，同 `user_id` 同文本）

**實測**：
```python
r1 = orch.handle_text(event_id="race-1", text="請說明糖尿病的一般飲食原則。") # ASYNC_PENDING
r2 = orch.handle_text(event_id="race-2", text="請說明糖尿病的一般飲食原則。") # ASYNC_PENDING
# wait 5s
pushes == 2  # 各推一次「幫你整理了衛教重點…資料來源：TFDA_129」
```

**程式對照**：
- `orchestrator._is_async_narrow_eligible` → ` _orch_should_use_formal(text,None)==True ` → 各自 ` _spawn_async_formal ` 起一 `threading.Thread(daemon=True)`。
- 去重僅 `event_id` 級：`_is_duplicate_push(event_id)` 檢查 `_pushed_events`（in-memory `set` + `_pushed_lock`）與 `repository.get_webhook_event(event_id).result.get("pushed")`。**無** `(user_id, normalized_text, 120s window)` 級去重。
- `line_bot/app.py` 側 ` _is_duplicate_push ` 僅查本進程 ` _pushed_events `，不查 repo；兩側 set 不共享。

**判定：部分 FAIL（可用性風險）**
- 同 `event_id` 重送：**PASS**（見 1b）。
- 同文本不同 `event_id`：**雙推**，用戶雙擊或網路重試會收兩則 push。若 UI 未做客戶端防抖，體感為重複騷擾。規格未明此為可接受，建議補文本級去重（見修正清單）。

### 1b. 同一問題 120s 內重複問（防重機制）

**實測同 `event_id` 重送（LINE webhook retry）**：
```python
r  = orch.handle_text(event_id="dup-evt", text="請說明糖尿病的一般飲食原則。") # ASYNC_PENDING, pushes→1
r2 = orch.handle_text(event_id="dup-evt", text="請說明糖尿病的一般飲食原則。") # status=ASYNC_PENDING replayed=True, pushes仍1
```
`repository.claim_webhook_event` + `complete_webhook_event(result={"pushed":...})` 保證同 `event_id` 冪等；`_push_with_retry` 內 `if _is_duplicate_push(event_id): return False` 阻第二推。

**判定：PASS（event_id 級）**
- 但**文本級**無去重，前述雙擊仍雙推。若文件承諾「120s 內同一問題不重推」，則不滿足。

### 1c. 交叉驗證
- `intake` 活躍時 `請說明…飲食原則` 走 `SIDE_ANSWER` **不** async（`_is_async_narrow_eligible` 首行 `if _is_intake_active(session,text): return False`）→ **PASS**，未互相覆蓋。

---

## 2. 服務重啟

**實測**：
- 背景任務以 `threading.Thread(target=_background, daemon=True).start()` 啟動；內部每次 `with ThreadPoolExecutor(max_workers=1) as ex: ex.submit(_call).result(timeout=120)`。
- `daemon=True` 保證 `uvicorn` 主線程退出時不阻塞重啟，無殭屍 thread 拖垮重啟（`threading.enumerate()` 見 daemon 1 條）。
- `orchestrator._pushed_events` 僅 in-memory `set`，註解明載：`# Service restart loss is acceptable (in-memory set only, documented).` 重啟期間 in-flight formal 丟失，符合文件「不補」。

**判定：PASS**
- 已如文件所述不補，且無殭屍。若需「重啟後補」則須將 in-flight 持久化（DB queue），現狀可接受。

---

## 3. push 失敗（LINE API 掛掉）

**實測**：
```python
def failing_push(self, u, t): raise Exception("LINE API down")
orch.handle_text(event_id="pushfail-evt", text="請說明…飲食原則。")
# background: _push_with_retry → for attempt in range(2): push_sender → exception → logger.warning → retry
# fail_count == 2（push 重試上限 2）
# 第二次失敗後外層 _schedule_formal_push 仍 try 備援 honest fallback push（亦失敗則吞）
# 主線程 2.5s 後仍可 handle_text(event_id="pushfail-evt2") → ASYNC_PENDING（worker 未炸）
```

**程式對照**：
- `line_bot/app._push_text` / `orchestrator._push_with_retry` 皆 `for attempt in range(2): try: api.push_message(...) except Exception: logger.warning ... if attempt==0: continue else: return False` —— **抛例外不會炸 worker thread**。
- ` _spawn_async_formal._background ` 最外層 `try: ... except Exception: logger.warning("schedule formal push crashed…")` 二次兜底。
- workflow 側亦重試 1 次（`for attempt in range(2): try: fut.result(timeout) except FuturesTimeoutError/Exception: if attempt==0: continue`）。

**判定：PASS**
- 例外被隔離，retry 上限明確（workflow 2 + push 2），主線程不受影響。

---

## 4. 資源洩漏（20 連發灌入）

**實測**：
```python
# slow runner 1s，async timeout 3s，20 連發各不同 event_id 同文本
threads before: 1
threads after burst: 40  (delta 39 ≈ 20*daemon + 20*executor)
pushes after 6s: 20/20
threads after completion: 9  # 未完全回收（ThreadPoolExecutor 殘留約 8）
```

**程式對照**：
- 每次 `_spawn_async_formal` 起一 `Thread`，其內每次 attempt 起一 `ThreadPoolExecutor(max_workers=1)`。**無**全域 `Semaphore` / 有界 `Queue` / 限流器。
- `line_bot/app._schedule_formal_push` 同構（Thread + Executor）。併發數 = QPS × formal 耗時（120s 窗內累積）。

**判定：部分 FAIL（可擴展性風險）**
- 20 併發可撐過，100 併發將同時 200 線程 + 200 executor，易踩 `too many open files` / LINE 429 / `OLLAMA` 限流。
- 現狀 **無上限排隊**，規格未提背壓策略。

**修正建議**：見 § 修正清單 `P3-R4`。

---

## 5. 安全回歸（紅旗 5 條，必須同步即時，不可非同步化）

**實測 5 紅旗**（`use_formal=True`, `fake_success` runner）：
```
"我胸痛呼吸困難快昏倒"  → FALLBACK 0.015s async=False ✓
"我大量吐血"            → FALLBACK 0.009s async=False ✓
"我意識不清叫不醒"      → FALLBACK 0.006s async=False ✓
"我持續嘔吐反覆嘔吐"    → FALLBACK 0.007s async=False ✓
"我高燒不退三天"        → FALLBACK 0.007s async=False ✓
"胸痛冒冷汗現在好多了"  → FALLBACK（單調性，未被洗白）✓
"我沒有胸痛"            → ASYNC_PENDING（否定不誤攔）✓
```
`_orch_should_use_formal("我胸痛…")==False`、`_is_formal_eligible` 亦 `False`；`graph.a_node` 在 LLM 前 `if _is_red_flag(raw_input): return FALLBACK A_EMERGENCY`  deterministic abort，**不經** `LangChainSignalExtractor` / `RAG` / `C`。

**invariants 交叉**：
- `intake` 中紅旗仍同步（`system_risk_classification.level==RED_FLAG` 後 `_merge_risk` 單調，`_process_text` 首行即 abort）。
- 背景 formal 路徑下紅旗仍走同步快路，未被 `ASYNC_PLACEHOLDER_REPLY` 延遲（實測 `elapsed <1s` vs 衛教 `0.3s + push`）。

**判定：PASS（100% abort，0 延遲）**

---

## 6. 不變量

| 檢查 | 實測 | 判定 |
|------|------|------|
| **無 hardcode 模型** | `formal_factory._build_formal_generator` 以 `env_value("ROUTER_LLM_MODEL","opencode/mimo-v2.5")` 讀 `.env`，`fallback` 僅為 env 預設；`_build_formal_retriever` 以 `embedding_model="ollama/bge-m3:latest"` 為 env `OLLAMA_EMBED_MODEL` 預設，非寫死調用；`grep -rn mimo` 僅在 `env_value` 與 `if "mimo" in model.lower()` 分支 | **PASS** |
| **LINE_USE_FORMAL 不回退** | `orchestrator.__init__(use_formal=None)` 時：若 `PYTEST_CURRENT_TEST` 存在預設 `False`，否則 `LINE_USE_FORMAL` env；`test_orchestrator_formal_switch.py` 驗 `use_formal` 預設與顯式 `True` 切換；`LINE_USE_FORMAL_DEFAULT=true` | **PASS** |
| **快路行為不回退** | `intake`（`為自己整理`→`NEEDS_CLARIFICATION`）、閒聊（`你可以跟我說什麼？/help/？`→`COMPLETED` <1s）、紅旗 <1s 皆同步，非 `ASYNC_PENDING` | **PASS** |
| **push 經 D gate** | `workflow/runner.run_workflow(use_formal=True)` 無論 `_eligible_for_formal` 真假皆走 `build_workflow_graph` 的 `D` 節點；`_format_formal_push_text` / `_format_push_answer` 僅在 `workflow.status==COMPLETED` 時回 `final_response+來源`，否則回 `HONEST_FALLBACK_TEXT`（`B_INSUFFICIENT/FORMAL_TIMEOUT/C_FAILURE/SYSTEM_DEPENDENCY/B_UNSAFE`）；實測 C 故意 `B_INSUFFICIENT` 時 push 為 honest，不洩露 `可自行加藥` 原文 | **PASS** |

---

## 修正清單（不動碼，依優先序）

### P0（若需嚴格不重推 / 不超線程則必修）
- [ ] **P3-R1 文本級去重**：在 `_spawn_async_formal` / `_schedule_formal_push` 前加 `(user_id, normalize(text), 120s)` 去重表（in-memory `dict` + TTL 或 DB `push_dedup` 表），同文本 120s 內第二請求回「已在查詢中，完成後會推送」而非再起線程。注意與 `event_id` 去重正交。
- [ ] **P3-R4 有界併發**：為 formal 背景引入 `BoundedExecutor`（`Semaphore(5)` 或 `ThreadPoolExecutor(max_workers=5)` 全域共享），超出時排隊或回「查詢排隊中」，避免 100+ 連發線程爆炸；`ASYNC_FORMAL_TIMEOUT_S` 內排隊時間計入 timeout。

### P1（建議）
- [ ] **P3-R2 跨進程去重**：`line_bot/app._pushed_events` 與 `orchestrator._pushed_events` 為進程內 set，多實例部署不互通；將 `push_dedup` 改以 `repository`（DB 唯一鍵 `event_id` + `pushed` 欄）為主，或用 Redis。
- [ ] **P3-R3 可觀測性**：為背景 push 加 `trace` / `logger.info(event_id, push_ok, latency)` 與 `health` 指標（in-flight count, queue depth），便於壓測時定位 40 線程來源（daemon vs executor）。
- [ ] **P3-R5 文件對齊**：在 `docs/plans/p3_formal_mode_plan` 補「重啟不補」與「無文本去重」的已知限制，與本報告一致，發版前向使用者揭露。

---

## 附錄：重現指令

```bash
# 差異與全量
git diff 98d2393..d7aa5f0 --stat
git diff 98d2393..d7aa5f0 -- line_bot/app.py tfda_context_gate/line_orchestration/orchestrator.py tfda_context_gate/workflow/runner.py | head -n 400

# 測試
PYTHONPATH=. python3 -m pytest -q  # 193 passed

# 本報告六項打擊（節選）
PYTHONPATH=. python3 /tmp/p3_adv_harness.py   # 1 競態 / 2 重啟 / 3 push失敗
PYTHONPATH=. python3 /tmp/p3_adv_harness2.py  # 4 20連發 / 5 紅旗 / 6 不變量
PYTHONPATH=. python3 /tmp/p3_adv_harness3.py  # 6c D-gate / 否定 / intake覆蓋
```
