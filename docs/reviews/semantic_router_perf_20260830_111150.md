# Semantic Router 效能驗收 — 20260830_111150

> 產生時間 2026-08-30T03:11:50.675030+00:00 | 耗時 32786ms | 序列 15 輪 × 3 模式 (cold/warm) | live 10 輪

> 註：p50/p95 為 fixture 決定性路徑（無真 LLM），live smoke 另列。目標斷言僅報告，live 波動不硬失敗。

## 模式 off / cold（15 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.4 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 0.0 | 0.0 |
| conversation_interpreter_ms | 0.1 | 4.5 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.1 | 0.1 |
| b_gate_ms | 0.1 | 0.1 |
| d_gate_ms | 0.2 | 0.4 |
| persistence_ms | 1.1 | 1.5 |
| total_ms | 37.3 | 56.6 |
| fallback_rate | 13.33% | count 2/15 |
| avg_llm_calls | 0.80 | avg_interpreter 0.80 |

### 各類 p50/p95/fallback/LLM（off/cold）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 43.7 | 43.7 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 36.6 | 36.6 | 0% | 1.0 |
| control_pause | control command PAUSED | 36.3 | 36.3 | 0% | 1.0 |
| identity | identity chitchat | 1.5 | 1.5 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 37.8 | 37.8 | 0% | 1.0 |
| mixed | MIXED intake+edu | 38.1 | 38.1 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 5.4 | 5.4 | 0% | 1.0 |
| third_party | third-party friend | 36.6 | 36.6 | 0% | 1.0 |
| hypothetical | hypothetical | 56.6 | 56.6 | 0% | 1.0 |
| question_drug | question with drug English | 37.8 | 37.8 | 0% | 1.0 |
| red_flag | red flag pure | 1.8 | 1.8 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.2 | 2.2 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 37.6 | 37.6 | 0% | 1.0 |
| chitchat_followup | chitchat + intake follow-up | 37.4 | 37.4 | 0% | 1.0 |
| unseen_slang | unseen TW slang low-sugar variant | 37.3 | 37.3 | 0% | 1.0 |

## 模式 off / warm（15 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 0.0 | 0.0 |
| conversation_interpreter_ms | 0.1 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.0 | 0.1 |
| b_gate_ms | 0.0 | 0.1 |
| d_gate_ms | 0.0 | 0.2 |
| persistence_ms | 1.3 | 1.7 |
| total_ms | 35.7 | 57.6 |
| fallback_rate | 33.33% | count 5/15 |
| avg_llm_calls | 0.60 | avg_interpreter 0.60 |

### 各類 p50/p95/fallback/LLM（off/warm）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 37.0 | 37.0 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 35.3 | 35.3 | 0% | 1.0 |
| control_pause | control command PAUSED | 35.7 | 35.7 | 0% | 1.0 |
| identity | identity chitchat | 1.8 | 1.8 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 38.0 | 38.0 | 0% | 1.0 |
| mixed | MIXED intake+edu | 37.2 | 37.2 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 37.2 | 37.2 | 0% | 1.0 |
| third_party | third-party friend | 37.2 | 37.2 | 0% | 1.0 |
| hypothetical | hypothetical | 37.6 | 37.6 | 0% | 1.0 |
| question_drug | question with drug English | 57.6 | 57.6 | 0% | 1.0 |
| red_flag | red flag pure | 2.4 | 2.4 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.0 | 2.0 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.3 | 2.3 | 100% | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.5 | 2.5 | 100% | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.4 | 2.4 | 100% | 0.0 |

## 模式 shadow / cold（15 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 163.0 | 181.3 |
| conversation_interpreter_ms | 0.1 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.1 | 0.2 |
| b_gate_ms | 0.1 | 0.1 |
| d_gate_ms | 0.2 | 0.2 |
| persistence_ms | 1.5 | 1.7 |
| total_ms | 200.3 | 220.8 |
| fallback_rate | 13.33% | count 2/15 |
| avg_llm_calls | 0.80 | avg_interpreter 0.80 |

### 各類 p50/p95/fallback/LLM（shadow/cold）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 193.3 | 193.3 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 190.4 | 190.4 | 0% | 1.0 |
| control_pause | control command PAUSED | 200.3 | 200.3 | 0% | 1.0 |
| identity | identity chitchat | 1.8 | 1.8 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 209.7 | 209.7 | 0% | 1.0 |
| mixed | MIXED intake+edu | 219.8 | 219.8 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 173.9 | 173.9 | 0% | 1.0 |
| third_party | third-party friend | 220.8 | 220.8 | 0% | 1.0 |
| hypothetical | hypothetical | 205.8 | 205.8 | 0% | 1.0 |
| question_drug | question with drug English | 201.2 | 201.2 | 0% | 1.0 |
| red_flag | red flag pure | 2.4 | 2.4 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 1.8 | 1.8 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 204.1 | 204.1 | 0% | 1.0 |
| chitchat_followup | chitchat + intake follow-up | 199.6 | 199.6 | 0% | 1.0 |
| unseen_slang | unseen TW slang low-sugar variant | 208.8 | 208.8 | 0% | 1.0 |

## 模式 shadow / warm（15 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 162.4 | 164.4 |
| conversation_interpreter_ms | 0.1 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.1 |
| answer_generator_ms | 0.0 | 0.1 |
| b_gate_ms | 0.0 | 0.1 |
| d_gate_ms | 0.0 | 0.2 |
| persistence_ms | 1.4 | 1.9 |
| total_ms | 190.6 | 230.8 |
| fallback_rate | 33.33% | count 5/15 |
| avg_llm_calls | 0.60 | avg_interpreter 0.60 |

### 各類 p50/p95/fallback/LLM（shadow/warm）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 199.4 | 199.4 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 189.7 | 189.7 | 0% | 1.0 |
| control_pause | control command PAUSED | 190.6 | 190.6 | 0% | 1.0 |
| identity | identity chitchat | 1.9 | 1.9 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 200.8 | 200.8 | 0% | 1.0 |
| mixed | MIXED intake+edu | 196.5 | 196.5 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 196.5 | 196.5 | 0% | 1.0 |
| third_party | third-party friend | 197.2 | 197.2 | 0% | 1.0 |
| hypothetical | hypothetical | 230.8 | 230.8 | 0% | 1.0 |
| question_drug | question with drug English | 203.4 | 203.4 | 0% | 1.0 |
| red_flag | red flag pure | 2.1 | 2.1 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.0 | 2.0 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 1.9 | 1.9 | 100% | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.4 | 2.4 | 100% | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 1.9 | 1.9 | 100% | 0.0 |

## 模式 guarded / cold（15 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 164.8 | 176.0 |
| conversation_interpreter_ms | 0.1 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.1 | 0.1 |
| b_gate_ms | 0.1 | 0.1 |
| d_gate_ms | 0.2 | 0.2 |
| persistence_ms | 1.5 | 1.8 |
| total_ms | 204.0 | 215.2 |
| fallback_rate | 13.33% | count 2/15 |
| avg_llm_calls | 0.80 | avg_interpreter 0.80 |

### 各類 p50/p95/fallback/LLM（guarded/cold）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 191.2 | 191.2 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 193.7 | 193.7 | 0% | 1.0 |
| control_pause | control command PAUSED | 204.0 | 204.0 | 0% | 1.0 |
| identity | identity chitchat | 2.0 | 2.0 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 210.4 | 210.4 | 0% | 1.0 |
| mixed | MIXED intake+edu | 199.4 | 199.4 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 171.3 | 171.3 | 0% | 1.0 |
| third_party | third-party friend | 204.5 | 204.5 | 0% | 1.0 |
| hypothetical | hypothetical | 207.0 | 207.0 | 0% | 1.0 |
| question_drug | question with drug English | 208.8 | 208.8 | 0% | 1.0 |
| red_flag | red flag pure | 1.5 | 1.5 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 1.9 | 1.9 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 208.9 | 208.9 | 0% | 1.0 |
| chitchat_followup | chitchat + intake follow-up | 215.2 | 215.2 | 0% | 1.0 |
| unseen_slang | unseen TW slang low-sugar variant | 214.6 | 214.6 | 0% | 1.0 |

## 模式 guarded / warm（15 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.3 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 162.6 | 168.5 |
| conversation_interpreter_ms | 0.1 | 0.3 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.0 | 0.1 |
| b_gate_ms | 0.0 | 0.1 |
| d_gate_ms | 0.0 | 0.2 |
| persistence_ms | 1.3 | 1.5 |
| total_ms | 196.2 | 213.9 |
| fallback_rate | 33.33% | count 5/15 |
| avg_llm_calls | 0.60 | avg_interpreter 0.60 |

### 各類 p50/p95/fallback/LLM（guarded/warm）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 198.6 | 198.6 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 213.9 | 213.9 | 0% | 1.0 |
| control_pause | control command PAUSED | 193.5 | 193.5 | 0% | 1.0 |
| identity | identity chitchat | 2.0 | 2.0 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 200.9 | 200.9 | 0% | 1.0 |
| mixed | MIXED intake+edu | 208.3 | 208.3 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 208.3 | 208.3 | 0% | 1.0 |
| third_party | third-party friend | 196.2 | 196.2 | 0% | 1.0 |
| hypothetical | hypothetical | 208.1 | 208.1 | 0% | 1.0 |
| question_drug | question with drug English | 201.6 | 201.6 | 0% | 1.0 |
| red_flag | red flag pure | 2.1 | 2.1 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.1 | 2.1 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.3 | 2.3 | 100% | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.0 | 2.0 | 100% | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.5 | 2.5 | 100% | 0.0 |

## Live smoke 10 輪（正式模型，經 env_value，無硬編碼）

- 跳過原因: no CONVERSATION_LLM_MODEL / ROUTER_LLM_MODEL in .env — live smoke skipped (honest: no formal model configured)
- 說明: .env 未配置 CONVERSATION_LLM_MODEL/ROUTER_LLM_MODEL 或 OPENCODE_API_KEY，屬誠實報告（非硬失敗）

## 目標斷言（僅報告）

- red flag <100ms 無 AI/RAG：見上表 red_flag p95 與 interpreter_calls/rag
- deterministic fast path warm p95 <200ms：見 guarded_warm deterministic_fast_path
- Semantic Router warm p95 <250ms：見 guarded_warm semantic_router_ms
- PURE_EDUCATION 不先呼叫 interpreter（spy 計數）：guarded 下 PURE_EDUCATION interpreter_calls 應為 0（若未命中閾值則誠實報告）
- PURE_INTAKE 短答案不呼叫 AI（is_fast_path_eligible）：短答案「我沒有過敏」在 pending 為 allergies 時 eligible=True → 0 AI calls

## 重現

```bash
source .venv/bin/activate  # 或 uv run
python scripts/semantic_router_perf.py
python scripts/semantic_router_perf.py --live-only
cat /tmp/semantic_router_perf.json | jq '.fixture.guarded_warm.stats'
```
