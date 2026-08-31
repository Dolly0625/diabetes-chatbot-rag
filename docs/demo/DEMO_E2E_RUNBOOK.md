# Demo E2E Runbook — 工程 Demo 驗收（temp SQLite，不依賴真 LINE/LLM）

> 此驗收包以臨時 SQLite 跑完整病患到醫護的資料流，不使用真 LINE token 或 LLM。

## 目標
以「可重複執行、臨時 SQLite、不使用真 LINE token/LLM」驗收三個產品情境；每一步人類可讀，任一情境失敗即以非 0 離開。

| 情境 | 契約 |
|---|---|
| 1 | 病患在 LINE 問糖尿病衛教（`請說明糖尿病的一般飲食原則。`），**資料不會誤寫入看診欄位**（`known_medications / allergies / chronic_conditions / family_history / symptom_onset / symptom_description / symptom_severity / questions_for_doctor`） |
| 2 | 看診前資料收集存在**未完成草稿**時，系統**必須**要求在「繼續上次整理」「開始新的整理」「取消整理」中選擇，**不能無提示自動續填**。缺任一項即驗收失敗。 |
| 3 | 使用者**確認提交並授權**後，醫護**只能讀到已確認的結構化摘要**，**不能寫入、不能看到未確認草稿**（唯讀、單次、短效、綁定醫護、hash 儲存、D gate PASS） |

## 前置（1 次）
```bash
git status                     # 位於 demo-flow-e2e-tests，工作區乾淨
python3 -m pip install -e . 2>&1 | tail -n 5  # 若已有依賴可跳過
# 不需 .env / LINE token / Ollama；本驗收預設 use_formal=False（確定性）
# 亦不需 GCP / 向量庫；可 mock 外部 LLM/RAG，但嚴禁以 mock 掩蓋授權或狀態行為
```

## 執行（3 種等價入口，擇一）

### A. 一鍵 Runner（最短）
```bash
python scripts/demo/run_e2e_acceptance.py
# 或
python scripts/demo/run_e2e_acceptance.py --verbose   # 印 snapshot/reply
python scripts/demo/run_e2e_acceptance.py --json      # 最末行輸出 JSON summary
```
**預期輸出（範例）：**
```
=== 情境 1：衛教不污染看診欄位 ===
  ✓ PASS — 衛教未污染任何看診欄位…
=== 情境 2：未完成草稿必須三選一 ===
  ✓ PASS — 系統正確要求三選一，且草稿保持原樣未被自動覆寫
=== 情境 3：醫護唯讀已確認摘要，隔離未確認草稿 ===
  ✓ PASS — 醫護唯讀已確認摘要…
=== 總結 ===
  情境 1（衛教不污染）: PASS
  情境 2（草稿三選一）: PASS
  情境 3（醫護唯讀已確認）: PASS
Overall: PASS
```
`Overall: PASS` 時 exit 0；任一 `FAIL` 時 exit 1。

### B. pytest 合約測試（同契約）
```bash
python -m pytest tfda_context_gate/tests/test_demo_e2e_contract.py -v
# 預期：7 passed, 0 failed
#  - test_s1_... 2 passed
#  - test_s2_draft_requires_three_way_choice  passed
#  - test_s2_no_silent...  passed（防呆：不可把壞行為當 pass）
#  - test_s3_... 3 passed
```

### C. 同時跑 Runner + pytest（推薦 Demo 前）
```bash
python scripts/demo/run_e2e_acceptance.py --verbose && \
python -m pytest tfda_context_gate/tests/test_demo_e2e_contract.py -v
```

## 重複與清理
- 每次執行使用 `tempfile.TemporaryDirectory(prefix="demo_e2e_")` + `SQLiteProductSessionRepository(tmp / *.sqlite3)`，離開即刪（含 `-wal/-shm`）。
- 可重複跑 3 次驗穩定性：
  ```bash
  for i in 1 2 3; do echo "Run $i"; python scripts/demo/run_e2e_acceptance.py || exit 1; done
  ```

## 技術要點
- **不輸出 PII / raw token / image**：僅印 `grant_id[:8]***`、`session_id[:8]***`、截斷 reply ≤300 字；`share_grants` 僅存 `token_hash`（64 hex），`SHA256(token)` 對照。
- **不依賴外部服務**：`ConversationOrchestrator(..., use_formal=False)`；`workflow_runner` 不呼叫 LLM/RAG。`--verbose` 亦不洩漏 `.env`。
- **嚴禁以 mock 掩蓋授權/狀態**：`test_s3_*` 實測 `ProductSession.status == SUBMITTED` + `permission_scopes` + `single_use` + `allowed_practitioner_hash` + `D gate PASS` + `audit log`；`test_s2` 必須驗證三選一與草稿不被改寫。
- **紅旗仍優先**：`RiskSignalPolicy` 在 orchestrator 內仍單調 `RED_FLAG`，與三情境正交，不影響本驗收。

## 誠實回報（本分支實測）
```bash
python -m pytest tfda_context_gate/tests/test_demo_e2e_contract.py -v
python scripts/demo/run_e2e_acceptance.py
python -m pytest -q  # 全量（見「全套 pytest」段）
```
- 情境 1：**PASS**（衛教不污染 8 欄）
- 情境 2：**PASS** — 未提交草稿再次進入時，明確顯示「繼續上次整理／開始新的整理／取消整理」，且不自動改寫草稿。
- 情境 3：**PASS**（SUBMITTED 唯讀、單次、綁定、hash 儲存、D gate、draft 隔離皆過）


## 全套 pytest 與相關測試
```bash
# 本驗收相關（不含 live 正式模型 smoke）
python -m pytest tfda_context_gate/tests/test_demo_e2e_contract.py \
               tfda_context_gate/tests/test_authorization_boundary.py \
               tfda_context_gate/tests/test_pending_lifecycle.py \
               tfda_context_gate/tests/test_p0_data_correctness.py \
               tfda_context_gate/tests/test_share_grants.py \
               tfda_context_gate/tests/test_conversation_orchestrator.py -v

# 全量（離線，跳過需 Ollama/.env 的 live-formal）
python -m pytest -q --ignore=tfda_context_gate/tests/test_tfda_retriever.py
```

## Commit 規範（僅含允許範圍）
```bash
git status
git diff --stat
git add scripts/demo/run_e2e_acceptance.py \
        tfda_context_gate/tests/test_demo_e2e_contract.py \
        docs/demo/DEMO_E2E_RUNBOOK.md
git commit -m "test(demo-e2e): add three-scenario acceptance runner and contract"
# 不要 merge / 不要 push；PR 將由人審
```

## Integration Branch 需補跑的命令
在 `integration` / `main` 合併前，由 CI 或人手補跑（需 .env + Ollama `bge-m3:latest` 才可跑 live-formal）：
```bash
# 1. 本驗收（離線）- 必須綠
python scripts/demo/run_e2e_acceptance.py
python -m pytest tfda_context_gate/tests/test_demo_e2e_contract.py -v

# 2. 全量離線回歸 - 必須綠
python -m pytest -q

# 3. 正式版三情境（live，需要 .env 的 OPENCODE_API_KEY 與 Ollama）
python -m pytest tfda_context_gate/tests/test_workflow_integration.py -q  # 15 passed
python -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'live-1','user_raw_input':'請說明糖尿病的一般飲食原則。','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).status)"  # COMPLETED
python -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'live-2','user_raw_input':'我下週要看醫生','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).question)"  # 3-stage question
python -c "from pathlib import Path; from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'live-3','user_raw_input':'我要準備看診','declared_role':'PATIENT','language':'zh-TW'}, image_bytes=Path('fixtures/images/medication_bag_front.jpg').read_bytes(), use_formal=True).status)"  # image

# 4. 草稿三選一合約
python -m pytest tfda_context_gate/tests/test_demo_e2e_contract.py::test_s2_draft_requires_three_way_choice -v  # 期望 PASS

# 5. 可選：LINE 真機 phone E2E（需 LINE_CHANNEL_SECRET/ACCESS_TOKEN）
python scripts/demo/check_line_phone_demo.py  # 或 docs/demo/LINE_PHONE_E2E_RUNBOOK.md
```

## 常見問題
- `IMPORT ERROR: bge-m3`：離線驗收不需 `bge-m3`；僅 live-formal 需要 `ollama pull bge-m3:latest` 與 `OLLAMA_BASE_URL`。
- 草稿三選一缺少任一按鈕或自動續填：視為 `FAIL`，不可帶著壞行為 demo。
- Demo 現場 network 無法拉 Ollama：使用 `--verbose` 的離線 runner 即可完整演示 3 情境；live-formal 僅作加分。

## 交付清單
- `scripts/demo/run_e2e_acceptance.py` — 主驗收 runner
- `tfda_context_gate/tests/test_demo_e2e_contract.py` — 合約測試
- `docs/demo/DEMO_E2E_RUNBOOK.md` — 本文件
