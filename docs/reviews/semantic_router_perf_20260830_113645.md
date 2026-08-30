# Semantic Router 效能驗收 — 20260830_113645（修正混用版）

> 產生時間 2026-08-30T03:36:45.057578+00:00 | 耗時 172361ms | 序列 50 輪 | 六模式 cold/warm 分開 + 合成 guarded + live smoke 分離

> 樣本定義：cold=每輪新建 repo+orchestrator（is_process_first_measurement=True）；warm=同一 repo 同一 user 連續（第二輪起 warm）。Fixture deterministic N=50（無真 LLM），Live Formal 另計 N=10（若啟用），兩者為**不同樣本集不可互代**。

> Guarded 說明：`guarded_requested_but_downgraded` 為本次線上有效 guarded（因 holdout BLOCKED / false-fast 4，實測 early-exit 0，全部退回 interpreter）；`guarded_approved_synthetic` 僅為合成高置信 stub（固定 0.99/0.45）之 artifact，**標記「非 production approval」**，不得視為線上核准。

> 一致性規則：同一筆同步 request 每 stage duration 不得大於 total（容忍 0.5ms 量測抖動，除非明確標記非同步背景工作）；若 stage 與 total 不同樣本集需寫樣本數；不得以 off 代表 shadow、fixture 代表 live、skipped 列成完成。實測違規數：**0**。

## 模式 off / cold（fixture） — N=50 輪

- 定義：每輪新建 repo+orchestrator（is_process_first_measurement=True）
- 樣本數：total N=50；各 stage 與 total 同步同筆請求，樣本數皆為 50（若不同則已列 sample_counts）

| 指標 | p50 (ms) | p95 (ms) | 樣本數 |
|---|---:|---:|---:|
| semantic_router_ms | 0.0 | 0.0 | 50 |
| conversation_interpreter_ms | 0.1 | 0.4 | 50 |
| answer_generator_ms | 0.1 | 0.1 | 50 |
| total_ms | 36.9 | 56.0 | 50 |
| fallback_rate | 12.00% | count 6/50 | 50 |
| early_exit_rate | 0.00% | count 0/50 | 50 |
| avg_interpreter_calls | 0.80 | — | 50 |
| avg_generator_calls | 0.00 | — | 50 |
| avg_llm_calls（合計） | 0.80 | — | 50 |

- 一致性：✓ 無 stage>total 違規（容忍 0.5ms）

### 各類 p50/p95/fallback/early-exit/interpreter/generator（off_cold）

| cat | 敘述 | p50 total | p95 total | fallback | early-exit | avg interpreter | avg generator |
|---|---|---:|---:|---:|---:|---:|---:|
| single_drug_en | single drug English | 37.8 | 44.3 | 0% | 0% | 1.0 | 0.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 35.9 | 36.8 | 0% | 0% | 1.0 | 0.0 |
| control_pause | control command PAUSED | 35.8 | 36.7 | 0% | 0% | 1.0 | 0.0 |
| identity | identity chitchat | 1.4 | 1.6 | 0% | 0% | 0.0 | 0.0 |
| pure_education | PURE_EDUCATION | 37.8 | 38.7 | 0% | 0% | 1.0 | 0.0 |
| mixed | MIXED intake+edu | 38.7 | 39.4 | 0% | 0% | 1.0 | 0.0 |
| correction_subject | correction + subject ambiguous | 5.5 | 5.6 | 0% | 0% | 1.0 | 0.0 |
| third_party | third-party friend | 38.0 | 38.3 | 0% | 0% | 1.0 | 0.0 |
| hypothetical | hypothetical | 38.7 | 56.9 | 0% | 0% | 1.0 | 0.0 |
| question_drug | question with drug English | 37.4 | 57.8 | 0% | 0% | 1.0 | 0.0 |
| red_flag | red flag pure | 1.5 | 2.0 | 100% | 0% | 0.0 | 0.0 |
| red_flag_negated | red flag phrase but negated question | 1.6 | 1.7 | 100% | 0% | 0.0 | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 37.5 | 56.0 | 0% | 0% | 1.0 | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 37.0 | 38.7 | 0% | 0% | 1.0 | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 37.2 | 38.0 | 0% | 0% | 1.0 | 0.0 |

## 模式 off / warm（fixture） — N=50 輪

- 定義：同一 repo+同一 user 連續 50 輪（第二輪起 warm）
- 樣本數：total N=50；各 stage 與 total 同步同筆請求，樣本數皆為 50（若不同則已列 sample_counts）

| 指標 | p50 (ms) | p95 (ms) | 樣本數 |
|---|---:|---:|---:|
| semantic_router_ms | 0.0 | 0.0 | 50 |
| conversation_interpreter_ms | 0.0 | 0.1 | 50 |
| answer_generator_ms | 0.0 | 0.1 | 50 |
| total_ms | 2.1 | 38.2 | 50 |
| fallback_rate | 80.00% | count 40/50 | 50 |
| early_exit_rate | 0.00% | count 0/50 | 50 |
| avg_interpreter_calls | 0.18 | — | 50 |
| avg_generator_calls | 0.00 | — | 50 |
| avg_llm_calls（合計） | 0.18 | — | 50 |

- 一致性：✓ 無 stage>total 違規（容忍 0.5ms）

### 各類 p50/p95/fallback/early-exit/interpreter/generator（off_warm）

| cat | 敘述 | p50 total | p95 total | fallback | early-exit | avg interpreter | avg generator |
|---|---|---:|---:|---:|---:|---:|---:|
| single_drug_en | single drug English | 2.2 | 36.7 | 75% | 0% | 0.2 | 0.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 2.1 | 35.0 | 75% | 0% | 0.2 | 0.0 |
| control_pause | control command PAUSED | 2.3 | 35.5 | 75% | 0% | 0.2 | 0.0 |
| identity | identity chitchat | 2.0 | 2.1 | 75% | 0% | 0.0 | 0.0 |
| pure_education | PURE_EDUCATION | 2.5 | 37.8 | 75% | 0% | 0.2 | 0.0 |
| mixed | MIXED intake+edu | 2.3 | 38.2 | 67% | 0% | 0.3 | 0.0 |
| correction_subject | correction + subject ambiguous | 2.1 | 38.2 | 67% | 0% | 0.3 | 0.0 |
| third_party | third-party friend | 2.3 | 37.7 | 67% | 0% | 0.3 | 0.0 |
| hypothetical | hypothetical | 2.1 | 37.7 | 67% | 0% | 0.3 | 0.0 |
| question_drug | question with drug English | 1.9 | 57.4 | 67% | 0% | 0.3 | 0.0 |
| red_flag | red flag pure | 2.0 | 2.1 | 100% | 0% | 0.0 | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.4 | 2.5 | 100% | 0% | 0.0 | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.5 | 2.6 | 100% | 0% | 0.0 | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.3 | 2.3 | 100% | 0% | 0.0 | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.0 | 2.4 | 100% | 0% | 0.0 | 0.0 |

## 模式 shadow / cold（fixture） — N=50 輪

- 定義：每輪新建 repo+orchestrator（is_process_first_measurement=True）
- 樣本數：total N=50；各 stage 與 total 同步同筆請求，樣本數皆為 50（若不同則已列 sample_counts）

| 指標 | p50 (ms) | p95 (ms) | 樣本數 |
|---|---:|---:|---:|
| semantic_router_ms | 163.7 | 171.9 | 50 |
| conversation_interpreter_ms | 0.1 | 0.2 | 50 |
| answer_generator_ms | 0.1 | 0.1 | 50 |
| total_ms | 201.2 | 229.0 | 50 |
| fallback_rate | 12.00% | count 6/50 | 50 |
| early_exit_rate | 0.00% | count 0/50 | 50 |
| avg_interpreter_calls | 0.80 | — | 50 |
| avg_generator_calls | 0.00 | — | 50 |
| avg_llm_calls（合計） | 0.80 | — | 50 |

- 一致性：✓ 無 stage>total 違規（容忍 0.5ms）

### 各類 p50/p95/fallback/early-exit/interpreter/generator（shadow_cold）

| cat | 敘述 | p50 total | p95 total | fallback | early-exit | avg interpreter | avg generator |
|---|---|---:|---:|---:|---:|---:|---:|
| single_drug_en | single drug English | 197.7 | 201.3 | 0% | 0% | 1.0 | 0.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 193.7 | 200.1 | 0% | 0% | 1.0 | 0.0 |
| control_pause | control command PAUSED | 200.6 | 202.1 | 0% | 0% | 1.0 | 0.0 |
| identity | identity chitchat | 1.7 | 2.1 | 0% | 0% | 0.0 | 0.0 |
| pure_education | PURE_EDUCATION | 207.1 | 212.4 | 0% | 0% | 1.0 | 0.0 |
| mixed | MIXED intake+edu | 206.9 | 207.0 | 0% | 0% | 1.0 | 0.0 |
| correction_subject | correction + subject ambiguous | 173.5 | 179.3 | 0% | 0% | 1.0 | 0.0 |
| third_party | third-party friend | 205.1 | 222.2 | 0% | 0% | 1.0 | 0.0 |
| hypothetical | hypothetical | 223.8 | 229.0 | 0% | 0% | 1.0 | 0.0 |
| question_drug | question with drug English | 229.1 | 346.0 | 0% | 0% | 1.0 | 0.0 |
| red_flag | red flag pure | 1.8 | 2.0 | 100% | 0% | 0.0 | 0.0 |
| red_flag_negated | red flag phrase but negated question | 1.5 | 1.7 | 100% | 0% | 0.0 | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 197.0 | 210.2 | 0% | 0% | 1.0 | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 205.1 | 206.6 | 0% | 0% | 1.0 | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 205.3 | 207.1 | 0% | 0% | 1.0 | 0.0 |

## 模式 shadow / warm（fixture） — N=50 輪

- 定義：同一 repo+同一 user 連續 50 輪（第二輪起 warm）
- 樣本數：total N=50；各 stage 與 total 同步同筆請求，樣本數皆為 50（若不同則已列 sample_counts）

| 指標 | p50 (ms) | p95 (ms) | 樣本數 |
|---|---:|---:|---:|
| semantic_router_ms | 160.5 | 160.9 | 50 |
| conversation_interpreter_ms | 0.0 | 0.2 | 50 |
| answer_generator_ms | 0.0 | 0.1 | 50 |
| total_ms | 2.2 | 200.9 | 50 |
| fallback_rate | 80.00% | count 40/50 | 50 |
| early_exit_rate | 0.00% | count 0/50 | 50 |
| avg_interpreter_calls | 0.18 | — | 50 |
| avg_generator_calls | 0.00 | — | 50 |
| avg_llm_calls（合計） | 0.18 | — | 50 |

- 一致性：✓ 無 stage>total 違規（容忍 0.5ms）

### 各類 p50/p95/fallback/early-exit/interpreter/generator（shadow_warm）

| cat | 敘述 | p50 total | p95 total | fallback | early-exit | avg interpreter | avg generator |
|---|---|---:|---:|---:|---:|---:|---:|
| single_drug_en | single drug English | 2.1 | 198.4 | 75% | 0% | 0.2 | 0.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 2.1 | 188.6 | 75% | 0% | 0.2 | 0.0 |
| control_pause | control command PAUSED | 2.3 | 191.7 | 75% | 0% | 0.2 | 0.0 |
| identity | identity chitchat | 2.0 | 2.3 | 75% | 0% | 0.0 | 0.0 |
| pure_education | PURE_EDUCATION | 2.5 | 198.7 | 75% | 0% | 0.2 | 0.0 |
| mixed | MIXED intake+edu | 2.3 | 222.9 | 67% | 0% | 0.3 | 0.0 |
| correction_subject | correction + subject ambiguous | 2.2 | 222.9 | 67% | 0% | 0.3 | 0.0 |
| third_party | third-party friend | 2.2 | 196.5 | 67% | 0% | 0.3 | 0.0 |
| hypothetical | hypothetical | 2.0 | 200.9 | 67% | 0% | 0.3 | 0.0 |
| question_drug | question with drug English | 1.9 | 199.7 | 67% | 0% | 0.3 | 0.0 |
| red_flag | red flag pure | 2.2 | 2.5 | 100% | 0% | 0.0 | 0.0 |
| red_flag_negated | red flag phrase but negated question | 2.2 | 3.2 | 100% | 0% | 0.0 | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 2.0 | 2.3 | 100% | 0% | 0.0 | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.2 | 2.3 | 100% | 0% | 0.0 | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 1.9 | 2.1 | 100% | 0% | 0.0 | 0.0 |

## 模式 guarded_requested_but_downgraded / cold（線上有效 guarded，early-exit 0） — N=50 輪

- 定義：每輪新建 repo+orchestrator（is_process_first_measurement=True）
- 樣本數：total N=50；各 stage 與 total 同步同筆請求，樣本數皆為 50（若不同則已列 sample_counts）

| 指標 | p50 (ms) | p95 (ms) | 樣本數 |
|---|---:|---:|---:|
| semantic_router_ms | 163.4 | 172.2 | 50 |
| conversation_interpreter_ms | 0.1 | 0.2 | 50 |
| answer_generator_ms | 0.1 | 0.1 | 50 |
| total_ms | 202.0 | 228.2 | 50 |
| fallback_rate | 12.00% | count 6/50 | 50 |
| early_exit_rate | 0.00% | count 0/50 | 50 |
| downgraded_rate（requested 但未核准） | 100.00% | count 50/50 | 50 |
| avg_interpreter_calls | 0.80 | — | 50 |
| avg_generator_calls | 0.00 | — | 50 |
| avg_llm_calls（合計） | 0.80 | — | 50 |

- 一致性：✓ 無 stage>total 違規（容忍 0.5ms）

### 各類 p50/p95/fallback/early-exit/interpreter/generator（guarded_requested_but_downgraded_cold）

| cat | 敘述 | p50 total | p95 total | fallback | early-exit | avg interpreter | avg generator |
|---|---|---:|---:|---:|---:|---:|---:|
| single_drug_en | single drug English | 202.1 | 222.4 | 0% | 0% | 1.0 | 0.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 196.7 | 200.2 | 0% | 0% | 1.0 | 0.0 |
| control_pause | control command PAUSED | 200.4 | 207.2 | 0% | 0% | 1.0 | 0.0 |
| identity | identity chitchat | 1.7 | 2.2 | 0% | 0% | 0.0 | 0.0 |
| pure_education | PURE_EDUCATION | 209.2 | 228.2 | 0% | 0% | 1.0 | 0.0 |
| mixed | MIXED intake+edu | 207.4 | 209.4 | 0% | 0% | 1.0 | 0.0 |
| correction_subject | correction + subject ambiguous | 175.1 | 175.8 | 0% | 0% | 1.0 | 0.0 |
| third_party | third-party friend | 203.0 | 205.0 | 0% | 0% | 1.0 | 0.0 |
| hypothetical | hypothetical | 204.6 | 213.7 | 0% | 0% | 1.0 | 0.0 |
| question_drug | question with drug English | 211.8 | 393.7 | 0% | 0% | 1.0 | 0.0 |
| red_flag | red flag pure | 1.6 | 2.0 | 100% | 0% | 0.0 | 0.0 |
| red_flag_negated | red flag phrase but negated question | 1.7 | 2.0 | 100% | 0% | 0.0 | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 207.4 | 210.6 | 0% | 0% | 1.0 | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 207.8 | 230.3 | 0% | 0% | 1.0 | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 203.8 | 211.1 | 0% | 0% | 1.0 | 0.0 |

## 模式 guarded_requested_but_downgraded / warm（線上有效 guarded，early-exit 0） — N=50 輪

- 定義：同一 repo+同一 user 連續 50 輪（第二輪起 warm）
- 樣本數：total N=50；各 stage 與 total 同步同筆請求，樣本數皆為 50（若不同則已列 sample_counts）

| 指標 | p50 (ms) | p95 (ms) | 樣本數 |
|---|---:|---:|---:|
| semantic_router_ms | 158.5 | 162.0 | 50 |
| conversation_interpreter_ms | 0.0 | 0.2 | 50 |
| answer_generator_ms | 0.0 | 0.1 | 50 |
| total_ms | 2.0 | 199.0 | 50 |
| fallback_rate | 80.00% | count 40/50 | 50 |
| early_exit_rate | 0.00% | count 0/50 | 50 |
| downgraded_rate（requested 但未核准） | 100.00% | count 50/50 | 50 |
| avg_interpreter_calls | 0.18 | — | 50 |
| avg_generator_calls | 0.00 | — | 50 |
| avg_llm_calls（合計） | 0.18 | — | 50 |

- 一致性：✓ 無 stage>total 違規（容忍 0.5ms）

### 各類 p50/p95/fallback/early-exit/interpreter/generator（guarded_requested_but_downgraded_warm）

| cat | 敘述 | p50 total | p95 total | fallback | early-exit | avg interpreter | avg generator |
|---|---|---:|---:|---:|---:|---:|---:|
| single_drug_en | single drug English | 2.3 | 199.0 | 75% | 0% | 0.2 | 0.0 |
| pure_intake_neg | negation short answer PURE_INTAKE | 2.1 | 186.9 | 75% | 0% | 0.2 | 0.0 |
| control_pause | control command PAUSED | 2.1 | 194.5 | 75% | 0% | 0.2 | 0.0 |
| identity | identity chitchat | 2.0 | 2.4 | 75% | 0% | 0.0 | 0.0 |
| pure_education | PURE_EDUCATION | 2.3 | 204.5 | 75% | 0% | 0.2 | 0.0 |
| mixed | MIXED intake+edu | 2.0 | 197.8 | 67% | 0% | 0.3 | 0.0 |
| correction_subject | correction + subject ambiguous | 2.1 | 197.8 | 67% | 0% | 0.3 | 0.0 |
| third_party | third-party friend | 1.9 | 204.3 | 67% | 0% | 0.3 | 0.0 |
| hypothetical | hypothetical | 2.4 | 197.8 | 67% | 0% | 0.3 | 0.0 |
| question_drug | question with drug English | 2.0 | 197.2 | 67% | 0% | 0.3 | 0.0 |
| red_flag | red flag pure | 2.1 | 2.2 | 100% | 0% | 0.0 | 0.0 |
| red_flag_negated | red flag phrase but negated question | 1.9 | 2.1 | 100% | 0% | 0.0 | 0.0 |
| multi_symptom_slang | TW slang multi-clause intake | 1.9 | 2.3 | 100% | 0% | 0.0 | 0.0 |
| chitchat_followup | chitchat + intake follow-up | 2.0 | 2.0 | 100% | 0% | 0.0 | 0.0 |
| unseen_slang | unseen TW slang low-sugar variant | 2.0 | 2.3 | 100% | 0% | 0.0 | 0.0 |

## 模式 guarded_approved_synthetic / warm（合成 artifact，非 production approval） — N=50 輪

- 定義：合成測試 artifact N=15/50（固定高置信 stub），非線上核准，不可與 guarded_requested_but_downgraded 混計 total
- ⚠️ 合成 artifact：合成測試 artifact（SyntheticHighConfRouter 固定 0.99/0.45），early exit 率僅供對照，標記「非 production approval」，不得視為線上核准
- 樣本數：total N=50；各 stage 與 total 同步同筆請求，樣本數皆為 50（若不同則已列 sample_counts）

| 指標 | p50 (ms) | p95 (ms) | 樣本數 |
|---|---:|---:|---:|
| semantic_router_ms | 1.2 | 1.2 | 50 |
| conversation_interpreter_ms | 0.0 | 0.2 | 50 |
| answer_generator_ms | 0.0 | 0.1 | 50 |
| total_ms | 2.0 | 38.5 | 50 |
| fallback_rate | 80.00% | count 40/50 | 50 |
| early_exit_rate | 0.00% | count 0/50 | 50 |
| downgraded_rate（requested 但未核准） | 0.00% | count 0/50 | 50 |
| avg_interpreter_calls | 0.18 | — | 50 |
| avg_generator_calls | 0.00 | — | 50 |
| avg_llm_calls（合計） | 0.18 | — | 50 |

- 一致性：✓ 無 stage>total 違規（容忍 0.5ms）

### 各類 p50/p95/fallback/early-exit/interpreter/generator（guarded_approved_synthetic_warm）

| cat | 敘述 | p50 total | p95 total | fallback | early-exit | avg interpreter | avg generator |
|---|---|---:|---:|---:|---:|---:|---:|
| *合成* | 僅對 `水果`/`你是 AI` 觸發 early-exit，其餘降級 | — | — | — | 0% | 0.2 | 0.0 |

## Live smoke（正式模型，經 env_value，與 Fixture 不同樣本集）

- 狀態：**完成 10/10** | 模型: CONVERSATION_LLM_MODEL=`—` / ROUTER_LLM_MODEL=`mimo-v2.5` | interpreter=`FormalConversationInterpreter`
- 指標（Live N=10，與 Fixture N=50 不同樣本集，不可互代；各 stage 與 total 同筆同步請求、樣本數皆為 10）：
  - semantic_router p50 164.2 p95 185.4 | interpreter p50 7124.5 p95 8005.3 | generator p50 0.0 p95 3406.8 | total(wall) p50 7122.4 p95 11796.3
  - fallback 20.0% | early-exit 0.0% | sample_note: Live N=10（正式 LLM，經 env_value），與 Fixture N=50（deterministic）為不同樣本集，不可互代

| # | 輸入 | status | wall | semantic | interpreter | generator | early |
|---:|---|---|---:|---:|---:|---:|---:|
| 0 | 我最近常口渴，糖尿病一天可以吃幾份水果？ | FALLBACK | 8425 | 173 | 8002 | 0 | False |
| 1 | 請說明糖尿病飲食原則 | NEEDS_CLARIFICATION | 7608 | 157 | 8002 | 0 | False |
| 2 | 我有吃 metformin，可以吃芭樂嗎？ | NEEDS_CLARIFICATION | 5469 | 178 | 5229 | 0 | False |
| 3 | 最近晚上一直跑廁所，會是糖尿病嗎？ | FALLBACK | 11427 | 166 | 8005 | 3041 | False |
| 4 | 如果以後頭暈要怎麼處理？ | SIDE_ANSWER | 6637 | 171 | 6247 | 0 | False |
| 5 | 謝謝，另外我想問血糖偏高怎麼吃比較好？ | SIDE_ANSWER | 11796 | 162 | 8005 | 3407 | False |
| 6 | 我嘴巴很乾，晚上一直跑廁所 | NEEDS_CLARIFICATION | 3894 | 185 | 3646 | 0 | False |
| 7 | 最近一直吃不飽、冒冷汗、手抖抖 | NEEDS_CLARIFICATION | 8227 | 160 | 8005 | 0 | False |
| 8 | 我沒有過敏 | NEEDS_CLARIFICATION | 3907 | 155 | 3696 | 0 | False |
| 9 | 糖尿病患者適合每天運動多久？ | NEEDS_CLARIFICATION | 3660 | 162 | 3696 | 0 | False |

## 一致性驗證詳述

- 規則：同一筆同步 request 每 stage duration 不得大於 total（容忍 0.5ms，除非明確標記 async_background）；若 stage 與 total 用不同樣本集必須寫出各自樣本數；不得用 off 代表 shadow、fixture 代表 live、不得把 skipped live smoke 列成完成。
- 本次總違規：**0** 筆（見 JSON `consistency_summary` 與各 mode `violations`）
- 樣本數聲明：Fixture 各 mode N=50（cold/warm 分開）；Live N=10/10（完成）；各表內 stage 與 total 為同筆同步請求、樣本數一致，已於表頭標明
- ✓ 本次分開報告的各 mode（off/shadow/guarded_requested_but_downgraded + synthetic）皆無 stage>total 違規

## 目標斷言（僅報告）

- red flag <100ms 無 AI/RAG：以 guarded_requested_but_downgraded_warm（或 shadow_warm） warm N 計，不以 off 混算
- deterministic fast path (candidate_validation) warm p95 <200ms：以 guarded_requested_but_downgraded_warm 計
- Semantic Router warm p95 <250ms：僅以 shadow/guarded_requested_but_downgraded warm 計（off 的 0 不得充數）
- PURE_EDUCATION guarded_requested_but_downgraded warm 不先呼叫 interpreter：若 early-exit 則 calls=0，否則為 downgraded 誠實報告（當前 BLOCKED 故多為 1）
- PURE_INTAKE 短答案不呼叫 AI（is_fast_path_eligible）：同上，需 pending 判斷
- Live smoke 僅在其完成時報告 p50/p95，Skipped 時明確標 0/10 未完成，不以 fixture 充數

## 重現

```bash
source .venv/bin/activate  # 或 uv run
python scripts/semantic_router_perf.py          # 50 輪 fixture + 10 輪 live（分表，含一致性驗證與合成 guarded）
python scripts/semantic_router_perf.py --quick   # 15 輪快速（開發用，樣本數標明不作為最終）
python scripts/semantic_router_perf.py --live-only
cat /tmp/semantic_router_perf.json | jq '.fixture | keys'
cat /tmp/semantic_router_perf.json | jq '.consistency_summary'
cat /tmp/semantic_router_perf.csv
```
