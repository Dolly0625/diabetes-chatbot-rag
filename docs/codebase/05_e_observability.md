# E Observability 深潛 — `e_observability/` 橫切觀測層

> **定位**：E 是橫切（cross-cutting）且**純觀測**的模組。負責結構化記錄 A / Query Expansion / RAG / B / Agent / C / D 的執行軌跡、評估標註與指標快照，**不修改 prompt、政策、模型、部署或醫療答案**。任何 sink 失敗皆 fail-open，不影響主流程回應。
>
> - **根目錄**：`tfda_context_gate/e_observability/`
> - **Schema 版本**：`E_SCHEMA_VERSION = "e.v0.1"`（`schemas.py:9`）
> - **最後核對**：2026-08-21（以 `e_observability/*.py`、`workflow/graph.py`、`workflow/runner.py` 為準）
> - **延伸閱讀**：[`00_overview.md`](./00_overview.md) · [`CURRENT_ARCHITECTURE.md`](../../archive/docs/CURRENT_ARCHITECTURE.md) · [`workflow/graph.py`](../../tfda_context_gate/workflow/graph.py) · [`workflow/runner.py`](../../tfda_context_gate/workflow/runner.py)

---

## 1. 設計原則

| 原則 | 說明 | 程式碼位置 |
|------|------|-----------|
| **純觀測** | E 不做醫療決策、不改 A/B/C/D 政策、不提供 Agent 執行權限。`__init__.py:1-5` 明確宣告 | `e_observability/__init__.py` |
| **Fail-open** | Sink 序列化或檔案寫入失敗僅寫入 `sink_errors`，不覆蓋業務結果、不拋新閘門決策 | `tracer.py:102-105,287-291` |
| **Per-request 隔離** | 每個 `TraceRecorder` 綁定單一 `request_id`（`trace_id = request_id`），擁有獨立 `_events` / `_evaluations` / `MetricsCollector` / `sink` | `tracer.py:107-133` |
| **StrictModel** | 所有 schema `extra="forbid"`，未知欄位直接報錯，避免隱式漂移 | `schemas.py:26-27` |
| **隱私優先** | `original_query` 入庫前先 `redact_text`，另存 `query_hash`（SHA-256）供關聯；所有 payload 經 `sanitize_value` 遞迴脫敏 | `tracer.py:117-126`、`privacy.py` |

---

## 2. 檔案地圖

| 檔案 | 職責 | 關鍵匯出 |
|------|------|---------|
| `schemas.py` | 定義 `RequestMetadata` / `TraceEvent` / `EvaluationRecord` / `MetricsSnapshot` / `LatencySummary` 與 `TraceStatus` | `E_SCHEMA_VERSION`, `TraceStatus`, `utc_now()` |
| `tracer.py` | `TraceRecorder` 與 `TraceSpan`（context manager） | `TraceRecorder`, `TraceSpan`, `_elapsed_ms()` |
| `sinks.py` | 僅兩種 sink：`InMemoryTraceSink` 與 `JsonlTraceSink` | `TraceSink` (Protocol), `TraceRecord` (Union) |
| `privacy.py` | 脫敏與雜湊 | `redact_text`, `hash_text`, `sanitize_value` |
| `metrics.py` | 單 recorder 內的計數與延遲彙總 | `MetricsCollector` |
| `trajectory.py` | 純展示層：將結構化 trace 渲染為人類可讀軌跡 | `format_trace_trajectory` |
| `demo.py` | 離線 E v0.1 demo（寫 JSONL 並印 `snapshot()`） | `main()` |
| `__init__.py` | 公開 API 匯總 | 見下表 |

**公開 API**（`__init__.py:20-33`）：

```python
from tfda_context_gate.e_observability import (
    E_SCHEMA_VERSION,
    EvaluationRecord,
    InMemoryTraceSink,
    JsonlTraceSink,
    MetricsCollector,
    MetricsSnapshot,
    RequestMetadata,
    TraceEvent,
    TraceRecorder,
    format_trace_trajectory,
    hash_text,
    redact_text,
    sanitize_value,
)
```

---

## 3. Schemas 深潛

### 3.1 `TraceStatus`

```python
TraceStatus = Literal[
    "STARTED",              # span 進入時自動寫入
    "COMPLETED",            # 正常完成
    "BLOCKED",              # A 政策阻擋（rag_allowed=False 且非依賴失敗）
    "INSUFFICIENT",         # B 證據不足
    "FALLBACK",             # 需 fallback（B 非 PASS、C/D 失敗、Agent 有界 fallback）
    "ERROR",                # 例外（span 內拋錯或 workflow 依賴失敗）
    "SKIPPED",              # 保留值，現行 graph 未使用
    "NEEDS_CLARIFICATION",  # ASK_USER 需追問
]
```

`STARTED` 僅作 span 生命週期標記，`trajectory.py` 渲染時會跳過（`status == "STARTED"` 不顯示）。

### 3.2 `RequestMetadata`

每個 `TraceRecorder` 初始化時建立，作為全 trace 的請求級中繼資料：

| 欄位 | 型別 | 說明 |
|------|------|------|
| `request_id` | `str` (min_length=1) | 請求唯一 ID，`trace_id` 預設等於它 |
| `trace_id` | `str \| None` | 追蹤 ID，`tracer.py:120` 設為 `request_id` |
| `thread_id` | `str \| None` | 選填，跨輪對話線程 ID |
| `schema_version` | `str` | 預設 `e.v0.1` |
| `timestamp` | `datetime` | `utc_now()`（UTC） |
| `declared_role` | `str \| None` | 宣告角色（PATIENT 等，僅觀測） |
| `original_query` | `str \| None` | **已脫敏**的原始提問（`redact_text` 後） |
| `query_hash` | `str \| None` | `hash_text(original_query)` 的 SHA-256 hex，供無原始文本關聯 |

> `original_query` 註解（`schemas.py:39-43`）：原始明文**不需要**持久化；`query_hash` 可在不存明文的生產 sink 中做關聯。

### 3.3 `TraceEvent` — 核心事件

`record_type` 固定為 `"trace_event"`。欄位分組如下（全部為 `StrictModel`，未列即 `None` / 空集合）：

**請求與追蹤身份**（繼承 `RequestMetadata` 欄位）：

`request_id`, `trace_id`, `thread_id`, `schema_version`, `timestamp`, `declared_role`, `original_query`, `query_hash`

**執行定位與時序**：

| 欄位 | 型別 | 說明 |
|------|------|------|
| `component` | `str` | 元件名：`A` / `QUERY_EXPANSION` / `RAG` / `B` / `AGENT` / `ASK_USER` / `QUERY_REWRITER` / `C` / `D` / `FALLBACK` / `SYSTEM` |
| `node_name` | `str` | 節點名：`input_router` / `query_expansion` / `retrieval` / `context_gate` / `planner` / `question_builder` / `query_rewriter` / `generator` / `output_gate` / `termination` / `request` / `workflow` |
| `status` | `TraceStatus` | 見 3.1 |
| `started_at` | `datetime \| None` | span 開始時間 |
| `completed_at` | `datetime \| None` | 完成時間；`STARTED` 事件為 `None` |
| `latency_ms` | `float \| None` | `max(0, (completed - started) * 1000)`；`STARTED` 為 `None` |

**A — Input Router + Policy Gate**：

`router_status`, `intent_tags: list[str]`, `risk_flags: list[str]`, `reason_codes: list[str]`, `rag_allowed: bool | None`, `prompt_guard_result: Any | None`

**RAG**：

`retrieval_query`, `retrieved_count`, `retrieved_evidence_ids: list[str]`, `retrieval_latency_ms`, `retrieval_attempt`, `retrieved_evidence: list[dict]`（每筆含 `evidence_id` / `rank` / `score` / `source` / `date`，見 `graph.py:_retrieved_evidence_trace`）

**B — Context Gate**：

`decision`, `outcome`, `approved_evidence_ids: list[str]`, `approved_evidence_count`, `b_attempt`, `relevance`, `sufficiency`, `conflict`, `safety`

**C — Generator**：

`candidate_decision`, `claim_count`, `evidence_ids: list[str]`

**D — Output Gate**：

`failure_type`, `failed_claims: list[Any]`, `invalid_evidence_ids: list[str]`, `fallback_reason`

**Query 改寫 / 追問展示欄位**（僅結構化觀測，不記錄模型隱藏推理）：

`current_query`, `rewritten_query`, `rewrite_attempt`, `missing_information: list[str]`, `identified_missing_information: list[str]`, `planner_context: dict | None`, `question`

**系統 / 依賴中繼資料**：

`model_name`, `token_usage: dict[str,int] | None`, `error_type`, `error_message`（已 `redact_text`）

**Agent v0.1 欄位**（全部 `Optional`，向後相容；無 Agent 時保持 `None` / 空集合）：

`agent_action`, `requested_action`, `requested_reason_code`, `reason_code`, `actions_taken: list[str]`, `agent_step`, `step_count`, `retry_count`, `rewrite_count`, `clarification_count`, `tool_name`, `termination_reason`

> 設計意圖（`schemas.py:46-51`）：可選欄位刻意覆蓋 A/RAG/B/C/D 並**預留 Agent 欄位**，使同一 E 契約同時涵蓋基線與 Agent 路徑，無需為 Agent 另建 schema。

### 3.4 `EvaluationRecord`

`record_type = "evaluation"`，供離線/人工標註：

| 欄位 | 型別 | 說明 |
|------|------|------|
| `request_id` / `thread_id` / `schema_version` / `timestamp` / `original_query` / `query_hash` | 同 TraceEvent | 請求身份（已脫敏） |
| `expected_decision` | `str \| None` | 期望決策（標註用） |
| `actual_decision` | `str \| None` | 實際決策 |
| `outcome` | `str \| None` | 結果標籤 |
| `failure_type` | `str \| None` | 失敗類型 |
| `reason_codes` | `list[str]` | 原因碼 |
| `metadata` | `dict[str, Any]` | 額外中繼資料（經 `sanitize_value`） |

### 3.5 `MetricsSnapshot` 與 `LatencySummary`

```python
class LatencySummary(StrictModel):
    count: int        # 樣本數
    total_ms: float   # 總延遲
    average_ms: float | None  # 平均延遲（round 到 3 位小數）

class MetricsSnapshot(StrictModel):
    record_type: Literal["metrics"] = "metrics"
    request_count: int
    event_count: int
    error_count: int      # status == ERROR
    fallback_count: int   # status in (FALLBACK, INSUFFICIENT)
    blocked_count: int    # status == BLOCKED
    by_component: dict[str, int]
    latency_by_component: dict[str, LatencySummary]
```

由 `MetricsCollector` 在每次 `observe(event)` 時累加，`snapshot()` 回傳排序後的快照（`metrics.py:37-56`）。

---

## 4. `TraceRecorder` 與 `TraceSpan` API

### 4.1 建構子與生命週期

```python
TraceRecorder(
    request_id: str,
    *,
    thread_id: str | None = None,
    declared_role: str | None = None,
    original_query: str | None = None,
    schema_version: str = E_SCHEMA_VERSION,
    sink: TraceSink | None = None,  # 預設 InMemoryTraceSink()
)
```

- `original_query` 立即經 `redact_text` 脫敏存入 `self.request.original_query`，同時計算 `query_hash = hash_text(original_query)`（`tracer.py:117-126`）。
- 初始化即寫入一筆 `SYSTEM / request / STARTED` 事件（`started_at = request.timestamp`），並呼叫 `MetricsCollector.start_request()`。
- 支援 `with TraceRecorder(...) as trace:` 用法；`__exit__` 時自動 `close()`，若有例外則以 `status="ERROR"` 關閉並記錄 `error_type` / `error_message`（已脫敏）。

**Per-request 隔離**：每個 recorder 擁有獨立 `self._events` / `self._evaluations` / `self._metrics` / `self.sink_errors` / `self._closed`，不同請求互不干擾。

### 4.2 `span()` — Context Manager

```python
def span(self, component: str, node_name: str, **fields: Any) -> TraceSpan
```

`TraceSpan` 行為（`tracer.py:24-96`）：

| 階段 | 動作 |
|------|------|
| `__enter__` | 記錄一筆 `STARTED` 事件（`started_at = utc_now()`，`latency_ms=None`），`_started_recorded = True` |
| `set(**fields)` | 合併欄位；若含 `status` 則覆蓋 `self.status`（預設 `COMPLETED`），回傳 `self` 供鏈式呼叫 |
| `finish(*, status=None, **fields)` | 若尚未 `STARTED` 則先補 `__enter__`；計算 `completed_at = utc_now()` 與 `latency_ms`，呼叫 `recorder.record()`；重複 `finish` 拋 `RuntimeError` |
| `__exit__` | 若有例外 → `finish(status="ERROR", error_type=..., error_message=redact_text(str(exc)))`；否則 `finish()`；**不吞例外**（`return False`） |

典型用法（見 `workflow/graph.py` 各節點）：

```python
with trace.span("A", "input_router") as span:
    result = route_request(request)
    span.set(
        status="COMPLETED" if result.rag_allowed else "BLOCKED",
        router_status=result.router_status.value,
        reason_codes=[c.value for c in result.reason_codes],
        rag_allowed=result.rag_allowed,
    )
# 離開 with 時自動 finish；若 route_request 拋錯則自動記 ERROR
```

### 4.3 `record()`

```python
def record(
    self,
    component: str,
    node_name: str,
    status: TraceStatus,
    *,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    latency_ms: float | None = None,
    **fields: Any,
) -> TraceEvent
```

- 若未提供 `started_at` 則取 `utc_now()`；若 `status != STARTED/SKIPPED` 且無 `completed_at` 則補 `utc_now()`；若無 `latency_ms` 且有 `completed` 則自動計算 `_elapsed_ms`。
- `fields` 經 `sanitize_value` 遞迴脫敏後展開為 `TraceEvent` 欄位。
- 自動填入 `request_id` / `trace_id` / `thread_id` / `schema_version` / `timestamp` / `declared_role` / `original_query` / `query_hash`（來自 `self.request`）。
- 寫入 `self._events`、呼叫 `self._metrics.observe(event)`、再 `self._emit(event)`。

### 4.4 `record_failure()`

```python
def record_failure(
    self,
    component: str,
    node_name: str,
    *,
    failure_type: str,
    reason_codes: list[str] | tuple[str, ...] = (),
    fallback_reason: str | None = None,
    status: TraceStatus = "FALLBACK",
    failed_claims: list[Any] | None = None,
    invalid_evidence_ids: list[str] | None = None,
    **fields: Any,
) -> TraceEvent
```

為 B `INSUFFICIENT`、D `FALLBACK`、依賴失敗等設計的快捷方法，預設 `status="FALLBACK"`，內部轉呼 `record()`。`workflow/runner.py` 的錯誤邊界即用它記錄 `SYSTEM/workflow/ERROR`。

### 4.5 `record_evaluation()`

```python
def record_evaluation(
    self,
    *,
    expected_decision: str | None = None,
    actual_decision: str | None = None,
    outcome: str | None = None,
    failure_type: str | None = None,
    reason_codes: list[str] | tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> EvaluationRecord
```

寫入 `EvaluationRecord`（`record_type="evaluation"`），`metadata` 經 `sanitize_value`。`workflow/runner.py:_finish` 每次皆呼叫一次，`e_observability/demo.py` 亦示範寫入 `UNLABELED_DEMO`。

### 4.6 `close()` / `snapshot()` / `metrics()`

| 方法 | 說明 |
|------|------|
| `close(*, status="COMPLETED", **fields)` | 寫入 `SYSTEM/request` 關閉事件（`started_at=request.timestamp`, `latency_ms` 為全請求耗時）；重複呼叫無操作（`_closed` guard） |
| `snapshot() -> dict` | 回傳 `{"request": ..., "events": ..., "evaluations": ..., "metrics": ..., "sink_errors": ...}`（皆 `model_dump(mode="json")`） |
| `metrics() -> MetricsSnapshot` | 委派 `MetricsCollector.snapshot()` |
| `events` / `evaluations` (property) | 回傳拷貝的 `list[TraceEvent]` / `list[EvaluationRecord]` |

---

## 5. Sinks — 僅兩種

> **MUST NOT**：E 僅提供 `InMemoryTraceSink` 與 `JsonlTraceSink`，不存在其他 sink 類型（如 SQLite / OpenTelemetry / HTTP）。未來替換需自行實作 `TraceSink` Protocol。

### 5.1 `TraceSink` Protocol

```python
class TraceSink(Protocol):
    def emit(self, record: TraceRecord) -> None: ...

TraceRecord = Union[TraceEvent, EvaluationRecord, MetricsSnapshot]
```

### 5.2 `InMemoryTraceSink`

```python
class InMemoryTraceSink:
    records: list[dict]  # 每筆為 record.model_dump(mode="json")
    def emit(self, record: TraceRecord) -> None:
        self.records.append(record.model_dump(mode="json"))
```

用途：測試與「呼叫方自行匯出」情境。`TraceRecorder` 預設即用它（`tracer.py:127`）。

### 5.3 `JsonlTraceSink`

```python
class JsonlTraceSink:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, record: TraceRecord) -> None:
        line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
```

- **Append-only JSONL**，每行一筆 `sort_keys=True` 的 JSON，無外部依賴。
- **Thread-safe**：以 `threading.Lock` 保護寫入。
- **自動建目錄**：`parent.mkdir(parents=True, exist_ok=True)`。

### 5.4 Fail-open

```python
def _emit(self, record: TraceRecord) -> None:
    try:
        self.sink.emit(record)
    except Exception as exc:
        self.sink_errors.append(f"{type(exc).__name__}: {redact_text(str(exc))}")
```

任何 sink 例外皆被捕捉，錯誤字串（已脫敏）寫入 `sink_errors: list[str]`，**不拋出、不覆蓋業務結果**。`snapshot()` 會一併回傳 `sink_errors`。

---

## 6. 隱私 — `privacy.py`

### 6.1 `redact_text(value: str) -> str`

依序套用三條正則，將常見憑證替換為 `[REDACTED]`，同時保留有用上下文：

| 模式 | 正則 | 範例 |
|------|------|------|
| `_ASSIGNED_SECRET` | `\b(api_key\|password\|secret\|authorization\|access_token\|token)\b\s*[:=]\s*\|\s+[^\s,;]+` | `api_key=sk-abc123` → `api_key=[REDACTED]` |
| `_BEARER` | `\bBearer\s+[^\s,;]+` | `Bearer eyJ...` → `Bearer [REDACTED]` |
| `_COMMON_API_KEY` | `\b(sk\|rk\|pk)-[A-Za-z0-9_-]{8,}\b` | `sk-12345678abcd` → `[REDACTED]` |

大小寫不敏感（`re.IGNORECASE`）。

### 6.2 `hash_text(value: str | None) -> str | None`

```python
hashlib.sha256(str(value).encode("utf-8")).hexdigest()
```

`None` 回 `None`。用於 `query_hash`，**不可逆**，可在不存明文的生產 sink 中做關聯。

### 6.3 `sanitize_value(value: Any, *, key: str | None = None) -> Any`

遞迴脫敏所有寫入 sink 前的 payload：

- 若 `key` 命中 `_SENSITIVE_KEY`（`api_key|password|secret|authorization|access_token|token`）→ 直接回 `[REDACTED]`，不檢查 value。
- `str` → `redact_text(value)`
- `Mapping` → 對每個 `value` 遞迴，`key` 傳入子呼叫
- `list | tuple` → 對每個元素遞迴
- 其他型別 → 原樣回傳

呼叫點：`tracer.py:171`（`record` 的 `fields`）、`tracer.py:240`（`record_evaluation` 的 `metadata`）、`tracer.py:117`（`original_query`）與 `tracer.py:92,274,291`（`error_message` / sink 例外）。

---

## 7. 指標 — `MetricsCollector`

`metrics.py` 為**單 recorder 內**的依賴-free 計數器：

| 計數器 | 遞增條件 |
|--------|---------|
| `request_count` | `start_request()`（建構子呼叫一次） |
| `event_count` | 每個 `observe(event)` |
| `error_count` | `event.status == "ERROR"` |
| `fallback_count` | `event.status in ("FALLBACK", "INSUFFICIENT")` |
| `blocked_count` | `event.status == "BLOCKED"` |
| `by_component[component]` | 每個事件 |
| `_latency_count` / `_latency_total` | `event.latency_ms is not None` |

`snapshot()` 將 `_latency_*` 聚合為 `LatencySummary(count, total_ms=round(total,3), average_ms=round(total/count,3))`，並對 `by_component` 與 `latency_by_component` 做 `sorted` 排序，確保輸出確定性。

---

## 8. 軌跡渲染 — `format_trace_trajectory` 與 `--show-trace`

### 8.1 `format_trace_trajectory(trace: Mapping[str, Any]) -> str`

**純展示層**（`trajectory.py:1-5`）：不做路由決策、不改 graph 狀態、不暴露模型隱藏推理。

輸入為 `TraceRecorder.snapshot()` 回傳的 `dict`（含 `request` / `events`），輸出為緊湊文字軌跡：

```
============================================================
TRACE: <trace_id> (request_id=<request_id>)
============================================================

[1] A
    status: COMPLETED
    latency: 1.23 ms
    route: G_GENERAL_EDUCATION
    prompt_guard: ALLOWED

[2] RAG #1
    status: COMPLETED
    query: 糖尿病飲食原則
    top1: tfda-risk-0042 score=0.900000 source=TFDA-demo-fixture date=2018/9/28
    ...

[3] B #1
    status: COMPLETED
    decision: PASS
    approved_evidence_count: 1
    relevance: DIRECT
    reason_codes: B_CONTEXT_CONTRACT_VALID

[4] AGENT #1
    status: COMPLETED
    action: REWRITE_QUERY
    reason_code: QUERY_FORMULATION_NEEDS_REWRITE
    ...

============================================================
```

關鍵邏輯：

- 跳過 `status == "STARTED"` 的事件（`trajectory.py:66-67`）。
- `_label()` 依 `component` 與嘗試次數產生標籤：`RAG #<retrieval_attempt>` / `B #<b_attempt>` / `AGENT #<agent_step>` / `AGENT / ASK_USER`（當 `node_name == question_builder`）/ `QUERY_REWRITE` / `SYSTEM`。
- `_append_evidence()` 僅取 `retrieved_evidence[:5]`，每筆顯示 `top<rank>: <evidence_id> score=<score> source=<source> date=<date>`，**不含原始文件內容**。
- 各 `component` 分支僅顯示對應欄位（`A` 顯示 `route`/`prompt_guard`；`B` 顯示 `decision`/`approved_evidence_count`/`relevance`/`sufficiency`/`conflict`/`safety`/`reason_codes`；`AGENT` 顯示 `planner_context.identified_missing_information`/`requested_action`/`agent_action`/`reason_code`/`agent_step`/`rewrite_count`/`clarification_count`/`termination_reason`；`ASK_USER` 顯示 `missing_information`/`question`；`QUERY_REWRITER` 顯示 `old`/`new`/`attempt`；`C`/`D`/`FALLBACK`/`SYSTEM` 各有對應欄位）。

### 8.2 `--show-trace` Flag

`agent/demo.py` 提供：

```bash
python3 -m tfda_context_gate.agent.demo --planner fixture --retriever fixture --show-trace
python3 -m tfda_context_gate.agent.demo --planner fixture --retriever fixture --show-trace --trace-output /tmp/agent-trace.jsonl
```

- `_print_trajectory()`（`agent/demo.py:187-222`）先印精簡 `component: status {details}`，再呼叫 `format_trace_trajectory(result.trace)`；若 `show_trace=True` 則印完整軌跡。
- 若指定 `--trace-output`，則將 `{case_label, status, final_response, trajectory, trace}` 以 JSONL 追加寫入檔案（`ensure_ascii=False, sort_keys=True`）。

同理，`e_observability/demo.py` 與 `workflow/demo.py` 亦可透過 `--log-path` 產生 JSONL，再用 `format_trace_trajectory` 離線渲染。

---

## 9. Workflow 整合 — E 如何包裹 A / QE / RAG / B / C / D / Agent

### 9.1 `workflow/graph.py` 的 Span 包裹

`build_workflow_graph(trace, ...)` 內每個節點皆以 `with trace.span(component, node_name) as span:` 包裹，並在節點邏輯後 `span.set(...)` 寫入結構化欄位。`span` 離開時自動 `finish()`，例外時自動記 `ERROR`。

| 節點函式 | `span` 參數 | `span.set` 關鍵欄位 | 狀態映射 |
|----------|------------|---------------------|---------|
| `a_node` | `("A", "input_router")` | `router_status`, `intent_tags`, `risk_flags`, `reason_codes`, `rag_allowed`, `prompt_guard_result` | `COMPLETED` (rag_allowed) / `FALLBACK` (F_ROUTER_DEPENDENCY) / `BLOCKED` (其他) |
| `query_expansion_node` | `("QUERY_EXPANSION", "query_expansion")` | `retrieval_query`, `reason_codes` (ORIGINAL_QUERY_PRESERVED / AGENT_REWRITTEN_QUERY) | `COMPLETED`（預設） |
| `rag_node` | `("RAG", "retrieval")` | `retrieval_query`, `retrieved_count`, `retrieved_evidence_ids`, `retrieved_evidence` (compact provenance), `retrieval_latency_ms`, `retrieval_attempt` | `COMPLETED` |
| `b_node` | `("B", "context_gate")` | `decision`, `approved_evidence_ids`, `approved_evidence_count`, `b_attempt`, `reason_codes`, `identified_missing_information`, `relevance/sufficiency/conflict/safety`, `step_count/retry_count/rewrite_count/clarification_count/actions_taken` | `COMPLETED` (PASS) / `INSUFFICIENT` (INSUFFICIENT) / `FALLBACK` (其他) |
| `planner_node` | `("AGENT", "planner")` | `agent_action`, `requested_action`, `reason_code/requested_reason_code`, `reason_codes`, `agent_step/step_count`, `retry_count/rewrite_count/clarification_count`, `actions_taken`, `identified_missing_information`, `planner_context`, `termination_reason`, `model_name`, `error_type/error_message` (失敗時) | `COMPLETED` / `FALLBACK` (觸發上限) / `ERROR` (Planner 例外) |
| `ask_user_node` | `("ASK_USER", "question_builder")` | `agent_action=ASK_USER`, `reason_code`, `missing_information`, `question`, `agent_step/step_count`, `clarification_count+1`, `termination_reason=NEEDS_CLARIFICATION` | `NEEDS_CLARIFICATION` |
| `rewrite_node` | `("QUERY_REWRITER", "query_rewriter")` | `retrieval_query`, `current_query`, `rewritten_query`, `rewrite_attempt`, `reason_codes`, `step_count/retry_count/actions_taken`, `termination_reason=REENTER_RAG_B`, `model_name` | `COMPLETED` |
| `c_node` | `("C", "generator")` | `candidate_decision`, `claim_count`, `evidence_ids` | `COMPLETED`（預設） |
| `d_node` | `("D", "output_gate")` | `decision`, `failure_type`, `reason_codes`, `failed_claims`, `invalid_evidence_ids`, `fallback_reason` | `COMPLETED` (PASS) / `FALLBACK` (其他) |

> `retrieved_evidence` 僅含 `evidence_id` / `rank` / `score` / `source` / `date`（`graph.py:137-149`），**不含原始文件內容**，避免軌跡洩漏全文。

### 9.2 `workflow/runner.py` 的 `_finish` 與 SYSTEM 關閉事件

`run_workflow()` 流程：

1. 解析 `RequestContext`；若 schema 無效 → `trace.record_failure("SYSTEM","workflow", status="ERROR", failure_type="SCHEMA", reason_codes=["REQUEST_SCHEMA_INVALID"])` → `_finish(..., status="FALLBACK")`。
2. 建立 `TraceRecorder(request_id, declared_role, original_query, sink=trace_sink)`（`runner.py:158-163`）。
3. `build_workflow_graph(trace, ...)` 並 `graph.invoke(state)`；期間所有節點 span 已寫入。
4. 依 `state["status"]` 呼叫 `_finish`；若 `graph.invoke` 拋錯則依 `runtime_stage["current"]` 映射 `stage_reason` / `stage_reason_code` 並 `record_failure("SYSTEM","workflow", status="ERROR")` → `_finish(..., status="FALLBACK")`。

`_finish(trace, request_id, state, status, final_response, fallback_reason)`（`runner.py:30-92`）：

```python
if status == "FALLBACK":
    trace.record("FALLBACK", "termination", "FALLBACK",
        agent_action=decision.action if decision else None,
        reason_codes=[fallback_reason] if fallback_reason else [],
        fallback_reason=fallback_reason,
        termination_reason=state.get("termination_reason"))

trace.record_evaluation(
    actual_decision="PASS" if status == "COMPLETED" else status,
    outcome=status,
    failure_type=None if status == "COMPLETED" else status,
    reason_codes=[fallback_reason] if fallback_reason else [],
    metadata={"source": "workflow.run_workflow", "orchestration": "langgraph"})

system_status = "BLOCKED" if status == "BLOCKED" \
    else "NEEDS_CLARIFICATION" if status == "NEEDS_CLARIFICATION" \
    else "COMPLETED"
trace.close(status=system_status,
    decision="PASS" if status == "COMPLETED" else status,
    outcome=status,
    fallback_reason=fallback_reason)
```

最終回傳 `WorkflowResult(..., trace=trace.snapshot())`，`trace` 內含完整 `request` / `events` / `evaluations` / `metrics` / `sink_errors`。

### 9.3 狀態映射總表

| Workflow `status` | `FALLBACK/termination` 事件 | `EvaluationRecord.outcome` | `SYSTEM/request` 關閉 `status` | 說明 |
|-------------------|----------------------------|---------------------------|-------------------------------|------|
| `COMPLETED` | （不寫） | `COMPLETED` | `COMPLETED` | D PASS，全流程成功 |
| `BLOCKED` | （不寫） | `BLOCKED` | `BLOCKED` | A 政策阻擋 |
| `NEEDS_CLARIFICATION` | （不寫） | `NEEDS_CLARIFICATION` | `NEEDS_CLARIFICATION` | ASK_USER 需追問 |
| `FALLBACK` | `FALLBACK/termination / FALLBACK` | `FALLBACK` | `COMPLETED` | B 不足/D 失敗/Agent 有界 fallback/依賴失敗 |
| `ERROR`（僅 SYSTEM/workflow 節點） | — | — | `ERROR`（`__exit__` 或 catch 區塊） | 未捕捉例外 |

> `SYSTEM/request` 的 `COMPLETED` 關閉事件**不代表業務成功**，僅表示「請求處理已結束」；需看 `outcome` / `fallback_reason` / `WorkflowResult.status` 判斷業務結果。`FALLBACK` 的 SYSTEM 關閉仍為 `COMPLETED`，是刻意設計：系統本身正常結束，只是業務走 fallback。

---

## 10. 最小可用範例（已對 `tracer.py` 核對）

與 `CURRENT_ARCHITECTURE.md` 範例一致，並補上 `record_failure` / `BLOCKED` 用法：

```python
from tfda_context_gate.e_observability import JsonlTraceSink, TraceRecorder

# 每個請求一個 recorder，sink 可選 InMemory（預設）或 Jsonl
with TraceRecorder(
    "request-001",
    declared_role="PATIENT",
    original_query="一般衛教問題",
    sink=JsonlTraceSink("/tmp/tfda-trace.jsonl"),
) as trace:
    # A 節點：用 span 包裹，set 寫入政策欄位
    with trace.span("A", "input_router") as span:
        result = route_request(request)  # 你的 A 邏輯
        span.set(
            status="COMPLETED" if result.rag_allowed else "BLOCKED",
            router_status=result.router_status.value,
            intent_tags=[t.value for t in result.intent_tags],
            reason_codes=[c.value for c in result.reason_codes],
            rag_allowed=result.rag_allowed,
            prompt_guard_result="ALLOWED" if result.rag_allowed else "BLOCKED",
        )

    # B 不足 / D fallback：用 record_failure
    # trace.record_failure("B", "context_gate", failure_type="INSUFFICIENT",
    #     reason_codes=["CONTEXT_INSUFFICIENT"], fallback_reason="B_INSUFFICIENT")

    # A 阻擋：可用 span.set(status="BLOCKED") 或直接 record
    # trace.record("A", "input_router", "BLOCKED", reason_codes=["POLICY_BLOCKED"])

    # 離線標註
    trace.record_evaluation(actual_decision="ANSWER", outcome="UNLABELED_DEMO")

# 離開 with 時自動 trace.close(status="COMPLETED") 並寫入 SYSTEM/request 關閉事件
# 取得快照
snapshot = trace.snapshot()
# snapshot = {"request": {...}, "events": [...], "evaluations": [...], "metrics": {...}, "sink_errors": [...]}

# 人類可讀軌跡
from tfda_context_gate.e_observability import format_trace_trajectory
print(format_trace_trajectory(snapshot))
```

驗證要點（對 `tracer.py`）：

- `TraceRecorder` 建構子簽名為 `(request_id, *, thread_id, declared_role, original_query, schema_version, sink)`，`request_id` 為位置參數，其餘為 keyword-only。
- `span()` 回傳 `TraceSpan`，支援 `with` 與 `span.set()` / `span.finish()`。
- `record()` 的 `status` 為必填位置參數，`started_at` / `completed_at` / `latency_ms` 為 keyword-only。
- `record_failure()` 預設 `status="FALLBACK"`，`record_evaluation()` 寫 `EvaluationRecord`。
- `TraceRecorder` 本身亦為 context manager，`__exit__` 自動 `close()`。

---

## 11. Agent 欄位 — 向後相容的 Optional

`TraceEvent` 的 Agent 相關欄位**全部 Optional**，使同一 `e.v0.1` 契約同時覆蓋「無 Agent 基線」與「有界 Agent」兩條路徑，無需另建 schema：

| 欄位 | 型別 | 寫入時機 |
|------|------|---------|
| `agent_action` | `str \| None` | Planner 選定動作：`ASK_USER` / `REWRITE_QUERY` / `FALLBACK` |
| `requested_action` | `str \| None` | Planner 原始請求動作（與 `agent_action` 差異在於是否被有界邏輯覆蓋為 FALLBACK） |
| `requested_reason_code` | `str \| None` | 原始 reason_code |
| `reason_code` | `str \| None` | 最終 reason_code（單值，與 `reason_codes[0]` 對應） |
| `actions_taken` | `list[str]` | 已執行動作序列 |
| `agent_step` | `int \| None` | 當前 Agent 步數（`graph.py:planner_node` 的 `steps`） |
| `step_count` | `int \| None` | 同 `agent_step`，冗餘欄位供不同消費者 |
| `retry_count` | `int \| None` | 重試次數（現行等於 `rewrite_count`） |
| `rewrite_count` | `int \| None` | 已改寫次數 |
| `clarification_count` | `int \| None` | 已追問次數 |
| `tool_name` | `str \| None` | 保留欄位，現行未使用 |
| `termination_reason` | `str \| None` | 終止原因：`MAX_AGENT_STEPS_EXCEEDED` / `MAX_REWRITES_EXCEEDED` / `MAX_CLARIFICATIONS_EXCEEDED` / `AGENT_SELECTED_FALLBACK` / `PLANNER_FAILURE` / `NEEDS_CLARIFICATION` / `REENTER_RAG_B` 等 |

無 Agent 時這些欄位保持 `None` / `[]`，不影響既有消費者。`trajectory.py` 對 `AGENT` / `ASK_USER` / `QUERY_REWRITER` 分支會按需顯示其中子集。

---

## 12. Demo 與 CLI

| 指令 | 說明 |
|------|------|
| `python3 -m tfda_context_gate.e_observability.demo --log-path /tmp/tfda-e.jsonl --query "請說明糖尿病的一般飲食原則。"` | E 離線 demo：依序寫 A/RAG/B/C/D span 與一筆 `EvaluationRecord`，最後印 `snapshot()` JSON |
| `python3 -m tfda_context_gate.agent.demo --planner fixture --retriever fixture --show-trace` | Agent 三案例離線軌跡，`--show-trace` 印 `format_trace_trajectory` |
| `python3 -m tfda_context_gate.agent.demo --planner fixture --retriever fixture --show-trace --trace-output /tmp/agent-trace.jsonl` | 同上，另將完整 trace + 軌跡以 JSONL 落地 |
| `python3 -m tfda_context_gate.workflow.demo --log-path /tmp/tfda-a-e-workflow.jsonl` | A–E 端到端基線，經 `run_workflow` 產生完整 E trace |

---

## 13. 硬性邊界與常見陷阱

- **E 只觀測**：不可改 prompt / policy / model / deployment / 醫療答案；`workflow/graph.py` 中 E 僅 `span.set`，不回傳決策、不改 `state` 的 `status` / `final_response`（除 `trace` 本身）。
- **僅兩種 sink**：`InMemoryTraceSink` 與 `JsonlTraceSink`；不要假設存在 SQLite / OpenTelemetry / HTTP sink。
- **脫敏不可繞過**：所有寫入 sink 前皆經 `sanitize_value`；`original_query` 與 `error_message` 皆先 `redact_text`；`query_hash` 為 SHA-256 hex，不可逆。
- **`STARTED` 不代表業務狀態**：`STARTED` 僅為 span 生命週期標記，`trajectory.py` 與指標統計皆以 `COMPLETED` / `BLOCKED` / `INSUFFICIENT` / `FALLBACK` / `ERROR` / `NEEDS_CLARIFICATION` 為準。
- **`SYSTEM/request` 的 `COMPLETED` ≠ 業務成功**：見 9.3，`FALLBACK` 業務的 SYSTEM 關閉仍為 `COMPLETED`，需看 `WorkflowResult.status` 與 `fallback_reason`。
- **`SKIPPED` 保留未用**：`TraceStatus` 定義含 `SKIPPED`，但現行 `graph.py` / `runner.py` 未產生該狀態。
- **證據僅 provenance**：`retrieved_evidence` 只含 `evidence_id` / `rank` / `score` / `source` / `date`，不含文件全文；`trajectory.py` 亦僅顯示前 5 筆。

---

## 14. 測試與驗證

```bash
# 僅 E
python3 -m pytest -q tfda_context_gate/tests/test_e_observability.py

# 含 E 的 workflow / Agent 整合
python3 -m pytest -q tfda_context_gate/tests/test_workflow_integration.py
python3 -m pytest -q tfda_context_gate/tests/test_agent_runtime.py

# 全量
python3 -m pytest -q
```

`test_e_observability.py` 覆蓋 `TraceRecorder` / `TraceSpan` / sink fail-open / 脫敏 / 指標快照；`test_agent_runtime.py` 覆蓋 `format_trace_trajectory` 渲染與 `--show-trace` 軌跡內容。

---

## 15. 文件地圖

```text
00_overview.md          ← 全景圖
05_e_observability.md   ← 本檔（E 深潛）
../../archive/docs/CURRENT_ARCHITECTURE.md          ← Source of Truth（模組/契約/邊界）
../../archive/docs/ARCHITECTURE_AUDIT.md            ← 審計證據
../../tfda_context_gate/README.md                        ← 專案總覽與 Demo 指令
```
