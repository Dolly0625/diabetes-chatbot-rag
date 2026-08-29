# P2A Live Smoke — deterministic 部分命中＋formal 補齊 延遲量測（Phase 2）

## 目標
驗證 `candidate_merge` 的「部分命中不阻擋 formal、合併後落地」與紅旗優先，且量測真實模型延遲，並以 staged per-turn 統計 Honest 回報。

## 執行

### 真實模型（需 .env）
```bash
python scripts/p2a_live_smoke.py
# 或
python -m scripts.p2a_live_smoke
# JSON 輸出
python scripts/p2a_live_smoke.py --json
```
- 使用真實 `.env` 的 `CONVERSATION_LLM_MODEL` / `ROUTER_LLM_MODEL` / `OPENCODE_API_KEY`
- 透過 `ConversationOrchestrator` 正式建構路徑（`ConversationInterpreterFactory.from_env()`），驗證 `interpreter is FormalConversationInterpreter`
- 不可直接塞 `FakeInterpreter`
- 不納入 `pytest` 預設（`scripts/` 目錄），需另行執行；準備好腳本但由人另行對 live 模型執行，避免與其他並行模型呼叫衝突

### Dry-run（CI，無需真模型）
```bash
python scripts/p2a_live_smoke.py --dry-run -q
```
- 以 `FakeConversationInterpreter` 演練四組情境、五個 turn（mixed 另含同 session warm 追問），僅供 CI 驗證腳本可跑；不觸發真實 formal workflow（避免 8s timeout）
- `mixed-intent` 會在同一 session 追加一輪教育追問，驗證 session 狀態續存；共 5 turns

## 四組情境、五個 turn（Phase 2 更新命名）

| 組 | 輸入 | 預期 |
|---|---|---|
| pure-intake | `我嘴巴很乾，晚上一直跑廁所` | `symptom_description` 保留多子句（deterministic 部分命中 + formal 補齊） |
| pure-education | `晚上常跑廁所會是糖尿病嗎？` | 問句不得污染病史（`symptom_description` 保持空，`candidate_merge` 攔截） |
| mixed-intent | `我最近常口渴，糖尿病一天可以吃幾份水果？` | `INTAKE_ANSWER + EDUCATION_QUESTION`；口渴落地，水果問題走教育支線；成功且 B PASS 時最多 2 次 model calls |
| mixed-intent-warm | `晚上常跑廁所會是糖尿病嗎？`（同 session 第二輪） | 純教育追問，驗證 multi-turn 狀態續存：前輪口渴仍在 `symptom_description`，問句不污染病史 |
| red-flag | `我胸口很痛喘不過氣` | 必為 `FALLBACK`（`RiskSignalPolicy RED_FLAG → A_EMERGENCY`），優先於任何 merge，不寫入 intake |

**Honest model calls：** 一輪 mixed-intent 成功且 B PASS 時**最多 2 次** LLM 呼叫：`conversation interpreter (1)` + `formal C generator (0–1)`；若 B 判定 evidence insufficient，C 會跳過。無額外 rephraser、無第三次 LLM。Warm 追問為額外一輪 education。

**Timeout：** interpreter 使用 `CONVERSATION_LLM_TIMEOUT_S`；C 使用
`C_GENERATOR_LLM_TIMEOUT_S`（未設定時依序採用 `FORMAL_C_LLM_TIMEOUT_S`、conversation timeout）。兩者都在 HTTP client 建立時設定 native timeout；外層 45/120 秒只負責 caller wait，沒有藉提高外層數字掩蓋 transport 延遲。

## 輸出（Phase 2 新增）

- 每組：`latency`、`staged`（`red_flag_and_auth_ms`, `conversation_interpreter_ms`, `candidate_validation_ms`, `rag_retrieval_ms`, `answer_generator_ms`, `b_gate_ms`, `d_gate_ms`, `persistence_ms`, `total_ms` 皆為 timings 無 PII）、fallback 分類、`intake_snapshot` 脫敏 JSON、`is_process_first_measurement/is_warm_process_measurement` 與 `is_session_first_turn/is_warm_session_turn`
- 彙總：總 fallback 量及分開的 `red_flag_safety`、`evidence_insufficient`、`timeout_dependency`（另列 other），system failure rate 不包含預期紅旗安全攔截；另有 per-stage p50/p95 與 process-first/process-warm 統計
- `process_first_measurement` 只代表本程序第一筆量測，**不等於模型、HTTP client 或容器 cold start**；session-first 則只代表該 session 的第一筆量測。若要聲稱真正 cold，必須每次重啟程序/模型後另行量測。
- 紅旗與反例的落地面試：pure-education `symptom_description` 為空；red-flag `status=FALLBACK` 且 `reply` 含 119/急診；warm 追問驗證同 session 症狀續存且不污染

## 預期參考值
- 目前實測 baseline（mimo-v2.5 + bge-m3，網路/模型浮動）：
  - `p50 ~2–5s` `p95 ~4–8s`；`red_flag_safety` 是預期安全攔截，不計入 system failure rate
  - Dry-run：`p50 ~60–120ms` `p95 ~100–200ms` `timeout 0/5`
  - process-first/process-warm 只作程序量測順序比較，不可解讀成模型 cold/warm 差異
- 視網路與模型負載浮動，僅作參考；重點在 staging 落地與無污染、紅旗優先

## 支架
- `tfda_context_gate/tests/fixtures/p2a_cases.json` — 22 句預期行為（6+6+5+5），供煙霧與批次驗證參考，不納入 pytest 預設
- `scripts/p2a_live_smoke.py` — 僅 scripts 執行，不放入預設 pytest；已依 Phase 2 加入 per-stage、fallback 分類與 process/session-first 統計

## Task C 並行評估（誠實報告）
- Interpreter 在 branches 之前為串行瓶頸：`conversation_interpreter_ms` 為主要成本（Formal 8s  timeout 內，deterministic <1ms）
- 可並行窗口僅在 interpreter 之後：`candidate_validation_ms`（~1ms）vs `education retrieval/answer`（`rag_retrieval_ms` + `answer_generator_ms` 2–45s）；並行化僅省 ~1ms，效益可忽略
- 若改為 interpreter 前推測性並行（以 raw text 先做 RAG），會失去 `resolved_education_query` 的指代消解（例：`那一天可以吃多少？`→`糖尿病一天可以吃多少水果？`）且可能繞過 `R_POLICY_BOUNDARY` 策略
- 結論：不強行並行化，維持 `red_flag → auth → subject → control → interpreter → (join) → candidate_validation → B/D → single persistence` 順序，僅先以 Task A 的 staged 數據量測證明；不新增第三次 LLM

## 如何重現 live smoke（人手執行）
```bash
# 確認 .env
grep -E "CONVERSATION_LLM_MODEL|ROUTER_LLM_MODEL|OPENCODE_API_KEY" .env
# 執行
python scripts/p2a_live_smoke.py
# 或 JSON
python scripts/p2a_live_smoke.py --json > /tmp/p2a_smoke.json
```
- 腳本已使用真實 `.env`，`--dry-run` 僅用 Fake
- 不與其他並行模型呼叫同時執行（文件指示由人另行執行）

## 支架與驗證
- `python -m pytest tfda_context_gate/tests/test_p2a1_latency_and_multi_intent.py -q` — 含 wall-clock timeout 測試
- `python -m pytest tfda_context_gate/tests/ -q` — 全量 ≥405 通過
