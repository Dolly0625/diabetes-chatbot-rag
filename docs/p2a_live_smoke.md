# P2A Live Smoke — deterministic 部分命中＋formal 補齊 延遲量測

## 目標
驗證 `candidate_merge` 的「部分命中不阻擋 formal、合併後落地」與紅旗優先，且量測真實模型延遲。

## 執行

### 真實模型（需 .env）
```bash
python scripts/p2a_live_smoke.py
# 或
python -m scripts.p2a_live_smoke
# JSON 輸出
python scripts/p2a_live_smoke.py --json
```
- 使用真實 `.env` 的 `CONVERSATION_LLM_MODEL` / `ROUTER_LLM_MODEL` / `OPENCODE_API_KEY`
- 透過 `ConversationOrchestrator` 正式建構路徑（`ConversationInterpreterFactory.from_env()`），驗證 `interpreter is FormalConversationInterpreter`
- 不可直接塞 `FakeInterpreter`

### Dry-run（CI，無需真模型）
```bash
python scripts/p2a_live_smoke.py --dry-run -q
```
- 以 `FakeConversationInterpreter` 演練四組流程，僅供 CI 驗證腳本可跑；不觸發真實 formal workflow（避免 8s timeout）

## 四組案例

| 組 | 輸入 | 預期 |
|---|---|---|
| 多症狀 | `我嘴巴很乾，晚上一直跑廁所` | `symptom_description` 保留 `口乾；頻尿` 多子句（deterministic 部分命中 + formal 補齊，經 `candidate_merge` 去重） |
| 多意圖 | `我最近常口渴，糖尿病一天可以吃幾份水果？` | `INTAKE_ANSWER + EDUCATION_QUESTION`；口渴落地，水果問題走教育支線 |
| 反例 | `晚上常跑廁所會是糖尿病嗎？` | 問句不得污染病史（`symptom_description` 保持空，`candidate_merge` 攔截 provenance_fail/問句污染） |
| 紅旗 | `我胸口很痛喘不過氣` | 必為 `FALLBACK`（`RiskSignalPolicy RED_FLAG → A_EMERGENCY`），優先於任何 merge，不寫入 intake |

## 輸出
- 每組：`latency`、`是否 fallback（timeout/schema）`、`intake_snapshot` 脫敏 JSON
- 彙總：`p50 / p95 / fallback_rate` 與 `session snapshot`
- 紅旗與反例的落地面試：反例 `symptom_description` 為空；紅旗 `status=FALLBACK` 且 `reply` 含 119/急診

## 預期參考值
- 目前實測 baseline（mimo-v2.5 + bge-m3，網路/模型浮動）：
  - `p50 ~3842ms`  `p95 ~5632ms`  `fallback 0/11`（歷史 11 輪）
- 本次 4 組（live）預期：`p50 2–5s` `p95 4–8s` `timeout/schema fallback 0/4`（僅紅旗 1/4 為預期 `FALLBACK`）
- Dry-run：`p50 ~60–100ms` `p95 ~70–120ms` `timeout 0/4`（`use_formal=False`，不計紅旗）

## 支架
- `tfda_context_gate/tests/fixtures/p2a_cases.json` — 22 句預期行為（6+6+5+5），供煙霧與批次驗證參考，不納入 pytest 預設
- `scripts/p2a_live_smoke.py` — 僅 scripts 執行，不放入預設 pytest
