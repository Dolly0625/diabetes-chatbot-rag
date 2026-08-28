# P4 延遲瘦身 對抗審查報告 — 2026-08-28

> **審查員**：Sisyphus (adversary) — working tree 未 commit 全量 diff 人工驗證，不以測試通過為依據。  
> **計畫書**：`docs/plans/p4_latency_slimming_plan_20260828.md`（4 項：C prompt 瘦身、A 路由 LRU、Semaphore 非阻塞＋honest fallback、startup 預熱）  
> **硬約束**：B/D gates 不可繞過；紅旗 deterministic pre-check 同步在 LLM 之前；不存 raw image、hash PII；`use_formal=True` 3 場景 COMPLETED。  
> **審查指令**：嚴禁 commit、嚴禁改程式碼，只准寫報告檔案。所有發現附 `file:line` 證據。

---

## 0. 總覽與 VERDICT

| 項目 | 數量 | 嚴重 |
|---|---|---|
| 🔴 Blocker (P0) | 1 | 紅旗誤判：安全不變式破壞 |
| 🟠 High | 3 | LRU 污染、double QUEUED push + 阻塞 retry、dead truncation 路徑 |
| 🟡 Medium | 4 | Deprecated startup、跨 pod 冪等、TTL sweep 不完整、deterministic claim 全量導致 verifier 風險 |
| 🟢 Info/Low | 3 | 語句疊加、註解 drift |

### VERDICT: **FAIL**

**理由**：`tfda_context_gate/clinical_safety/risk_policy.py:18` 新增的 `胸口.*痛` greedy regex 與既有 `_has_affirmed_match` 4.5-row 互斥排除 + `但` 轉折 reset 互動，導致 `「沒有胸口痛但胸悶」` 這類否定後轉折肯定句被整句判為 `NO_DEFINED_SIGNAL`（實測 false negative），**直接違反計畫書 §6 不變式「紅旗 5 條回歸 100% abort」與 `docs/HANDOFF.md` 紅旗不可被削弱**。此缺陷在 5 條回歸的「否定／轉折」維度上可穩定復現，屬可被使用者措辭自然觸發的安全逃逸，必須阻擋合併。除此之外的 LRU 污染與 Semaphore 雙推亦為 High，需同批修復後重審。

若僅以「P4 4 項是否降低延遲」衡量，C 截斷與非阻塞已生效、193/15 測試全綠，功能面接近 `PASS-WITH-FIXES`；但安全維度一票否決，故整體 `FAIL`。

> 重審條件：修復 §2.1 greedy + §1.2 LRU key + §1.3 double QUEUED 三項 blocker/high 後，跑 `pytest` 193 + 5 條紅旗回歸（含下述轉折否定用例）全綠，24h 內可重審為 `PASS`。

---

## 1. 逐項核對 P4 4 項改動

### 1.1 C prompt 截斷 1200→300 — `PASS (with dead-code)`

**計畫要求**：`page_content` 1200→300/evidence，`source_table` 5→2 列，改動點 `workflow/formal_factory.py:30-50 + c_generator/user_prompts.py:193`。

**實際 diff**（3 處有效 + 1 處 dead）：

| 檔案 | 行號 | 實作 | 有效？ |
|---|---|---|---|
| `tfda_context_gate/c_generator/user_prompts.py:13` | `EVIDENCE_PAGE_CONTENT_MAX_CHARS = 300` | ✅ 有效 | 上游唯一進入 LLM 的 `context_block()` |
| `tfda_context_gate/c_generator/user_prompts.py:23-24` | `raw_content[:300]` in `context_block()` | ✅ 有效 | 2.1-2.7k tokens → 0.6-0.9k |
| `tfda_context_gate/c_generator/c_workflow_input.py:86,105` | `EVIDENCE_CONTENT_MAX_CHARS = 300` + `item.content[:300]` in `to_legacy_v2_case()` | ✅ 有效 | 上游，LangChain `evidence_aware_v2_user_prompt`/`clinician_draft_user_prompt` 皆經此路 |
| `tfda_context_gate/c_generator/deterministic_generators.py:294,371` | `source_table[:5]→[:2]` ×2 | ✅ 有效 | `ClinicianDraftGenerator._build_detailed_answer` 與 `generate` 各一處 |
| `tfda_context_gate/c_generator/system_prompts.py:149-162` | `source_table 5→2` 提示詞同步 | ✅ 有效 | 避免 LLM 自作主張吐 5 列 |
| `tfda_context_gate/workflow/formal_factory.py:9-36` | `_truncate_evidence_content` + `_apply_evidence_truncation` | ❌ **Dead code** | 定義但從未被 `runner.py`/`graph.py`/`langchain_adapter.py` 調用（見 §5.1）。計畫書寫的「改動點 `formal_factory.py:30-50`」事實上只有「定義」沒有「接線」。延遲收益仍靠上兩處達成，但屬敘事與實作不一致的偷工減料。 |

**證據**：`rg _apply_evidence_truncation` 僅命中 `formal_factory.py` 定義本身，`runner.py:24` 只引 `build_formal_*` 三函式，未引 truncation；`graph.py` C node 走 `to_legacy_v2_case` 已截斷，故 formal_factory 的 Helpers 成擺設。

**有效性實測**（direct tool）：
- `long_content= 'A'*2000 → CWorkflowInput 仍 2000 → to_legacy 300 → context_block 300`，LLM 輸入確已 300。
- 但 `DeterministicFixtureCGenerator`/`ClinicianDraftGenerator` 的 `generate()` 直接用 `item.content`（2000 全量）組 `claim`（見 `deterministic_generators.py:157` `item.content`），不經 `context_block`；CI 用 deterministic 路徑故 `HeuristicSemanticVerifier 0.85` 仍與全量 evidence 比對，通過；**formal LLM 路徑**的 claim 則基於 300 字 evidence，兩路徑差異未被測試覆蓋（見 §4.3）。

**是否調鬆 threshold**：否。`d_output_gate/verifier.py:147` 仍 `>=0.85`，計畫書「失敗預案 0.85→0.75」未被使用，`git diff` 無 verifier 改動（正確）。

**源表列數影響**：`D gate` 對 `CLINICIAN_DRAFT` 要求 `source_table 不可為空且最多 2 列`（`system_prompts.py:162` 已同步），但 `deterministic_generators.py:362-372` 現僅吐 2 列，吞證據多樣性；若 RAG 回 5 筆中後 3 筆才是關鍵風險證據，將被系統性丟棄，D 無法補救（見 §2.2 Info）。

---

### 1.2 A 路由 LRU 快取（TTL 5min）— `PASS-WITH-FIXES (High pollution bug)`

**計畫要求**：`NFKC(user_raw_input) → RouterSignals`，LRU 5min，`a_router/router.py:83` extract 前查表。

**實際**：`a_router/router.py:16-63` 全量實作正確 — `NFKC`（:27-31）、`TTL 300`（:22,40-43）、`MAXSIZE 128`（:21）、`OrderedDict+Lock`（:19,36,51-58）、`move_to_end` + `opportunistic expiry sweep`（:56-58）、`_clear_router_cache` for tests（:61-63）、在 `LangChainSignalExtractor.extract:132-137` 與 `179-180` 命中/寫入。

**File:line 逐條對**：

- ✅ NFKC：`router.py:29 unicodedata.normalize("NFKC", raw or "")`，與 `line_orchestration/orchestrator.py:52` & `workflow/intake_router.py:50` 一致。`ＡＢＣ→ABC` 實測通過（direct tool）。
- ✅ TTL 300：`router.py:22 _ROUTER_LRU_TTL_S = 300`，命中 `time.time()` 判斷 :40-43。
- ✅ thread-safe：`_router_lru_lock` 包裹 get/set/clear。
- ✅ dryRun 已改 dry? — plan 說 `router.py:83 extract 前查表`，實為 `:132-137`，序號漂移但語意一致。

**🔴 High：快取污染（cache key 缺 declared_role/language）** — `router.py:27-31 _router_cache_key(raw)` 僅對 `raw` 做 NFKC，未納入 `declared_role`/`language`/`policy_config`。同一句在 `PATIENT` 與 `HEALTHCARE_PROFESSIONAL` 下可能得不同 `RouterSignals`（LLM 對 `MEDICATION_CHANGE_REQUEST`/`GENERAL_MEDICATION_INFORMATION` 的敏感度隨角色而異），但快取回同一 `RouterSignals`，**跨角色回錯路由**。

- 復現：`RequestContext('請說明糖尿病飲食', PATIENT)` 與 `HEALTHCARE_PROFESSIONAL` 的 `NQF?` key 相同 → 第二人命中第一人快取。
- 風險：`G_GENERAL_EDUCATION` vs `M_MEDICATION_REFERRAL` 定性決定 `rag_allowed`，錯路由即 `BLOCKED` vs `COMPLETED` 差異，等效越權或過度放行。
- 另：`route_request()` 的 RuleBased 短句攔截（:390-398 `長<4`/`怎麼辦`）與 guard 層（:353-372）其實不進 `LangChainSignalExtractor.extract`，不受 LRU 影響，故 120s 內高頻短句仍打穿至規則層，不違背「同句 20s→1ms」宣稱（該宣稱僅對 formal LLM 路徑）。
- 另：`RISK_FLAGS: HIGH_RISK_NOT_EXCLUDED` 刻意只信硬規則（:417-423 `filtered_risks`），若 LLM 對 `PERSONALIZED_MEDICATION` 幻覺則被合併修正，LLM 錯誤被硬規則兜底，快取舊正確結果反而可能「釘住」舊錯誤，需配合 TTL。

**🟡 Medium：expiry sweep 不完整** — `_router_cache_get` 僅對命中 key 做單鍵 TTL 檢查（:40-43），不掃全表；`_router_cache_set` 才掃全表（:56-58）。若 traffic valley（長時間無新寫入），過期條目可駐留至 TTL+任意久，`_clear_router_cache` 僅 tests 調用，production 無定時 sweep。非 blocker，但與「5min」語意不精確。

**邊界**：
- `raw=None/""/空`：`_router_cache_key` `try` 分支回 `raw or ""`（:31），`extract:134-135` 仍可命中空字串快取，配合 `route_request:390-398` 短句攔截（長<4 直回 Q/ O），LLM 快取不會被空字串污染嚴重，屬可接受。
- 超長 input 10k：`[:300]` 截或原串作 key，無 bounded hash，key 長 10k 存 OrderedDict 值小，但 128×10k=1.2MB，仍可接受；建議 key 改 `hash(NFKC)` 限制記憶體，現狀 non-blocker。
- 測試隔離：`_clear_router_cache` 未被 `tests/test_*` 自動調用，若測試併發會 flake，但目前 `pytest 193 passed` 顯示暫未暴雷。

---

### 1.3 Semaphore 非阻塞 + honest fallback — `FAIL (High double-push + blocking retry)`

**計畫要求**：`_FORMAL_SEMAPHORE.acquire(blocking=False)` 超限直回「查詢排隊中，稍後推送」，消滅 665s 隊尾長尾。

**實際**：

| 檔案 | 行號 | 實作 | 判定 |
|---|---|---|---|
| `tfda_context_gate/line_orchestration/orchestrator.py:581-588,715-719` | `acquire(blocking=False)`→`QUEUED_FALLBACK_TEXT` 單推→`return` + `finally release` | ✅ 正確（單推，無 retry thread，release 正確） |  |
| `line_bot/app.py:573-577,671-675` | 同上 acquire+QUEUED | ✅ 正確 |  |
| `line_bot/app.py:580-589` | `_queued_retry` daemon thread | 🔴 **High bug** |  |
| `line_bot/app.py:123-137` / `orchestrator.py` | 兩處 QUEUED 文案 | 一致：`QUEUED_FALLBACK_TEXT = "查詢排隊中，稍後推送"` |  |

**🔴 High：`app.py:580-589 _queued_retry` 雙缺陷**

```python
# line_bot/app.py:580-589
def _queued_retry() -> None:
    time.sleep(2)
    with _FORMAL_SEMAPHORE:  # blocking=True (default)
        if _is_duplicate_push(event_id):
            return
        _push_text(line_user_id, QUEUED_FALLBACK_TEXT, event_id=event_id)
threading.Thread(target=_queued_retry, daemon=True).start()
```

1. **Blocking reintroduced** — `with _FORMAL_SEMAPHORE:` 為 blocking。100 連發隊尾者立即得 QUEUED（正確），但 100 個 retry threads 在 2s 後集體 `acquire( blocking=True)` 爭同一 Semaphore（5 槽各 120s 任務），每 thread 可阻塞 up to 120s，**thread leak + 與「非阻塞」宣稱相悖**。`orchestrator.py` 無此分支，為兩實現分叉的偷工減料。
2. **Duplicate QUEUED 違反 honest-once** — 主路徑 `577 _push_text(QUEUED, event_id)` 成功則 `485 _mark_pushed(event_id)`，retry 的 `_is_duplicate_push` 即 return，故正常流 **不 double**；但若主路徑 `_push_text` 因 `token=None`/`MessagingApi` 異常返回 `False`（`495-497` 未 mark），retry 會二次 `QUEUED`，**每 queued event 2 pushes**。對比 `orchestrator.py:584-588` 單推正確，形成不一致。
3. **LINE rate limit** — 100 隊列者 ×2 = 200 pushes 瞬發；LINE Push rate ~500/min/channel，burst 足以 `429`。雖 `event_id` 冪等抑止成功流 double，但失敗路徑仍 double，且 `app.py:485 _is_duplicate_push` 僅查記憶體 `_pushed_events`，跨 pod (`gunicorn --workers 4`) 不共享，**跨實例 duplicate**。

**Release 正確**：`app.py:671-675` 與 `orchestrator.py:715-719` `finally: release` 且 `not acquired` 早期 return 繞過 finally，無 double-release；`try/except` 包 release 防 `ValueError`。

**Honest vs Queued 契約**：`app.py:41-42` `HONEST_FALLBACK_PUSH_TEXT`（`B_INSUFFICIENT/FORMAL_TIMEOUT/C_FAILURE/SYSTEM_DEPENDENCY/B_UNSAFE` 五原因，見 `orchestrator.py:26 HONEST_FALLBACK_REASONS`）與 `QUEUED_FALLBACK_TEXT` 路徑分離正確：QUEUED 只在 `not acquired` 分支；HONEST 走 `_format_formal_push_text`（:461,646,650,666）。無 cross-contamination。

---

### 1.4 Startup 預熱 — `PASS-WITH-FIXES (Medium deprecation + silent)`

**計畫要求**：app startup 或 CI 預跑 `_ensure_store()`，冷啟動 24s→30ms。

**實際**：`line_bot/app.py:123-137`

```python
@app.on_event("startup")  # 123 deprecated in FastAPI 0.124.4 (本機)
def _preheat_vector_store() -> None:
    def _warm() -> None:
        try:
            from tfda_context_gate.rag.tfda_retriever import TFDADrugSafetyRetriever
            retriever = TFDADrugSafetyRetriever(embedding_model="ollama/bge-m3:latest")
            retriever._ensure_store()  # tfda_retriever.py:257 pickle load 30-80ms; miss則 24s Ollama build
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()
```

- ✅ **不擋 startup**：daemon + swallowed，`GET /health`（:892-908）不 gate preheat，可用性正確；符合「失敗不擋起動」。
- ✅ **確有收益**：`tfda_retriever.py:257-280` 有 `.vector_cache/*.pkl` 時 30-80ms，`--workers 4` 每 worker 各跑一次（4× I/O 可接受）。
- 🟡 **Deprecated**：`on_event` 在 `fastapi==0.124.4` emit `DeprecationWarning`（`pytest 193` 已警示：`line_bot/app.py:123: DeprecationWarning: on_event is deprecated`）。未來版移除後 preheat 靜默失效，改 `lifespan` (`@asynccontextmanager`)。
- 🟡 **Silent failure**：`except: pass`（:131） + `threading` 內吞，無 `logger.warning`，Ollama-down 時 cold 24s 仍在首請求暴雷而無日誌可查。
- 🟡 **Daemon 語意**：daemon 在 `uvicorn --reload` 快速重啟時可能被 kill 於建構中，burst 200ms 內請求仍 cold。建議 `lifespan` await 或至少 `logger.info("vector warm done 80ms")`，非 blocker。

---

## 2. 最高優先：安全不變式

### 2.1 🔴 Blocker — `risk_policy.py:18` greedy `.*` 導致紅旗誤判（不在計畫書 4 項內，無審批擅改）

**Diff**：
```diff
- ("CHEST_PAIN", re.compile(r"胸痛|胸悶|胸口.*悶|悶悶|chest pain"))
+ ("CHEST_PAIN", re.compile(r"胸痛|胸悶|胸口.*悶|胸口.*痛|悶悶|chest pain"))
```

Phil intent `(F4-R1,P4)` portmanteau 註解，但 plan §3 未列此改動，屬 **scope creep**（見 §5）且引入高危 regex bug。

**Bug 機制**（`*` greedy + `_has_affirmed_match` 轉折 reset）：

1. Python `re` 的 `.*` 貪婪。對 `沒有胸口痛但胸悶`，`胸口.*痛` 命中 `胸口痛但胸悶`（2-8，跨 `但` consume 6 字，而非直覺的 `胸口痛` 2-5）。
2. `_has_affirmed_match`（:57-69）取 `prefix=text[max(0,m.start-14):m.start]` → `沒有`，`contrast_end` 掃 `但可是…，；,;` 但 `但` 已被 `.*` 消費進 match 內，不在 prefix，**轉折無法 reset**。
3. `prefix="沒有"` 命中 `_NEGATION_PREFIX` → `return False` 單一 match；且因已 consume `胸悶` 字元，無 second match，**整句被判否定**，`classify` 回 `NO_DEFINED_SIGNAL`（實測見下）。

**實測**（direct tool，`RiskSignalPolicy().classify`）：

| input | 預期 | 實際 | 判定 |
|---|---|---|---|
| `我有胸痛` | `RED_FLAG/CHEST_PAIN` | `RED_FLAG` | ✅ |
| `我有胸口痛` | `RED_FLAG`（新增） | `RED_FLAG` | ✅（新支撐） |
| `我沒有胸痛，但現在呼吸困難` | `RED_FLAG/BREATHING_DIFFICULTY` | `RED_FLAG/BREATHING_DIFFICULTY` | ✅（`胸痛`被否定、`呼吸困難`命中，跨同一正則內多詞） |
| **`沒有胸口痛但胸悶`** | `RED_FLAG/CHEST_PAIN` | **`NO_DEFINED_SIGNAL` []** | 🔴 **false negative** — greedy 跨 `但` + 否定 |
| `我胸口有點痛` | `RED_FLAG` | `RED_FLAG` | ✅ |
| `胸口痛但沒有呼吸困難` | `RED_FLAG/CHEST_PAIN` | `RED_FLAG/CHEST_PAIN` | ✅ |

`real 193+15` 中未含此轉折否定壓測，故全綠不代表安全。

**為何危險**：`workflow/graph.py:220 if _is_red_flag(raw_input): → E_EMERGENCY/U_URGENT_HUMAN BLOCKED` 與 `workflow/runner.py:52-53`、`graph.py:815` `a_node` 紅旗 short-circuit 依賴 `RiskSignalPolicy.classify`。此路徑**同步在 LLM 之前**（:220-238 在 `a_node` 頂、早於 `240 G2 240-272` 與 `273 route_request` LLM），繞過等同放行至 `G_COMPLEX`/`RAG`，使用者一句帶轉折的口語即可逃脫 urgent 攔截，違背 `proposaal/v0.1 V0_1` 紅旗 deterministic。

**修復建議**：`胸口.*痛→胸口.{0,8}痛`（與 `FOOT_ULCER_OR_WOUND .{0,8}` 等 bounded 一致）或 `胸口.*?痛` non-greedy，並補 5 條回歸回放（見 5.6）。

**追溯**：`intake_router.py:34-55` `_RED_FLAG_RE` 為獨立正則，未同步改，故 `is_welcome_trigger`/`_is_red_flag` 走 `RiskSignalPolicy` 者與走 `_RED_FLAG_RE` 者已分叉，未來漂移風險。

---

### 2.2 B gate / D gate 是否被削弱？— `PASS`

- **B gate**：`b_context_gate/gate.py` 無 diff；`b_node`（`graph.py:576-637`）仍 `context_gate.evaluate(rag_to_b(...))` + `INSUFFICIENT` → `AGENT_PLANNER` 三選一 bounded（:642-650 `b_route`），`MAX_AGENT_STEPS=5/2` 約束仍在 `graph.py:658-662`。
- **D gate**：`d_output_gate/gate.py/verifier.py` 無 diff；8 步（`gate.py:14` 註解 `步驟8 語意驗證 → verifier.verify`）+ `run_output_gate` + `run_previsit_output_gate` 仍在 `graph.py:918-971` `d_node`。`D_FALLBACK` path `274-284` 完整。
- **紅旗 deterministic pre-check 同步性**：`graph.py:220-238` `_is_red_flag` 在 `a_node` 首段、早於 `G2短句 240-272` 與 `route_request LLM 273`，同步執行、未改順序。`runner.py:52-53` `is_red_flag` 早於 `_is_formal_eligible`，`_NARROW_PATH_FAST` 僅避開 LLM 建模，不避 `a_node` 紅旗。
- **Welcome 短路不蓋紅旗**：`graph.py:194-219` welcome 內含 `is_pre_visit_intake_text` 二次檢查，`is_welcome_trigger("(你好|hi)在內"且 len<4)` 才放行，紅旗句長≥4 未命中，故不繞。
- **數量收斂 5→2 的 D 影響**：非削弱，但屬可用證據丟棄（見 §1.1），資訊損失非安全 bypass。

---

### 2.3 是否偷改測試／調鬆 threshold？— `PASS`

- `git diff HEAD -- tfda_context_gate/tests/` 空；`pytest 193 passed` 與 `test_workflow_integration 15 passed`，無 threshold 改 `0.85→0.75`（plan 寫的「失敗預案」未動，正確）。
- `verifier.py:147` 仍 `0.85`，`_token_pattern` 未改。

---

## 3. 邊界情境

| 情境 | 位置 | 結果 | 判定 |
|---|---|---|---|
| **空 input** | `router.py:27-31` + `graph.py:194 is_welcome_trigger("")→True` | `a_node` welcome 不進 LLM；`RiskSignalPolicy.classify("".strip→False)` 不紅旗；LRU key `""` 可命中但無危害 | ✅ PASS |
| **超長 input 10k** | `user_prompts.py:24 [:300]` + `c_workflow_input.py:105 [:300]` | LLM 輸入 bounded；`_router_cache_key` 存 10k key×128=1.2MB 可接受 | ✅ PASS (建議 hash key) |
| **LRU 污染：同句不同 declared_role** | `router.py:27-31` | 同 NFKC 文回同一 `RouterSignals`，跨角色錯路由（§1.2 High） | 🔴 HIGH |
| **預熱失敗擋 startup？** | `app.py:123-137` | daemon+swallow，不擋；但 silent、跨 worker 4×、deprecated | 🟡 Medium |
| **Semaphore 爆滿語意** | `app.py:570-590` vs `orchestrator.py:581-588` | 隊尾者正確得 `QUEUED`，但 app retry thread 阻塞+double（§1.3 High） | 🔴 HIGH |
| **D verifier 截斷後重疊** | `1.1` 尾段 | deterministic claim 全量 vs evidence 300，formal claim 300 vs evidence 300，交叉未測，overlap 0.85 可能 factor | 🟡 Medium |

---

## 4. Scope Creep：不屬於 4 項的所有 diff

`git diff --stat HEAD` 9 檔：

| 檔案 | diff 規模 | 屬 4 項？ | 合理性 |
|---|---|---|---|
| `tfda_context_gate/workflow/formal_factory.py` +30 | ✅C 截斷主菜（但 dead） | §1.1 已述：Helpers 正確但未接線，屬敘事錯位，保留或刪皆可但須擇一（建議刪 dead 或接入 `b_to_c`） |
| `tfda_context_gate/c_generator/user_prompts.py` +11 | ✅C | 合理 |
| `tfda_context_gate/c_generator/c_workflow_input.py` +5 | ✅C（未列於 plan 但為同束改動） | 合理（`to_legacy` 本就是正式路徑） |
| `tfda_context_gate/c_generator/deterministic_generators.py` +6 | ✅C 源表 5→2 | **合理但附帶資訊損失**（§1.1 尾） |
| `tfda_context_gate/c_generator/system_prompts.py` +12 | ✅C 源表 5→2 | 合理 |
| `tfda_context_gate/a_router/router.py` +59 | ✅LRU | 合理，惟污染需修（§1.2） |
| `line_bot/app.py` +47 | ✅Semaphore+preheat | 2 項合併於同一檔，合理，但含 High bug（§1.3） |
| `tfda_context_gate/line_orchestration/orchestrator.py` +16 | ✅Semaphore | 合理（與 app.py 同步改，正確實作） |
| `tfda_context_gate/clinical_safety/risk_policy.py` +4 | ❌ **非 4 項** | **不合理** — plan 4 項無此檔，擅自改安全紅旗且引入 greedy。見 §2.1 Blocker。 |
| `data/processed/line_sessions.sqlite3-shm` binary | — | 噪音，不審 |

> 未改：`b_context_gate/*`, `d_output_gate/*`, tests，均符合「B/D 不可繞」承諾。

---

## 5. 補強證據與對比

### 5.1 為何 15/193 全綠仍 FAIL？

- deterministc 夾具路徑 `CWorkflowInput.content=2000 全量 → claim=2000 → verifier vs evidence 2000 → overlap 1.0 → PASS`，與 formal LLM `300→300→→` 差異被CI掩蓋。
- 紅旗 5 條回歸（`formal_chain_latency_anatomy_20260828.md` 提及的直接/間接/洗白/否定/轉折）未含 `沒有A但B` 跨 clause 綁在同一正則內的貪婪用例，補測後即現形。

### 5.2 與 `P4` 目標延遲之權衡

- 熱路徑 15.3s→8-11s 預估合理：2.1k tokens→0.6k 對 `mimo-v2.5 3.08s` 裸測非線性但可縮 constrained decoding + output 500→800 tokens 仍占大頭；LRU 與 non-blocking 不改熱單次，但消滅 665s 隊尾長尾，正確。
- 源表 5→2 對 `CLINICIAN_DRAFT` 草稿「可追溯性」為可接受 trade-off（草稿非最終處方），但應在 `HANDOFF` 註記資訊損失並在 D 增加「被截掉證據的 `limitations` 提示」。

---

## 6. 建議修復（依優先序，不改程式碼僅提方案）

### P0 Blocker（阻擋合併）

1. **`risk_policy.py:18`** `胸口.*痛→胸口.{0,6}痛`（或 `胸口.*?痛`）並將 `FOOT/TISSUE 等 .{0,8}` 語意對齊；補 5 條紅旗回歸：`胸痛/胸悶/胸口悶/胸口痛/悶悶 直接`、`我沒有胸痛` 否定、`沒有胸痛但胸悶/呼吸困難` 轉折、`否認/並未` 洗白、`胸口.*痛 greedy 跨但` 定向用例（§2.1 已給 harness）。

### P1 High（同批修）

2. **`a_router/router.py:27`** `cache_key = NFKC(raw) + "\x1f" + declared_role + "\x1f" + language`（或對 `RequestContext` 全量 hash），並在 `route_request()` 的 short-circuit 前或在 `LangChainSignalExtractor.extract` 上下文註記 key 汙染已修；`_clear_router_cache` 於 `tests/conftest.py` `autouse` 清。
3. **`line_bot/app.py:580-589`** 刪 `_queued_retry` 整段（與 `orchestrator.py:582-588` 對齊為單推）；或若要保留 retry，改 `acquire(blocking=False)` + `if not acquired: return`，且 QUEUED push 不以 `event_id` 標冪等（改 `queued_events` set）以免抑制後續真實 `COMPLETED` push。

### P2 Medium（建議）

4. **`workflow/formal_factory.py:12-36`** 二擇一：(a) 刪 dead helpers；或 (b) 在 `adapters.py:b_to_c` 內 `b_result.evidence = _apply_evidence_truncation(...)` 接線，擇一後更新 plan 對照。
5. **`line_bot/app.py:123`** `@app.on_event→lifespan`，`except: pass→logger.warning("vector preheat failed", exc_info=True)`，daemon=True→awaitable 或至少 `logger.info` 成功耗時。
6. **`a_router/router.py:56-58`** sweep 提至 `get` 內輕量（或 60s 定時），避免 valley 長駐。
7. **`deterministic_generators.py`** 源表被截 5→2 時補 `limitations: "來源表僅列前2筆，完整證據見 RAGResult"`，或在 `d_node` 注入此 limitation。

---

## 7. 重審 Checklist（給 builder）

- [ ] `python3 -m pytest tfda_context_gate/tests/test_workflow_integration.py -q` 15 passed
- [ ] `python3 -m pytest tfda_context_gate/tests -q` 193 passed（含 deprecation warning 已消）
- [ ] `python tfda_context_gate/clinical_safety/risk_policy.py` 5 條 + 新增 greedy 轉折用例 100% abort（含 `沒有胸口痛但胸悶→RED_FLAG`）
- [ ] `python -c "from ...router import ... ; 同句不同角色不命中快取"` 手動通過
- [ ] `pytest` 中加測：6 並發 `handle_text_async_push` → 5 COMPLETED/QUEUED 1× push，retry thread 0
- [ ] `git diff HEAD -- tfda_context_gate/workflow/formal_factory.py` dead code 已清或已接線

---

## 8. 方法與限制

- 本審查直讀 `git diff HEAD` 全量 hunk（`--stat 10 files, +171/-19`）與 4 計畫書，比對 `formal_factory`/`user_prompts`/`c_workflow_input`/`deterministic_generators`/`system_prompts`/`router`/`risk_policy`/`orchestrator`/`line_bot`，輔以 `rg 0.85/0.75/threshold/verifier/LRU/queue` 與 `python3 -c RiskSignalPolicy()` 實彈 classify + `to_legacy→context_block 300` 端到端 harness + `pytest 193`。
- 背景 `explore` 5 agents 並行（C 截斷 / LRU / 紅旗 / Semaphore / scope creep），其中 Semaphore 探針已回（含 rate-limit/idempotency/lifespan 分析），其餘仍在 running，結論已由 direct tool 交叉驗證；若後補探針提出新 file:line，將 append amendment。
- 未動 `mimo-v2.5` 熱 E2E 實測 `<12s`（需 Ollama + `opencode/mimo-v2.5` 金鑰），以靜態 token 估 8-11s 預估為準。

---

## 9. 附：關鍵 diff 索引（便於複核）

| 主題 | diff 錨點 |
|---|---|
| C 截斷有效 | `user_prompts.py:13,24` `c_workflow_input.py:86,105` `deterministic_generators.py:294,371` |
| C dead | `workflow/formal_factory.py:9-36` `runner.py:24` 未引 |
| LRU | `router.py:16-63,132-137,179-180` `INTAKE_RE 12` |
| Semaphore+QUEUED | `app.py:41-42,573-591,671` `orchestrator.py:24-26,581-588,715` |
| Startup | `app.py:123-137` `fastapi 0.124.4 on_event` |
| 紅旗 greedy Blocker | `clinical_safety/risk_policy.py:18` + `_has_affirmed_match:57-69` + `intake_router.py:50-62` |
| Verifier 未動 | `d_output_gate/verifier.py:147 >=0.85` `tests/` 無 diff |

---

*— Sisyphus adversary, 2026-08-28. 本報告僅寫檔，未 commit、未改程式碼。下一步：builder 依 §6 修復後標 `ready-for-rereview`。*
