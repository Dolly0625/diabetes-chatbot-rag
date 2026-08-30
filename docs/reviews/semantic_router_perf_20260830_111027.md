# Semantic Router 效能驗收 — 20260830_111027

> 產生時間 2026-08-30T03:10:27.065658+00:00 | 耗時 128940ms | 序列 50 輪 × 3 模式 (cold/warm) | live 10 輪

> 註：p50/p95 為 fixture 決定性路徑（無真 LLM），live smoke 另列。目標斷言僅報告，live 波動不硬失敗。

## 模式 off / cold（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.2 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 0.0 | 0.0 |
| conversation_interpreter_ms | 0.1 | 0.5 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.1 | 0.1 |
| b_gate_ms | 0.1 | 0.1 |
| d_gate_ms | 0.2 | 0.2 |
| persistence_ms | 1.1 | 1.5 |
| total_ms | 38.4 | 56.3 |
| fallback_rate | 12.00% | count 6/50 |
| avg_llm_calls | 0.80 | avg_interpreter 0.80 |

### 各類 p50/p95/fallback/LLM（off/cold）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 38.6 | 48.3 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 37.7 | 39.5 | 0% | 1.0 |
| control_pause | control command PAUSED | 36.8 | 39.6 | 0% | 1.0 |
| identity | identity chitchat | 1.5 | 1.8 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 38.9 | 41.1 | 0% | 1.0 |
| mixed | MIXED intake+edu | 38.6 | 41.5 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 5.4 | 6.0 | 0% | 1.0 |
| third_party | third-party friend | 38.7 | 39.3 | 0% | 1.0 |
| hypothetical | hypothetical | 38.7 | 58.9 | 0% | 1.0 |
| question_drug | question with drug English | 39.3 | 62.9 | 0% | 1.0 |
| red_flag | red flag pure | 1.5 | 1.6 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 1.7 | 1.8 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 37.2 | 56.3 | 0% | 1.0 |
| chitchat_followup | chitchat + intake follow-up | 38.8 | 38.9 | 0% | 1.0 |
| unseen_slang | unseen TW slang low-sugar variant | 38.6 | 38.8 | 0% | 1.0 |

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
| persistence_ms | 1.2 | 1.5 |
| total_ms | 2.3 | 39.6 |
| fallback_rate | 80.00% | count 40/50 |
| avg_llm_calls | 0.18 | avg_interpreter 0.18 |

### 各類 p50/p95/fallback/LLM（off/warm）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 2.2 | 38.9 | 75% | 0.2 |
| pure_intake_neg | negation short answer PURE_INTAKE | 2.4 | 37.0 | 75% | 0.2 |
| control_pause | control command PAUSED | 2.3 | 36.8 | 75% | 0.2 |
| identity | identity chitchat | 2.2 | 2.3 | 75% | 0.0 |
| pure_education | PURE_EDUCATION | 2.7 | 39.6 | 75% | 0.2 |
| mixed | MIXED intake+edu | 2.4 | 39.6 | 67% | 0.3 |
| correction_subject | correction + subject ambiguous | 2.2 | 39.6 | 67% | 0.3 |
| third_party | third-party friend | 2.3 | 38.6 | 67% | 0.3 |
| hypothetical | hypothetical | 2.4 | 38.2 | 67% | 0.3 |
| question_drug | question with drug English | 2.2 | 58.8 | 67% | 0.3 |
| red_flag | red flag pure | 2.5 | 2.5 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.3 | 2.3 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.6 | 2.6 | 100% | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.1 | 2.5 | 100% | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.1 | 2.1 | 100% | 0.0 |

## 模式 shadow / cold（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 171.9 | 200.0 |
| conversation_interpreter_ms | 0.1 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.1 | 0.1 |
| b_gate_ms | 0.1 | 0.1 |
| d_gate_ms | 0.2 | 0.2 |
| persistence_ms | 1.4 | 1.7 |
| total_ms | 210.9 | 611.9 |
| fallback_rate | 12.00% | count 6/50 |
| avg_llm_calls | 0.80 | avg_interpreter 0.80 |

### 各類 p50/p95/fallback/LLM（shadow/cold）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 417.1 | 611.5 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 219.5 | 240.1 | 0% | 1.0 |
| control_pause | control command PAUSED | 209.5 | 427.6 | 0% | 1.0 |
| identity | identity chitchat | 1.8 | 1.9 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 212.5 | 610.9 | 0% | 1.0 |
| mixed | MIXED intake+edu | 213.4 | 438.0 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 567.2 | 577.7 | 0% | 1.0 |
| third_party | third-party friend | 234.7 | 614.4 | 0% | 1.0 |
| hypothetical | hypothetical | 208.5 | 293.6 | 0% | 1.0 |
| question_drug | question with drug English | 212.1 | 241.9 | 0% | 1.0 |
| red_flag | red flag pure | 1.9 | 1.9 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.0 | 2.2 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 207.2 | 229.7 | 0% | 1.0 |
| chitchat_followup | chitchat + intake follow-up | 611.9 | 612.9 | 0% | 1.0 |
| unseen_slang | unseen TW slang low-sugar variant | 298.5 | 589.2 | 0% | 1.0 |

## 模式 shadow / warm（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.3 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 162.0 | 200.0 |
| conversation_interpreter_ms | 0.0 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.0 | 0.1 |
| b_gate_ms | 0.0 | 0.1 |
| d_gate_ms | 0.0 | 0.2 |
| persistence_ms | 1.1 | 1.8 |
| total_ms | 2.2 | 427.7 |
| fallback_rate | 80.00% | count 40/50 |
| avg_llm_calls | 0.18 | avg_interpreter 0.18 |

### 各類 p50/p95/fallback/LLM（shadow/warm）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 2.2 | 484.5 | 75% | 0.2 |
| pure_intake_neg | negation short answer PURE_INTAKE | 2.2 | 192.6 | 75% | 0.2 |
| control_pause | control command PAUSED | 2.3 | 202.9 | 75% | 0.2 |
| identity | identity chitchat | 2.1 | 2.3 | 75% | 0.0 |
| pure_education | PURE_EDUCATION | 2.4 | 202.0 | 75% | 0.2 |
| mixed | MIXED intake+edu | 2.4 | 427.7 | 67% | 0.3 |
| correction_subject | correction + subject ambiguous | 2.4 | 427.7 | 67% | 0.3 |
| third_party | third-party friend | 2.6 | 199.7 | 67% | 0.3 |
| hypothetical | hypothetical | 2.2 | 527.3 | 67% | 0.3 |
| question_drug | question with drug English | 2.2 | 201.1 | 67% | 0.3 |
| red_flag | red flag pure | 2.2 | 2.3 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.0 | 2.5 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.2 | 2.2 | 100% | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.1 | 2.4 | 100% | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.2 | 2.4 | 100% | 0.0 |

## 模式 guarded / cold（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.2 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 171.4 | 200.0 |
| conversation_interpreter_ms | 0.1 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.1 | 0.1 |
| b_gate_ms | 0.1 | 0.1 |
| d_gate_ms | 0.2 | 0.2 |
| persistence_ms | 1.5 | 1.9 |
| total_ms | 210.9 | 612.2 |
| fallback_rate | 12.00% | count 6/50 |
| avg_llm_calls | 0.80 | avg_interpreter 0.80 |

### 各類 p50/p95/fallback/LLM（guarded/cold）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 200.9 | 229.3 | 0% | 1.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 491.6 | 612.8 | 0% | 1.0 |
| control_pause | control command PAUSED | 441.2 | 608.0 | 0% | 1.0 |
| identity | identity chitchat | 2.0 | 2.5 | 0% | 0.0 |
| pure_education | PURE_EDUCATION | 441.7 | 644.3 | 0% | 1.0 |
| mixed | MIXED intake+edu | 217.0 | 311.8 | 0% | 1.0 |
| correction_subject | correction + subject ambiguous | 179.9 | 427.3 | 0% | 1.0 |
| third_party | third-party friend | 211.6 | 244.3 | 0% | 1.0 |
| hypothetical | hypothetical | 232.6 | 463.9 | 0% | 1.0 |
| question_drug | question with drug English | 210.3 | 490.7 | 0% | 1.0 |
| red_flag | red flag pure | 1.8 | 2.3 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.1 | 2.2 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 292.9 | 605.9 | 0% | 1.0 |
| chitchat_followup | chitchat + intake follow-up | 239.1 | 354.9 | 0% | 1.0 |
| unseen_slang | unseen TW slang low-sugar variant | 211.4 | 610.3 | 0% | 1.0 |

## 模式 guarded / warm（50 輪）

| 階段 | p50 (ms) | p95 (ms) |
|---|---:|---:|
| red_flag_and_auth_ms | 0.3 | 0.3 |
| deterministic_fast_path_ms | 0.0 | 0.0 |
| semantic_router_ms | 165.0 | 200.0 |
| conversation_interpreter_ms | 0.0 | 0.2 |
| rag_retrieval_ms | 0.0 | 0.0 |
| answer_generator_ms | 0.0 | 0.1 |
| b_gate_ms | 0.0 | 0.1 |
| d_gate_ms | 0.0 | 0.2 |
| persistence_ms | 1.1 | 1.9 |
| total_ms | 2.2 | 438.0 |
| fallback_rate | 80.00% | count 40/50 |
| avg_llm_calls | 0.18 | avg_interpreter 0.18 |

### 各類 p50/p95/fallback/LLM（guarded/warm）

| cat | 敘述 | p50 total | p95 total | fallback | avg LLM |
|---|---|---:|---:|---:|---:|
| single_drug_en | single drug English | 2.2 | 438.0 | 75% | 0.2 |
| pure_intake_neg | negation short answer PURE_INTAKE | 2.6 | 188.3 | 75% | 0.2 |
| control_pause | control command PAUSED | 2.1 | 200.6 | 75% | 0.2 |
| identity | identity chitchat | 2.1 | 2.6 | 75% | 0.0 |
| pure_education | PURE_EDUCATION | 2.3 | 552.9 | 75% | 0.2 |
| mixed | MIXED intake+edu | 2.5 | 196.5 | 67% | 0.3 |
| correction_subject | correction + subject ambiguous | 2.1 | 196.5 | 67% | 0.3 |
| third_party | third-party friend | 2.2 | 527.1 | 67% | 0.3 |
| hypothetical | hypothetical | 2.0 | 200.9 | 67% | 0.3 |
| question_drug | question with drug English | 2.2 | 204.3 | 67% | 0.3 |
| red_flag | red flag pure | 2.3 | 2.9 | 100% | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.1 | 2.2 | 100% | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.2 | 2.3 | 100% | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.2 | 2.5 | 100% | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.3 | 2.8 | 100% | 0.0 |

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
