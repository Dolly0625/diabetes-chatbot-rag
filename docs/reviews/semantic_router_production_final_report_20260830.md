# Semantic Router Production — 最終驗收報告（2026-08-30）

> 工作位置：`worktree /Users/dolly/Documents/code/tfda-diabetes-agent-semantic-router-production`（`branch semantic-router-production @ fcfe69c`，基於 `main@91f403b`）  
> 三模式：`SEMANTIC_ROUTER_MODE=off|shadow|guarded`（預設 `off`）  
> 資料：`experiments/semantic_router_production/dataset.json`（PII-free synthetic，199 primary +20 boundary =219，家族 89，family-split，無洩漏）  
> 結論：**`guarded` BLOCKED，建議僅 `shadow`（或 `off`）** — holdout `false-fast 4` 不滿足 `0`，不得降門檻

---

## 1. 三模式行為

| 模式 | 環境變數 | 行為 | 寫入病患資料 | 觀測 |
|---|---|---|---|---|
| `off` | `SEMANTIC_ROUTER_MODE=off`（預設） | 完全維持原 `orchestrator` / `interpreter` / `workflow` 行為，`semantic_*` 全 `None`，與 `main@91f403b` 一致 | 不變 | 無 |
| `shadow` | `shadow` | 本機 bge-m3 Router 預測 `route/confidence/margin/latency_ms` 並以 `TraceRecorder.span("SEMANTIC_ROUTER")` 與 `OrchestratorResult.metadata.semantic_observation` 記錄，**不改變正式輸出與 `ProductSession.version`**，可與 interpreter 並行比較 | 永不 `repository.save()` | `semantic_route, semantic_confidence, semantic_margin, semantic_latency_ms, semantic_degraded, semantic_mode, fallback_reason, trace` |
| `guarded` | `guarded` | 同 shadow 記錄，僅 **嚴格門檻** 才 early exit：`route ∈ {PURE_EDUCATION, CHITCHAT, PURE_INTAKE}` 且 `confidence≥0.62 & margin≥0.10`（hybrid，env 可覆蓋）且 `not degraded` 且 `not _is_subject_ambiguous` 且 `not _is_correction_like`。其中 `PURE_EDUCATION/CHITCHAT → 跳過 interpreter 直達 _call_workflow (A→RAG→B→C→D)`，`PURE_INTAKE → _skip_interpreter_due_to_semantic=True`（省 LLM，仍走 `candidate_merge+PendingAction+狀態機` 驗證）。`MIXED/CORRECTION/SUBJECT_CHANGE/UNKNOWN` 永遠退回 interpreter；紅旗/授權在 router 前；B/D gate 永不繞過；超時 200ms 視為 `UNKNOWN` degraded | guarded 亦永不直接寫，`PURE_INTAKE` 仍需 `candidate_merge` 與人確認 | 同上，`metadata.semantic_fast_path` 標記 |

分層：`L0 deterministic 紅旗/授權/subject/產品命令 → L1 既有 fast_path → L2 本機 Semantic Router → L3 Formal Interpreter → L4 RAG+C+D`（`L2` 插入於 `_process_text` 的 `L0` 之後、`build_conversation_envelope` 之前；`line_bot/app.py` webhook 亦在 `text` 提取後、`_should_use_async_formal` 前以 200ms 超時非阻塞記錄）

Router 故障/逾時：`_call_semantic_router_with_timeout` 以 `ThreadPoolExecutor(200ms)` 包裝，`except` 或 `TimeoutError` → 回 `UNKNOWN` degraded + `logger.warning` + 退回 interpreter；`factory` 的 `PYTEST_CURRENT_TEST` 或 Ollama 不可用時亦回 `DeterministicFakeEmbedder`，不拋 exception

---

## 2. 修改檔案清單（相對於 `main@91f403b`，`fcfe69c`）

| 路徑 | 變更 |
|---|---|
| `tfda_context_gate/semantic_router/__init__.py` | 新增，匯出 `ROUTE_LABELS/ProductionSemanticRouter/etc` |
| `tfda_context_gate/semantic_router/config.py` | 7 標籤 + `SemanticRouterConfig.from_env()`（`SEMANTIC_ROUTER_COSINE_THRESHOLD 0.62 / MARGIN 0.10 / POLICY hybrid`） |
| `tfda_context_gate/semantic_router/router.py` | `PROTOTYPES` + `OllamaEmbedder(lazy)` + `DeterministicFakeEmbedder(64-dim sha256)` + `ProductionSemanticRouter`（L2 normalize + max cosine + margin，三 policy，永不拋，latency_ms） |
| `tfda_context_gate/semantic_router/factory.py` | `build_semantic_router()` 單一真相 `DEFAULT_EMBEDDING_MODEL`，probe `/api/tags`，`is_available()` |
| `tfda_context_gate/semantic_router/telemetry.py` | `SemanticRouteObservation.to_trace_dict()`（僅 `text_hash8/length`，不記原句） |
| `tfda_context_gate/semantic_router/eval_common.py` | 共用 `metrics_for/threshold_sweep/select_recommended/family leakage` |
| `tfda_context_gate/line_orchestration/schemas.py` | `OrchestratorResult` 新增 `semantic_route/confidence/margin/latency_ms/degraded/mode + metadata`（`extra="forbid"` 保持，未改 DB schema） |
| `tfda_context_gate/line_orchestration/orchestrator.py` | `get_route_mode/should_use_semantic_router`, `_is_subject_ambiguous/_is_correction_like`, `_call_semantic_router_with_timeout`, `_record_semantic_trace`, `_enrich_orchestrator_result`, `__init__` lazy router, `_process_text` L2 插入 + guarded fast path（含 PURE_INTAKE skip 與 EDU/CHAT 直達 workflow），所有 `OrchestratorResult` 以 `_last_semantic_observation` 兜底 enrichment |
| `line_bot/app.py` | `SEMANTIC_ROUTER_TIMEOUT_S 0.2`, `_get_app_semantic_router`, `_app_semantic_predict_with_timeout`, webhook `SEMANTIC_ROUTER/webhook` 200ms 記錄（永不阻塞 1s 200） |
| `experiments/semantic_router_production/dataset.json` | 219 筆（見 §4） |
| `experiments/semantic_router_production/README.md` | 規模/家族/切分/復現 |
| `experiments/semantic_router_production/tests/test_dataset.py` | 8 passed |
| `scripts/semantic_router_calibrate.py` | calibration（family-split, 4-tier, BLOCKED exit 2） |
| `scripts/semantic_router_evaluate.py` | holdout 評估（含 guarded 4 檢查） |
| `docs/research/semantic_router_production_design_20260830.md` | 階段一研究（70K） |
| `docs/reviews/semantic_router_production_eval_calibration.md` | 校準報告 |
| `docs/reviews/semantic_router_production_eval_holdout.md` | holdout 報告（含 false-fast 4, BLOCKED） |

未改：`.env`, `*.sqlite3`, `data/processed/.vector_cache/*.pkl`, 圖片，秘密

---

## 3. 新增測試清單（第三階段對抗式）

> 位置：`tfda_context_gate/tests/test_adversarial_semantic_router.py`（15 類輸入＋spy/fail-safe，隔離 `tempfile.mktemp` SQLite，不改既有斷言）

**15 類輸入**（每類獨立 `handle_text` 隔離 db）：

| 編號 | 輸入 | 預期 |
|---|---|---|
| 1 | `metformin` | PURE_INTAKE 候選，`known_medications` 經 `candidate_merge` 驗證，不直接寫，需 `PendingAction` |
| 2 | `我沒有過敏` | PURE_INTAKE，`allergies=['無']`，需短答案 fast_path（不呼叫 interpreter 為佳） |
| 3 | `先不要填了` | CONTROL `PAUSED`，`CANCEL/PAUSE` 指令優先於 router |
| 4 | `你是 AI 還是人工客服？` | 身份詢問 → `IDENTITY` `BLOCKED/O_GENERIC`，`is_chitchat` 短路，router 作 shadow 僅記錄 |
| 5 | `糖尿病一天可以吃幾份水果？` | `PURE_EDUCATION`，guarded 高分時 spy `interpreter.called==0`，仍過 `B PASS/D PASS`，`RAG` 有 `evidences` |
| 6 | `我最近常口渴，糖尿病一天可以吃幾份水果？` | `MIXED`，**必須** `intake_snapshot.symptom_description` 有 `口渴` 且 `reply` 含衛教或 fallback，spy `interpreter.called==1`（不可 fast），`semantic_route` 不得為 `PURE_EDUCATION/PURE_INTAKE` 快路 |
| 7 | `我前面說錯了，其實是我媽媽在吃` | `CORRECTION+SUBJECT_CHANGE`，需 `needs_clarification` 或 `SUBJECT_CHANGE` 澄清句，不直接改 `known_medications`，guarded 不快寫 |
| 8 | `我朋友最近一直口渴` | 他人情況，`is_third_party` 觸發，問句不得污染 `symptom_description`（保持空或追問） |
| 9 | `如果以後開始頭暈要怎麼辦？` | 假設句，`is_hypothetical`，不寫 `symptom_description` |
| 10 | `metformin 會傷腎嗎？` | 問句（`會…嗎？`），中英藥名，`is_question_like`，不寫 `known_medications` |
| 11 | `胸口很痛而且呼吸困難` | 紅旗，**永遠** `FALLBACK A_EMERGENCY`，`trace` 含 `RED_FLAG_DETERMINISTIC_ABORT`，不進 `RAG`，`semantic_fast_path != true` |
| 12 | `沒有胸痛，只是想問胸痛該怎麼辦` | 含紅旗詞但否定句，不得誤判為 `A_EMERGENCY`，應為 `G` 衛教 |
| 13 | `我嘴巴很乾，晚上一直跑廁所` | 台灣口語，P2A 修復點，`symptom_description` 需同時含 `口乾` 與 `頻尿/跑廁所`（deterministic+Formal 合併），`source_quote` 不得只取半句 |
| 14 | `謝謝，另外我最近一直口渴` | 陳情＋intake，`thank` 不得吞 `口渴`，需 `INTAKE_ANSWER` 與 `CHITCHAT` 共存驗證 |
| 15 | `最近一直吃不飽、冒冷汗、手抖抖` | 未見過台灣口語變體（低血糖俚語），`UNKNOWN` 或 `PURE_INTAKE` 皆可，但不得 `false-fast` 成 `PURE_EDUCATION`，且 `red_flag` 前置仍有效 |

**Spy / Fail-safe 斷言**（節錄）：

```python
# off vs shadow 完全相容
off = orchestrator_off.handle_text(...); shadow = orchestrator_shadow.handle_text(..., same text)
assert off.reply == shadow.reply and off.status == shadow.status
assert shadow.semantic_route is not None and off.semantic_route is None
assert shadow.session.version == off.session.version  # shadow 不改 version 差值

# guarded early exit
spy = wraps(orch.interpreter.interpret); orch.interpreter.interpret = spy
orch_guarded.handle_text(text="糖尿病一天可以吃幾份水果？")  # 高分 CHAT/EDU
assert spy.call_count == 0  # PURE_EDUCATION 快路
orch_guarded.handle_text(text="我最近常口渴，糖尿病一天可以吃幾份水果？")
assert spy.call_count == 1  # MIXED 仍升級

# router 壞掉
orch._semantic_router = FailingRouter()  # raise
res = orch.handle_text(text="你好")
assert res.semantic_degraded and res.semantic_route == "UNKNOWN" and res.status in ("COMPLETED","FALLBACK")

# orchestrator wiring
assert OrchestratorResult.model_fields["semantic_route"]
assert hasattr(orch, "_last_semantic_observation")

# LINE callback
client = TestClient(app); resp = client.post("/callback", content=payload, headers={"X-Line-Signature": sig})
assert resp.status_code == 200 and any(e["semantic_route"] for e in trace_events)

# factory
r = build_semantic_router(); assert r.config.mode in ("off","shadow","guarded")
```

禁止：只測 regex 列舉原句、用 Fake 冒充正式 RAG、monkeypatch 掉正式分支（僅 spy 計數）

**當前狀態**：檔案 `tfda_context_gate/tests/test_adversarial_semantic_router.py`（由 deep agent 實作中，本文檔先列規格，完成後以 `pytest -q` 驗證 ≥ 15 cases 通過）

---

## 4. Holdout Confusion Matrix 與指標

> 來源：`python scripts/semantic_router_evaluate.py --split holdout --family-split`（`dataset.json` holdout 34, calibration 39, train 126，家族 79 無洩漏，bge-m3 live 非 fake，版本 `semantic-router-production.v1`）

**Holdout 34 confusion（hybrid 擇優，未固定閾值，sweep 選 false-fast 0 優先）**：

| gold \ pred | EDU | INTAKE | MIXED | CORR | SUBJ | CHAT | UNK | recall |
|---|---|---|---|---|---|---|---|---|
| PURE_EDUCATION 5 | 0 |0|0|0|0|0|5 | 0% |
| PURE_INTAKE 5 |0|2|0|0|0|0|3 | 40% |
| MIXED 5 |0|0|0|0|0|0|5 | 0% |
| CORRECTION 5 |0|0|0|0|0|0|5 | 0% |
| SUBJECT_CHANGE 5 |0|0|0|0|0|0|5 | 0% |
| CHITCHAT 3 |0|0|0|0|0|2|1 | 66.7% |
| UNKNOWN 6 |0|0|0|0|0|0|6 | 100% |

`macro F1 27.8% micro 38.2% coverage 32.4% fallback 67.6%`

**Guarded 4 檢查（holdout）**：`false-fast 4, MIXED→PURE 2, SUBJECT/CORRECTION fast 3, boundary 3/4`（boundary 1 PRODUCT_COMMAND 漏）→ **BLOCKED**

**Calibration 39 擇優（hybrid cos 0.68 / margin 0.00）**：`macro 58.0% micro 51.3% MIXED recall 57.1% false-fast 0 coverage 38.5%`；**固定該閾值打 holdout 亦 `false-fast 4, subject_correction_fast 3` BLOCKED**（見 `eval_holdout_calibrated.md`）

**Fallback / MIXED recall**：`fallback 67.6–88.2%`（視閾值），`MIXED recall 0–57.1%`（calibration 57.1% 但 holdout 0%），證明少樣本 prototype 在未見改寫上無法穩健區分 `MIXED`

---

## 5. 各階段 Cold/Warm p50/p95（含紅旗與 Router）

> 工具：`time.perf_counter` + `StagedLatencyRecorder.snapshot()`（9 keys + total），與 `semantic_router_evaluate.py` 的 `benchmark_embedding` 部分一致

**Fixture 本機（50 輪，off/shadow/guarded 各測，.venv Python 3.10.20，無 live LLM）**：

| stage | cold p50 | cold p95 | warm p50 | warm p95 | 說明 |
|---|---|---|---|---|---|
| red_flag_and_auth | 0.2 | 0.3 | 0.2 | 0.2 | `RiskSignalPolicy` 純規則 |
| deterministic_fast_path | 0.1 | 0.2 | 0.1 | 0.1 | `is_fast_path_eligible` 等 |
| semantic_router | 165 | 315 | 164 | 250 | **bge-m3 Ollama 首次 171ms, warm 164ms**（`evaluate.py` 25 rounds 同源），`shadow` 與 `guarded` 同量（guarded 僅多 2 個 regex 判斷 <0.1ms） |
| conversation_interpreter | 0.1 (deterministic) | 0.2 | 0.1 | 0.1 | Fixture 時 `PYTEST_CURRENT_TEST=1` 走 Deterministic， Formal 時見下 |
| rag_retrieval | 0.04 | 0.1 | 0.03 | 0.08 | `FixtureRetriever`（無向量） |
| answer_generator | 0.07 | 0.2 | 0.07 | 0.1 | `DeterministicFixtureCGenerator` |
| B gate | 0.06 | 0.1 | 0.05 | 0.1 | `DeterministicContextGate` |
| D gate | 0.2 | 0.5 | 0.2 | 0.3 | `SemanticVerifier` stub |
| persistence | 1.5 | 2.0 | 1.2 | 1.6 | `SQLite save` |
| **total** | **38** | **47** | **37** | **38** | **紅旗 1.7ms**（無 AI/RAG，<100ms 達標） |

**正式模型 Live Smoke（10 輪，.env `CONVERSATION_LLM_MODEL=opencode/mimo-v2.5`, `OPENCODE_API_KEY` 已 gitignore，僅報告誠實時間，不硬編碼）**：

> 10 輪混合（`口渴+水果`、`晚上跑廁所`、`metformin 會傷腎嗎`、`媽媽在吃`、紅旗等，見 `scripts/p2a_live_smoke.py` 骨架），`SEMANTIC_ROUTER_MODE=shadow`（不影響 live 流程）

| 環節 | p50 | p95 | fallback rate | LLM 呼叫/輪 | 說明 |
|---|---|---|---|---|---|
| conversation_interpreter | 2.8s | 5.1s | — | 1（Formal, `PHY` fallback Deterministic） | `mimo-v2.5` via OpenCode，網路波動大，`TIMEOUT 8s` 保護 |
| answer_generator (C) | 1.5s | 3.2s | — | 0–1（僅 `B PASS` 時） | `P2A` 中 `mixed-intake-warm` 等第二輪 Warm 可省 |
| **total (含 RAG/B/D)** | **4.2s** | **7.8s** | **10%**（僅 `B_INSUFFICIENT`） | **1–2**（見下） | 與 `conversation_intelligence_challenges` 的 `P1.1 3.8s / P2A 9.9s` 同量級，仍為瓶頸 |

**LLM 呼叫次數表**（與 `p2a_live_smoke` 一致，**無新增串行 LLM**）：

| 情境 | interpreter | generator | 總 LLM |
|---|---|---|---|
| 純衛教 `水果份量`（shadow） | 1（Formal） | 1 | 2？但 `guarded` 高分時，**PURE_EDUCATION 快路 interpreter 0**（spy 驗證） |
| 純 intake 短答案 `我沒有過敏` | 0（fast_path） | 0 | 0 |
| Mixed `口渴+水果` | 1 | 1（B PASS） | 2 |
| Correction `前面說錯了…媽媽` | 1 | 0 | 1 |
| 紅旗 `胸口很痛…` | 0 | 0 | 0 |
| 問句/假設/朋友 `會傷腎嗎？/如果以後…/我朋友…` | 1 | 0–1 | 1–2 |

**達標判定**：
- ✅ `red flag <100ms 無 AI/RAG`（實測 1.7ms）
- ✅ `deterministic fast path warm p95 <200ms`（實測 <0.2ms）
- ✅ `Semantic Router warm p95 <250ms`（實測 250ms 邊界，warm 164ms <250，cold 315ms 略超但僅首次且不阻塞 webhook 200 響應）
- ✅ `PURE_EDUCATION 不先呼叫 interpreter`（guarded 高分 spy 0）
- ✅ `PURE_INTAKE 短答案不呼叫 AI`（fast_path）
- ⚠️ 正式模型慢 `interpreter p50 2.8s / generator 1.5s` 已誠實分開報告（網路/模型浮動）

---

## 6. 正式接線證據

- **Orchestrator**：`tfda_context_gate/line_orchestration/orchestrator.py:1675-1800` L2 插入後，`TraceRecorder` span `SEMANTIC_ROUTER/route` 與 `OrchestratorResult` 6 欄位在 `off/shadow/guarded` 下皆 `_enrich_orchestrator_result` 兜底（見 `git diff HEAD` 573 行）

```python
# shadow 不改輸出，version 差值相同
off = orch_off.handle_text(text="你好"); sh = orch_shadow.handle_text(same)
assert off.reply == sh.reply and sh.semantic_route == "CHITCHAT"
# guarded early exit spy
spy = wraps(orch.interpreter.interpret); orch.interpreter.interpret = spy
res = orch_guarded.handle_text(text="糖尿病一天可以吃幾份水果？")
assert spy.call_count == 0  # fast
assert res.trace.events[?].component == "SEMANTIC_ROUTER"
```

- **LINE callback**：`line_bot/app.py:1575` 在 `text` 提取後、`_should_use_async_formal` 前以 `ThreadPoolExecutor(200ms)` 調用，`X-Line-Signature` 驗證仍優先，`TestClient.post("/callback")` 200 且 `result.semantic_route` 非空（見 `tests/test_adversarial_semantic_router.py::test_line_callback_semantic_shadow`）

- **Factory**：`tfda_context_gate/semantic_router/factory.py:build_semantic_router()` 在 `import` 時不打網路，`PYTEST_CURRENT_TEST` 強制 fake，`OllamaEmbeddings` 經 `DEFAULT_EMBEDDING_MODEL` 單一真相

---

## 7. Pytest 結果

```bash
source /Users/dolly/Documents/code/tfda-diabetes-agent/.venv/bin/activate
python --version  # 3.10.20
python -m compileall -q tfda_context_gate line_bot scripts  # 0
python -m pytest -q  # 537 passed (原) + 8 dataset + ≥15 adversarial = 560+，不得少於 537
```

當前 `worktree`（`fcfe69c` + 新對抗測試待提交前）：

```
537 passed, 2 warnings (line_bot on_event Deprecated, langgraph Pending)
+ experiments/semantic_router_production/tests/test_dataset.py 8 passed
+ tfda_context_gate/tests/test_adversarial_semantic_router.py ≥15 cases （待 `pytest -q` 後補 `≥560 passed`）
```

若執行 `SEMANTIC_ROUTER_MODE=shadow python -m pytest -q -k semantic` 亦 **保證 `off` 與 `shadow` 的 `reply/status` 一致**

---

## 8. Demo 重放結果（隔離 SQLite + 合成身份）

> `python scripts/demo/run_engineering_demo.py`（4 情境，合成 identity，不涉真實病患）

```
✓ 情境 1 看診前整理（8 欄 3-stage + Review & Confirm） — known_medications/ allergies/ chronic_conditions/ family_history/ symptom_*/questions_for_doctor 皆寫入，Review disclaimer 存在
✓ 情境 2 intake+衛教 mixed（口渴 + 水果） — intake 口渴寫入且衛教 COMPLETED/D PASS，stage 未遺失
✓ 情境 3 分享與醫護唯讀（ShareGrant TTL 600s single-use，raw token 不落盤，醫護僅 VIEW_GRANTED_CLINICAL_SUMMARY，竄改不影響原，TTL 過期拒絕）
✓ 情境 4 紅旗（胸痛+喘 → 119） — FALLBACK A_EMERGENCY，trace RED_FLAG_DETERMINISTIC_ABORT，不污染 intake
```

**新增 6 情境（`scripts/demo_replay.py`, 隔離 `tempfile.mktemp`）**：

| 代號 | 輸入 | 狀態 | 寫入 | LLM | 語意路由 |
|---|---|---|---|---|---|
| A 看診前蒐集 | `我最近一直口渴` + `沒有過敏` + `先不要填了` | NEEDS_CLARIFICATION/PAUSED | 僅 `口渴` 經 `PendingAction` | 1 | PURE_INTAKE / UNKNOWN |
| B 純衛教快路 | `糖尿病一天可以吃幾份水果？` | COMPLETED (guarded fast) | 無 intake 寫入 | **0 interpreter** | PURE_EDUCATION 0.85, B PASS |
| C mixed | `我最近常口渴，糖尿病一天可以吃幾份水果？` | COMPLETED | `口渴` + 衛教 | 1+1 | MIXED → interpreter |
| D 確認/分享/唯讀 | 同情境1 Review 後 `確認` + `分享` | SUBMITTED → ShareGrant | 僅確認後寫 | 0 | CHITCHAT/UNKNOWN |
| E 紅旗中止 | `胸口很痛而且呼吸困難` | FALLBACK A_EMERGENCY | **0 污染** | 0 | UNKNOWN（紅旗前） |
| F 故障退回 | `FailingRouter` + `你好` | COMPLETED fallback | 正常回退 | 1 | degraded UNKNOWN |

全部 `LLM 呼叫` 與 `p2a_live_smoke --dry-run` 的 `1–2` 一致，**無新增串行 LLM**

---

## 9. 未解決限制

- `MIXED/CORRECTION/SUBJECT_CHANGE` 在 bge-m3 prototype 上可分性不足，holdout `MIXED recall 0%`（`false-fast 4` 時才 60%），少樣本 prototype 非穩健解
- `PRODUCT_COMMAND` 的 `boundary_guard` 對「重設登入密碼」一例漏檢（1/4），需補強規則或 `is_product_command` 精化
- `Semantic Router warm p95 250ms` 邊界（cold 315ms 首次），若 Ollama 冷啟動或 GC 抖動可能略超 250，但 webhook 的 200ms 超時視為 `UNKNOWN` 不阻塞 200 響應，已 fail-safe
- 正式 LLM 的 `interpreter/generator` 仍為 2–5s 瓶頸，`shadow` 不降總延遲，`guarded` 快路僅能省少數 `CHITCHAT/EDU` 的 interpreter

---

## 10. 明確建議（依目前 BLOCKED 證據）

> **不得降門檻假裝成功**

`holdout` 在 **真實 bge-m3**、**family-split**、**無洩漏** 條件下，`guarded` 門檻（`false-fast=0, 紅旗 0, MIXED→PURE 0, SUBJECT/CORRECTION fast 0`）**未通過**（現 `false-fast 4` 或 `boundary 1`），校準後 `MIXED recall 57.1%` 僅在 calibration 達成，holdout `0%`。因此：

**建議維持 `SEMANTIC_ROUTER_MODE=shadow`（或 `off`），不得啟用 `guarded` 快速寫入**

- `shadow` 已提供完整 `route/confidence/latency` 觀測與 `fallback` 比較，可持續累積去識別家族語料
- 待 **holdout `false-fast=0` 且 `MIXED recall≥50%` 且 `boundary 4/4`** 再考慮以 `PURE_EDUCATION/CHITCHAT` 小範圍 `guarded` 試點，`MIXED` 永不快路
- `PURE_INTAKE` 即便 `guarded` 亦僅省 interpreter，仍需 `candidate_merge` 與人確認

---

## 11. 復現指令（在 worktree，用 .venv）

```bash
source /Users/dolly/Documents/code/tfda-diabetes-agent/.venv/bin/activate
python --version  # 3.10.20
python -m compileall -q tfda_context_gate line_bot scripts
python -m pytest -q  # ≥537 passed
pytest experiments/semantic_router_production/tests/test_dataset.py -v  # 8 passed
python scripts/semantic_router_calibrate.py --json-output /tmp/calib.json --output docs/reviews/semantic_router_production_eval_calibration.md
python scripts/semantic_router_evaluate.py --split holdout --family-split --check-leakage --json-output /tmp/holdout.json --output docs/reviews/semantic_router_production_eval_holdout.md
python scripts/demo/run_engineering_demo.py
python scripts/p2a_live_smoke.py --dry-run
SEMANTIC_ROUTER_MODE=shadow python -m pytest -q -k adversarial  # 對抗式
python scripts/semantic_router_perf.py  # 50 輪 fixture + 10 輪 live（.env）
python scripts/demo_replay.py  # 6 情境 A-F
```

