# Semantic Router production evaluation

> Dataset: `experiments/semantic_router_production/dataset.json` | Version: `semantic-router-production.v1` | Split evaluated: `holdout`
> Generated: 2026-08-30T02:55:49Z | Family-split: `False` | Leakage threshold: `0.95`

✅ 本次使用專案既有本機 Ollama embedding；沒有呼叫生成式 LLM。

## 1. Dataset & family split

- Total primary: 199 | Evaluated rows: 34 | Families: 79 | Boundary: 20
- Family leakage: ✅ PASS — 無家族跨集合洩漏；no leakage
  - 家族總數：79
- Text similarity leakage (> 0.95): ✅ 0 warnings

### 每 split 分佈

| split | count | families | labels |
|---|---:|---:|---|
| calibration | 39 | 15 | `{"PURE_EDUCATION": 9, "PURE_INTAKE": 4, "MIXED": 7, "CORRECTION": 6, "SUBJECT_CHANGE": 5, "CHITCHAT": 3, "UNKNOWN": 5}` |
| holdout | 34 | 13 | `{"PURE_EDUCATION": 5, "PURE_INTAKE": 5, "MIXED": 5, "CORRECTION": 5, "SUBJECT_CHANGE": 5, "CHITCHAT": 3, "UNKNOWN": 6}` |
| train | 126 | 51 | `{"PURE_EDUCATION": 23, "PURE_INTAKE": 19, "MIXED": 19, "CORRECTION": 17, "SUBJECT_CHANGE": 16, "CHITCHAT": 16, "UNKNOWN": 16}` |
| **total** | **199** | **79** | `{"PURE_EDUCATION": 37, "PURE_INTAKE": 28, "MIXED": 31, "CORRECTION": 28, "SUBJECT_CHANGE": 26, "CHITCHAT": 22, "UNKNOWN": 27}` |

## 2. Embedding latency

| phase | p50 | p95 | rounds |
|---|---:|---:|---:|
| cold | 167.2 ms | 167.2 ms | 1 |
| warm | 163.5 ms | 171.0 ms | 25 |

## 3. Threshold sweep

固定閾值模式：`policy=hybrid` cos=0.68 margin=0.0

- macro F1 27.8%, micro F1 38.2%, MIXED recall 20.0%, coverage 32.4%, false-fast 4

Per-class 指標（chosen）：

| label | precision | recall | F1 | support |
|---|---:|---:|---:|---:|
| PURE_EDUCATION | 0.0% | 0.0% | 0.0% | 5 |
| PURE_INTAKE | 100.0% | 100.0% | 100.0% | 5 |
| MIXED | 20.0% | 20.0% | 20.0% | 5 |
| CORRECTION | 100.0% | 20.0% | 33.3% | 5 |
| SUBJECT_CHANGE | 0.0% | 0.0% | 0.0% | 5 |
| CHITCHAT | 0.0% | 0.0% | 0.0% | 3 |
| UNKNOWN | 26.1% | 100.0% | 41.4% | 6 |

- High-confidence coverage: 32.4%
- Fallback rate: 67.6%
- False-fast: **4**

## 4. Confusions

| id | gold | pred | top score | margin | family | text |
|---|---|---|---:|---:|---|---|
| edu-gliclazide-info-01 | PURE_EDUCATION | UNKNOWN | 0.4439 | 0.0500 | edu-gliclazide-info | gliclazide 這個藥是做什麼的？ |
| edu-gliclazide-info-02 | PURE_EDUCATION | UNKNOWN | 0.4852 | 0.0541 | edu-gliclazide-info | 想了解 gliclazide（磺醯脲類）的一般作用。 |
| edu-gliclazide-info-03 | PURE_EDUCATION | UNKNOWN | 0.5942 | 0.0452 | edu-gliclazide-info | 請介紹一下 gliclazide 這類降血糖藥的特性。 |
| edu-negation-01 | PURE_EDUCATION | MIXED | 0.7969 | 0.0850 | edu-negation | 我沒有糖尿病，想先了解預防的飲食原則可以嗎？ |
| edu-negation-02 | PURE_EDUCATION | MIXED | 0.6873 | 0.0125 | edu-negation | 不是要問用藥，只是想知道一般人的血糖正常範圍。 |
| mixed-fruit-intake-02 | MIXED | UNKNOWN | 0.6097 | 0.0090 | mixed-fruit-intake | 常跑廁所又口乾，想問水果份量怎麼抓，也請幫我記下來給醫師看。 |
| mixed-fruit-intake-03 | MIXED | UNKNOWN | 0.6783 | 0.0306 | mixed-fruit-intake | 想了解糖尿病水果怎麼吃，同時把我口渴的症狀整理進摘要。 |
| mixed-negation-mixed-01 | MIXED | UNKNOWN | 0.5878 | 0.0398 | mixed-negation-mixed | 我沒有在吃 gliclazide，請幫我改成 metformin，並說明 metformin 的一般 |
| mixed-negation-mixed-02 | MIXED | UNKNOWN | 0.6785 | 0.0159 | mixed-negation-mixed | 不是要問藥，是想問水果份量，但也請幫我保留剛才的用藥紀錄。 |
| correction-symptom-01 | CORRECTION | UNKNOWN | 0.5615 | 0.0135 | correction-symptom | 不是頭暈那個，是視線有點模糊才對。 |
| correction-symptom-02 | CORRECTION | UNKNOWN | 0.5868 | 0.0365 | correction-symptom | 剛才講反了，是晚上頻尿不是白天。 |
| correction-symptom-03 | CORRECTION | UNKNOWN | 0.5811 | 0.0083 | correction-symptom | 我收回剛才的說法，症狀不是口渴是頭暈。 |
| correction-mixed-fix-01 | CORRECTION | UNKNOWN | 0.6456 | 0.0099 | correction-mixed-fix | 請把上一則的用藥改成 metformin，然後保留水果問題。 |
| subject-intake-to-edu-01 | SUBJECT_CHANGE | MIXED | 0.7163 | 0.0059 | subject-intake-to-edu | 先不談用藥，改問飲食原則。 |
| subject-intake-to-edu-02 | SUBJECT_CHANGE | MIXED | 0.7520 | 0.0664 | subject-intake-to-edu | 看診資料先放一邊，我想問糖尿病成因。 |
| subject-intake-to-edu-03 | SUBJECT_CHANGE | UNKNOWN | 0.6650 | 0.0238 | subject-intake-to-edu | 暫停整理，先幫我解釋一下血糖機怎麼用。 |
| subject-slang-switch-01 | SUBJECT_CHANGE | UNKNOWN | 0.5640 | 0.0166 | subject-slang-switch | 口渴那題先不談，改問跑廁所是不是跟血糖有關？ |
| subject-slang-switch-02 | SUBJECT_CHANGE | UNKNOWN | 0.6156 | 0.0718 | subject-slang-switch | 水果先不問了，改問一直很渴要怎麼辦？ |
| chitchat-identity-01 | CHITCHAT | UNKNOWN | 0.6696 | 0.1339 | chitchat-identity | 你是誰？可以自我介紹一下嗎？ |
| chitchat-identity-02 | CHITCHAT | UNKNOWN | 0.5684 | 0.0248 | chitchat-identity | 請問你是醫生嗎？ |

## 5. Boundary comparison (guard-before-router)

- Boundary rows: 4; bypassed: 4; correct: 4/4

| id | expected | detected | bypassed |
|---|---|---|---|
| boundary-red-pure-01 | RED_FLAG | RED_FLAG | yes |
| boundary-red-pure-02 | RED_FLAG | RED_FLAG | yes |
| boundary-product-2-01 | PRODUCT_COMMAND | PRODUCT_COMMAND | yes |
| boundary-product-2-02 | PRODUCT_COMMAND | PRODUCT_COMMAND | yes |

## 6. Guarded checks (holdout)

- Verdict: **BLOCKED — 建議僅 shadow**
- false-fast=4, MIXED→PURE=0, SUBJECT/CORRECTION fast=3, boundary_leak=0
- Blocked reasons: `false-fast=4≠0, SUBJECT_CHANGE/CORRECTION fast=3≠0`

## 7. Reproduce

```bash
python scripts/semantic_router_evaluate.py --dataset experiments/semantic_router_production/dataset.json --split holdout --family-split --check-leakage
python scripts/semantic_router_evaluate.py --dataset experiments/semantic_router_production/dataset.json --split holdout --family-split --json-output /tmp/holdout.json
PYTEST_CURRENT_TEST=1 python scripts/semantic_router_evaluate.py --split all --json-output /tmp/fake.json  # fake mode
```
