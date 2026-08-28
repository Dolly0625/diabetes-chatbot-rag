# C — Evidence-aware Generator 深潛

> **模組定位**：`c_generator/` 是「證據可追溯生成」實驗模組。正式 workflow 只認 **v2**（`EvidenceAwareV2Answer` + `CWorkflowInput`）；**v1 僅為 legacy 實驗保留**，不參與 `workflow.run_workflow`。本文件以 `c_generator/schemas.py`、`c_generator/workflow_adapter.py`（re-export，實際定義已拆至 `c_workflow_input.py`/`deterministic_generators.py`/`langchain_adapter.py`，舊路徑仍可用但新路徑為主）、`c_generator/prompts.py`（re-export 至 `system_prompts.py`/`user_prompts.py`）、`experiments/c_generator/generator.py`、`experiments/c_generator/b_to_c_interface.py`、`experiments/c_generator/v2_run_experiment.py` 為準。

- **最後核對**：2026-08-21（以 `CURRENT_ARCHITECTURE.md § C` 與原始碼為準）
- **上層導覽**：[`00_overview.md`](./00_overview.md) · [`CURRENT_ARCHITECTURE.md`](../../archive/docs/CURRENT_ARCHITECTURE.md) · [`workflow_adapter.py`](../../tfda_context_gate/c_generator/workflow_adapter.py)

---

## 1. 為什麼有 v1 / v2？一句話區分

| 維度 | **v1（Legacy）** | **v2（Canonical，正式契約）** |
|------|-----------------|-------------------------------|
| Schema | `EvidenceAwareAnswer` | `EvidenceAwareV2Answer` |
| Claim 欄位 | `claims: list[EvidenceClaim]` | `supported_claims: list[V2SupportedClaim]` + `unsupported_requests: list[V2UnsupportedRequest]` |
| Decision | `ANSWER` / `INSUFFICIENT` 二選一 | `ANSWER` / `PARTIAL` / `INSUFFICIENT` 三選一 |
| 缺口表達 | 只能塞 `limitations: list[str]` | 明確拆成 `unsupported_requests`（缺什麼、為什麼缺）+ `limitations`（日期/範圍/衝突補充） |
| 適用場景 | 早期實驗：整題能答或整題拒答 | 現行正式：**部分可答、部分缺資料**時用 `PARTIAL`，避免 over-refusal |
| Workflow 使用 | ❌ 不使用 | ✅ `workflow.runner` 唯一使用 |

> **不可默默合併**：`CURRENT_ARCHITECTURE.md` 明確寫道「C v1 remains legacy/experiment code and is not used by `workflow.run_workflow`. Do not silently collapse v1 and v2.」任何文件或程式碼都必須保留此區分。

### 1.1 v1 Schema（`c_generator/schemas.py:8-18`）

```python
class EvidenceClaim(BaseModel):
    claim_id: str          # 如 claim_1
    claim: str             # 一句事實
    evidence_ids: list[str]

class EvidenceAwareAnswer(BaseModel):
    decision: Literal["ANSWER", "INSUFFICIENT"]
    answer: str
    claims: list[EvidenceClaim] = []
    limitations: list[str] = []
```

- `claims` 最多 4 條（見 `prompts.py:EVIDENCE_AWARE_SYSTEM` 約束），每條一句。
- `decision` 禁止 `SUFFICIENT`，禁止 Markdown code fence。

### 1.2 v2 Schema（`c_generator/schemas.py:21-37`）

```python
class V2SupportedClaim(BaseModel):
    claim_id: str          # 只能是 c1 / claim_1 這類短標籤，禁止塞 evidence ID
    claim: str             # 被 context 明確支持的事實
    evidence_ids: list[str]  # 至少一個，且必須是 B-approved 子集

class V2UnsupportedRequest(BaseModel):
    request: str           # 被要求但 context 無法回答的部分
    reason: str            # 為什麼無法回答

class EvidenceAwareV2Answer(BaseModel):
    decision: Literal["ANSWER", "PARTIAL", "INSUFFICIENT"]
    answer: str
    supported_claims: list[V2SupportedClaim] = []
    unsupported_requests: list[V2UnsupportedRequest] = []
    limitations: list[str] = []
```

- `supported_claims` 最多 3 條、`unsupported_requests` 最多 3 條、`limitations` 最多 2 條（`prompts.py:EVIDENCE_AWARE_V2_SYSTEM` 第 10 點）。
- `claim_id` 與 `evidence_ids` 嚴格分離：不可把 evidence ID 塞進 `claim_id` 或 `claim` 文字來代替 citation。

### 1.3 Decision 語意對照

| Decision | v1 含義 | v2 含義 |
|----------|---------|---------|
| `ANSWER` | 核心問題有足夠文件支持 | 主要要求都有足夠文件支持 |
| `PARTIAL` | （不存在） | **至少一部分有支持、一部分沒有**；回答有支持部分，並在 `unsupported_requests` 明確指出缺口。不可因一半缺資料就整題 `INSUFFICIENT` |
| `INSUFFICIENT` | 核心問題完全無直接支持 | 核心要求完全無直接支持；即使如此，若 context 有同主題前提事實仍應列在 `supported_claims`，只有完全無相關事實時才可空陣列 |

**v2 為何是 canonical**：真實 TFDA 場景常見「一半有證據、一半缺分母/劑量/症狀/頻率」的提問（見 `experiments/c_generator/v2_run_experiment.py:PARTIAL_CASE_TYPES = {"numeric_trap", "partial_guess"}`）。v1 的二元判斷會導致 **over-refusal**（明明有 12 例卻因缺百分比而整題拒答）。v2 用 `PARTIAL` 保留已支持事實、同時標示缺口，符合 `CURRENT_ARCHITECTURE.md` 的「C v2 may cite only B-approved evidence IDs」與 D 的證據校驗鏈。

---

## 2. 正式 Workflow 輸入：`CWorkflowInput`

定義於 `c_generator/workflow_adapter.py:23-33`，是 **唯一進入 C v2 的正式形狀**：

```python
class CWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 禁止額外欄位

    request_id: str              # 對應 B 的 request_id
    schema_version: str = "c.v2" # 固定 C_V2_SCHEMA_VERSION
    original_query: str          # 使用者原句（非改寫後）
    b_decision: str              # B 的 decision（PASS / INSUFFICIENT 等）
    approved_evidence_ids: list[str]  # B 已批准的 evidence_id 清單
    evidence: list[CanonicalEvidence] # 對應的 CanonicalEvidence 物件
```

| 欄位 | 來源 | 說明 |
|------|------|------|
| `request_id` | `CanonicalBResult.request_id` | 經 `c_input_from_b_result()` 轉換 |
| `original_query` | workflow 初始 `RequestContext.user_raw_input` | 需外部傳入，不從 B 推導 |
| `b_decision` | `CanonicalBResult.decision` | 供 prompt 標示 `B Context Gate decision` |
| `approved_evidence_ids` | `CanonicalBResult.approved_evidence_ids` | **唯一可引用 ID 集合** |
| `evidence` | `CanonicalBResult.evidence` | 完整 `CanonicalEvidence[]`，含 `evidence_id`/`content`/`source`/`metadata`/`score`/`date` |

轉換函式：

- `c_input_from_b_result(b_result, *, original_query)`（`workflow_adapter.py:41-48`）— workflow 內部唯一正規入口。
- `to_legacy_v2_case(request)`（`workflow_adapter.py:51-72`）— 僅在 **live-chain 邊界**把 `CWorkflowInput` 轉回舊實驗的 `case` dict（`case_id`/`query`/`b_decision`/`approved_document_ids`/`contexts[]`），供 `evidence_aware_v2_user_prompt()` 使用。

---

## 3. 兩個 Generator：Fixture vs Live Adapter

### 3.1 `DeterministicFixtureCGenerator`（離線 Mock）

> **非生產元件**。`CURRENT_ARCHITECTURE.md § Mock` 明確標示為「offline MOCK/FIXTURE components for E2E contract validation, not production retrieval/judging/generation」。

- 位置：`c_generator/workflow_adapter.py:75-118`
- 建構：`DeterministicFixtureCGenerator(max_evidence=None)`，`max_evidence` 若提供必須 `>=1`
- 行為：
  - 取 `evidence` 中 `evidence_id ∈ approved_evidence_ids` 的子集（`usable`），若 `max_evidence` 設限則截斷。
  - 若 `usable` 為空 → 回 `INSUFFICIENT`，`supported_claims=[]`，`unsupported_requests=[{request: original_query, reason: "沒有可用的 B-approved evidence"}]`。
  - 否則 → 回 `ANSWER`，每條 `usable` 轉一條 `V2SupportedClaim(claim_id=c{index}, claim=item.content, evidence_ids=[item.evidence_id])`，`answer` 為 `"根據提供的資料：" + claims 串接`。
- 用途：`workflow.demo --retriever fixture` 與 `tests/test_workflow_integration.py` 的確定性基線，**不可宣稱為生產生成器**。

### 3.2 `LangChainCV2Generator`（正式 Live 適配）

- 位置：`c_generator/workflow_adapter.py:121-145`
- 建構：`LangChainCV2Generator(chain)` — **不自行建模**，由呼叫方注入已配置好的 structured-output chain。
- 行為：`generate(request)` 內部呼叫 `to_legacy_v2_case(request)` → `evidence_aware_v2_user_prompt()` → `chain.invoke([SystemMessage(EVIDENCE_AWARE_V2_SYSTEM), HumanMessage(user_prompt)])` → `EvidenceAwareV2Answer.model_validate(parsed)`。
- 與實驗的差異：實驗的 `experiments/c_generator/generator.py:build_llm()` / `experiments/c_generator/v2_run_experiment.py:build_llm()` 會自行讀 `OPENROUTER_API_KEY` / `GENERATOR_MODEL` 建 `ChatOpenRouter`；workflow 的 adapter **禁止隱式建模**，保持依賴注入。

| 特性 | `DeterministicFixtureCGenerator` | `LangChainCV2Generator` |
|------|----------------------------------|--------------------------|
| 是否需外部 LLM | 否 | 是（注入 `chain`） |
| 是否調 prompt | 否（直接拼 `content`） | 是（`EVIDENCE_AWARE_V2_SYSTEM` + `evidence_aware_v2_user_prompt`） |
| 回傳型別 | `EvidenceAwareV2Answer`（本地構造） | `EvidenceAwareV2Answer`（`model_validate` 解析） |
| 生產可用 | ❌ 僅契約測試 | ✅ 正式路徑（需外部注入） |
| 對應 `CURRENT_ARCHITECTURE.md` | Mock / Fixture | Real / Adapter |

---

## 4. 證據引用規則（Evidence-citing Rule）

**硬性邊界**（`CURRENT_ARCHITECTURE.md § Hard Boundaries`）：

> - B approval is explicit; retrieval alone is not approval.
> - C claims must cite evidence IDs.
> - C v2 may cite only B-approved evidence IDs.

具體落實（`prompts.py:EVIDENCE_AWARE_V2_SYSTEM` 第 3、5 點 + `experiments/c_generator/v2_run_experiment.py:output_protocol_metrics`）：

1. **只能引用 `approved_evidence_ids` 的子集**：`supported_claims[].evidence_ids` 必須 `⊆ approved_evidence_ids`，不可自創 ID。`experiments/c_generator/v2_run_experiment.py` 會計算 `evidence_id_boundary_violation_count` 與 `citation_accuracy`。
2. **每條 supported claim 必須帶至少一個 evidence ID**：空 `evidence_ids` 計為 `supported_claim_missing_evidence_id_count`，`citation_coverage` 下降。
3. **claim_id ≠ evidence_id**：`claim_id` 只能是 `c1`/`c2` 短標籤，evidence ID 必須放在 `evidence_ids` 陣列，不可塞進 `claim_id` 或 `claim` 文字。
4. **不可用記憶或常識補完**：`BASE_SYSTEM` 明確「只能使用下方提供的 TFDA 文件，不可以使用記憶、常識或文件以外的資料補充」。
5. **禁止猜測數字**：發生率、百分比、分母、劑量、頻率、症狀、監測方式、因果關係、個人風險若無直接文件支持，必須列為 `unsupported_requests`，不可自行填數（`EVIDENCE_AWARE_V2_SYSTEM` 第 5 點）。
6. **D 會二次校驗**：`d_output_gate` 檢查 `invalid_evidence_ids`，越界直接 `FALLBACK`。

---

## 5. B → C 介面：`build_interface`

位置：`experiments/c_generator/b_to_c_interface.py:11-77`

```
B 產物 ──► build_interface(b_run_dir, output_path) ──► interface_cases.json ──► C 實驗 / workflow
         docs_path: b_run_dir/data/processed/langchain_documents.json
         phase5_path: b_run_dir/results/hybrid_narrow_top4.json
```

步驟：

1. 讀 `langchain_documents.json` 建 `by_id` 索引。
2. 讀 `hybrid_narrow_top4.json` 取 `runs[0].context_rows` 得 `s1_ids`（S1 案例的真實 B narrow_top4 context）。
3. 依 `C_CASE_SET` 環境變數選 `build_case_specs()`（baseline 20 題）或 `build_hard_case_specs()`（hard 30 題）。
4. 對每個 `CaseSpec`：
   - `S1` 用 `s1_ids`，其餘用 `spec.context_ids`。
   - 校驗每個 `document_id` 存在於 `by_id`，否則 `RuntimeError`。
   - 組 `contexts[]`（含 `document_id`/`row_index`/`發布日期`/`藥品成分`/`page_content`/`source_dataset`）。
   - 組 `interface_cases[]`：`case_id`/`case_type`/`query`/`b_decision`/`stress_test`/`approved_document_ids`/`context_document_ids`/`contexts`/`ground_truth`（`expected_decision`/`expected_handling`/`supported_facts`/`unavailable_facts`）/`provenance`（`b_run_dir`/`b_phase5_reference`/`fixture_note`）。
5. 寫入 `output_path`（預設 `RESULTS_DIR/interface_cases.json`）。

> `provenance.fixture_note` 明確：「S1 uses the actual B narrow_top4 context and PASS result. Other cases use manually specified B-to-C interface fixtures over the same TFDA corpus; no new retrieval result is claimed.」

`CaseSpec` 來源（`experiments/c_generator/experiment_cases.py:7-17`）：

```python
@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    case_type: str          # sufficient / partial / distractor / insufficient
    query: str
    context_ids: tuple[str, ...]
    approved_ids: tuple[str, ...]
    b_decision: str         # PASS / FALLBACK
    expected_handling: str
    supported_facts: tuple[str, ...]
    unavailable_facts: tuple[str, ...]
    stress_test: bool = False
```

---

## 6. 實驗流程：`run_generators` / `invoke_one` / `experiments/c_generator/v2_run_experiment`

### 6.1 v1 實驗（`experiments/c_generator/generator.py`）

- `METHODS = ("baseline", "grounded", "evidence_aware")`
- `build_llm()`：讀 `OPENROUTER_API_KEY`、`GENERATOR_MODEL`（fallback `JUDGE_MODEL` / `nvidia/nemotron-3-super-120b-a12b:free`）、`base_url`、`GENERATOR_REQUEST_TIMEOUT`、`GENERATOR_MAX_TOKENS=3584`、`GENERATOR_REASONING_EFFORT=low`，建 `ChatOpenRouter(temperature=0, timeout=ms, max_retries=0)`。
- `build_chains(llm)`：`evidence_aware` 用 `llm.with_structured_output(EvidenceAwareAnswer, method="json_schema", strict=True, include_raw=True)`，其餘兩者為裸 `llm`。
- `invoke_one(chain, method, case, limiter)`：
  - 依 `method` 選 `BASELINE_SYSTEM` / `GROUNDED_SYSTEM` / `EVIDENCE_AWARE_SYSTEM`。
  - `user_prompt = generator_user_prompt(case, method)`（含 `Case ID`/`Query`/`B decision`/`Approved evidence IDs`/`Context documents`）。
  - 經 `invoke_with_rate_limit(..., label=f"c.generator.{method}.{case_id}")` 調用。
  - `evidence_aware` 需 `response["parsed"]` 非空，否則 `RuntimeError`；其餘取 `response.content`。
  - 回傳 `timestamp`/`case_id`/`case_type`/`method`/`query`/`b_decision`/`approved_document_ids`/`output`/`raw_content`/`usage`/`response_metadata_keys`/`timing`/`error`。
- `run_generators(cases, results_path, limiter, smoke_only)`：`smoke_only` 時僅跑 `cases[:1]`；逐 `case × method` 調 `invoke_one`，寫 JSONL，附 `model_config`/`endpoint_configured`。

### 6.2 v2 實驗（`experiments/c_generator/v2_run_experiment.py`）

> 刻意不調 baseline/grounded，只改 Evidence-aware 的 schema 與決策策略；輸入為 **frozen v1 interface 的逐字複製**。

- `load_frozen_interface()`：讀 `C_V1_RUN_DIR`（預設 `tfda_context_gate/runs/c_hard_nemotron_20260819/results/interface_cases.json`），`shutil.copyfile` 到 `RESULTS_DIR/interface_cases.json`，計算 `SHA-256` 供稽核。
- `run_generator(cases, output_path, smoke_only)`：
  - `build_llm()` 同 v1，`chain = llm.with_structured_output(EvidenceAwareV2Answer, ...)`。
  - `smoke_only` 時僅跑 `SMOKE_CASE_IDS = ("X2", "P1", "I1")`。
  - 逐 case 調 `invoke_v2(chain, case, limiter)`（`label=f"c.v2.evidence_aware.{case_id}"`，`System=EVIDENCE_AWARE_V2_SYSTEM`，`Human=evidence_aware_v2_user_prompt(case)`），寫 `v2_generator_outputs.jsonl`。
- `run_auxiliary_judge(cases, rows, path)`：用 `AUXILIARY_V2_JUDGE_SYSTEM` + `evaluation_v2_user_prompt` 調 `V2AuxiliaryEvaluation`（`decision: ANSWER/PARTIAL/INSUFFICIENT` + `supported_claim_count`/`partially_supported_claim_count`/`unsupported_claim_count`/`partial_answer_correct`/`over_refusal`/`insufficient_handling_correct`），結果僅為輔助、非 Ground Truth。
- `summarize_v2()`：計算 `manual_decision_accuracy`（對 `expected_v2_decision`）、`partial_answer_accuracy_protocol`（結構化檢查：`decision==PARTIAL && claims && unsupported_requests && 無缺 ID/越界`）、`over_refusal_rate`、`citation_accuracy/coverage`、`evidence_id_boundary_violation_count` 等；`write_metrics` / `write_comparison` / `write_report` 產出 `v2_evaluation_summary.json` / `v1_v2_comparison.csv` / `C_v2_Partial_Answer_Generator.md`。

---

## 7. Prompt 模板（以 `c_generator/prompts.py` 為準，不可自創）

### 7.1 共用基底 `BASE_SYSTEM`

```
你是一個醫療 RAG 的 Generator。
你只能使用下方提供的 TFDA 文件，不可以使用記憶、常識或文件以外的資料補充。
這是研究實驗，不是對個人的醫療診斷或治療建議。
若文件沒有回答問題所需的資訊，請明確說明「提供的文件不足以回答這一部分」，不要猜測、不要捏造數字。
回答要使用繁體中文，簡潔但要保留文件中的重要限制與時間背景。
```

### 7.2 三種 v1 System

- `BASELINE_SYSTEM` = `BASE_SYSTEM` +「直接回答；不要輸出 Evidence ID，也不要假裝有引用。」
- `GROUNDED_SYSTEM` = `BASE_SYSTEM` +「逐項對照文件後回答；只能陳述文件能支持的內容。若細節（發生率、死亡率、個人風險、監測建議）不在文件中，必須指出缺口。」
- `EVIDENCE_AWARE_SYSTEM` = `BASE_SYSTEM` +「把重要事實拆成 claims；`evidence_ids` 只能用 approved IDs；無支持的 claim 改列 `limitation`；核心不足時 `decision=INSUFFICIENT`；只輸出 `decision/answer/claims/limitations` 四欄位；`claims` 最多 4 個、每條一句。」

### 7.3 v2 System `EVIDENCE_AWARE_V2_SYSTEM`（重點節錄）

- 先拆 Query 為獨立要求，再逐項檢查支持度。
- `decision` 三選一語意如 §1.3。
- 10 條嚴格規則：僅用 TFDA 文件、內部拆解不輸出 CoT、`supported_claims` 必帶 approved ID、`unsupported_requests` 說明缺口、禁止猜測數字/症狀/頻率、半支持必須 `PARTIAL`（如「12 例」有但「百分比」無 → 保留 claim + 列缺口）、核心全無才 `INSUFFICIENT`、比較文件時「沒有通報 ≠ 沒有風險」不算矛盾、只輸出五欄位、長度上限（3/3/2）。
- 含最小格式示例（`{"decision":"PARTIAL", ...}`）。

### 7.4 User Prompt

- `generator_user_prompt(case, method)`：`Case ID` / `Query` / `B Context Gate decision` / `Approved evidence IDs` / `Context documents`（`document_id`/`發布日期`/`藥品成分`/`page_content` 以 `--- DOCUMENT SEPARATOR ---` 分隔）/ `請完成 {method} Generator 的輸出。`
- `evidence_aware_v2_user_prompt(case)`：同上，另依 `approved_document_ids` 是否為空、`query` 是否含「矛盾」/「mg/kg」/「只和/只發生/僅與/只有」/「標示日期」注入 `no_evidence_hint` / `query_shape_hint`（如無 evidence 時要求極短 `INSUFFICIENT`、矛盾題要求「不必然矛盾」結論、mg/kg 題要求 `PARTIAL` 等）。

> 以上皆為 `prompts.py` 原文轉述，未新增模板。

---

## 8. 最小可跑範例

### 8.1 Fixture 路徑（離線、確定性、非生產）

```python
from tfda_context_gate.b_context_gate.schemas import CanonicalEvidence
from tfda_context_gate.c_generator.workflow_adapter import (
    CWorkflowInput,
    DeterministicFixtureCGenerator,
)

req = CWorkflowInput(
    request_id="demo-001",
    original_query="SGLT2 抑制劑酮酸中毒有哪些安全說明？",
    b_decision="PASS",
    approved_evidence_ids=["tfda-risk-0019"],
    evidence=[
        CanonicalEvidence(
            evidence_id="tfda-risk-0019",
            content="SGLT2 抑制劑可能導致酮酸中毒，TFDA 已發布安全資訊。",
            source="tfda-risk-0019",
            metadata={},
            score=0.9,
            date="2023-01-01",
            version="v1",
        )
    ],
)

ans = DeterministicFixtureCGenerator().generate(req)
print(ans.model_dump(mode="json"))
# {"decision":"ANSWER","answer":"根據提供的資料：SGLT2 ...","supported_claims":[{"claim_id":"c1",...}],...}

# 若 approved_evidence_ids 為空 → INSUFFICIENT
empty = CWorkflowInput(
    request_id="demo-002",
    original_query="未知風險的發生率是多少？",
    b_decision="INSUFFICIENT",
    approved_evidence_ids=[],
    evidence=[],
)
print(DeterministicFixtureCGenerator().generate(empty).decision)  # INSUFFICIENT
```

### 8.2 Adapter 轉換（銜接 B 與 Live Chain）

```python
from tfda_context_gate.c_generator.workflow_adapter import (
    c_input_from_b_result,
    to_legacy_v2_case,
    LangChainCV2Generator,
)
from tfda_context_gate.c_generator.prompts import evidence_aware_v2_user_prompt

# 1. B → C 正規轉換
c_input = c_input_from_b_result(b_result, original_query="Hydrochlorothiazide 與皮膚癌的關聯？")

# 2. 僅在 live-chain 邊界轉回舊 case 形狀（供 prompt 使用）
legacy_case = to_legacy_v2_case(c_input)
user_prompt = evidence_aware_v2_user_prompt(legacy_case)

# 3. 注入已配置的 structured-output chain（不隱式建模）
# chain = llm.with_structured_output(EvidenceAwareV2Answer, method="json_schema", strict=True, include_raw=True)
# answer: EvidenceAwareV2Answer = LangChainCV2Generator(chain).generate(c_input)
```

### 8.3 實驗腳本

```bash
# v1 三方法實驗（baseline / grounded / evidence_aware）
python -m experiments.c_generator.generator  # 經 run_generators / invoke_one

# v2 正式實驗（frozen interface + partial-answer）
python -m experiments.c_generator.v2_run_experiment              # 全量
python -m experiments.c_generator.v2_run_experiment --smoke-only # 僅 X2/P1/I1
python -m experiments.c_generator.v2_run_experiment --evaluate-only  # 僅重算評估

# B → C 介面重建
C_B_RUN_DIR=runs/b_nemotron_20260818 C_INTERFACE_PATH=results/interface_cases.json \
  python -m experiments.c_generator.b_to_c_interface
```

---

## 9. 檔案一覽

| 檔案 | 職責 | 關鍵符號 |
|------|------|----------|
| `c_generator/schemas.py` | v1/v2 與輔助評估的 Pydantic 契約 | `EvidenceClaim`、`EvidenceAwareAnswer`、`V2SupportedClaim`、`V2UnsupportedRequest`、`EvidenceAwareV2Answer`、`AuxiliaryEvaluation`、`V2AuxiliaryEvaluation` |
| `c_generator/workflow_adapter.py` | 正式 workflow 邊界與雙 Generator 實作（re-export，實際定義已拆至 `c_workflow_input.py`/`deterministic_generators.py`/`langchain_adapter.py`，舊路徑仍可用但新路徑為主） | `CWorkflowInput`、`C_V2_SCHEMA_VERSION`、`CGenerator`、`c_input_from_b_result`、`to_legacy_v2_case`、`DeterministicFixtureCGenerator`、`LangChainCV2Generator` |
| `c_generator/prompts.py` | 全部 System / User prompt 模板（re-export 至 `system_prompts.py`/`user_prompts.py`，舊路徑仍可用但新路徑為主） | `BASE_SYSTEM`、`BASELINE_SYSTEM`、`GROUNDED_SYSTEM`、`EVIDENCE_AWARE_SYSTEM`、`EVIDENCE_AWARE_V2_SYSTEM`、`AUXILIARY_JUDGE_SYSTEM`、`AUXILIARY_V2_JUDGE_SYSTEM`、`context_block`、`generator_user_prompt`、`evidence_aware_v2_user_prompt`、`evaluation_user_prompt`、`evaluation_v2_user_prompt` |
| `experiments/c_generator/generator.py` | v1 實驗執行期（live LLM） | `METHODS`、`content_to_text`、`extract_usage`、`build_llm`、`build_chains`、`invoke_one`、`run_generators` |
| `experiments/c_generator/v2_run_experiment.py` | v2 正式實驗（frozen interface、partial-answer、輔助 judge、對比報告） | `load_frozen_interface`、`invoke_v2`、`run_generator`、`run_auxiliary_judge`、`expected_v2_decision`、`output_protocol_metrics`、`summarize_v2`、`write_comparison`、`PROMPT_VERSION` |
| `experiments/c_generator/b_to_c_interface.py` | B 產物 → C 介面 fixture 轉換 | `build_interface`、`CaseSpec` 來源 `build_case_specs` / `build_hard_case_specs` |
| `experiments/c_generator/experiment_cases.py` | Baseline 20 題 fixture | `CaseSpec`、`build_case_specs`（S1-S5 sufficient / P1-P5 partial / D1-D5 distractor / I1-I5 insufficient） |
| `experiments/c_generator/hard_experiment_cases.py` | Hard 30 題 fixture（`C_CASE_SET=hard` 時） | `build_hard_case_specs` |
| `workflow/runner.py` + `workflow/graph.py` | 正式編排（調用 `CWorkflowInput` + Generator） | `run_workflow`、`build_workflow_graph` |

---

## 10. 常見陷阱與紅線

- **不要把 Fixture 當生產**：`DeterministicFixtureCGenerator` 只是把 `evidence.content` 原樣當 claim 回傳，無任何 LLM 判斷；上線必須注入 `LangChainCV2Generator`。
- **不要自創 evidence ID**：`evidence_ids` 越界會被 `experiments/c_generator/v2_run_experiment` 計為 `evidence_id_boundary_violation_count`，並被 D 判 `FALLBACK`。
- **不要把 `PARTIAL` 當 `INSUFFICIENT`**：`numeric_trap` / `partial_guess` 類型的正確答案是 `PARTIAL`（保留「12 例」等已支持事實 + 列「百分比」缺口），整題拒答屬 `over_refusal`。
- **不要在 prompt 塞 Ground Truth**：`experiments/c_generator/v2_run_experiment.py` 的 `v2_config.json` 明確 `generator_prompt_includes_ground_truth: false`，Ground Truth 僅在評估階段使用。
- **不要繞過 D**：任何 `EvidenceAwareV2Answer` 都必須經 `d_output_gate.gate.run_output_gate` 才可回傳使用者。

---

## 11. 延伸閱讀

- `CURRENT_ARCHITECTURE.md § C — c_generator — Canonical Generator = v2`
- `ARCHITECTURE_AUDIT.md`（模組盤點與 schema 矩陣）
- `c_generator/prompts.py`（prompt 原文）
- `experiments/c_generator/v2_run_experiment.py:488-537`（`write_report` 產出的 `C_v2_Partial_Answer_Generator.md` 範本）
