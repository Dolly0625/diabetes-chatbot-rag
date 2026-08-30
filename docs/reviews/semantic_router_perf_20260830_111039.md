# Semantic Router 效能驗收 — 20260830_111039

> 產生時間 2026-08-30T03:10:39.951154+00:00 | 耗時 126169ms | 序列 50 輪 × 3 模式 (cold/warm) | live 10 輪

> 註：p50/p95 為 fixture 決定性路徑（無真 LLM），live smoke 另列。目標斷言僅報告，live 波動不硬失敗。

## 模式 off / cold（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.2 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 0.0 | 0.0 |
| conversation_interpreter_ms | 0.1 | 0.4 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.1 | 0.1 |
| b_gate_ms | 0.1 | 0.1 |
| d_gate_ms | 0.1 | 0.2 |
| persistence_ms | 1.1 | 1.5 |
| total_ms | 37.6 | 56.1 |
| fallback_rate | 12.00% | count 6/50 |
| avg_llm_calls | 0.80 | avg_interpreter 0.80 |

### 各類 p50/p95/fallback/LLM（off/cold）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 38.7 | 46.6 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 36.5 | 37.5 | 0% | 1.0 |
| control_pause | control command PAUSED | 36.9 | 37.3 | 0% | 1.0 |
| identity | identity chitchat | 1.4 | 1.8 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 38.0 | 38.6 | 0% | 1.0 |
| mixed | MIXED intake+edu | 38.5 | 40.4 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 5.7 | 6.5 | 0% | 1.0 |
| third_party | third-party friend | 38.2 | 38.4 | 0% | 1.0 |
| hypothetical | hypothetical | 37.9 | 39.7 | 0% | 1.0 |
| question_drug | question with drug English | 61.0 | 61.2 | 0% | 1.0 |
| red_flag | red flag pure | 1.6 | 1.7 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 1.5 | 1.7 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 37.1 | 37.4 | 0% | 1.0 |
| chitchat_followup | chitchat + intake follow-up | 38.7 | 39.4 | 0% | 1.0 |
| unseen_slang | unseen TW slang low-sugar variant | 37.8 | 38.0 | 0% | 1.0 |

## 模式 off / warm（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.3 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 0.0 | 0.0 |
| conversation_interpreter_ms | 0.0 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.0 | 0.1 |
| b_gate_ms | 0.0 | 0.1 |
| d_gate_ms | 0.0 | 0.2 |
| persistence_ms | 1.1 | 1.5 |
| total_ms | 2.2 | 38.7 |
| fallback_rate | 80.00% | count 40/50 |
| avg_llm_calls | 0.18 | avg_interpreter 0.18 |

### 各類 p50/p95/fallback/LLM（off/warm）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 2.3 | 38.4 | 75% | 0.2 |
| pure_intake_neg | negation short answer PURE_INTAKE | 2.4 | 36.3 | 75% | 0.2 |
| control_pause | control command PAUSED | 2.1 | 36.9 | 75% | 0.2 |
| identity | identity chitchat | 2.1 | 2.4 | 75% | 0.0 |
| pure_education | PURE_EDUCATION | 2.3 | 62.7 | 75% | 0.2 |
| mixed | MIXED intake+edu | 2.4 | 38.6 | 67% | 0.3 |
| correction_subject | correction + subject ambiguous | 2.4 | 38.6 | 67% | 0.3 |
| third_party | third-party friend | 2.6 | 38.9 | 67% | 0.3 |
| hypothetical | hypothetical | 2.3 | 38.5 | 67% | 0.3 |
| question_drug | question with drug English | 2.5 | 38.7 | 67% | 0.3 |
| red_flag | red flag pure | 2.1 | 2.4 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.3 | 2.5 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.1 | 2.3 | 100% | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.0 | 2.0 | 100% | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.0 | 2.3 | 100% | 0.0 |

## 模式 shadow / cold（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 174.9 | 200.0 |
| conversation_interpreter_ms | 0.1 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.1 | 0.1 |
| b_gate_ms | 0.1 | 0.1 |
| d_gate_ms | 0.2 | 0.2 |
| persistence_ms | 1.3 | 1.7 |
| total_ms | 217.4 | 615.0 |
| fallback_rate | 12.00% | count 6/50 |
| avg_llm_calls | 0.80 | avg_interpreter 0.80 |

### 各類 p50/p95/fallback/LLM（shadow/cold）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 211.2 | 579.0 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 236.2 | 250.0 | 0% | 1.0 |
| control_pause | control command PAUSED | 388.3 | 615.2 | 0% | 1.0 |
| identity | identity chitchat | 1.7 | 1.8 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 609.8 | 616.1 | 0% | 1.0 |
| mixed | MIXED intake+edu | 594.9 | 615.0 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 186.1 | 583.3 | 0% | 1.0 |
| third_party | third-party friend | 218.8 | 610.0 | 0% | 1.0 |
| hypothetical | hypothetical | 211.9 | 607.4 | 0% | 1.0 |
| question_drug | question with drug English | 600.6 | 609.1 | 0% | 1.0 |
| red_flag | red flag pure | 2.0 | 2.4 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 1.9 | 2.0 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 539.3 | 614.6 | 0% | 1.0 |
| chitchat_followup | chitchat + intake follow-up | 217.3 | 217.7 | 0% | 1.0 |
| unseen_slang | unseen TW slang low-sugar variant | 210.4 | 239.8 | 0% | 1.0 |

## 模式 shadow / warm（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.3 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 158.9 | 200.0 |
| conversation_interpreter_ms | 0.0 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.0 | 0.1 |
| b_gate_ms | 0.0 | 0.1 |
| d_gate_ms | 0.0 | 0.2 |
| persistence_ms | 1.1 | 1.7 |
| total_ms | 2.2 | 534.4 |
| fallback_rate | 80.00% | count 40/50 |
| avg_llm_calls | 0.18 | avg_interpreter 0.18 |

### 各類 p50/p95/fallback/LLM（shadow/warm）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 2.3 | 254.0 | 75% | 0.2 |
| pure_intake_neg | negation short answer PURE_INTAKE | 2.2 | 346.7 | 75% | 0.2 |
| control_pause | control command PAUSED | 2.1 | 194.8 | 75% | 0.2 |
| identity | identity chitchat | 2.1 | 2.3 | 75% | 0.0 |
| pure_education | PURE_EDUCATION | 2.2 | 201.6 | 75% | 0.2 |
| mixed | MIXED intake+edu | 2.2 | 534.4 | 67% | 0.3 |
| correction_subject | correction + subject ambiguous | 2.0 | 534.4 | 67% | 0.3 |
| third_party | third-party friend | 2.3 | 195.5 | 67% | 0.3 |
| hypothetical | hypothetical | 2.1 | 601.8 | 67% | 0.3 |
| question_drug | question with drug English | 2.0 | 198.2 | 67% | 0.3 |
| red_flag | red flag pure | 2.2 | 2.3 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.2 | 2.2 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.5 | 2.5 | 100% | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.3 | 2.4 | 100% | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.3 | 3.1 | 100% | 0.0 |

## 模式 guarded / cold（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 169.6 | 200.0 |
| conversation_interpreter_ms | 0.1 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.1 | 0.1 |
| b_gate_ms | 0.1 | 0.1 |
| d_gate_ms | 0.1 | 0.2 |
| persistence_ms | 1.4 | 1.7 |
| total_ms | 208.2 | 607.7 |
| fallback_rate | 12.00% | count 6/50 |
| avg_llm_calls | 0.80 | avg_interpreter 0.80 |

### 各類 p50/p95/fallback/LLM（guarded/cold）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 273.6 | 604.5 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 203.1 | 210.4 | 0% | 1.0 |
| control_pause | control command PAUSED | 206.5 | 607.0 | 0% | 1.0 |
| identity | identity chitchat | 1.8 | 2.0 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 325.4 | 627.8 | 0% | 1.0 |
| mixed | MIXED intake+edu | 441.3 | 633.9 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 400.6 | 403.5 | 0% | 1.0 |
| third_party | third-party friend | 207.3 | 606.1 | 0% | 1.0 |
| hypothetical | hypothetical | 607.4 | 607.7 | 0% | 1.0 |
| question_drug | question with drug English | 208.2 | 241.9 | 0% | 1.0 |
| red_flag | red flag pure | 1.8 | 2.4 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.2 | 2.3 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 207.9 | 214.5 | 0% | 1.0 |
| chitchat_followup | chitchat + intake follow-up | 213.3 | 332.1 | 0% | 1.0 |
| unseen_slang | unseen TW slang low-sugar variant | 210.4 | 211.0 | 0% | 1.0 |

## 模式 guarded / warm（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.3 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 159.4 | 163.3 |
| conversation_interpreter_ms | 0.0 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.0 | 0.1 |
| b_gate_ms | 0.0 | 0.1 |
| d_gate_ms | 0.0 | 0.2 |
| persistence_ms | 1.1 | 1.4 |
| total_ms | 2.2 | 203.2 |
| fallback_rate | 80.00% | count 40/50 |
| avg_llm_calls | 0.18 | avg_interpreter 0.18 |

### 各類 p50/p95/fallback/LLM（guarded/warm）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 2.4 | 198.9 | 75% | 0.2 |
| pure_intake_neg | negation short answer PURE_INTAKE | 2.3 | 192.9 | 75% | 0.2 |
| control_pause | control command PAUSED | 2.3 | 198.9 | 75% | 0.2 |
| identity | identity chitchat | 2.1 | 2.3 | 75% | 0.0 |
| pure_education | PURE_EDUCATION | 2.3 | 229.8 | 75% | 0.2 |
| mixed | MIXED intake+edu | 2.1 | 203.2 | 67% | 0.3 |
| correction_subject | correction + subject ambiguous | 2.1 | 203.2 | 67% | 0.3 |
| third_party | third-party friend | 2.6 | 202.4 | 67% | 0.3 |
| hypothetical | hypothetical | 2.0 | 210.6 | 67% | 0.3 |
| question_drug | question with drug English | 2.1 | 198.8 | 67% | 0.3 |
| red_flag | red flag pure | 2.3 | 2.5 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.2 | 2.3 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.3 | 2.5 | 100% | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.1 | 2.2 | 100% | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.3 | 2.6 | 100% | 0.0 |

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
