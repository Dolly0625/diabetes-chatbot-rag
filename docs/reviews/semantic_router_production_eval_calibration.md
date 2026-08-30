# Semantic Router 生產校準報告（calibration）

> 產生時間：2026-08-30T03:33:25Z  |  資料集：`/Users/dolly/Documents/code/tfda-diabetes-agent-semantic-router-production/experiments/semantic_router_production/dataset.json`  |  版本：`semantic-router-production.v1`
> 指令：`python scripts/semantic_router_calibrate.py --dataset experiments/semantic_router_production/dataset.json --output docs/reviews/semantic_router_production_eval_calibration.md --json-output /tmp/semantic_router_calibration.json`

✅ 本次使用專案既有本機 Ollama embedding；沒有呼叫生成式 LLM。

## 1. 家族切分與洩漏檢查

- Backend: `ollama`；模型：`ollama/bge-m3:latest`（source: `existing:tfda_context_gate.rag.tfda_retriever.DEFAULT_EMBEDDING_MODEL`）；host：`localhost`。
- Leakage 檢查：✅ PASS — 無跨 split 洩漏；no leakage。
- 家族總數：79；洩漏家族：[]

### 每 split 分佈

| split | count | families | labels | sources |
|---|---:|---:|---|---|---|
| calibration | 39 | 15 | `{"PURE_EDUCATION": 9, "PURE_INTAKE": 4, "MIXED": 7, "CORRECTION": 6, "SUBJECT_CHANGE": 5, "CHITCHAT": 3, "UNKNOWN": 5}` | `{"synthetic_pii_free": 39}` |
| holdout | 34 | 13 | `{"PURE_EDUCATION": 5, "PURE_INTAKE": 5, "MIXED": 5, "CORRECTION": 5, "SUBJECT_CHANGE": 5, "CHITCHAT": 3, "UNKNOWN": 6}` | `{"synthetic_pii_free": 34}` |
| train | 126 | 51 | `{"PURE_EDUCATION": 23, "PURE_INTAKE": 19, "MIXED": 19, "CORRECTION": 17, "SUBJECT_CHANGE": 16, "CHITCHAT": 16, "UNKNOWN": 16}` | `{"synthetic_pii_free": 126}` |
| **total** | **199** | **79** | `{"PURE_EDUCATION": 37, "PURE_INTAKE": 28, "MIXED": 31, "CORRECTION": 28, "SUBJECT_CHANGE": 26, "CHITCHAT": 22, "UNKNOWN": 27}` | — |

- calibration 集大小：39；holdout：34；train：126；boundary：20

## 2. Embedding 延遲

| phase | p50 | p95 | rounds |
|---|---:|---:|---:|
| cold first query | 171.3 ms | 171.3 ms | 1 |
| warm query | 160.0 ms | 165.0 ms | 25 |

## 3. 校準閾值擇優（calibration split，四階規則）

擇優規則依序：`MIXED recall≥75% & false-fast=0` → `MIXED≥75%` → `false-fast=0` → `max macro_F1`；`UNKNOWN` 表示 abstain 交回 LLM。

| policy | thresholds | macro F1 | micro F1 | MIXED recall | coverage | fallback | false-fast |
|---|---:|---:|---:|---:|---:|---:|---:|
| cosine | 0.68 | 58.0% | 51.3% | 57.1% | 38.5% | 61.5% | 0 |
| margin | 0.06 | 47.3% | 43.6% | 28.6% | 30.8% | 69.2% | 0 |
| hybrid | cos=0.68, margin=0.00 | 58.0% | 51.3% | 57.1% | 38.5% | 61.5% | 0 |

最大 MIXED recall（診斷用，非安全推薦）：

| policy | threshold(s) | max MIXED recall | macro F1 | coverage | false-fast |
|---|---:|---:|---:|---:|---:|
| cosine | 0.58 | 71.4% | 62.1% | 76.9% | 9 |
| margin | 0.00 | 71.4% | 59.8% | 100.0% | 13 |
| hybrid | cos=0.58, margin=0.00 | 71.4% | 62.1% | 76.9% | 9 |

Per-class 指標（chosen hybrid 在 calibration 集）：

| label | precision | recall | F1 | support |
|---|---:|---:|---:|---:|
| PURE_EDUCATION | 100.0% | 11.1% | 20.0% | 9 |
| PURE_INTAKE | 100.0% | 25.0% | 40.0% | 4 |
| MIXED | 100.0% | 57.1% | 72.7% | 7 |
| CORRECTION | 100.0% | 33.3% | 50.0% | 6 |
| SUBJECT_CHANGE | 100.0% | 80.0% | 88.9% | 5 |
| CHITCHAT | 100.0% | 100.0% | 100.0% | 3 |
| UNKNOWN | 20.8% | 100.0% | 34.5% | 5 |

- High-confidence coverage：38.5%
- Abstain / LLM fallback：61.5%
- False-fast：**0**（prediction != UNKNOWN 且 != gold）
- MIXED recall：57.1%

### 校準推薦閾值（供 holdout 評估使用）

- 推薦 policy：`hybrid`
- `cosine_threshold=0.68, margin_threshold=0.00`
- 建議後續以此閾值執行：`python scripts/semantic_router_evaluate.py --dataset /Users/dolly/Documents/code/tfda-diabetes-agent-semantic-router-production/experiments/semantic_router_production/dataset.json --cosine-threshold 0.68 --margin-threshold 0.00 --policy hybrid --split holdout`

## 4. 混淆案例（calibration 集，chosen hybrid）

| id | gold | prediction | top score | margin | family | text |
|---|---|---|---:|---:|---|---|
| edu-fruit-01 | PURE_EDUCATION | UNKNOWN | 0.6573 | 0.0158 | edu-fruit | 糖尿病的朋友吃水果要注意什麼，一天能吃多少？ |
| edu-fruit-03 | PURE_EDUCATION | UNKNOWN | 0.6526 | 0.0185 | edu-fruit | 請問糖尿病在水果份量上怎麼拿捏比較安全？ |
| edu-sleep-01 | PURE_EDUCATION | UNKNOWN | 0.5828 | 0.0196 | edu-sleep | 熬夜或睡不好會影響血糖嗎？ |
| edu-sleep-02 | PURE_EDUCATION | UNKNOWN | 0.5662 | 0.0267 | edu-sleep | 睡眠不足是不是會讓血糖變高？ |
| edu-sleep-03 | PURE_EDUCATION | UNKNOWN | 0.5521 | 0.0029 | edu-sleep | 最近常晚睡，想知道對血糖有沒有影響。 |
| edu-metformin-side-01 | PURE_EDUCATION | UNKNOWN | 0.4924 | 0.0117 | edu-metformin-side | metformin 常見的副作用有哪些？ |
| edu-metformin-side-02 | PURE_EDUCATION | UNKNOWN | 0.4539 | 0.0131 | edu-metformin-side | 吃 metformin（二甲雙胍）會有什麼副作用？ |
| edu-metformin-side-03 | PURE_EDUCATION | UNKNOWN | 0.5019 | 0.0144 | edu-metformin-side | 我聽說 metformin 會拉肚子，是常見的嗎？ |
| intake-family-history-01 | PURE_INTAKE | UNKNOWN | 0.6479 | 0.0330 | intake-family-history | 家人有糖尿病史，請幫我記在家庭史欄位。 |
| intake-family-history-02 | PURE_INTAKE | UNKNOWN | 0.5984 | 0.0117 | intake-family-history | 幫我補一下家庭史：爸爸有第二型糖尿病。 |
| intake-slang-intake-02 | PURE_INTAKE | UNKNOWN | 0.6608 | 0.0072 | intake-slang-intake | 這幾天口乾到一直想喝水，請幫我記起來回診要講。 |
| mixed-metformin-edu-intake-01 | MIXED | UNKNOWN | 0.5668 | 0.0001 | mixed-metformin-edu-intake | 我有在吃 metformin，另外想問水果可以吃多少？請一起幫我整理用藥清單。 |
| correction-med-01 | CORRECTION | UNKNOWN | 0.6567 | 0.1137 | correction-med | 更正一下，藥名是 gliclazide 才對，不是 metformin。 |
| correction-med-02 | CORRECTION | UNKNOWN | 0.6068 | 0.0129 | correction-med | 我沒有在吃二甲雙胍，請改成目前無此用藥。 |
| correction-med-03 | CORRECTION | UNKNOWN | 0.5593 | 0.0494 | correction-med | 等等，胰島素是 insulin glargine 不是 lispro，請更正。 |
| correction-subject-01 | CORRECTION | UNKNOWN | 0.6160 | 0.0233 | correction-subject | 不是我本人的狀況，是我媽媽最近一直口渴才對。 |
| subject-hypothetical-switch-02 | SUBJECT_CHANGE | UNKNOWN | 0.6626 | 0.0691 | subject-hypothetical-switch | 如果之後要打 insulin，現在先不談這個改問別的。 |
| mixed-extra-symptom-intake-01 | MIXED | UNKNOWN | 0.6293 | 0.0006 | mixed-extra-symptom-intake | 最近腳有點麻、口渴頻尿，想整理症狀也想知道是不是血糖的關係？ |
| mixed-extra-symptom-intake-03 | MIXED | UNKNOWN | 0.6170 | 0.0508 | mixed-extra-symptom-intake | 冒冷汗、手抖是不是低血糖？另外幫我把這些紀錄整理給醫師。 |

## 5. 下一步

- 請執行 `python scripts/semantic_router_evaluate.py --split holdout` 在 holdout 上驗證 guarded 門檻（false-fast=0、紅旗漏攔=0、MIXED→PURE=0、SUBJECT_CHANGE/CORRECTION 不得快寫入）；若不通過，報告須誠實寫明「建議僅 shadow」且脚本以 exit 2 標記 blocked。
- 若需 deterministic 複現：`PYTEST_CURRENT_TEST=1 python scripts/semantic_router_calibrate.py --json-output /tmp/calib_fake.json`（報告將標示 BLOCKED）。

## 6. 復現指令

```bash
python scripts/semantic_router_calibrate.py --dataset /Users/dolly/Documents/code/tfda-diabetes-agent-semantic-router-production/experiments/semantic_router_production/dataset.json --output docs/reviews/semantic_router_production_eval_calibration.md --json-output /tmp/semantic_router_calibration.json
python scripts/semantic_router_evaluate.py --dataset /Users/dolly/Documents/code/tfda-diabetes-agent-semantic-router-production/experiments/semantic_router_production/dataset.json --split holdout --json-output /tmp/semantic_router_holdout.json --output docs/reviews/semantic_router_production_eval_holdout.md
PYTEST_CURRENT_TEST=1 python scripts/semantic_router_calibrate.py --json-output /tmp/calib_fake.json  # fake 模式複現
```

_機器生成報告 — backend blocked=False — 請勿手動竄改閾值造假_
