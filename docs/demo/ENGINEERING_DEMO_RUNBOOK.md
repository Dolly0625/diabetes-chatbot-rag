# Engineering Demo Runbook

> 基線 `main 41f4725`，worktree `tfda-diabetes-agent-demo-scenarios`（branch `demo-scenario-pack`）。  
> 本包 **不修改核心 production code**：僅新增 `scripts/demo/`、`docs/demo/`，不碰 `orchestrator/interpreter/runner/candidate_merge/line_bot/app.py`，不改 `.env`，不 push/merge。

## 目標
從專案根目錄可執行的展示腳本與文件，deterministic 預設不依賴 LINE/GCP/外部 LLM，涵蓋 4 條工程情境，每步印出人類可讀結果，失敗以非 0 離開，暫存 SQLite 用 `tempfile` 自動清理，不輸出 ID/token/API key/raw image。

## 前置
```bash
git worktree add /Users/dolly/Documents/code/tfda-diabetes-agent-demo-scenarios -b demo-scenario-pack 41f4725
cd /Users/dolly/Documents/code/tfda-diabetes-agent
# 或直接在 worktree
cd /Users/dolly/Documents/code/tfda-diabetes-agent-demo-scenarios
cat AGENTS.md | head -n 30
```

## 執行
```bash
# Deterministic（預設，離線可跑）
python scripts/demo/run_engineering_demo.py

# 可選：呼叫外部 LLM/RAG（需 .env 與 Ollama bge-m3）
python scripts/demo/run_engineering_demo.py --live-formal

# 重複跑 3 次驗證穩定性
for i in 1 2 3; do echo "Run $i"; python scripts/demo/run_engineering_demo.py || exit 1; done

# 測試無退步
python -m pytest tfda_context_gate/tests/test_workflow_integration.py tfda_context_gate/tests/test_agent_demo_cases.py -q
python -m pytest tfda_context_gate/tests/test_share_grants.py -q

# 風格檢查
git diff --check
```

## 架構與 Deterministic 路徑
- `run_workflow(..., query_expander=IdentityQueryExpander(), retriever=FixtureRetriever(), context_gate=DeterministicContextGate(), generator=DeterministicFixtureCGenerator())`
- `FixtureRetriever` 固定回 `E1/E2`（PASS）與 `E3`（未核准）三筆證據，不打外部服務。
- `IdentityQueryExpander` 原樣透傳，不做 LLM 改寫。
- `DeterministicContextGate` 以 `fixture_b_approved` 判定 `PASS/INSUFFICIENT`。
- `DeterministicFixtureCGenerator` 產生「幫你整理了衛教重點…」等可驗證回答，`D` 閘門驗證 evidence boundary。

紅旗與分享：
- `fallbacks.py` 的 `A_EMERGENCY` 含「撥打 119 或前往最近的急診」。
- `tfda_context_gate/workflow/intake_router.py` 的 `is_red_flag` 透過 `RiskSignalPolicy().classify(...).level == RED_FLAG` 判定，`a_node` 直接 `RED_FLAG_DETERMINISTIC_ABORT`，不進 `QUERY_EXPANSION/RAG/intake`。
- `tfda_context_gate/sharing/service.py` 的 `ShareGrantService.create` 需 `status == SUBMITTED` 且持有 `SHARE_OWN_SUMMARY`，TTL 10 分鐘，`redeem` 需 `VIEW_GRANTED_CLINICAL_SUMMARY`，單次使用，僅存 `token_hash`。

Intake：
- `tfda_context_gate/intake/schemas.py` 的 `INTAKE_STAGES = {stage1: [known_medications, allergies, chronic_conditions, family_history], stage2: [symptom_onset, symptom_description, symptom_severity], stage3: [questions_for_doctor]}` 與 `STAGE_QUESTIONS`。
- `generate_previsit_summary` 僅整理已提供事實，`summary_text` 拼接 8 欄，附 `disclaimer`，不推定診斷。

## 四情境說明

### 情境 1：病患看診前整理
輸入流：`為自己整理 → metformin → 沒有過敏 → 高血壓 → 口乾＋晚上頻尿（三個月前/中度） → 想問醫師的問題 → Review & Confirm`  
驗證：`PreVisitIntakeTool.extract_fields_from_utterance` 分 stage 抽取，`FixtureRetriever` 等 deterministic 組件驅動 `run_workflow(..., task_type=pre_visit_intake)` 的 `INTAKE_CHECK → stage1/2/3 → REVIEW_CONFIRM`，`generate_previsit_summary` 產生 `provided_fields/missing_fields/Disclaimer/Timeline`，`WorkflowResult.question/intake_snapshot/previsit_summary` 可達。

### 情境 2：intake＋衛教多意圖
輸入：`我最近常口渴，糖尿病一天可以吃幾份水果？`  
驗證：
- `PreVisitIntakeTool` 能抽到 `symptom_description` 含「口渴」並寫入 `intake`。
- `run_workflow` deterministic 回衛教（`COMPLETED/D PASS`）或誠實 `B_INSUFFICIENT` fallback，兩者皆視為通過，但不得遺失 `intake_stage` 與 `known_medications`（`metformin` 仍保留）。

### 情境 3：分享與醫護閱讀
步驟：`ProductSession SUBMITTED (PERMISSION_SHARE_OWN_SUMMARY) → ShareGrantService.create (TTL 10min, token_hash 落盤) → Practitioner redeem (需 VIEW_GRANTED_CLINICAL_SUMMARY, 單次) → 驗證唯讀與權限邊界`  
驗證：`raw token` 不落地、`grant.expires_at - created_at ≈ 600s`、第二次 redeem 失敗、綁定 `allowed_practitioner_hash` 錯配失敗、過期後 `ShareGrantDenied(expired)`、醫護 `ActorAccessContext` 無 `CREATE_OWN_INTAKE`。暫存 SQLite 走 `tempfile.TemporaryDirectory` 自動清理。

> 公開 API 是否足夠：目前 `line_bot` 與 `product_session` 皆無對外 `POST /share` 公開路由，本 demo 僅透過 `ShareGrantService` + `SQLiteProductSessionRepository` 直接驗證。若需真機「分享連結」需另增 API 與鑑權，本文件已記錄此缺口，未改 backend。

### 情境 4：紅旗
輸入：`我胸口很痛而且喘不過氣`  
驗證：`run_workflow` 立即 `FALLBACK/BLOCKED` 且 `fallback_reason=A_EMERGENCY`，`final_response` 含 `119`，`trace.events` 含 `RED_FLAG_DETERMINISTIC_ABORT` 且 `component=A/status=BLOCKED`，無 `RAG` 事件（不等待 AI），`intake_snapshot` 未被污染（`symptom_description` 不含胸痛）。

## 技術要求對照
- deterministic 預設不需 LINE/GCP：`FixtureRetriever/Deterministic*` 無外部依賴。
- 可選 `--live-formal`：僅在 flag 下走 `use_formal=True`，預設不呼叫外部 LLM。
- 不輸出 ID/token/API key/raw image：僅印 `mask` 後 `grant_id` 前 6 碼與截斷摘要。
- 每步人類可讀：`-> Step / ✓ 已記錄 ...`。
- 失敗 exit 非 0：`main` 回 `1`，場景函式拋異常即視為失敗。
- tempfile SQLite：`TemporaryDirectory` + `SHA256(token)` 比對，離開即刪（含 WAL）。

## 仍缺的真機條件
- 真實 LINE webhook：需 `LINE_CHANNEL_SECRET/ACCESS_TOKEN` 與 `X-Line-Signature` 驗證，`line_bot/app.py` 的 `MessagingApiBlob` 下載與 `QR-first → PaddleOCR` 流程無法在 deterministic 覆蓋。
- GCP/正式向量庫：`TFDA 129 + HPA 9 chunks` 的 `bge-m3` 檢索與 `data/processed/.vector_cache/*.pkl` 需 Ollama 本地服務，`--live-formal` 才會命中。
- 公開分享 API：無對外 `POST /sessions/:id/share` 與短連結兌換端點，僅服務層單元驗證。
- OCR 實拍：`fixtures/images/medication_bag_front.jpg` 的實測需 `image_bytes` 與 `MedicationBagOCRService`，demo 未上傳 raw image。

## 常見問題
- `pytest` 若缺 `langchain_huggingface/sentence_transformers`，`real_retriever` 測試會 skip，不影響 deterministic。
- `git diff --check` 若報 whitespace，執行 `sed -i '' 's/[[:space:]]*$//' scripts/demo/run_engineering_demo.py` 後重測。

## 交付清單
- `scripts/demo/run_engineering_demo.py` — 主腳本
- `docs/demo/ENGINEERING_DEMO_RUNBOOK.md` — 本文件
- `docs/demo/EXPECTED_OUTPUT.md` — 預期輸出範例
