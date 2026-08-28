# AGENTS.md — TFDA Diabetes Agent Handover for New Window (2026-08-27)

## Identity
You are Sisyphus (SF Bay Area engineer). Follow `oh-my-openagent.json` model routing: `opencode-go/muse-spark-1.2-contributor` for all categories (atlas/explore/librarian/oracle/sisyphus/deep/quick/ultrabrain), `deepseek-v4-flash` only for metis/momus if needed. No GLM-5.1.

## Project Goal
Build `v0.1` safety baseline per `docs/proposal/v0.1/V0_1_提案書.md` (main): A→B→C→D with E trace, 3 flows (patient education / pre-visit intake / clinician draft) must all formally PASS with `mimo-v2.5 + bge-m3`. `V0.2` is future blueprint, not current acceptance.

## Current Status (All Formal PASS)
- `b_context_gate` 15 fields (7+4 RAG +4 risk) + task_type/tool_context, `adapters.normalize_evidence` multi-key fallback
- `a_router` formal via `LangChainSignalExtractor.from_env()` (`.env` `opencode/mimo-v2.5`, `OPENCODE_API_KEY` in `.env` already gitignored), `policy` fixed for `GENERAL_MEDICATION_INFORMATION` → G
- `rag` Ollama `bge-m3:latest` with disk cache `data/processed/.vector_cache/*.pkl` (24s→0.17s), TFDA 129 + HPA 9 chunks (food_nutrition/diet_guide/diabetes_book) merged, 74 tests pass
- `tool_contract` (Registry/Executor allowlist `TFDA_RISK/HPA_DIET_GUIDE`), `intake` 8 fields + 3-stage topic-chunked + Review&Confirm + red-flag deterministic pre-check
- `c_generator` ClinicianEvidenceDraft detailed 4 sections (300-400 chars), `d_output_gate` 8 steps, `e_observability` 8 states
- `workflow` supports `use_formal=True` (formal A+RAG+C) + `image_bytes` → `MedicationBagOCRService` (QR-first → PaddleOCR fallback + TFDA 44k correction) → `PreVisitIntake`
- `line_bot/app.py` FastAPI `/callback` with `X-Line-Signature`, Text/Image handling, `MessagingApiBlob` download, never stores raw image in WorkflowState, stream via `stream_workflow`
- `stream` buffered-then-stream after D PASS, E records first_token latency
- Refactor Step1-2 done: `.gitignore` added, `report_handoff`→`archive/report_handoff_20260821`, `tfda_medrax2_experiment`→`experiments/archive/`, `參考檔案`→`docs/references/`, `藥袋*.jpg`→`fixtures/images/`, `rag/phase_scripts/00-05*.py` moved, `agent_demo_case_schema.py` moved to `agent/`, `deliverables/staging` deleted (6.2M→1.4M), `line_bot` paths fixed, 21 tests pass

## How to Run (Local, No LINE/GCP Needed)
```bash
# Baseline
python3 -m pytest tfda_context_gate/tests/test_workflow_integration.py -q  # 15 passed

# Formal 3 scenarios
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'1','user_raw_input':'請說明糖尿病的一般飲食原則。','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).status)"  # COMPLETED

# Intake
python3 -c "from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'2','user_raw_input':'我下週要看醫生','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True).question)"

# Bag image
python3 -c "from pathlib import Path; from tfda_context_gate.workflow.runner import run_workflow; print(run_workflow({'request_id':'3','user_raw_input':'我要準備看診','declared_role':'PATIENT','language':'zh-TW'}, image_bytes=Path('fixtures/images/medication_bag_front.jpg').read_bytes(), use_formal=True).status)"

# Stream
python3 -c "from tfda_context_gate.workflow.runner import stream_workflow; print(''.join(stream_workflow({'request_id':'4','user_raw_input':'請說明糖尿病的一般飲食原則。','declared_role':'PATIENT','language':'zh-TW'}, use_formal=True))[:200])"
```

## Key Files
- `.env` → `ROUTER_LLM_MODEL=opencode/mimo-v2.5`, `OPENCODE_API_KEY`, `OLLAMA_BASE_URL=http://localhost:11434`, `bge-m3:latest` in `ollama list`
- `tfda_context_gate/workflow/runner.py` → `run_workflow(..., use_formal, image_bytes, intake_data, task_type)` + `stream_workflow`
- `tfda_context_gate/intake/` → 8 fields, 3-stage, bag reminder, FHIR
- `tfda_context_gate/rag/` → `tfda_retriever.py` (bge-m3 + cache), `hpa_retriever.py`, `phase_scripts/`
- `line_bot/app.py` → FastAPI, simulate_* helpers for local test without LINE

## Next Steps (User Wants)
- Keep fixed workflow A→B→C→D, bounded Agent 3-choice (ASK_USER/REWRITE/FALLBACK), not planner CALL_TOOL
- Pre-visit intake already 3-stage, bag reminder with 2-attempt, proactive trigger via natural phrase `要看醫生/回診` (no button needed)
- OCR local `mimo vision` for now, future switch to `PaddleOCR` via `OCRService` interface (QR-first)
- Next refactor: Step3 c_generator split, workflow slim, Step4 docs move — each with pytest verification
- Docs: `docs/HANDOFF.md` is human handover, this file is for agent

## Constraints
- Never bypass B/D gates, never store raw image in WorkflowState, always hash PII, keep 15 passed tests green
- Use `.env` as single source for models, never hardcode `qwen3-14b`
