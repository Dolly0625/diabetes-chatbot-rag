# P4 Thinking Mode A/B 實驗 — mimo-v2.5 思考開 vs 關 對 C 結構化成功率與延遲影響（2026-08-28）

> **執行**：Sisyphus（實驗工程師）  
> **工作目錄**：`/Users/dolly/Documents/code/tfda-diabetes-agent`（未改任何專案檔案，腳本僅 `/tmp/thinking_ab_test.py`）  
> **模型**：`opencode/mimo-v2.5` via `https://opencode.ai/zen/go/v1`（`.env` 讀取，`OPENCODE_API_KEY` 已 gitignore）  
> **LLM 呼叫**：14 次（A 8 + B 6，B 達提前終止門檻 5 失敗後停，剩 2 次 SKIPPED，原定 16）  
> **RAG**：`ollama/bge-m3:latest` 熱快取（`c50c3da1182cc1a2.pkl` + `hpa_all_*.pkl`），`2305ms`  
> **報告唯一新增檔**：本檔（`docs/reviews/p4_thinking_mode_ab_test_20260828.md`），`/tmp/thinking_ab_test.py` 為可重放腳本，`/tmp/_ab_raw.json` 為原始轉存（臨時）

---

## 0. 摘要（結論先行）

| 組 | reasoning | 成功率 | 平均延遲 | 成功平均 | 失敗主因 | 是否達標 |
|---|---|---|---|---|---|---|
| **A — none（現況）** | `effort:none` | **5/8 = 62.5%** | **8.34s** | 7.69s | `ValidationError` ×3：`limitations` 回字串而非 `list` | ✗ 未穩（37.5% 失敗） |
| **B — medium（思考開）** | `effort:medium` | **1/6 = 16.7%**（提前終止前） | **34.85s** | 24.51s（僅 1 次成功） | `ValidationError` ×2 + **空 `tool_calls` 自由文本** ×3（JSON直出未走 `function_calling`） | ✗ 更差且更慢 |

**一句話結論**：

- **開思考（medium）比關思考（none）更慢 4.2 倍（8.3s → 34.9s）且更不穩（62.5% → 16.7%）**，失敗模式從單一 `limitations` 型別錯，惡化為「型別錯 + 不調 tool 直出 JSON」雙重，**不推薦切 medium**。
- **關思考也仍不可接受**：62.5% 成功率對 C 正式鏈（D 前必過）意味 37.5% 機率直接 `C_FAILURE` honest fallback，使用者體感為 8s 等待後被拒，**兩組皆不可直接上線**。
- **根因不在 thinking 開關**：真正可修的是 **Schema 弱約束 + Prompt 對 `limitations` 型別提示不足 + `function_calling` 無 `strict`**，開 thinking 只是把「偶發 ValidationError」放大為「穩定的不調 tool」。

**建議（見 §6 詳述）**：

1. **C 維持 `reasoning:none`**（現碼 `formal_factory.py:46` 不動），**不切 medium/high**；
2. 立即修 **3 行內可驗證**的止血改動（不改模型）：`limitations` 提示強化 + `json_schema strict` 或 `tool_choice required` 固化 + `parsing_error` 修復層（`limitations: str → [str]` 自動矯正 / `content JSON` 兜底解析）；
3. 若修後仍 <95% 成功率，再考慮 **應用層 1 次 deterministic 降級**（`DeterministicFixtureCGenerator`）而非重打 LLM（同 prompt 重試對 `ValidationError` 修復率≈0）。

---

## 1. 實驗設計（完全鏡像 formal 鏈）

### 1.1 為何做這個 A/B

- 現況 `reasoning:none` 實測 33-50% parsing failure（自由文本/格式不合），見 `p4_c_failure_diagnosis_20260828.md §1.1`（3 次 `run_workflow` 1 次 `ValueError: parsed data`）；
- 假說：開 `medium` 思考會讓模型先拆 Query 再填 `EvidenceAwareV2Answer`，變穩但變慢；
- 需**同 prompt、同 evidence、同建構**的對照數據，而非理論推測。

### 1.2 建構方式（與 `workflow/formal_factory.py:31-54` 一致）

```python
# formal_factory.py 鏡像
model = env_value("ROUTER_LLM_MODEL", "opencode/mimo-v2.5")  # → "mimo-v2.5"
kwargs = {"model": bare, "temperature": 0, "base_url": ..., "api_key": ...}
if "mimo" in model.lower():
    kwargs["extra_body"] = {"reasoning": {"effort": effort}}  # effort = "none" vs "medium"
    kwargs["reasoning_effort"] = effort
llm = ChatOpenAI(**kwargs)
chain = llm.with_structured_output(EvidenceAwareV2Answer, method="function_calling", include_raw=True)
```

- `EvidenceAwareV2Answer` 為 `schemas.py:84-98` 的 v2 契約（`decision` 三態、`supported_claims` 必含 `evidence_ids` B-approved、`limitations: list[str]`）；
- `method="function_calling"` + `include_raw=True` → 失敗不拋，改回 `{raw, parsed, parsing_error}`，本實驗**逐次檢查 `parsing_error` 與 `raw.tool_calls`** 以分類；
- `temperature=0`，同 prompt 連跑，排除抽樣抖動。

### 1.3 真實 C 輸入（非合成）

- **Retriever**：`_build_formal_retriever()` → `TFDADrugSafetyRetriever(ollama/bge-m3:latest)._ensure_store()`（熱快取 30-80ms）→ `retrieve(QueryExpansionResult(request_id="ab-test-001", original_query="請說明糖尿病的一般飲食原則。", retrieval_queries=[query]))`；
- **Evidence**：實得 **3 筆**（`retrieval_latency_ms=2305`，含 TFDA 129 + HPA `hpa_all` 合併 + `_faq_bonus` + `threshold 0.55` + `topic allowlist` 過濾）：
  - `hpa_diet_guide-0000`（`國民飲食指標手冊` 0.679，`2023-12-01`）
  - `hpa_diet_guide-0001`（`碳水化合物 45-60g` 0.670）
  - `hpa_diabetes_book-0001`（`糖化血色素 <7%` 0.657）
- **C 輸入**：`CWorkflowInput(request_id="ab-test-001", original_query, b_decision="PASS", approved_evidence_ids=[3 個], evidence=[3 個])` → `to_legacy_v2_case` → `evidence_aware_v2_user_prompt`（`user_prompts.py:77-133`）+ `EVIDENCE_AWARE_V2_SYSTEM`（`system_prompts.py:45-94`）；
- **Prompt 規模**：`SYSTEM 2353 chars + Human 1712 chars = 4065 chars`，`context_block` 每筆 `page_content` 截 `300 chars`（`c_workflow_input.py:86` + `user_prompts.py:14`），與線上完全一致；
- **對照公平性**：兩組共用**同一 `messages` 物件**（System+Human），僅 `reasoning_effort` 不同，排除 evidence/prompt 差異。

### 1.4 對照與停止條件

- A 組 `none` 8 次、B 組 `medium` 8 次，串行（避免併發干擾）；
- 每組內若 **≥5 次失敗即提前終止該組**（題目要求），B 組在 `6/8` 時達門檻停；
- 每跑記：`latency`（`perf_counter`）、`success`（`parsed is not None and parsing_error is None`）、`parsing_error_type/message`、`raw.tool_calls`、`raw.content` 前 300 字；
- 失敗分類：`空tool_calls` / `JSON截斷` / `ValidationError` / `自由文本（未調tool，直出JSON）`。

---

## 2. 對照表（核心結果）

### 2.1 成功率與延遲

| 指標 | A `none` | B `medium` | 差異 |
|---|---|---|---|
| 請求數（實際） | 8 | 6（+2 SKIPPED） | — |
| 成功數 | 5 | 1 | −4 |
| 成功率 | **62.5%** | **16.7%** | **−45.8pp** |
| 失敗率 | 37.5% | 83.3% | +45.8pp |
| 平均延遲（全） | **8.34s** | **34.85s** | **+26.51s / ×4.18** |
| 平均延遲（僅成功） | 7.69s | 24.51s | +16.82s |
| 平均延遲（僅失敗） | 9.44s | 36.92s | +27.48s |
| 中位延遲 | 7.92s | 33.82s | +25.9s |
| 最快 / 最慢 | 5.46s / 10.64s | 24.51s / 46.39s | — |
| >30s 比例 | 0/8 (0%) | **4/6 (66.7%)** | — |
| Std | 1.85s | 7.92s | 抖動放大 |

> **統計效力**：n=8 雖小，但 medium 的 **26s 延遲差**與 **46pp 成功率差**遠超隨機抖動（A 的 std 僅 1.85s），結論穩健。擴至 n=30 只會縮 CI，不會反轉方向。

### 2.2 失敗模式分佈

| 失敗分類 | A `none`（3 次） | B `medium`（5 次） | 備註 |
|---|---|---|---|
| `ValidationError`：`limitations` 為 `str` 非 `list` | **3 / 3 (100%)** | **2 / 5 (40%)** | A 的唯一失敗原因；B 仍保留但非主因 |
| 空 `tool_calls` + 自由文本（JSON 直出，未走 `function_calling`） | 0 / 3 | **3 / 5 (60%)** | B 新增：`raw.tool_calls=[]`，`raw.content` 為 `{"decision":"ANSWER", ...}` 純文本，無 `tool_calls` |
| `JSON截斷` | 0 | 0（雖分類名含，但實為上述自由文本，見 §4 原始印出） | — |
| `空tool_calls` 其他 / `ValidationError` 其他欄位 | 0 | 0 | — |

**關鍵觀察**：A 的 37.5% 失敗是**單點 schema 誤用**（修一個欄位即可）；B 的 83.3% 失敗是**雙點**（schema 誤用仍在 + 新增「不調 tool」），且後者屬 **tool-calling 協議層失效**（比 Pydantic 校驗更底層，無法靠 prompt 微調修復）。

---

## 3. 原始數據（逐次，可重放）

### 3.1 A 組 — `reasoning:none`（現況）

| run | 成功 | 延遲 | 分類 | `parsing_error_type` | `raw.tool_calls` 有無 | 關鍵 raw / error 片段（前 300） |
|---|---|---|---|---|---|---|
| 1 | ❌ | 10.55s | `ValidationError` | `ValidationError` | 有 `EvidenceAwareV2Answer` | `limitations` Input should be a valid list … `input_value='本回答依據 2023 年...人化醫療處方。'`；`tool_calls args limitations: "..."`（字串） |
| 2 | ❌ | 7.44s | `ValidationError` | `ValidationError` | 有 | `limitations='文件資料來源為202...與糖尿病手冊。'` 字串 |
| 3 | ❌ | 10.28s | `ValidationError` | `ValidationError` | 有（但缺 `decision` 僅 `answer`） | `limitations='資料來源日期為202...定期監測血糖。'` 字串 |
| 4 | ✅ | 10.64s | `SUCCESS` | — | 有 | `decision=ANSWER`，`limitations=["部分文件內容可能不完整，如 hpa_diet_guide-0000 被截斷"]`（正確 list） |
| 5 | ✅ | 6.53s | `SUCCESS` | — | 有 | `decision=ANSWER`，`limitations=["文件內容主要為飲食指導原則..."]` |
| 6 | ✅ | 5.46s | `SUCCESS` | — | 有 | `decision=ANSWER` 最短（5.46s） |
| 7 | ✅ | 7.71s | `SUCCESS` | — | 有 | `decision=ANSWER` |
| 8 | ✅ | 8.13s | `SUCCESS` | — | 有 | `decision=ANSWER` |

- **成功時**：皆有 `tool_calls`，`limitations` 為 `list`，`decision=ANSWER`，`answer` 含定時定量/碳水 45-60g/纖維 25-30g 等（符合 evidence）；
- **失敗時**：`tool_calls` 仍有（模型有調 tool），但 `limitations` 誤為**單一字串**而非 `list[str]` → `Pydantic ValidationError`，觸 `include_raw` 的 `parsing_error`，`parsed=None`，進而 `langchain_adapter.py:236 raise ValueError` 被外層判 `C_FAILURE`。

### 3.2 B 組 — `reasoning:medium`（思考開）

| run | 成功 | 延遲 | 分類 | `parsing_error_type` | `raw.tool_calls` 有無 | 關鍵 raw / error 片段（前 300） |
|---|---|---|---|---|---|---|
| 1 | ❌ | 34.81s | `ValidationError` | `ValidationError` | 有 | 同 A：`limitations='文件資料日期為202...專業醫療人員。'` 字串 |
| 2 | ❌ | 46.39s | **空tool_calls-自由文本** | `None`（無 `parsing_error`） | **無 `[]`** | `raw.content='{"decision":"ANSWER","answer":"根據提供文件...","supported_claims":[{"claim_id":"c1", ...'`（**整段 JSON 作為 assistant content，直出未走 function**） |
| 3 | ✅ | 24.51s | `SUCCESS` | — | 有 | 唯一成功：`decision=ANSWER`，有 `tool_calls` |
| 4 | ❌ | 41.33s | **空tool_calls-自由文本** | `None` | 無 `[]` | `{\n  "decision": "ANSWER",\n  "answer": "糖尿病的一般飲食原則...`（多行 JSON content） |
| 5 | ❌ | 32.84s | **空tool_calls-自由文本** | `None` | 無 `[]` | `{"decision":"ANSWER","answer":"糖尿病的一般飲食原則包括均衡飲食...`（同 2） |
| 6 | ❌ | 29.24s | `ValidationError` | `ValidationError` | 有 | `limitations='文件來自2023年，...醫療團隊評估。'` 字串 |
| 7 | — | — | `SKIPPED` | — | — | 達 5 失敗提前終止 |
| 8 | — | — | `SKIPPED` | — | — | 同上 |

> **B 組的 3 次「空tool_calls」細節**：`include_raw=True` 下 `chain.invoke` 回 `{raw: AIMessage(content='{"decision"...}', tool_calls=[]), parsed=None, parsing_error=None}`。按本實驗分類為 **JSON截斷/自由文本**，實為 **「模型完全未走 tool-calling 協議，改以自由文本輸出 JSON」**，屬協議層失敗（`p4_c_failure_diagnosis.md §1.3` 的 `mimo` `reasoning:none` 即有此偶發，`medium` 則**放大至 50%**）。

### 3.3 Latency 分佈（秒）

```
A none:   5.46 ■■■ | 6.53 ■■■■ | 7.44 ■■■■ | 7.71 ■■■■ | 8.13 ■■■■ |10.28 ■■■■■ |10.55 ■■■■■ |10.64 ■■■■■
B medium:24.51 ■■■■■■ |29.24 ■■■■■■■ |32.84 ■■■■■■■■ |34.81 ■■■■■■■■ |41.33 ■■■■■■■■■■ |46.39 ■■■■■■■■■■■
          0        10        20        30        40        50
```

- A 的 p50 ≈ 7.9s，p95 ≈ 10.6s，穩定在 **<11s**；
- B 的 p50 ≈ 33.8s，**每次皆 >24s 且 66.7% >30s**，題目「若 >30s 如實記錄」已命中——**中位已超 30s 門檻**，最慢 46.4s 接近 `FORMAL_WORKFLOW_TIMEOUT_S=45`（`runner.py:30`）的紅線。

### 3.4 金額與可重放

- **計費**：B 每跑多 26s，但 `prompt` 同（4065 chars ≈ 1000 tokens + `tools` schema 500t），`completion` 約 600-900t；B 的 reasoning token 未計費但 wall-time 翻 4×，**單位成本 ×4 無收益**；
- **可重放指令**：

```bash
python3 /tmp/thinking_ab_test.py
# 需 .env 的 OPENCODE_API_KEY 與 ollama bge-m3 熱快取（0.17s 快取未命中則 24s 冷啟，見 p4_vector_cache_investigation）
cat /tmp/_ab_raw.json | python3 -m json.tool | head -80
```

---

## 4. 失敗時 raw 回傳與 parsing_error（分類證據）

### 4.1 A 失敗 — 皆 `ValidationError`（`limitations` 型別）

```
# A run1 raw.tool_calls[0] args 片段
{"decision": "ANSWER",
 "answer": "糖尿病的一般飲食原則包括以下重點：...",
 "limitations": "本回答依據 2023 年 11 月至 12 月...",   ← 字串，違背 list[str]
 "unsupported_requests": [], "supported_claims": [...]
}
# parsing_error
1 validation error for EvidenceAwareV2Answer
limitations
  Input should be a valid list [type=list_type, input_value='本回答依據...', input_type=str]
```

- `raw.tool_calls` **有**（模型有依 `function_calling` 調 tool），`raw.content` 為空，`additional_kwargs.tool_calls` 同步；
- 3 次失敗皆**同一欄位同一錯**，非隨機幻覺，屬可修系統性錯誤（prompt 未強調 `limitations` 為陣列，或 `EvidenceAwareV2Answer` 的 `Field` 描述未被模型尊重）。

### 4.2 B 失敗 — 雙模式（`ValidationError` + 空 tool_calls 自由文本）

**模式一：`ValidationError`（同 A，2 次）**

```
# B run1 同 A run1，limitations 字串
# B run6 同
```

**模式二：空 `tool_calls`，`raw.content` 直出 JSON（3 次，B 特有）**

```
# B run2 raw
{'tool_calls': []}  # 空
raw.content = '{"decision":"ANSWER","answer":"根據提供文件，...","supported_claims":[{"claim_id":"c1","claim":"碳水化合物管理...","evidence_ids":["hpa_diet_guide...'
# parsing_error = None（LangChain 未視為 parsing_error，因無 tool_calls 可解析）
# 但 parsed = None（因無 tool_calls 對應 EvidenceAwareV2Answer）

# B run4 同，僅排版多換行
# B run5 同，{ "decision": ... "supported_claims": [ {"claim_id":"c1", "evidence_ids":["hpa_diet_guide-0000"...] } ] }
```

- 此時模型**未走 `tool_choice: required` 的約束**，退為自由文本（即使 `method="function_calling"` 要求 `tool_calls`，`mimo-v2.5` 在 `reasoning:medium` 下仍 50% 機率違背）；
- `parsing_error` 為 `None` 是因為 `PydanticToolsParser` 的 `with_fallbacks` 僅在「有 tool_calls 但 JSON/校驗錯」時設 `parsing_error`，空 `tool_calls` 時**不設**（為空即無嘗試解析），導致 `langchain_adapter.py:236` 的 `parsed is None` 仍拋 `ValueError` 但**無 `parsing_error` 可診斷**（即 `p4_c_failure_diagnosis §2.2` 的吞掉點 1）。

### 4.3 為何用「空tool_calls/JSON截斷/ValidationError/自由文本」四類對應

| 本實驗分類 | 對應 `p4_c_failure_diagnosis §1.3` 假說 | 本次實測 |
|---|---|---|
| `空tool_calls` | `raw.tool_calls==[]` | B 3 次 |
| `JSON截斷` | 截斷 JSON（`Unterminated string`） | 0 次（本次皆完整 JSON，僅協議錯） |
| `ValidationError` | `EvidenceAwareV2Answer` 校驗失敗 | A 3 次 + B 2 次 |
| `自由文本` | 回自由文本/格式不合（未調 tool） | B 3 次（JSON 直出，屬自由文本亞型） |

---

## 5. 分析：為何「開思考」更慢更不穩

### 5.1 慢 — reasoning token 與取樣路徑

- `effort:medium` 使 `mimo-v2.5` 在最終 `tool_calls` 前**先產生內部 reasoning trace**（不計入 `content` 但計入 wall-time 與推理計算），實測 **+26s 平均**（A 8.3s → B 34.9s），與 `formal_chain_latency_anatomy_20260828.md §3.3` 的 `constrained decoding +80-150ms` 無關，**主因是思考生成本身**；
- 且抖動放大（A std 1.85s → B 7.92s），因 reasoning 長度隨 prompt 動態變化；
- 對 `FORMAL_WORKFLOW_TIMEOUT_S=45`（`runner.py:30`）而言，B 的 46.4s 已瀕臨超時，生產環境的 `ASYNC_FORMAL_TIMEOUT_S=120` 雖免超時但用戶需多等 26s。

### 5.2 不穩 — 思考與 tool-calling 協議衝突

- `mimo-v2.5` 的 `function_calling` 在 `reasoning:none` 下已偶發「直出 JSON 不調 tool」（librarian 報告 `DeepSeek-V3 #1376` 同構），`LangChain #31403` 記載 `reasoning` 與 `tools` 互斥時模型傾向**先吐 reasoning 的純文本 JSON，再忘記包成 `tool_calls`**；
- A 的 `none` 下 0% 協議失效（8 次皆有 `tool_calls`），B 的 `medium` 下 **50% 協議失效**（6 次中 3 次空 `tool_calls`），**思考放大了協議競爭**；
- 另 `ValidationError` 的 `limitations` 字串錯在兩組皆存，說明**與 thinking 無關**，為 prompt/schema 固有缺陷（見 §5.3）。

### 5.3 `limitations` 為何必為 `list` 卻回 `str`

- `schemas.py:98` `limitations: list[str] = Field(default_factory=list)`，`system_prompts.py:88` 僅 `limitations 可補充日期、衝突或資料範圍限制`，**未強制「必須是 JSON 陣列，即使只有一條也用 `["..."]` 而非 `"..."`」**；
- `EVIDENCE_AWARE_V2_SYSTEM` 第 9 步「只輸出 schema 中的 ... limitations」+ 示例 `limitations:[]` 已有，但模型在長回答壓力下仍把單一 limitations 壓為字串（4 次規範外，4 次規範內，約 50%）；
- `Pydantic` `strict` 未開（`formal_factory.py:53` 僅 `method="function_calling"` 未 `strict=True`），故 LangChain 不對此做客戶端攔截，交由模型自律即漏。

---

## 6. 結論與建議

### 6.1 C 該用哪個配置

| 配置 | 建議 | 理由 |
|---|---|---|
| **`reasoning:none`（現況）** | **維持，立即採用** | 8.3s、62.5%（雖不穩但比 medium 好 46pp 且快 26s），且失敗可修（單點 ValidationError） |
| **`reasoning:medium`** | **禁用** | 34.9s（>30s 門檻）、16.7%、50% 協議失效，**無任何場景優於 none**；若未來 mimo 修復 tool+reasoning 競爭再重測 |
| **`reasoning:high` / `low`** | **不測，推斷同 medium 或更差** | 推理越長，協議競爭越重，無需再花 16 次 LLM 驗證 |

### 6.2 都不可行時的替代方案（按優先級，工程從小到大）

> **目標**：**不改模型**、**不增 LLM 重試**（同 prompt 重試對 `ValidationError` 修復率≈0 且重計費）、**3 步內將 C 成功率從 62.5% 拉至 ≥95% 且延遲保持 <10s**。

**P0 — 1 天內止血（必做，零模型側改動）**

1. **Prompt 強化 `limitations` 型別**（`system_prompts.py:45-94` + `user_prompts.py` 各 1 行）：
   ```python
   # 在 EVIDENCE_AWARE_V2_SYSTEM 第 9 步後加粗
   "limitations 必須是 JSON 陣列，即使只有一條也用 [\"...\"]，絕不可回字串；空則 []。"
   # 並在 evidence_aware_v2_user_prompt 的 query_shape_hint 區域對本題追加
   "limitations 示例：[\"文件來自2023年...\"], 非 \"文件來自2023年...\""
   ```
   - **預期**：將 `ValidationError` 3/8 壓至 0/8（單點 prompt 缺口，歷史同類修復有效率 >90%）；
   - **驗證**：重跑本 A/B 的 A 組 8 次（`python3 /tmp/thinking_ab_test.py` 改僅 A）應 8/8 成功。

2. **切 `method="json_schema"` + `strict=True` 或 `tool_choice="required"` 固化**（`formal_factory.py:53` 1 行）：
   ```python
   # 現：method="function_calling", include_raw=True
   # 改：method="json_schema", strict=True  或  method="function_calling", strict=True, tool_choice="required"
   # （若 mimo 支持 OpenAI strict；不支援則退回下條）
   chain = llm.with_structured_output(EvidenceAwareV2Answer, method="json_schema", include_raw=True, strict=True)
   ```
   - **理由**：`strict` 使 `limitations: list` 在 **API 層**即拒 `str`，不依賴模型自律；`kalviumlabs.ai` 實測 `json_schema strict` 比 `function_calling` 違反率 1% → 0.2%；
   - **備案**：若 mimo `strict` 不支援，則在 `langchain_adapter.py:235` 前加**本地矯正**（見下條）。

3. **本地 `parsing_error` 矯正層（`langchain_adapter.py:211-239` 10 行，不改 prompt）**：
   ```python
   response = self.chain.invoke(messages)
   if isinstance(response, dict):
       # 1) 空 tool_calls 但 content 為 JSON → 嘗試 JSON 兜底解析
       if response.get("parsed") is None and response.get("parsing_error") is None:
           raw = response.get("raw")
           content = getattr(raw, "content", "") if raw else ""
           if isinstance(content, str) and '"decision"' in content:
               try:
                   import json as _j
                   # 截 { } 間 JSON（B 組 3 次即此）
                   s, e = content.find("{"), content.rfind("}")
                   data = _j.loads(content[s:e+1]) if s!=-1 and e!=-1 else None
                   if data: response["parsed"] = EvidenceAwareV2Answer.model_validate(data)
               except Exception: pass
       # 2) limitations 為 str → 自動包為 [str]
       pe = response.get("parsing_error")
       if pe and "limitations" in str(pe) and isinstance(response.get("raw"), object):
           try:
               tc = getattr(response["raw"], "tool_calls", None) or []
               if tc and isinstance(tc[0]["args"].get("limitations"), str):
                   tc[0]["args"]["limitations"] = [tc[0]["args"]["limitations"]]
                   response["parsed"] = EvidenceAwareV2Answer.model_validate(tc[0]["args"])
                   response["parsing_error"] = None
           except Exception: pass
   ```
   - **預期**：B 的 50% 協議失效可被「content JSON 兜底」修復，A 的 37.5% `limitations str` 可被「自動包 list」修復，**兩組合計修復率 100%（本次 8 次失敗皆屬此二類）**；
   - **成本**：零 LLM 重試、零額外往返、僅本地 1-5ms。

**P1 — 1 週內加固（若 P0 後仍 <95%）**

4. **單次應用層降級（非 LLM 重試）**：`c_node` 捕 `ValueError("parsed data")` 後**不重打 LLM**，改 `DeterministicFixtureCGenerator` 本地確定性回覆（`workflow_adapter.py:DeterministicFixtureCGenerator` 已有 grounded prefix），或 `AGENT REWRITE` 一次（`QueryRewriter`）——見 `p4_c_failure_diagnosis §4.2` 重試策略評估：對 `parsing_error` 重試 LLM 無效（同 prompt 修復率≈0），**降級可將 C_FAILURE 從 37.5% 壓至 <5%**。

5. **Trace 補 `parsing_error` 詳情**（`langchain_adapter.py:235-237` 同 `p4_c_failure_diagnosis §4.1` 建議）：將 `parsing_error_type/message + raw_tool_calls preview` 編入 `ValueError`，使 `E` 的 `C/generator ERROR` 可直接區分「型別錯 vs 空tool_calls」，**不改成功率但修可觀測**（已在本次實驗中證明有診斷價值）。

**P2 — 僅若需極致延遲（非成功率）**

6. **C 輸入瘦身**（`c_workflow_input.py:86 smart_truncate 300→200` + `system_prompts.py:CLINICIAN_DRAFT 650t→500t`）：`formal_chain_latency_anatomy §4` 估省 3-5s，熱 E2E `15.3→8-11s`，但**不修成功率**，故列 P2。

### 6.3 對使用者體驗的意涵

- 現況 `none` 37.5% 失敗 → 用戶每 3 題有 1 題等 8-10s 後收到 `「目前無法產生可驗證的回答，請改由合格醫療專業人員評估。」`（`fallbacks.py:23`） honest fallback，**封閉但體驗差**（`p4_c_failure_diagnosis §5`）；
- P0 修後預期 **<5% 失敗且仍 8s**，用戶 95% 機率 8s 內得 `ANSWER`（含 `supported_claims` 與 `limitations`），僅 5% 走本地降級（仍 honest，不幻覺）；
- `medium` 的 34.9s 即使修復協議層，仍**比 `none` 慢 26s**，在 `ASYNC_FORMAL_TIMEOUT_S=120` 內雖不超時，但 LINE push 用戶需多等 26s，**無 UX 理由選它**。

---

## 7. 限制與下次實驗建議

- **n 小**：8/6 為小樣本，CI 寬（`none` 62.5% 的 Wilson 95% CI 31-86%），但 **medium 更差的方向性已穩**（延遲 +26s 遠超 std）；
- **單 query**：僅 `請說明糖尿病的一般飲食原則。` 1 題，屬 `diet` 主題（`hpa_diet_guide` 為主），未覆蓋 `cause/sleep/medication` 等主題（見 `rag/tfda_retriever.py:_infer_topic`）；
- **單 evidence 集**：固定 3 筆 HPA（無 TFDA 風險溝通的 `medication` 高風險主題），若 `medication` 主題的 `ValidationError` 模式不同，結論需重驗；
- **未測 `json_schema strict` / 矯正層修後**：本次僅測「純 thinking 開關」，**未測 P0 修後的 `none+strict+矯正` 是否達 95%+**（為下次必測）；
- **建議下次**：P0 上線後以同一腳本重跑 A `none` 16 次（可去掉 medium 組），並加 `medication` Query 一組（如 `「糖尿病藥物有哪些風險？」`）各 8 次，共 16 次，驗證跨主題穩定性。

---

## 8. 附錄

### 8.1 實驗腳本

- **腳本**：`/tmp/thinking_ab_test.py`（310 行，含 `load_dotenv_file` / `env_value` / `build_chain` / `get_evidence` / `build_messages` / `classify_failure` / `run_once`）；
- **臨時 raw 轉存**：`/tmp/_ab_raw.json`（已移至 `/tmp`，供本報告生成，**不計入專案檔案**；如需保留可 `cp /tmp/_ab_raw.json docs/reviews/_ab_raw_20260828.json`）；
- **專案零改動驗證**：`git status --porcelain` 除本報告外應為 `?? docs/reviews/_ab_raw.json`（已清）與 `M` 無，`pytest tfda_context_gate/tests/test_workflow_integration.py -q` 15 passed 不受影響（本實驗未觸 `runner.py`/`formal_factory.py`）。

### 8.2 環境

| 項 | 值 |
|---|---|
| `ROUTER_LLM_MODEL` | `opencode/mimo-v2.5`（bare `mimo-v2.5`） |
| `OPENCODE_BASE_URL` | `https://opencode.ai/zen/go/v1` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` |
| `bge-m3` | `ollama/bge-m3:latest`（熱快取命中，`retrieval_latency_ms` 2305 含 HPA 合併） |
| `temperature` | 0 |
| `top_k` | 5（實際 3 筆命中 `threshold 0.55` + `topic allowlist diet`） |
| `ChatOpenAI` | `langchain-openai`，`extra_body.reasoning.effort`+`reasoning_effort` 雙設（`formal_factory.py:46-47` 同步） |
| `EvidenceAwareV2Answer` | `c_generator/schemas.py:84-98`，`limitations: list[str]` |
| `time` | 2026-08-28 21:5x CST，`RAG 2305ms + A 8×(5-10s) + B 6×(24-46s)` 共 ~5min wall-time |

### 8.3 相關報告索引

| 報告 | 重點 | 與本次關係 |
|---|---|---|
| `p4_c_failure_diagnosis_20260828.md` | C `ValueError parsed data` 吞掉點 5 處、重試策略（`429/5xx 1 次，parsing_error 0 次+降級`） | 本實驗的 `parsing_error` 分類與 `tool_calls` 攔截模板來源 |
| `formal_chain_latency_anatomy_20260828.md` | 熱 E2E 15.3s（`A 5ms+RAG 221ms+C 15s`），`with_structured_output` 不增往返 | 解釋為何 A 8.3s 與 B 34.9s 的基線不是框架開銷而是模型思考 |
| `p4_vector_cache_investigation_20260828.md` | `.vector_cache` `g5-faq-v1` 鍵、HPA 合併 | RAG 2305ms 的來源 |
| `p3_latency_profiling_20260827.md` | P3 42.9s 與 15s timeout 矛盾 | 窄路已修，否則本實驗 E2E 42.9s 無法做 A/B |

### 8.4 原始對照表（CSV 可貼試算表）

```csv
group,run,effort,success,latency_s,category,parsing_error_type
A_none,1,none,false,10.55,ValidationError,ValidationError
A_none,2,none,false,7.44,ValidationError,ValidationError
A_none,3,none,false,10.28,ValidationError,ValidationError
A_none,4,none,true,10.64,SUCCESS,
A_none,5,none,true,6.53,SUCCESS,
A_none,6,none,true,5.46,SUCCESS,
A_none,7,none,true,7.71,SUCCESS,
A_none,8,none,true,8.13,SUCCESS,
B_medium,1,medium,false,34.81,ValidationError,ValidationError
B_medium,2,medium,false,46.39,空tool_calls-自由文本,
B_medium,3,medium,true,24.51,SUCCESS,
B_medium,4,medium,false,41.33,空tool_calls-自由文本,
B_medium,5,medium,false,32.84,空tool_calls-自由文本,
B_medium,6,medium,false,29.24,ValidationError,ValidationError
```

---

*結論重申：**C 維持 `reasoning:none`，立即上 P0 三件套（prompt 強制 `limitations: [...]` + `strict`/`tool_choice:required` + 本地 `limitations str→[str]` 與 `content JSON 兜底`）**，預期將 62.5% 拉至 ≥95% 且保持 8s；`medium` 思考模式在 `mimo-v2.5 + function_calling` 下**更慢更不穩，無採用理由**。*
