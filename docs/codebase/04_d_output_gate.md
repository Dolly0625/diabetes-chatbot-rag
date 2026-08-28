# D Mandatory Output Gate 深潛

> **模組**：`tfda_context_gate/d_output_gate/` · **入口**：`gate.py:run_output_gate()` · **版本**：`d.v0.1`
> **定位**：最終強制閘門（Mandatory Gate）—— 任何 C 候選回答未經 D 不得直接回傳給使用者。只回 `PASS` 或 `FALLBACK`，fail-closed。

---

## 1. 為什麼需要 D

| 問題 | D 的回答 |
|------|----------|
| C 幻覺引用不存在的證據？ | 擋（`EVIDENCE_ID_NOT_FOUND`） |
| C 引用了檢索到但 B 未批准的證據？ | 擋（`EVIDENCE_ID_NOT_APPROVED_BY_B`） |
| A 已判定非衛教路由或高風險，C 仍給答案？ | 擋（`POLICY_*`） |
| C 寫「你可以自行停藥」？ | 擋（`POLICY_EXPLICIT_OUTPUT_REDLINE`） |
| C 的 claim 與證據文字語意不符？ | 擋（`SEMANTIC`） |
| 驗證器本身掛掉？ | 擋（`DEPENDENCY` → 安全 fallback） |
| C 誠實說「不知道」且無 supported_claims？ | 放行（`D_SAFE_ABSTENTION_ACCEPTED`） |

D 不做檢索、不做生成、不改政策，只做**驗證與否決**。

---

## 2. 8 步驗證流水線（ARCHITECTURE_AUDIT 定版）

`gate.py:run_output_gate()` 以**確定性優先、fail-closed** 順序執行，任一步失敗即回 `FALLBACK`，不再往下走：

```
payload (dict | OutputGateRequest)
  │
  ▼
[1] 適配 A/B/C 多形輸入 → OutputGateRequest
  │   build_gate_request() 接受 canonical D 形狀與既有 fixture 形狀
  │   失敗 → SCHEMA / D_INPUT_SCHEMA_INVALID
  ▼
[2] 驗證 A 政策快照（PolicySnapshot）
  │   parse_policy() 嚴格校驗；失敗 → SCHEMA / A_POLICY_SCHEMA_INVALID
  ▼
[3] 驗證 B 證據集合（EvidenceSet）
  │   parse_evidence_set() 嚴格校驗；失敗 → SCHEMA / B_EVIDENCE_SCHEMA_INVALID
  ▼
[4] 驗證 C 候選形狀與 claim evidence_ids
  │   parse_candidate_response() + _validate_candidate_shape()
  │   失敗 → SCHEMA / C_OUTPUT_SCHEMA_INVALID / C_ANSWER_HAS_NO_SUPPORTED_CLAIMS 等
  │   每條 supported_claim 必須帶非空 evidence_ids，否則 CLAIM_WITHOUT_EVIDENCE_ID
  ▼
[5] 要求 B PASS 且引用皆為已批准 ID
  │   evidence_set.decision != "PASS" → EVIDENCE / B_EVIDENCE_SET_NOT_APPROVED
  │   _validate_evidence_ids() 比對三類錯誤：
  │     missing（引用不存在）→ EVIDENCE_ID_NOT_FOUND
  │     not_approved（存在但未批准）→ EVIDENCE_ID_NOT_APPROVED_BY_B
  │     malformed_approved（批准清單無對應 record）→ B_APPROVED_EVIDENCE_MISSING_RECORD
  │   任一命中 → EVIDENCE，填 invalid_evidence_ids
  ▼
[6] 強制 A 路由/風險與 D 紅線
  │   check_policy_snapshot() → POLICY
  │   check_candidate_red_lines() → POLICY / POLICY_EXPLICIT_OUTPUT_REDLINE
  ▼
[7] 處理安全棄權（abstention）
  │   若 candidate.supported_claims 為空 → 直接 PASS / D_SAFE_ABSTENTION_ACCEPTED
  │   不呼叫語意驗證器（無 claim 可驗）
  ▼
[8] 可插拔語意驗證（有 claim 時）
  │   verifier.verify(candidate, evidence_set, policy) → SemanticVerificationResult
  │   驗證器拋異常或回傳型別錯誤 → DEPENDENCY / VERIFIER_DEPENDENCY_FAILURE
  │   有 failed_claims / unsupported_answer_claims → SEMANTIC
  │   全部通過 → PASS / OUTPUT_GATE_PASSED
```

> 順序是刻意設計的：**SCHEMA → EVIDENCE → POLICY → SEMANTIC → DEPENDENCY**，便宜的確定性檢查先擋，昂貴的語意判斷最後做。

---

## 3. 核心 Schema

### 3.1 OutputGateRequest（D 唯一輸入契約）

`schemas.py:OutputGateRequest` — adapter 產出的邊界物件，原始值在 gate 內才逐層驗證：

```python
class OutputGateRequest(StrictModel):
    request_id: str          # 追蹤用，payload 缺失時補 "unknown"
    schema_version: str      # 預設 "d.v0.1"
    policy: dict[str, Any]           # 原始 A 快照，待 parse_policy()
    evidence_set: dict[str, Any]     # 原始 B 結果，待 parse_evidence_set()
    candidate_response: Any           # 原始 C 回應，待 parse_candidate_response()
```

`StrictModel`（`extra="forbid"`）禁止未定義欄位，避免寬鬆輸入掩蓋錯誤。

### 3.2 PolicySnapshot（A 的獨立字串快照）

```python
class PolicySnapshot(StrictModel):
    router_status: str        # 非 enum，純字串；由 gate 對照 KNOWN_ROUTER_STATUSES
    rag_allowed: bool
    risk_flags: list[str] = []
    intent_tags: list[str] = []
    reason_codes: list[str] = []
```

**為何不用 A 的 enum？** `ARCHITECTURE_AUDIT.md` 明確指出：A 的 Pydantic enum 是嚴格型別，D 的快照是**刻意獨立的字串快照**，adapter 不做完整 A 模型轉換（`adapters.py:build_gate_request` 僅抽 `router_status`/`rag_allowed`/`risk_flags`/`intent_tags`/`reason_codes` 五欄）。D 把 A 的欄位當「事實」對待，不推斷也不覆寫 A 的政策決定。若 `router_status` 不在已知集合，D 以 `POLICY_UNKNOWN_ROUTER_STATUS` 擋掉。

### 3.3 EvidenceSet（B 的正規化輸出）

```python
class EvidenceRecord(StrictModel):
    evidence_id: str
    content: str
    metadata: dict[str, Any] = {}

class EvidenceSet(StrictModel):
    decision: str                          # 必須為 "PASS" 才放行
    approved_evidence_ids: list[str] = []  # B 明確批准的 ID，與 evidence 分離
    evidence: list[EvidenceRecord] = []    # 檢索到的全部上下文
```

關鍵設計：**檢索到 ≠ 已批准**。`approved_evidence_ids` 必須由 B 明確標記，adapter 絕不從 `evidence` 自動推導（見 `adapters.py:70-72` 註解）。

### 3.4 CandidateResponse（C v0.1 / v2 正式契約）

```python
class SupportedClaim(StrictModel):
    claim_id: str
    claim: str
    evidence_ids: list[str] = []   # 每條 claim 必帶，否則 CLAIM_WITHOUT_EVIDENCE_ID

class UnsupportedRequest(StrictModel):
    request: str
    reason: str = ""

class CandidateResponse(StrictModel):
    decision: Literal["ANSWER", "PARTIAL", "INSUFFICIENT"]
    answer: str
    supported_claims: list[SupportedClaim] = []
    unsupported_requests: list[UnsupportedRequest] = []
    limitations: list[str] = []
```

形狀規則（`_validate_candidate_shape`）：

| decision | 要求 | 違規碼 |
|----------|------|--------|
| `ANSWER` | 至少一條 `supported_claims` | `C_ANSWER_HAS_NO_SUPPORTED_CLAIMS` |
| `PARTIAL` | `supported_claims` 或 `unsupported_requests` 至少一邊非空 | `C_PARTIAL_HAS_NO_SUPPORTED_OR_UNSUPPORTED_FIELDS` |
| `INSUFFICIENT` | 不可帶 `supported_claims` | `C_INSUFFICIENT_HAS_SUPPORTED_CLAIMS` |

### 3.5 OutputGateResult（D 唯一輸出）

```python
class OutputGateResult(StrictModel):
    request_id: str
    schema_version: str
    decision: Literal["PASS", "FALLBACK"]
    passed: bool                          # decision == "PASS"
    failure_type: Literal["NONE","SCHEMA","EVIDENCE","POLICY","SEMANTIC","DEPENDENCY"]
    reason_codes: list[str] = []          # 去重保序
    failed_claims: list[ClaimFailure] = []
    invalid_evidence_ids: list[str] = []  # 三類 evidence 錯誤的聯集
    final_response: str                   # PASS 時為 candidate.answer，FALLBACK 時為安全回覆
    candidate_decision: str | None = None # 原始 C decision（ANSWER/PARTIAL/INSUFFICIENT）
    verifier: str | None = None           # 實際執行的 verifier 名稱
```

`failure_type` 與 `reason_codes` 對照（**以 `gate.py` / `policy.py` 為準，不可自創**）：

| failure_type | 觸發情境 | 常見 reason_codes |
|--------------|----------|-------------------|
| `NONE` | PASS | `OUTPUT_GATE_PASSED` / `D_SAFE_ABSTENTION_ACCEPTED` |
| `SCHEMA` | 任一輸入 schema 非法或形狀違規 | `D_INPUT_SCHEMA_INVALID`、`A_POLICY_SCHEMA_INVALID`、`B_EVIDENCE_SCHEMA_INVALID`、`C_OUTPUT_SCHEMA_INVALID`、`C_ANSWER_HAS_NO_SUPPORTED_CLAIMS`、`CLAIM_WITHOUT_EVIDENCE_ID` 等 |
| `EVIDENCE` | B 未 PASS 或 evidence_id 校驗失敗 | `B_EVIDENCE_SET_NOT_APPROVED`、`EVIDENCE_ID_NOT_FOUND`、`EVIDENCE_ID_NOT_APPROVED_BY_B`、`B_APPROVED_EVIDENCE_MISSING_RECORD` |
| `POLICY` | A 快照或紅線違規 | `POLICY_UNKNOWN_ROUTER_STATUS`、`POLICY_ROUTE_NOT_GENERAL_EDUCATION`、`POLICY_RAG_NOT_ALLOWED`、`POLICY_HARD_RISK_PRESENT`、`POLICY_MEDICATION_CHANGE_REQUEST`、`POLICY_EXPLICIT_OUTPUT_REDLINE` |
| `SEMANTIC` | 驗證器判定 claim 不被證據支持 | `CLAIM_NOT_SUPPORTED_BY_EVIDENCE`、`CLAIM_SEMANTIC_VERIFICATION_FAILED`、`ANSWER_HAS_UNSUPPORTED_FACTUAL_CLAIMS`、`SEMANTIC_OVERCONFIDENCE`、`SEMANTIC_PERSONALIZED_DIAGNOSIS` |
| `DEPENDENCY` | 驗證器拋異常或回傳型別錯誤 | `VERIFIER_DEPENDENCY_FAILURE` + 異常類名 |

`DEFAULT_FALLBACK`（`gate.py:26`）：

> `目前無法驗證這份回答是否有足夠依據，因此無法提供可靠回覆；請改由合格醫療專業人員評估。`

---

## 4. Adapter 正規化（為何 D 能吃多種歷史形狀）

`adapters.py:build_gate_request()` 的職責是**不改 A/B/C 實作**，只在邊界做名稱正規化。缺失欄位刻意保留缺失，讓 gate 以確定性 reason fail-closed，而非捏造介面。

### 4.1 A 側（policy）

```python
a_raw = _first(raw, "policy", "a_result", "policy_result") or raw
policy = {
    "router_status": a.get("router_status"),
    "rag_allowed": a.get("rag_allowed"),
    "risk_flags": a.get("risk_flags", []),
    "intent_tags": a.get("intent_tags", []),
    "reason_codes": a.get("reason_codes", []),
}
```

接受 `policy` / `a_result` / `policy_result` 三種 key；若皆無則把整個 `raw` 當 A 快照嘗試解析（讓 schema 驗證去報錯）。

### 4.2 B 側（evidence_set）— 重點

```python
b_raw = _first(raw, "evidence_set", "b_result", "b_context", "context_gate_result") or raw
approved = _first(b, "approved_evidence_ids", "approved_document_ids") or []
raw_records = _first(b, "evidence", "contexts", "retrieved_contexts") or []
# 每筆 record：
evidence_id = _first(item, "evidence_id", "document_id", "chunk_id", "id")
content     = _first(item, "content", "page_content", "text")
```

| 歷史名稱 | 正規化後 | 說明 |
|----------|----------|------|
| `b_decision` | `decision` | B 最終判定，僅 `PASS` 放行 |
| `approved_document_ids` | `approved_evidence_ids` | B 明確批准清單 |
| `contexts` / `retrieved_contexts` | `evidence` | 檢索上下文陣列 |
| `document_id` / `chunk_id` / `id` | `evidence_id` | 正式流程只認 `evidence_id` |
| `page_content` / `text` | `content` | 證據文字 |

> 若 `approved_evidence_ids` 缺失，adapter **不**從 `evidence` 推導，直接給 `[]`，後續 `_validate_evidence_ids` 會以 `EVIDENCE_ID_NOT_APPROVED_BY_B` 擋掉。這是刻意的安全選擇。

### 4.3 C 側（candidate_response）

```python
c_raw = _first(raw, "candidate_response", "c_result", "output")
# 若無三者 → ValueError("missing candidate_response/c_result/output")
```

`parse_candidate_response()` 額外處理 **C v1 兼容**：

- 若含 `supported_claims` 或 `unsupported_requests` → 直接以 v2 驗證。
- 若含 `claims`（v1）→ 逐條轉為 `SupportedClaim`，`unsupported_requests` 補 `[]`。
- 兩者皆無 → `ValueError("C response has neither v2 supported_claims nor v1 claims")`。

### 4.4 workflow 層的 `c_to_d`（`workflow/adapters.py:27`）

正式編排的唯一組裝點，**不做任何轉換**，只做 `model_dump` 打包：

```python
def c_to_d(*, request_id: str, a_result: AResult, b_result: CanonicalBResult,
           c_result: EvidenceAwareV2Answer) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "schema_version": "d.v0.1",
        "a_result": a_result.model_dump(mode="json"),
        "b_result": b_result.model_dump(mode="json"),
        "c_result": c_result.model_dump(mode="json"),
    }
```

此 dict 直接餵給 `run_output_gate()`，由 `build_gate_request` 再正規化。好處是 workflow 不需知道 D 內部欄位名，D 也不需依賴 A/B/C 的 Pydantic 類別。

---

## 5. 政策與紅線（`policy.py`）

### 5.1 `check_policy_snapshot()` — A 快照強制檢查

```python
KNOWN_ROUTER_STATUSES = {
    "E_EMERGENCY", "U_URGENT_HUMAN", "M_MEDICATION_REFERRAL",
    "R_POLICY_BOUNDARY", "Q_CLARIFICATION",
    "G_GENERAL_EDUCATION", "O_OUT_OF_SCOPE", "F_ROUTER_DEPENDENCY",
}
HARD_POLICY_RISKS = {
    "POSSIBLE_EMERGENCY", "MENTAL_HEALTH_CRISIS",
    "PERSONALIZED_MEDICATION", "HIGH_RISK_NOT_EXCLUDED",
    "PROMPT_INJECTION_SUSPECTED",
}
```

| 檢查 | 條件 | reason_code |
|------|------|-------------|
| 未知路由 | `router_status not in KNOWN_ROUTER_STATUSES` | `POLICY_UNKNOWN_ROUTER_STATUS` |
| 非衛教路由 | `router_status != "G_GENERAL_EDUCATION"` | `POLICY_ROUTE_NOT_GENERAL_EDUCATION` |
| RAG 未允許 | `rag_allowed is not True` | `POLICY_RAG_NOT_ALLOWED` |
| 硬風險存在 | `risk_flags ∩ HARD_POLICY_RISKS != ∅` | `POLICY_HARD_RISK_PRESENT` |
| 用藥變更意圖 | `"MEDICATION_CHANGE_REQUEST" in intent_tags` | `POLICY_MEDICATION_CHANGE_REQUEST` |

任一命中即 `POLICY` FALLBACK。注意：D 不重新做 A 的政策判斷，只**複核** A 已給的快照。

### 5.2 `check_candidate_red_lines()` — D 顯式輸出紅線

`PolicyRuleConfig` 預設三條正則（`re.IGNORECASE`），掃描 `answer + 所有 claim` 拼接文字：

```python
prohibited_patterns = (
    r"(?:你|您|病人|患者)?\s*(?:可以|應該|請)\s*(?:自行)?(?:停藥|換藥)",
    r"(?:自行|直接)\s*(?:增加|減少|調整|加倍|減半)\s*(?:用藥|藥物|藥量|劑量)",
    r"(?:把|將).{0,12}(?:劑量|藥量).{0,12}(?:調整|改成|增加|減少)",
)
```

命中任一即 `POLICY_EXPLICIT_OUTPUT_REDLINE`。文件註明：這些是**顯式候選紅線**，非臨床閾值，生產前須覆核。

**範例**：

- `「你可以自行停藥」` → 命中 pattern 1 → FALLBACK
- `「請自行增加藥量」` → 命中 pattern 2 → FALLBACK
- `「把劑量調整成兩倍」` → 命中 pattern 3 → FALLBACK

可注入自訂 `PolicyRuleConfig(prohibited_patterns=(...))` 覆蓋預設。

---

## 6. 安全棄權（Abstention）

```python
# gate.py:162
if not candidate.supported_claims:
    return _result(request, decision="PASS", failure_type="NONE",
                   reason_codes=["D_SAFE_ABSTENTION_ACCEPTED"],
                   final_response=candidate.answer, ...)
```

當 C 誠實回 `INSUFFICIENT`（或 `PARTIAL` 但無 supported_claims 且有 unsupported_requests）且 `supported_claims == []`，D **不呼叫語意驗證器**，直接 PASS。這讓「我不知道 / 文件不足」的誠實回答不會被誤擋，同時 `final_response` 仍為 `candidate.answer`（通常是說明限制與建議就醫的文字）。

---

## 7. 可插拔語意驗證器（`verifier.py`）

### 7.1 介面

```python
class SemanticVerifier(Protocol):
    name: str
    def verify(self, candidate: CandidateResponse,
               evidence_set: EvidenceSet,
               policy: PolicySnapshot) -> SemanticVerificationResult: ...

class SemanticVerificationResult:
    failed_claims: list[ClaimFailure]
    unsupported_answer_claims: list[ClaimFailure]
    reason_codes: list[str]
```

`run_output_gate(..., verifier=...)` 接受任意符合此 Protocol 的物件；未傳時預設 `HeuristicSemanticVerifier()`。若 verifier 拋異常或回傳非 `SemanticVerificationResult`，D 以 `DEPENDENCY / VERIFIER_DEPENDENCY_FAILURE` fail-closed，並在 `reason_codes` 附上異常類名（如 `RuntimeError`）。

### 7.2 HeuristicSemanticVerifier（Demo 性質，非醫療驗證）

> **⚠️ 重要**：此驗證器**不是**醫療正確性驗證，僅為展示 D 介面的詞彙重疊 demo。生產需替換為獨立評估的 claim/NLI 或 LLM verifier，並搭配版本化評估集。

- **名稱**：`heuristic-demo-not-medical-safety`
- **邏輯**：
  1. 對每條 `supported_claim`，取其 `evidence_ids` 對應的 `evidence.content`，計算 claim 與證據的 token 重疊率（中文按字、英數按詞，`[A-Za-z0-9]+|[\u4e00-\u9fff]`）。
  2. 重疊率 ≥ 0.85 或 claim 原文被證據包含 → 視為支持；否則產生 `ClaimFailure(status="UNSUPPORTED")` 並加 `CLAIM_NOT_SUPPORTED_BY_EVIDENCE`。
  3. 額外掃描 `candidate.answer`：
     - 含 `保證|一定|絕對|百分之百|guarantee|always|never` → `SEMANTIC_OVERCONFIDENCE`
     - 含 `你就是糖尿病` / `確診為` 等個人化診斷句式 → `SEMANTIC_PERSONALIZED_DIAGNOSIS`
- **限制**：無臨床有效性聲明、無 NLI、無同義改寫理解，僅 demo。

### 7.3 MappingSemanticVerifier（測試替身）

`verifier.py:MappingSemanticVerifier` 為確定性測試替身，可按 `claim_id` 指定 `SUPPORTED`/`UNSUPPORTED` 與自訂 reason，或以 `fail_reason` 模擬 verifier 依賴失敗（見 `tests/test_d_output_gate.py`）。

### 7.4 正式 Verifier TODO

`verifier.py:11` 註明 `Pluggable semantic verifier boundary for a future independent judge`。正式替換需滿足：獨立評估、版本化、有評估集覆蓋率報告，且 `name` 應反映真實模型/版本。

---

## 8. 最小可跑範例

### 8.1 直接呼叫 `run_output_gate`

```python
from tfda_context_gate.d_output_gate.gate import run_output_gate
from tfda_context_gate.d_output_gate.verifier import HeuristicSemanticVerifier

payload = {
    "request_id": "demo-001",
    "schema_version": "d.v0.1",
    # A 側：任意含 a_result / policy 的形狀皆可，adapter 會正規化
    "a_result": {
        "router_status": "G_GENERAL_EDUCATION",
        "rag_allowed": True,
        "risk_flags": [],
        "intent_tags": [],
    },
    # B 側：展示歷史名稱正規化（b_decision / approved_document_ids / contexts / document_id）
    "b_result": {
        "b_decision": "PASS",
        "approved_document_ids": ["e1"],
        "contexts": [
            {"document_id": "e1", "page_content": "SGLT2 抑制劑可能導致酮酸中毒。", "metadata": {}}
        ],
    },
    # C 側：v2 正式形狀
    "c_result": {
        "decision": "ANSWER",
        "answer": "SGLT2 抑制劑可能導致酮酸中毒。",
        "supported_claims": [
            {"claim_id": "c1", "claim": "SGLT2 抑制劑可能導致酮酸中毒。", "evidence_ids": ["e1"]}
        ],
        "unsupported_requests": [],
        "limitations": [],
    },
}

result = run_output_gate(payload, verifier=HeuristicSemanticVerifier())
print(result.decision)      # PASS
print(result.failure_type)  # NONE
print(result.reason_codes)  # ['OUTPUT_GATE_PASSED']
print(result.final_response)
```

### 8.2 經 `workflow/adapters.py:c_to_d` 組裝（正式編排路徑）

```python
from tfda_context_gate.workflow.adapters import c_to_d
from tfda_context_gate.d_output_gate.gate import run_output_gate

# a_result: AResult, b_result: CanonicalBResult, c_result: EvidenceAwareV2Answer 皆為 Pydantic 模型
payload = c_to_d(request_id="req-123", a_result=a_result, b_result=b_result, c_result=c_result)
result = run_output_gate(payload)  # 預設 HeuristicSemanticVerifier
if result.decision == "FALLBACK":
    print(result.failure_type, result.reason_codes, result.invalid_evidence_ids)
```

### 8.3 自訂紅線與 fallback

```python
from tfda_context_gate.d_output_gate.policy import PolicyRuleConfig

custom_rules = PolicyRuleConfig(prohibited_patterns=(r"自行.*用藥",))
result = run_output_gate(
    payload,
    policy_rules=custom_rules,
    fallback_response="自訂安全回覆：請諮詢醫療專業人員。",
)
```

### 8.4 注入正式 Verifier（示意）

```python
class MyNliVerifier:
    name = "nli-v1.0-eval-2026-08"
    def verify(self, candidate, evidence_set, policy):
        # 呼叫獨立評估過的 NLI/LLM judge，回 SemanticVerificationResult
        ...

result = run_output_gate(payload, verifier=MyNliVerifier())
```

---

## 9. 常見 FALLBACK 案例對照

| 案例 | payload 特徵 | failure_type | reason_codes | invalid_evidence_ids |
|------|--------------|--------------|--------------|----------------------|
| 引用不存在的證據 | `evidence_ids=["missing"]` | `EVIDENCE` | `EVIDENCE_ID_NOT_FOUND` | `["missing"]` |
| 引用未批准證據 | `e2` 存在但 `approved=["e1"]` | `EVIDENCE` | `EVIDENCE_ID_NOT_APPROVED_BY_B` | `["e2"]` |
| B 批准清單含幽靈 ID | `approved=["ghost"]` 無對應 record | `EVIDENCE` | `B_APPROVED_EVIDENCE_MISSING_RECORD` | `["ghost"]` |
| claim 無 evidence_ids | `evidence_ids=[]` | `SCHEMA` | `CLAIM_WITHOUT_EVIDENCE_ID` | — |
| ANSWER 無 claim | `decision="ANSWER", supported_claims=[]` | `SCHEMA` | `C_ANSWER_HAS_NO_SUPPORTED_CLAIMS` | — |
| A 非衛教路由 | `router_status="M_MEDICATION_REFERRAL"` | `POLICY` | `POLICY_ROUTE_NOT_GENERAL_EDUCATION` | — |
| A 高風險 | `risk_flags=["PERSONALIZED_MEDICATION"]` | `POLICY` | `POLICY_HARD_RISK_PRESENT` | — |
| 紅線 | `answer="你可以自行停藥"` | `POLICY` | `POLICY_EXPLICIT_OUTPUT_REDLINE` | — |
| 過度自信 | `answer="一定不會有風險"` | `SEMANTIC` | `SEMANTIC_OVERCONFIDENCE` | — |
| 個人化診斷 | `answer="你就是糖尿病"` | `SEMANTIC` | `SEMANTIC_PERSONALIZED_DIAGNOSIS` | — |
| 驗證器掛掉 | `verifier` 拋 `RuntimeError` | `DEPENDENCY` | `VERIFIER_DEPENDENCY_FAILURE`, `RuntimeError` | — |
| 安全棄權 | `decision="INSUFFICIENT", supported_claims=[]` | `NONE` (PASS) | `D_SAFE_ABSTENTION_ACCEPTED` | — |

---

## 10. 與其他模組的邊界

- **A → D**：A 是政策權威，D 只讀快照，不覆寫 `router_status`/`rag_allowed`。`workflow/graph.py` 中 D 節點前已由 A 決定是否進 RAG，D 再做最終複核。
- **B → D**：B 的 `decision` 與 `approved_evidence_ids` 是 D 的唯一證據真相來源。`evidence` 僅作內容比對，不代表批准。
- **C → D**：C 的 `supported_claims[].evidence_ids` 必須是 `approved_evidence_ids` 的子集；D 不修 C 的文字，只否決。
- **D → E**：`workflow/graph.py` 在 `trace.span("D", "output_gate")` 內呼叫 `run_output_gate`，將 `decision`/`reason_codes`/`failure_type` 寫入 `TraceEvent`，E 僅觀測不改決策。
- **D → Workflow**：`run_workflow()` 的最終 `final_response` 取 `result.final_response`（PASS 用 `candidate.answer`，FALLBACK 用 `DEFAULT_FALLBACK` 或自訂 `fallback_response`）。

---

## 11. 檔案索引

| 檔案 | 職責 |
|------|------|
| `d_output_gate/schemas.py` | `PolicySnapshot` / `EvidenceSet` / `CandidateResponse` / `OutputGateRequest` / `OutputGateResult` / `ClaimFailure` |
| `d_output_gate/adapters.py` | `build_gate_request` / `parse_policy` / `parse_evidence_set` / `parse_candidate_response` / `validation_error_text` |
| `d_output_gate/policy.py` | `KNOWN_ROUTER_STATUSES` / `HARD_POLICY_RISKS` / `PolicyRuleConfig` / `check_policy_snapshot` / `check_candidate_red_lines` |
| `d_output_gate/verifier.py` | `SemanticVerifier` (Protocol) / `SemanticVerificationResult` / `HeuristicSemanticVerifier` / `MappingSemanticVerifier` |
| `d_output_gate/gate.py` | `run_output_gate` / `_validate_candidate_shape` / `_validate_evidence_ids` / `_semantic_failure` / `DEFAULT_FALLBACK` |
| `d_output_gate/__init__.py` | 公開 `run_output_gate` / `OutputGateRequest` / `OutputGateResult` |
| `workflow/adapters.py:c_to_d` | 正式編排的 A/B/C → D payload 組裝 |

---

## 12. 已知限制

- `HeuristicSemanticVerifier` 為 demo heuristic，無臨床有效性，生產必須替換為獨立評估的 verifier。
- `PolicyRuleConfig` 的三條紅線為顯式候選規則，非臨床閾值，需覆核後用於生產。
- D 不做事實查核（fact-checking）與劑量計算，僅做結構/政策/證據引用/語意重疊檢查。
- `request_id` / `schema_version` 在 payload 嚴重畸形時可能回退為 `"unknown"` / `"d.v0.1"`，以確保 fail-closed 仍有可追蹤結果。

