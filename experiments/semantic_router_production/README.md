# Semantic Router Production 評估集 — PII-free 家族切分

> 位置：`experiments/semantic_router_production/dataset.json`（單一真相來源）
> 替代路徑 `data/semantic_router/eval_dataset.json` 已棄用，統一使用 `experiments/semantic_router_production/dataset.json`
> 版本：`semantic-router-production.v1`
> 生成：`build_dataset.py`（deterministic, 合成語料，無真實病歷）
> 來源：重新改寫並擴充 `tfda-diabetes-agent-semantic-router-eval/experiments/semantic_router_eval/dataset.json`（84 → 199+20），**不直接複製原文**，僅保留概念

## 1. 規模

| 集合 | 筆數 | 家族數 | 來源 |
|---|---:|---:|---|
| primary | **199** | **79** | 全合成 `synthetic_pii_free` |
| boundary_comparison | **20** | **10** | 全合成 |
| **總計** | **219** | **89** | PII-free |

- primary ≥180 ✅（目標 180，實作 199）
- boundary ≥12 ✅（含 4 紅旗混合句如「本來想問水果份量，但現在胸悶又喘不過氣」「想整理看診資料，可是現在胸口痛到冒冷汗、喘不過氣」）
- 每類至少 20：

| label | count |
|---|---:|
| PURE_EDUCATION | 37 |
| MIXED | 31 |
| PURE_INTAKE | 28 |
| CORRECTION | 28 |
| UNKNOWN | 27 |
| SUBJECT_CHANGE | 26 |
| CHITCHAT | 22 |

> MIXED / SUBJECT_CHANGE / CORRECTION / UNKNOWN 刻意加量（31/26/28/27），符合「混合/主體切換/修正/未知需更多」要求

Boundary 類別：`RED_FLAG 8 / AUTHORIZATION 6 / PRODUCT_COMMAND 6`

## 2. 格式

```json
{
  "version": "semantic-router-production.v1",
  "description": "PII-free, family-split ...",
  "splits": {"train": "60% families", "calibration": "20% families", "holdout": "20% families"},
  "primary": [{"id","label","family_id","split","text","source"}],
  "boundary_comparison": [{"id","category","family_id","split","text"}],
  "families": [{"family_id","label/category","split","count"}]
}
```

- 新增欄位：`family_id`（如 `edu-fruit`, `mixed-fruit-intake`, `correction-time`, `subject-edu-to-intake`）、`split`（`train / calibration / holdout`）
- 保留：`id`, `text`, `label`/`category`
- `text` 皆為台灣中文（zh-TW），PII-free

範例：
```json
{"id":"mixed-fruit-intake-01","label":"MIXED","family_id":"mixed-fruit-intake","split":"holdout","text":"我最近常口渴、很渴，糖尿病一天可以吃幾份水果？另外幫我整理成看診資料。","source":"synthetic_pii_free"}
```

## 3. 家族定義

每個 `family_id` 對應**一個原型語意**的 2–4 條近似改寫（paraphrase），共用同一家族 ID。
例如：

| family_id | label | 範例改寫 |
|---|---|---|
| `edu-fruit` | PURE_EDUCATION | 糖尿病的朋友吃水果… / 想請問水果選擇… / 水果份量怎麼拿捏… |
| `intake-med-list` | PURE_INTAKE | 我有在吃 metformin 和 gliclazide… / 目前用藥有 insulin glargine… |
| `mixed-fruit-intake` | MIXED | 我最近常口渴…另外幫我整理成看診資料 / 常跑廁所又口乾…也請幫我記下來… |
| `correction-time` | CORRECTION | 我剛才說錯了，不是昨天，是上週… / 更正一下是前天… |
| `subject-edu-to-intake` | SUBJECT_CHANGE | 先不談飲食了，改幫我整理看診資料 / 換個話題… |
| `unknown-control` | UNKNOWN | 請把我的帳號資料全部刪除掉 / 我要重設登入密碼才行 |

完整家族清單見 `dataset.json` 內 `families`（89 條）或執行：
```bash
python3 -c "import json,collections; d=json.load(open('experiments/semantic_router_production/dataset.json')); [print(f['family_id'], f.get('label',f.get('category')), f['split'], f['count']) for f in d['families']]"
```

## 4. 切分原則（防洩漏）

- **按家族切分**：同一 `family_id` 的所有改寫全在同一 `split`，近似句不得跨集合
- 分配：按 label 分組後排序，每 5 家族 → 3 train / 1 calibration / 1 holdout（approx 60/20/20）
- 結果：

| split | primary | families | label 分佈 |
|---|---:|---:|---|
| train | 126 | 47 | PURE_EDUCATION 23, MIXED 19, PURE_INTAKE 19, CORRECTION 17, SUBJECT_CHANGE 16, UNKNOWN 16, CHITCHAT 16 |
| calibration | 39 | 16 | PURE_EDUCATION 9, MIXED 7, CORRECTION 6, SUBJECT_CHANGE 5, UNKNOWN 5, PURE_INTAKE 4, CHITCHAT 3 |
| holdout | 34 | 16 | UNKNOWN 6, PURE_EDUCATION 5, PURE_INTAKE 5, CORRECTION 5, SUBJECT_CHANGE 5, MIXED 5, CHITCHAT 3 |
| total | 199 | 79 | — |

| split | boundary |
|---|---:|
| train | 12 |
| calibration | 4 |
| holdout | 4 |

- holdout 為最終評估唯一可用集；calibration 用於 `fit()`/threshold sweep；train 用於原型建構
- 切分腳本：`build_dataset.py` 內 `assign_splits()`；驗證：`scripts/semantic_router_evaluate.py --family-split --check-leakage`

## 5. 防洩漏檢查

兩層：

1. **家族洩漏**：`tfda_context_gate.semantic_router.eval_common.check_family_leakage` — 同一 family_id 出現在多個 split 即 FAIL
2. **文本相似度**：`scripts/semantic_router_evaluate.py --check-leakage --leak-threshold 0.95` — 以 `difflib.SequenceMatcher.ratio() > 0.95` 視為洩漏警告（跨 split 近似句）

```bash
# 快速檢查
python3 scripts/semantic_router_evaluate.py --family-split --check-leakage --split all
# 嚴格校準：calibration 擇優 + holdout 驗證
python3 scripts/semantic_router_calibrate.py --dataset experiments/semantic_router_production/dataset.json --json-output /tmp/calib.json
python3 scripts/semantic_router_evaluate.py --dataset experiments/semantic_router_production/dataset.json --split holdout --family-split --check-leakage --json-output /tmp/holdout.json --output docs/reviews/semantic_router_production_eval_holdout.md
```

當前資料集：`家族洩漏 0`，`文本相似度 >0.95 跨 split 0 warnings` ✅

## 6. 類別覆蓋（任務要求清單）

| 要求 | 對應家族舉例 |
|---|---|
| 純衛教 | `edu-*` 14 家族（含水果/飲食/運動/睡眠/成因/藥物） |
| 純 intake | `intake-*` 12 家族（含用藥清單/症狀紀錄/家族史/藥袋） |
| mixed | `mixed-*` 12 家族（含 fruit-intake, metformin-edu, insulin-edu） |
| 跨輪修正 | `correction-*` 10 家族 + `mixed-correction-mixed` |
| 本人/家屬切換 | `correction-subject`, `subject-family-switch`, `mixed-other-person-mixed` |
| 否定句 | `edu-negation`, `intake-negation-intake`, `correction-negation`, `unknown-negation-vague`, `mixed-negation-mixed` |
| 問句 | `edu-question`, `mixed-question-mixed`, `subject-question-switch` |
| 假設句 | `edu-hypothetical`, `mixed-hypothetical-mixed`, `correction-hypothetical`, `unknown-hypothetical-vague` |
| 他人情況 | `edu-other-person`, `mixed-other-person-mixed`（我同事/朋友/媽媽） |
| 閒聊 | `chitchat-*` 7 家族 |
| 身份詢問 | `chitchat-identity`, `unknown-identity`, `chitchat-extra-identity`（你是誰/你是醫生嗎/機器人） |
| 控制命令 | `unknown-control` + `boundary-product-*`（刪除/重設密碼/清除紀錄） |
| 紅旗混合句 | `boundary-red-mixed-*` 3 家族 + `mixed-extra-symptom-intake`（胸悶喘不過氣/胸痛冒冷汗/頭暈快昏倒） |
| 台灣口語俚語 | `edu-slang-thirst`, `intake-slang-intake`, `mixed-slang-mixed`, `correction-slang`, `subject-slang-switch`, `unknown-slang-vague`（跑廁所/口乾/很渴/吃不飽/冒冷汗/手抖） |
| 中英混合藥名 | `edu-metformin-side`, `edu-gliclazide-info`, `edu-insulin-glargine`, `intake-med-list`, `mixed-gliclazide-edu-intake`, `mixed-insulin-edu-intake`, `mixed-extra-drug-food`（metformin/gliclazide/insulin glargine/insulin lispro/empagliflozin/sitagliptin） |

> 全部合成，無真實病歷；藥名僅作一般衛教/用藥清單語意測試，非處方建議

## 7. PII 保證

- 生成時掃描：`email`、`09xxxxxxxx` 手機、`patient/U/user-數字` 病歷號 — 0 命中
- 測試：`pytest experiments/semantic_router_production/tests/test_dataset.py -k pii`（見下）
- 來源標註 `synthetic_pii_free`，描述明示 `Synthetic, no real records`

## 8. 使用方式

```bash
# 校準（calibration 擇優）
python3 scripts/semantic_router_calibrate.py --dataset experiments/semantic_router_production/dataset.json --output docs/reviews/semantic_router_production_eval_calibration.md --json-output /tmp/calib.json

# holdout 評估（family-split + 洩漏檢查）
python3 scripts/semantic_router_evaluate.py --dataset experiments/semantic_router_production/dataset.json --split holdout --family-split --check-leakage --output docs/reviews/semantic_router_production_eval_holdout.md --json-output /tmp/holdout.json

# 固定閾值複現（例）
python3 scripts/semantic_router_evaluate.py --dataset experiments/semantic_router_production/dataset.json --split holdout --family-split --cosine-threshold 0.62 --margin-threshold 0.10 --policy hybrid --json-output /tmp/fixed.json

# fake 模式（無 Ollama 時 plumbing 驗證）
PYTEST_CURRENT_TEST=1 python3 scripts/semantic_router_evaluate.py --split all --family-split --check-leakage --json-output /tmp/fake.json
```

## 9. 與研究文件對應

- 研究設計：`docs/research/semantic_router_production_design_20260830.md` §11–13（分層路由、隔離實驗、家族切分需求）
- 本資料集為 §11 所述「需重設計後再用」的 `PROTOTYPES` + 閾值校準所依賴的 held-out 人審集雛形（合成版，未達臨床驗證）
- `UNKNOWN` 為 abstain 類，無 prototype，低信心交回 LLM（`fallback = UNKNOWN → LLM`）

## 10. 驗證

```bash
pytest experiments/semantic_router_production/tests -q
python3 scripts/semantic_router_evaluate.py --family-split --check-leakage --leak-threshold 0.95 --split all --json-output /tmp/verify.json
cat /tmp/verify.json | python3 -m json.tool | head -n 40
```

_Machine-generated dataset — do not edit manually, rerun `build_dataset.py`._
