# TFDA Diabetes Agent — 糖尿病衛教 / 看診前 / 醫護草稿

> **一句話**：`LINE → A→B→C→D` 四門 + `E` 軌跡，三情境全走正式版 `mimo-v2.5 + bge-m3`。`A` 擋風險、`B` 審證據、`C` 煮答案、`D` 最終驗、`E` 全程記，`Agent` 只在 `B INSUFFICIENT` 才動（三選一：`ASK_USER/REWRITE/FALLBACK`）。

## 快速開始

```bash
# 離線基線（15 passed）
python3 -m pytest tfda_context_gate/tests/test_workflow_integration.py -q

# 正式版 3 情境（需 .env 的 mimo-v2.5 + Ollama bge-m3）
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'1','user_raw_input':'請說明糖尿病的一般飲食原則。','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).status)"  # 衛教 COMPLETED

python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'2','user_raw_input':'我下週要看醫生','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).question)"  # 看診前 3階段

python3 -c "from pathlib import Path; from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'3','user_raw_input':'我要準備看診','declared_role':'PATIENT','language':'zh-TW'}, image_bytes=Path('fixtures/images/medication_bag_front.jpg').read_bytes(), use_formal=True).status)"  # 藥袋圖片

# Stream
python3 -c "from tfda_context_gate.workflow.runner import stream_workflow; print(''.join(stream_workflow({'request_id':'4','user_raw_input':'請說明糖尿病的一般飲食原則。','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True))[:200])"
```

## 架構

```
User
  → A: Input Router + Policy Gate (a_router) — 擋 停藥/診斷/注入，僅 G 進 RAG
  → QueryExpansion (Identity, 保留 original_query)
  → RAG (rag/tfda_retriever + hpa_retriever, bge-m3 + .vector_cache, TFDA 129 + HPA 9)
  → B: Context Gate (b_context_gate, 15欄, TFDA_RISK/HPA_DIET_GUIDE)
  → C: Generator (c_generator, 患者白話 200字 / 醫護詳細 4段 + source_table)
  → D: Output Gate (d_output_gate, 8步, PASS/FALLBACK)
  → Answer / Fallback
  ↘ B INSUFFICIENT → Agent (ASK_USER/REWRITE/FALLBACK, max 2步, 3選一) → E
E: Observability (e_observability, 8狀態, TraceEvent 全程)
```

**硬邊界**：`A` 策略權威不可被 C/D 蓋過；`B` 顯式批准才進 `C`；`C` 只能引 `approved_evidence_ids`；`D` 必過；`E` 只觀測不改答案；圖片不進 `WorkflowState`。

## 模組與契約

| 模組 | 職責 | 關鍵檔案 |
|---|---|---|
| `a_router` | 驗證 + 正則/LLM 訊號 + `policy_gate` 8路由 | `router.py`, `labels.py`, `guard.py` |
| `query_expansion` | 保留 `original_query`，吐 `retrieval_queries` | `expander.py` |
| `rag` | `RAGResult` + `TFDADrugSafetyRetriever` (bge-m3) | `tfda_retriever.py`, `hpa_retriever.py`, `phase_scripts/` |
| `b_context_gate` | `CanonicalEvidence 15欄` + `DeterministicContextGate` | `schemas.py`, `gate.py` |
| `c_generator` | `EvidenceAwareV2Answer` + `ClinicianEvidenceDraft` | `workflow_adapter.py`, `prompts.py` |
| `d_output_gate` | 8步驗證，`PASS/FALLBACK` | `gate.py`, `verifier.py` |
| `e_observability` | `TraceEvent` 8態 + `TraceRecorder` | `tracer.py`, `schemas.py` |
| `workflow` | `run_workflow` / `stream_workflow` (LangGraph) | `runner.py`, `graph.py` |
| `intake` | 8欄 `PreVisitIntake` + 3階段 + 藥袋提醒 | `schemas.py`, `tool.py` |
| `tool_contract` | `Registry/Executor` allowlist | `registry.py` |

## 詳細去哪看

*   程式導讀：`../docs/codebase/00_overview.md` → `01_a_router` / `02_rag_b` / `03_c_generator` / `04_d_output_gate` / `05_e_observability` / `06_workflow_agent`
*   提案：`docs/proposal/v0.1/V0_1_提案書.md`（主提案，`v0.1` 驗收以此為準），`V0.2` 僅藍圖在 `docs/proposal/`
*   交接：`docs/HANDOFF.md`（人） / `AGENTS.md`（Agent）
*   架構總圖與審計：已歸檔 `archive/docs/`，連結在 `HANDOFF`

## 限制

Demo/MVP，非臨床系統。`declared_role` 非身分驗證，`FixtureRetriever` 僅供離線測試，`Qwen3Guard` 需另裝，`declared_role` 不授權。
