# RAG 與 B Context Gate 深潛

> **範圍**：`rag/` + `b_context_gate/` + `query_expansion/` 銜接 + 根目錄 `01_*.py` ~ `05_*.py` 研究期腳本關係。**最後核對**：2026-08-21（以 `rag/schemas.py`、`rag/retriever.py`、`rag/tfda_retriever.py`、`rag/tfda_smoke_cases.py`、`b_context_gate/schemas.py`、`b_context_gate/gate.py`、`b_context_gate/adapters.py`、`query_expansion/` 為準）。

---

## 1. 定位：RAG 與 B 在 A→E 管線中的位置

```
A (route_request) → Query Expansion (Identity) → RAG (Fixture / TFDA Real) → B (DeterministicContextGate) → C → D
                                              ↑                              ↑
                                     QueryExpansionResult              CanonicalBInput
                                     RAGResult                         CanonicalBResult
```

- **RAG 的職責**：把 `QueryExpansionResult.retrieval_queries[]` 轉成 `RAGResult.evidence: CanonicalEvidence[]`。不做政策判斷、不批證據，只負責「檢索並保留溯源」。
- **B 的職責**：判斷檢索結果是否**足夠且安全**可用。只有 `decision == "PASS"` 才放行到 C；`INSUFFICIENT` 才可能進 Agent 有界復原；`UNSAFE` / `REVIEW` / `FALLBACK` 直接結束。
- **Query Expansion 的銜接**：v0.1 為確定性 `IdentityQueryExpander`，原句不改、只發一條檢索句，確保 RAG 輸入可追溯。詳見 §2。

> 紅線：**檢索到 ≠ 已批准**。C 只能引用 `b_result.approved_evidence_ids` 的子集，D 會校驗 `invalid_evidence_ids`。

---

## 2. Query Expansion 與 RAG 的銜接

### 2.1 QueryExpansionResult（`query_expansion/schemas.py`）

```python
class QueryExpansionResult(StrictModel):
    request_id: str
    schema_version: str = "query_expansion.v0.1"
    original_query: str          # 使用者原句，v0.1 不改寫
    retrieval_queries: list[str]  # 實際送檢索的句子，v0.1 恰一條 == original_query
    strategy: str = "identity"   # 實際值為 "identity-deterministic"
```

- `QueryExpansionInput` 由 `query_expansion/adapters.py:from_a_result()` 從 `AResult` 轉來，欄位：`request_id` / `original_query` / `router_status` / `intent_tags` / `declared_role` / `language`。
- `IdentityQueryExpander.expand()`（`query_expansion/expander.py`）是唯一 v0.1 實作：

```python
class IdentityQueryExpander:
    name = "identity-deterministic"
    def expand(self, request: QueryExpansionInput) -> QueryExpansionResult:
        return QueryExpansionResult(
            request_id=request.request_id,
            original_query=request.original_query,
            retrieval_queries=[request.original_query],
            strategy=self.name,
        )
```

- **為何重要**：RAG 的 `Retriever` Protocol 只認 `QueryExpansionResult`，不直接吃 `AResult` 或原始字串，確保「A 政策 → 擴寫 → 檢索」邊界清晰。

### 2.2 RAG 如何消費 QueryExpansionResult

`rag/retriever.py:Retriever` Protocol：

```python
class Retriever(Protocol):
    def retrieve(self, request: QueryExpansionResult) -> RAGResult: ...
```

- `FixtureRetriever` 與 `TFDADrugSafetyRetriever` 皆實作此 Protocol，可互相替換（injectable）。
- `rag/demo.py` 與 `workflow/runner.py` 皆先走 `IdentityQueryExpander` 再呼叫 `retriever.retrieve()`。

---

## 3. 核心 Schema 深潛

### 3.1 CanonicalEvidence（`b_context_gate/schemas.py`）

正式流程唯一的證據單位，`rag` 與 `b_context_gate` 共用：

| 欄位 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `evidence_id` | `str` (min 1) | ✅ | 唯一真相 ID。正式流程只認此欄，legacy `document_id` / `chunk_id` / `id` 僅在 adapter 轉換 |
| `content` | `str` (min 1) | ✅ | 證據正文。對應 legacy `page_content` / `text` |
| `source` | `str \| None` | — | 來源標示。優先取 `source` / `source_dataset`，否則 `None`；adapter 不捏造 |
| `metadata` | `dict[str, Any]` | — | 透傳欄位。TFDA 真實語料含 `document_id` / `row_index` / `發布日期` / `藥品成分` / `source_dataset` 等；未知欄位亦收斂至此 |
| `score` | `float \| None` | — | 檢索分數。`similarity_score` / `reranker_score` 正規化至此；Fixture 無分數時為 `None` |
| `date` | `str \| None` | — | 發布日期。優先 `date`，否則 `metadata["發布日期"]` |
| `version` | `str \| None` | — | 版本號，未提供時 `None` |

> 設計原則：`source` / `score` / `date` / `version` 未提供時保持 `None`，adapter 絕不發明溯源。

### 3.2 RAGResult（`rag/schemas.py`）

```python
class RAGResult(StrictModel):
    request_id: str
    schema_version: str = "rag.v0.1"
    original_query: str
    retrieval_queries: list[str]   # min 1，來自 QueryExpansionResult
    evidence: list[CanonicalEvidence] = []
    retrieval_latency_ms: float | None = None  # >=0，Fixture/Real 皆記錄
```

- `rag_to_b_input()`（同檔）是 RAG→B 的唯一轉接：

```python
def rag_to_b_input(rag_result: RAGResult) -> CanonicalBInput:
    return CanonicalBInput(
        request_id=rag_result.request_id,
        original_query=rag_result.original_query,
        retrieval_queries=rag_result.retrieval_queries,
        evidence=rag_result.evidence,
    )
```

### 3.3 CanonicalBInput（`b_context_gate/schemas.py`）

```python
class CanonicalBInput(StrictModel):
    request_id: str
    schema_version: str = "b.v0.1"
    original_query: str
    retrieval_queries: list[str]   # min 1
    evidence: list[CanonicalEvidence] = []
```

- 由 `rag_to_b_input()` 產生，或由 `adapt_legacy_b_result()` 的 `evidence` 正規化後間接構成。

### 3.4 CanonicalBResult（`b_context_gate/schemas.py`）

```python
BDecision = Literal["PASS", "INSUFFICIENT", "UNSAFE", "REVIEW", "FALLBACK"]

class CanonicalBResult(StrictModel):
    request_id: str
    schema_version: str = "b.v0.1"
    decision: BDecision
    approved_evidence_ids: list[str] = []
    evidence: list[CanonicalEvidence] = []
    reason_codes: list[str] = []
    identified_missing_information: list[str] = []  # max 8，中性觀測，不建議 Agent 動作
    retrieval_feedback: dict[str, Any] = {}
    relevance: str | None = None
    sufficiency: str | None = None
    conflict: str | None = None
    safety: str | None = None
```

| `decision` | 含義 | 下游行為 |
|------------|------|----------|
| `PASS` | 上下文足夠且安全，批准 `approved_evidence_ids` | 進 C 生成 |
| `INSUFFICIENT` | 無證據或無批准證據 | 無 Planner → `FALLBACK`；有 Planner → 進 Agent（`ASK_USER` / `REWRITE_QUERY` / `FALLBACK`） |
| `UNSAFE` | 檢出重複 `evidence_id` 等不可復原問題 | 直接 `FALLBACK`，不進 Agent |
| `REVIEW` | 需人工覆核（保留給真實 judge） | 直接 `FALLBACK`，不進 Agent |
| `FALLBACK` | 其他不可復原 | 直接 `FALLBACK` |

> `DeterministicContextGate` 實際只回 `PASS` / `INSUFFICIENT` / `UNSAFE`；`REVIEW` / `FALLBACK` 為 `BDecision` 型別保留，供真實 LLM judge（`04_llm_judge.py` / `05_hybrid.py`）使用。

### 3.5 QueryExpansionResult 重申

見 §2.1。補充：`strategy` 在 Fixture 路徑固定為 `"identity-deterministic"`，`retrieval_queries` 長度恆為 1。

---

## 4. FixtureRetriever：確定性測試替身

**檔案**：`rag/retriever.py`

```python
def default_fixture_evidence() -> list[CanonicalEvidence]:
    return [
        CanonicalEvidence(evidence_id="E1", content="一般糖尿病飲食原則包括均衡飲食與控制總熱量。", source="fixture", metadata={"fixture_case": "normal", "fixture_b_approved": True}),
        CanonicalEvidence(evidence_id="E2", content="飲食安排應依個人狀況與醫療專業人員建議調整。", source="fixture", metadata={"fixture_case": "normal", "fixture_b_approved": True}),
        CanonicalEvidence(evidence_id="E3", content="本筆是檢查 evidence boundary 的未核准候選資料。", source="fixture", metadata={"fixture_case": "normal", "fixture_b_approved": False}),
    ]

class FixtureRetriever:
    name = "fixture-retriever"
    def __init__(self, evidence: list[CanonicalEvidence] | None = None) -> None:
        self.evidence = list(evidence) if evidence is not None else default_fixture_evidence()
    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        started = time.perf_counter()
        return RAGResult(
            request_id=request.request_id,
            original_query=request.original_query,
            retrieval_queries=request.retrieval_queries,
            evidence=list(self.evidence),
            retrieval_latency_ms=(time.perf_counter() - started) * 1000,
        )
```

- **特性**：不做向量檢索、不改 `original_query`、不聲稱來自 phase 腳本；每次回傳固定 3 筆（E1/E2 批准、E3 未批准），用於測試 B 的 `approved_evidence_ids` 邊界。
- **可注入**：建構子接受 `evidence` 覆寫，`workflow/runner.py` 與測試皆可替換。
- **輔助**：`adapt_legacy_retrieval()` 可把 phase 2/3/5 的舊 `records` 正規化為 `RAGResult`，但不改動那些實驗腳本本身。

---

## 5. TFDADrugSafetyRetriever：真實向量檢索

**檔案**：`rag/tfda_retriever.py`

### 5.1 語料與路徑解析

- **語料**：`data/processed/langchain_documents.json`，129 筆 TFDA 風險溝通紀錄，每筆為一 `Document`（不做 chunking，不合成證據）。
- **路徑解析** `resolve_documents_path()` 優先序：

```
1. 顯式 path 參數
2. 環境變數 TFDA_DOCUMENTS_PATH
3. /mnt/data/langchain_documents.json（使用者掛載）
4. tfda_context_gate/data/processed/langchain_documents.json（預設）
```

找不到即 `FileNotFoundError`，列出所有搜尋路徑。

- **載入驗證** `load_tfda_rows()`：每列必須有 `id` 或 `metadata.document_id`、非空 `page_content`、合法 `metadata` dict；重複 `evidence_id` 即 `TFDADatasetError`。

### 5.2 向量索引

| 項目 | 值 |
|------|-----|
| Embedding 模型 | `intfloat/multilingual-e5-small`（`DEFAULT_EMBEDDING_MODEL`，可用 `EMBED_MODEL` 環境變數覆寫） |
| 向量庫 | `langchain_core.vectorstores.InMemoryVectorStore` |
| 依賴 | `langchain-huggingface` + `sentence-transformers`（缺失即 `RuntimeError` 提示安裝 `requirements.txt`） |
| 索引時機 | **lazy**：首次 `retrieve()` 才建索引（`_ensure_store()`），之後複用 |
| 文件建構 | 每列 `row["page_content"]` 為 `Document.page_content`，`metadata` 透傳並補 `document_id` |
| Embedding 參數 | `encode_kwargs={"normalize_embeddings": True, "prompt": "passage: "}` / `query_encode_kwargs={"normalize_embeddings": True, "prompt": "query: "}` |

### 5.3 檢索邏輯

```python
class TFDADrugSafetyRetriever:
    name = "tfda-huggingface-inmemory-vector-retriever"
    def __init__(self, *, documents_path=None, top_k=5, embedding_model=None): ...
    def retrieve(self, request: QueryExpansionResult) -> RAGResult:
        store = self._ensure_store()
        ranked: dict[str, tuple[Document, float]] = {}
        for query in request.retrieval_queries:          # 支援多條檢索句（v0.1 僅一條）
            for doc, score in store.similarity_search_with_score(query, k=self.top_k):
                # 同一 evidence_id 取最高分去重
                ...
        results = sorted(ranked.values(), key=lambda x: x[1], reverse=True)[:self.top_k]
        # 轉 CanonicalEvidence，保留 score / date / version / source
```

- **去重**：多條 `retrieval_queries` 命中同一 `evidence_id` 時保留最高分。
- **排序**：按 `score` 降冪取 `top_k`（預設 5）。
- **輸出**：每筆 `CanonicalEvidence` 含 `score`（`float`）、`date`（`發布日期`）、`version`、`source`（`source_dataset`）、完整 `metadata`。
- **可注入**：`top_k` / `documents_path` / `embedding_model` 皆可建構時指定，測試可用 `FixtureRetriever` 替換而無需建索引。

---

## 6. DeterministicContextGate：離線 B 閘門

**檔案**：`b_context_gate/gate.py`

### 6.1 定位：Mock / Demo，非臨床 adjudicator

```python
class DeterministicContextGate:
    """Small offline B boundary for the deterministic baseline.
    This is a workflow adapter/demo gate, not a replacement for the existing
    B LLM context judge.
    """
    name = "deterministic-context-gate-fixture"
    def __init__(self, *, approval_mode: Literal["fixture", "all_retrieved"] = "fixture") -> None: ...
    def evaluate(self, request: CanonicalBInput) -> CanonicalBResult: ...
```

- **Protocol**：`ContextGate` 僅要求 `evaluate(CanonicalBInput) -> CanonicalBResult`。
- **兩種模式**：

| `approval_mode` | `name` | 批准邏輯 | 用途 |
|-----------------|--------|----------|------|
| `"fixture"`（預設） | `deterministic-context-gate-fixture` | 僅 `metadata["fixture_b_approved"] is True` 者批准 | 契約測試：E1/E2 過、E3 不過，驗證 `approved_evidence_ids` 邊界 |
| `"all_retrieved"` | `deterministic-context-gate-demo-all-retrieved` | 全部 `evidence` 皆批准 | **Real corpus demo 專用**：讓 129 筆真實檢索結果能走通 C/D 展示管線 |

> ⚠️ **`all_retrieved` 不是臨床核准**。它僅為 demo 標示（`reason_codes` 含 `DEMO_RETRIEVED_EVIDENCE_APPROVED`、`safety="DEMO_RETRIEVED_APPROVED"`），表示「已檢索的證據在 demo 中被標為可用」，不等於醫療審核通過。真實 B 判斷由 `04_llm_judge.py` / `05_hybrid.py` 的 LLM judge 承擔（實驗性質，亦非已核可元件）。

### 6.2 決策分支

```python
def evaluate(self, request: CanonicalBInput) -> CanonicalBResult:
    if not request.evidence:
        return CanonicalBResult(decision="INSUFFICIENT", reason_codes=["CONTEXT_INSUFFICIENT", "NO_RETRIEVED_EVIDENCE"], relevance="NONE", sufficiency="INSUFFICIENT", safety="NOT_ASSESSED", ...)

    # 重複 evidence_id → UNSAFE（fail-closed）
    if duplicate_ids:
        return CanonicalBResult(decision="UNSAFE", reason_codes=["DUPLICATE_EVIDENCE_ID"], relevance="UNKNOWN", sufficiency="UNSAFE", safety="FAIL", ...)

    approved = [e.evidence_id for e in request.evidence if approval_mode == "all_retrieved" or e.metadata.get("fixture_b_approved") is True]
    if not approved:
        return CanonicalBResult(decision="INSUFFICIENT", reason_codes=["CONTEXT_INSUFFICIENT", "NO_APPROVED_EVIDENCE"], ...)

    return CanonicalBResult(decision="PASS", approved_evidence_ids=approved, reason_codes=["B_CONTEXT_CONTRACT_VALID", "EVIDENCE_APPROVED" / "DEMO_RETRIEVED_EVIDENCE_APPROVED"], relevance="RETRIEVED", sufficiency="SUFFICIENT", safety="FIXTURE_APPROVED" / "DEMO_RETRIEVED_APPROVED", ...)
```

- **fail-closed**：無 `fixture_b_approved` 標記的紀錄預設不批准，避免「檢索到即自動可用」。
- **重複 ID 零容忍**：任何重複 `evidence_id` 直接 `UNSAFE`，不進 Agent。

---

## 7. Adapters：Legacy 正規化層

**檔案**：`b_context_gate/adapters.py` + `rag/retriever.py:adapt_legacy_retrieval`

正式流程只認 `evidence_id` / `evidence` / `decision`，但 phase 腳本歷史欄位為 `document_id` / `chunk_id` / `contexts` / `b_decision` 等。Adapter 負責收斂，不改動腳本本身。

### 7.1 normalize_evidence

```python
def normalize_evidence(value: Any) -> CanonicalEvidence:
    raw = _as_dict(value)  # 支援 BaseModel 或 Mapping
    evidence_id = _first(raw, "evidence_id", "document_id", "chunk_id", "id")
    content     = _first(raw, "content", "page_content", "text")
    # evidence_id / content 缺失即 ValueError
    # metadata 收斂未知欄位；source/date/version/score 多鍵回退
```

| 目標欄位 | 嘗試鍵（依序） |
|----------|---------------|
| `evidence_id` | `evidence_id` → `document_id` → `chunk_id` → `id` |
| `content` | `content` → `page_content` → `text` |
| `source` | `source` → `source_dataset`（含 `metadata` 回退） |
| `date` | `date` → `metadata["date"]` → `metadata["發布日期"]` |
| `score` | `score` → `similarity_score` → `reranker_score` |
| `metadata` | 原 `metadata` dict + 未知頂層鍵全收斂 |

### 7.2 normalize_evidence_list / adapt_legacy_retrieval

```python
def normalize_evidence_list(values: list[Any] | None) -> list[CanonicalEvidence]: ...

def adapt_legacy_retrieval(records, *, request_id, original_query, retrieval_queries, retrieval_latency_ms=None) -> RAGResult:
    return RAGResult(..., evidence=normalize_evidence_list(records), ...)
```

- 用於把 phase 2/3/5 的 `records`（含 `similarity_score` / `reranker_score`）轉為 `RAGResult`，不改實驗腳本。

### 7.3 adapt_legacy_b_result

```python
def adapt_legacy_b_result(value: Any, *, request_id, original_query, retrieval_queries=None) -> CanonicalBResult:
    raw = _as_dict(value)
    decision = _first(raw, "decision", "b_decision")
    approved = _first(raw, "approved_evidence_ids", "approved_document_ids") or []
    raw_evidence = _first(raw, "evidence", "contexts", "retrieved_contexts", "context_rows") or []
    evidence = normalize_evidence_list(raw_evidence)
    return CanonicalBResult(decision=str(decision), approved_evidence_ids=[str(x) for x in approved], evidence=evidence, ...)
```

| 目標欄位 | 嘗試鍵 |
|----------|--------|
| `decision` | `decision` → `b_decision` |
| `approved_evidence_ids` | `approved_evidence_ids` → `approved_document_ids` |
| `evidence` | `evidence` → `contexts` → `retrieved_contexts` → `context_rows` |

---

## 8. 最小可跑範例

### 8.1 FixtureRetriever + DeterministicContextGate（完全離線）

```python
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult
from tfda_context_gate.rag.retriever import FixtureRetriever
from tfda_context_gate.rag.schemas import rag_to_b_input
from tfda_context_gate.b_context_gate.gate import DeterministicContextGate

# 1. 模擬 Query Expansion 輸出（v0.1 為 Identity）
qe_result = QueryExpansionResult(
    request_id="demo-001",
    original_query="糖尿病飲食要注意什麼？",
    retrieval_queries=["糖尿病飲食要注意什麼？"],
    strategy="identity-deterministic",
)

# 2. RAG 檢索（Fixture：固定回 E1/E2/E3）
retriever = FixtureRetriever()
rag_result = retriever.retrieve(qe_result)
print(rag_result.evidence[0].evidence_id)  # E1
print(rag_result.retrieval_latency_ms)     # 毫秒級

# 3. RAG → B 轉接
b_input = rag_to_b_input(rag_result)

# 4. B 閘門（fixture 模式：僅 E1/E2 批准）
gate = DeterministicContextGate(approval_mode="fixture")
b_result = gate.evaluate(b_input)
print(b_result.decision)               # PASS
print(b_result.approved_evidence_ids)  # ["E1", "E2"]
print(b_result.reason_codes)           # ["B_CONTEXT_CONTRACT_VALID", "EVIDENCE_APPROVED"]

# 5. Real corpus demo 模式（全部批准，僅供展示）
demo_gate = DeterministicContextGate(approval_mode="all_retrieved")
demo_result = demo_gate.evaluate(b_input)
print(demo_result.decision)     # PASS
print(demo_result.safety)       # DEMO_RETRIEVED_APPROVED（非臨床核准）
```

### 8.2 TFDADrugSafetyRetriever（真實語料，需安裝依賴）

```python
from tfda_context_gate.query_expansion.schemas import QueryExpansionResult
from tfda_context_gate.rag.tfda_retriever import TFDADrugSafetyRetriever
from tfda_context_gate.b_context_gate.gate import DeterministicContextGate
from tfda_context_gate.rag.schemas import rag_to_b_input

retriever = TFDADrugSafetyRetriever(top_k=5)  # lazy 建索引，首次較慢
qe_result = QueryExpansionResult(
    request_id="tfda-p1",
    original_query="我有在打胰島素，一直打在同一個位置會有什麼問題嗎？",
    retrieval_queries=["我有在打胰島素，一直打在同一個位置會有什麼問題嗎？"],
    strategy="identity-deterministic",
)
rag_result = retriever.retrieve(qe_result)
for ev in rag_result.evidence:
    print(ev.evidence_id, ev.metadata.get("藥品成分"), ev.score, ev.date)

# 真實語料 demo 需用 all_retrieved 才能走通 C/D
b_result = DeterministicContextGate(approval_mode="all_retrieved").evaluate(rag_to_b_input(rag_result))
```

---

## 9. Smoke Cases：P1/P2/H1/H2/H3/C1/C2 含義

**檔案**：`rag/tfda_smoke_cases.py` — 9 個角色化 smoke case，8 個預期檢索命中、1 個 A 攔截、1 個澄清候選。

| Case | 角色 | 查詢（節錄） | 預期檢索 | 關鍵詞（`expected_terms`） | 邊界標記 |
|------|------|-------------|----------|---------------------------|----------|
| **P1** | PATIENT | 胰島素一直打同一位置會怎樣？ | ✅ | `Insulin`、`注射部位`、`cutaneous amyloidosis`、`輪替`、`血糖控制` | — |
| **P2** | PATIENT | 吃 SGLT2 抑制劑，腳有傷口/疼痛要注意什麼？ | ✅ | `SGLT2`、`傷口`、`膚色變化`、`足部疼痛`、`潰瘍`、`截肢`、`預防性足部護理` | — |
| **P3** | PATIENT | 血糖穩了可自己停藥嗎？ | ❌ | — | `A_BLOCK`（預期被 A 攔截，不進 RAG） |
| **H1** | HEALTHCARE_PROFESSIONAL | SGLT2 患者足部安全警訊？ | ✅ | `SGLT2`、`醫療人員`、`足部`、`預防性足部護理` | — |
| **H2** | HEALTHCARE_PROFESSIONAL | 胰島素注射部位皮膚澱粉樣變性如何影響血糖？ | ✅ | `Insulin`、`cutaneous amyloidosis`、`注射部位`、`血糖控制` | — |
| **H3** | HEALTHCARE_PROFESSIONAL | TFDA 對胰島素注射部位輪替的安全提醒？ | ✅ | `Insulin`、`注射部位`、`輪替`、`TFDA` | — |
| **C1** | CAREGIVER | 家人用 SGLT2，要注意哪些足部變化？ | ✅ | `SGLT2`、`傷口`、`膚色變化`、`足部疼痛`、`足部護理` | — |
| **C2** | CAREGIVER | 家人總打同一位置胰島素要提醒什麼？ | ✅ | `Insulin`、`注射部位`、`輪替`、`皮膚澱粉樣變性` | — |
| **C3** | CAREGIVER | 家人吃糖尿病藥後腳怪怪的要注意什麼？ | ❌ | — | `clarification_candidate=True`（語意模糊，預期進 Agent `ASK_USER`） |

- **判定**：`TFDASmokeCase.matches(evidence)` 檢查 `expected_terms` 是否全出現在 `content` / `source` / `date` / `metadata` 的串接文本中（case-insensitive）。
- **執行**：`rag/demo.py:run_case()` 先走 `a_router.route_request()`，僅當 `a_result.rag_allowed` 且非 `clarification_candidate` 才呼叫 `TFDADrugSafetyRetriever.retrieve()`。
- **角色差異**：`declared_role` 不授權，僅影響呈現；三角色對同一主題（胰島素輪替 / SGLT2 足部）用不同口吻提問，驗證檢索對角色表述的魯棒性。

---

## 10. Phase 腳本關係：00–05 為何留在根目錄

> `CURRENT_ARCHITECTURE.md` 已說明：`00_*.py` ~ `05_*.py` 使用 `from run_config import ...` 等 script-local import，搬移會破壞可重現性，故刻意保留在 `tfda_context_gate/` 根目錄。正式 workflow（`workflow/runner.py`）不依賴它們；`rag/` 與 `b_context_gate/` 僅透過 adapter 正規化其輸出。

### 10.1 各腳本前 80 行摘要（不重寫邏輯）

#### 01_build_documents.py — 建庫

- 讀 `data/raw/drug_risk_communication.json`，按 `CONTENT_FIELDS`（藥品成分/適應症/藥理作用機轉/訊息緣由/安全資訊分析/TFDA說明）組 `page_content`。
- 每列產一 `Document(id="tfda-risk-XXXX", page_content, metadata={document_id, row_index, source_dataset, 發布日期, 藥品成分})`。
- 輸出 `data/processed/langchain_documents.json`（129 筆，含 `raw_record` 溯源）。

#### 02_similarity_retrieval.py — 相似度檢索基線

- 載入 `langchain_documents.json`，經 `contract_gate`（檢查 `document_id` / `row_index` / `藥品成分` / `發布日期` / `page_content` 非空、去重）後建 `InMemoryVectorStore`（`multilingual-e5-small`，`passage:` / `query:` prompt）。
- 對 `NARROW_QUERY`（酮酸中毒）與 `BROAD_QUERY`（SGLT2 安全警訊）各取 `top_k=10`，輸出 `narrow_query_top10.json` / `broad_query_top10.json` 與 `phase2_similarity_output.txt`。

#### 03_reranker.py — Cross-Encoder 重排

- 同 Phase 2 的 `contract_gate` 與 embedding，候選 `CANDIDATE_K=20`，用 `BAAI/bge-reranker-v2-m3`（`CrossEncoderReranker`）重排取 `top_n=10`。
- 同時呼叫 `cross_encoder.score()` 取得真實 `reranker_score`，保留 `original_similarity_rank/score` 對照。
- 輸出 `narrow/broad_query_reranked_top10.json` 與候選集 JSON。

#### 04_llm_judge.py — LLM 上下文裁判

- 讀 Phase 3 的 `narrow_query_reranked_top10.json`，對 10 篇文件逐篇做 `DocumentAssessment`（`relevance: DIRECT/PARTIAL/IRRELEVANT` / `sufficiency` / `topic_match: EXACT/SAME_DRUG_DIFFERENT_RISK/OTHER`），再對 3 個集合（`top4` / `without_correct_context` / `with_correct_context`）做 `ContextSetAssessment`（`decision: PASS/REVIEW/FALLBACK`）。
- 經 `ChatOpenRouter`（預設 `nvidia/nemotron-3-super-120b-a12b:free`，`temperature=0`，`REPEAT_COUNT=3`）結構化輸出，經 `RollingRequestRateLimiter` 限流。
- 輸出 `document_judge_results.json` / `human_vs_judge.csv` / `judge_confusion_matrix.csv` / `set_*.json` / `phase4_latency.json`。

#### 05_hybrid.py — 混合管線（檢索→重排→裁判）端到端

- 串 `contract_gate → similarity_retrieval (k=20) → cross_encoder_reranker → set_level_llm_judge`，對 `NARROW` / `BROAD` 各測 `top_n=3/4/5` 共 6 變體 + 1 個 `fallback_ablation`（移除酮酸中毒正解，僅留同藥不同風險）。
- 每變體 1 次 LLM 呼叫（`HybridContextDecision: PASS/REVIEW/FALLBACK`），`REPEAT_COUNT=3`，含 `warm_up` 與 `phase4_latency.json` 對照的 `cost_latency` 統計。
- 輸出 `hybrid_narrow/broad_top{3,4,5}.json` / `hybrid_fallback_ablation.json` / `phase5_cost_latency.json` / `phase5_trace.json`。

### 10.2 正式模組如何銜接 Phase 產物

```
Phase 腳本（實驗）                正式模組（契約）
─────────────────                ──────────────
02/03 的 similarity/reranker  →  rag/tfda_retriever.py（精簡版 real retriever，不含 reranker）
04/05 的 LLM judge            →  b_context_gate/gate.py（DeterministicContextGate，確定性替身）
兩者的 JSON 輸出             →  b_context_gate/adapters.py + rag/retriever.py:adapt_legacy_retrieval
                                正規化為 CanonicalEvidence / CanonicalBResult，不改腳本
```

- **不搬移、不重寫**：adapter 僅做欄位映射，phase 腳本保持可獨立重跑。
- **分數不捏造**：`score` / `similarity_score` / `reranker_score` 僅在來源提供時透傳，否則 `None`。

---

## 11. 硬性邊界與常見誤解

| 誤解 | 正確 |
|------|------|
| `all_retrieved` 代表臨床核准 | ❌ 僅為 demo 標示（`DEMO_RETRIEVED_APPROVED`），不等於醫療審核。真實 B 判斷需獨立評估的 judge |
| 檢索分數可自行補 | ❌ 未提供即 `None`，adapter 不發明分數 |
| `document_id` 可直接當 `evidence_id` 用 | ❌ 正式流程只認 `CanonicalEvidence.evidence_id`，`document_id` 僅在 adapter 轉換 |
| Phase 腳本可搬進 `rag/` | ❌ 會破壞 `run_config` 可重現性，刻意保留在根目錄 |
| B 的 `PASS` 代表答案正確 | ❌ 僅代表「上下文足夠且安全可用」，答案正確性由 C/D 另行把關 |
| `declared_role` 會提升權限 | ❌ 僅影響呈現，不授權資料/工具/模型 |

---

## 12. 測試與 Demo 指令

```bash
# 單測（TFDA 檢索含 7 個 embedding smoke，缺 HF 環境時 skip）
python3 -m pytest -q tfda_context_gate/tests/test_tfda_retriever.py
python3 -m pytest -q tfda_context_gate/tests/test_workflow_integration.py

# RAG smoke（角色案例）
python3 -m tfda_context_gate.rag.demo --all --top-k 5
python3 -m tfda_context_gate.rag.demo --case P1 --top-k 5
python3 -m tfda_context_gate.rag.demo --query "胰島素注射部位輪替" --role PATIENT --top-k 5

# Workflow 端到端（fixture vs real）
python3 -m tfda_context_gate.workflow.demo --retriever fixture --log-path /tmp/tfda-offline.jsonl
python3 -m tfda_context_gate.workflow.demo --retriever real --case P1 --log-path /tmp/tfda-real-workflow.jsonl

# Phase 腳本（需 pip install -r tfda_context_gate/requirements.txt，04/05 需 OPENROUTER_API_KEY）
python3 tfda_context_gate/01_build_documents.py
python3 tfda_context_gate/02_similarity_retrieval.py
python3 tfda_context_gate/03_reranker.py
python3 tfda_context_gate/04_llm_judge.py --smoke-test
python3 tfda_context_gate/05_hybrid.py
```

---

## 13. 延伸閱讀

- `00_overview.md` — 全景圖與 A/B/C/D/E 流程
- `CURRENT_ARCHITECTURE.md` — Source of Truth（模組/契約/邊界/Mock 清單）
- `ARCHITECTURE_AUDIT.md` — 審計證據
- `REAL_TFDA_DATASET_AUDIT.md` — 129 筆語料稽核
- `REAL_TFDA_SMOKE_REPORT.md` — 真實向量檢索 smoke 報告
- `rag/schemas.py` / `rag/retriever.py` / `rag/tfda_retriever.py` / `b_context_gate/schemas.py` / `b_context_gate/gate.py` / `b_context_gate/adapters.py` — 本文件對應原始碼
