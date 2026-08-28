"""E 觀測層追蹤器（Tracer）— 包住 A-E 的 fail-open 觀測核心。

本模組是 E 層的核心，負責「如何包住 A-E」：
- TraceRecorder：單次請求的觀測容器，構造時即做脫敏與 SYSTEM/STARTED 初始化
- TraceSpan：span() 上下文管理器，實現 STARTED → COMPLETED/ERROR 生命週期
- record / record_failure / record_evaluation / close / snapshot / metrics 等 API
- 與 workflow/graph.py 的對應：每個 graph 節點皆以 trace.span(component, node_name) 包裝

關鍵設計：
- fail-open：_emit() 以 try/except 包住 sink.emit()，任何錯誤僅記入 sink_errors，不影響業務
- 脫敏：構造時對 original_query 做 redact_text + hash_text；每次 record 對 fields 做 sanitize_value
- E 不改答案：僅記錄、不介入路由或改寫（見模組 docstring 與各方法註解）
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .metrics import MetricsCollector
from .privacy import hash_text, redact_text, sanitize_value
from .schemas import (
    E_SCHEMA_VERSION,
    EvaluationRecord,
    RequestMetadata,
    TraceEvent,
    TraceStatus,
    utc_now,
)
from .sinks import InMemoryTraceSink, TraceRecord, TraceSink


def _elapsed_ms(started_at: datetime, completed_at: datetime) -> float:
    """計算延遲毫秒（completed_at - started_at），下限為 0。

    參數:
        started_at: 起始時間
        completed_at: 完成時間

    回傳:
        延遲毫秒（float，至少 0.0）
    """
    return max(0.0, (completed_at - started_at).total_seconds() * 1000)


class TraceSpan:
    """上下文管理器：記錄完成或例外，但不吞掉例外。

    生命週期（對應 workflow/graph.py 的 with trace.span(...) 用法）：
    1. __enter__：寫入一筆 status=STARTED 的事件（started_at 為構造時記錄）
    2. 正常離開 __exit__：寫入 COMPLETED（或 set() 指定的 status）
    3. 拋例外離開 __exit__：寫入 ERROR（含脫敏後的 error_type / error_message），並重新拋出

    與 TraceRecorder 的關係：
    - 由 TraceRecorder.span() 建立，持有 recorder 引用
    - finish() 為顯式完成入口，__exit__ 內部亦呼叫 finish()
    """

    def __init__(
        self,
        recorder: "TraceRecorder",
        component: str,
        node_name: str,
        fields: dict[str, Any],
    ) -> None:
        """初始化 Span（尚未寫入 STARTED，待 __enter__ 時寫入）。

        參數:
            recorder: 所屬的 TraceRecorder
            component: 元件名（A / RAG / B / C / D / AGENT / SYSTEM 等）
            node_name: 節點名（input_router / retrieval / context_gate 等）
            fields: 初始結構化欄位（將在 finish 時一併寫入）
        """
        self.recorder = recorder
        self.component = component
        self.node_name = node_name
        self.started_at = utc_now()  # 記錄 span 起始時間，用於後續計算 latency_ms
        self.status: TraceStatus = "COMPLETED"  # 預設完成狀態，可由 set() 覆蓋
        self.fields = dict(fields)
        self._closed = False  # 是否已 finish（避免重複寫入）
        self._started_recorded = False  # 是否已寫入 STARTED 事件

    def set(self, **fields: Any) -> "TraceSpan":
        """設定結構化欄位，包含最終 status（在離開上下文前呼叫）。

        例如：span.set(status="BLOCKED", router_status="BLOCKED", reason_codes=[...])

        參數:
            **fields: 待寫入的欄位，若含 status 則更新 self.status

        回傳:
            self（支援鏈式呼叫）
        """

        if "status" in fields:
            self.status = fields.pop("status")  # 提取 status 作為 span 最終狀態
        self.fields.update(fields)
        return self

    def finish(self, *, status: TraceStatus | None = None, **fields: Any) -> TraceEvent:
        """顯式完成 span，寫入一筆 TraceEvent。

        參數:
            status: 覆蓋最終狀態（若提供）
            **fields: 額外欄位（合併至 self.fields）

        回傳:
            寫入的 TraceEvent

        流程：
        1. 若尚未寫入 STARTED，先呼叫 __enter__ 補寫
        2. 標記 _closed=True，計算 completed_at 與 latency_ms
        3. 呼叫 recorder.record() 寫入事件（含脫敏與 metrics 更新）
        """
        if self._closed:
            raise RuntimeError("trace span has already been finished")
        if not self._started_recorded:
            self.__enter__()  # 確保 STARTED 已寫入，維持生命週期完整性
        self._closed = True
        if status is not None:
            self.status = status
        self.fields.update(fields)
        completed_at = utc_now()
        return self.recorder.record(
            self.component,
            self.node_name,
            self.status,
            started_at=self.started_at,
            completed_at=completed_at,
            latency_ms=_elapsed_ms(self.started_at, completed_at),
            **self.fields,
        )

    def __enter__(self) -> "TraceSpan":
        """進入上下文：寫入 STARTED 事件（僅一次）。

        回傳:
            self
        """
        if not self._started_recorded:
            self.recorder.record(
                self.component,
                self.node_name,
                "STARTED",  # 【生命週期起點】標記 span 開始，trajectory 渲染時會跳過此事件
                started_at=self.started_at,
                completed_at=None,
                latency_ms=None,  # STARTED 無延遲，待 finish 時計算
                **self.fields,
            )
            self._started_recorded = True
        return self

    def __exit__(self, exc_type, exc_value, _traceback) -> bool:
        """離開上下文：根據是否拋例外決定寫入 COMPLETED 或 ERROR。

        參數:
            exc_type: 例外類型（若無例外則為 None）
            exc_value: 例外值
            _traceback: 追蹤資訊（未使用）

        回傳:
            False（不吞掉例外，讓上層 graph 節點感知失敗）

        邏輯：
        - 若已 _closed（顯式 finish 過），直接返回
        - 若有例外 → finish(status="ERROR", error_type, error_message=redact_text(...))
        - 若無例外 → finish()（使用 set() 設定的 status，預設 COMPLETED）
        """
        if self._closed:
            return False
        if exc_value is not None:
            self.finish(
                status="ERROR",  # 【生命週期異常分支】捕獲例外，標記為 ERROR
                error_type=exc_type.__name__ if exc_type else type(exc_value).__name__,
                error_message=redact_text(str(exc_value)),  # 錯誤訊息亦需脫敏
            )
        else:
            self.finish()  # 【生命週期正常分支】無例外，按 set() 的 status 完成
        return False  # 不吞例外


class TraceRecorder:
    """單次請求的 E 觀測容器，包住 A/RAG/B/Agent/C/D 全流程。

    Observability is fail-open: a sink serialization or filesystem error is
    captured in ``sink_errors`` and never replaces a component's business
    result with a new gate decision.

    核心職責：
    - 構造時脫敏 original_query（redact_text + hash_text）並寫入 SYSTEM/STARTED
    - 提供 span() 上下文管理器供 workflow/graph.py 各節點包裝
    - 提供 record / record_failure / record_evaluation / close / snapshot / metrics API
    - fail-open：_emit() 捕獲所有 sink 錯誤，僅記入 sink_errors
    """

    def __init__(
        self,
        request_id: str,
        *,
        thread_id: str | None = None,
        declared_role: str | None = None,
        original_query: str | None = None,
        schema_version: str = E_SCHEMA_VERSION,
        sink: TraceSink | None = None,
    ) -> None:
        """構造 TraceRecorder（請求級觀測容器）。

        參數:
            request_id: 請求唯一 ID（亦作為 trace_id）
            thread_id: 會話線程 ID（可選）
            declared_role: 宣告角色（可選）
            original_query: 使用者原始查詢（可選，構造時即脫敏）
            schema_version: 綱要版本（預設 e.v0.1）
            sink: 寫出端（預設 InMemoryTraceSink）

        初始化流程：
        1. 對 original_query 做 redact_text（脫敏文本）與 hash_text（不可逆雜湊）
        2. 建立 RequestMetadata（共享中繼資料，所有事件皆攜帶）
        3. 初始化 sink / 事件列表 / 指標收集器 / sink_errors
        4. 呼叫 _metrics.start_request() 計數一次請求
        5. 寫入 SYSTEM/request/STARTED 事件（標記請求開始，對應 close() 的 COMPLETED/ERROR）
        """
        redacted_query = redact_text(original_query) if original_query is not None else None  # 脫敏後文本
        self.request = RequestMetadata(
            request_id=request_id,
            trace_id=request_id,  # trace_id 預設等於 request_id
            thread_id=thread_id,
            schema_version=schema_version,
            declared_role=declared_role,
            original_query=redacted_query,  # 僅存脫敏後文本
            query_hash=hash_text(original_query),  # 同時存不可逆雜湊以便關聯
        )
        self.sink = sink or InMemoryTraceSink()  # 預設記憶體 sink
        self._events: list[TraceEvent] = []
        self._evaluations: list[EvaluationRecord] = []
        self._metrics = MetricsCollector()
        self._metrics.start_request()  # 請求計數 +1
        self.sink_errors: list[str] = []  # 收集 sink 寫入錯誤（fail-open）
        self._closed = False
        self.record(
            "SYSTEM",
            "request",
            "STARTED",  # 【初始化】寫入請求級 STARTED，對應 close() 的完成事件
            started_at=self.request.timestamp,
            completed_at=None,
            latency_ms=None,
        )

    @property
    def events(self) -> list[TraceEvent]:
        """回傳已記錄事件的淺拷貝（避免外部直接改動內部列表）。"""
        return list(self._events)

    @property
    def evaluations(self) -> list[EvaluationRecord]:
        """回傳已記錄評估的淺拷貝。"""
        return list(self._evaluations)

    def span(self, component: str, node_name: str, **fields: Any) -> TraceSpan:
        """建立一個 TraceSpan 上下文管理器（供 workflow/graph.py 包裝節點）。

        用法：
            with trace.span("A", "input_router") as span:
                result = route_request(...)
                span.set(status="COMPLETED", router_status=..., ...)

        參數:
            component: 元件名（A / RAG / B / C / D / AGENT / SYSTEM 等）
            node_name: 節點名
            **fields: 初始欄位

        回傳:
            TraceSpan 實例（進入時寫 STARTED，離開時寫 COMPLETED/ERROR）
        """
        return TraceSpan(self, component, node_name, fields)

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
    ) -> TraceEvent:
        """寫入一筆 TraceEvent（核心寫入 API，所有 span 與直接呼叫皆經此）。

        參數:
            component: 元件名
            node_name: 節點名
            status: 8 種 TraceStatus 之一
            started_at: 起始時間（預設 utc_now）
            completed_at: 完成時間（STARTED/SKIPPED 為 None，其餘預設 utc_now）
            latency_ms: 延遲毫秒（若未提供且 completed 非 None 則自動計算）
            **fields: 任意結構化欄位（寫入前經 sanitize_value 脫敏）

        回傳:
            建立的 TraceEvent

        流程：
        1. 補齊 started_at / completed_at / latency_ms（STARTED 與 SKIPPED 無 completed）
        2. 對 fields 做 sanitize_value 遞迴脫敏
        3. 建立 TraceEvent（攜帶 RequestMetadata 的共享欄位）
        4. 附加至 _events、更新 _metrics、呼叫 _emit() 寫入 sink
        """
        started = started_at or utc_now()
        completed = completed_at
        if completed is None and status not in ("STARTED", "SKIPPED"):
            completed = utc_now()  # 非 STARTED/SKIPPED 需有完成時間
        if latency_ms is None and completed is not None:
            latency_ms = _elapsed_ms(started, completed)  # 自動計算延遲
        payload = sanitize_value(fields)  # 【脫敏】遞迴清洗所有欄位
        event = TraceEvent(
            request_id=self.request.request_id,
            trace_id=self.request.trace_id,
            thread_id=self.request.thread_id,
            schema_version=self.request.schema_version,
            timestamp=utc_now(),
            declared_role=self.request.declared_role,
            original_query=self.request.original_query,
            query_hash=self.request.query_hash,
            component=component,
            node_name=node_name,
            status=status,
            started_at=started,
            completed_at=completed,
            latency_ms=latency_ms,
            **payload,
        )
        self._events.append(event)
        self._metrics.observe(event)  # 更新指標計數與延遲
        self._emit(event)  # 寫入 sink（fail-open）
        return event

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
    ) -> TraceEvent:
        """寫入一筆失敗/兜底事件（record 的便捷封裝）。

        參數:
            component: 元件名
            node_name: 節點名
            failure_type: 失敗類型
            reason_codes: 原因碼列表
            fallback_reason: 兜底原因
            status: 狀態（預設 FALLBACK）
            failed_claims: 未通過的聲明
            invalid_evidence_ids: 無效證據 ID
            **fields: 額外欄位

        回傳:
            建立的 TraceEvent
        """
        return self.record(
            component,
            node_name,
            status,
            failure_type=failure_type,
            reason_codes=list(reason_codes),
            fallback_reason=fallback_reason,
            failed_claims=failed_claims or [],
            invalid_evidence_ids=invalid_evidence_ids or [],
            **fields,
        )

    def record_evaluation(
        self,
        *,
        expected_decision: str | None = None,
        actual_decision: str | None = None,
        outcome: str | None = None,
        failure_type: str | None = None,
        reason_codes: list[str] | tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> EvaluationRecord:
        """寫入一筆離線評估紀錄（用於人工/離線分析，不影響線上流程）。

        參數:
            expected_decision: 預期決策（標註）
            actual_decision: 實際決策
            outcome: 評估結果
            failure_type: 失敗類型
            reason_codes: 原因碼
            metadata: 額外中繼資料（寫入前經 sanitize_value 脫敏）

        回傳:
            建立的 EvaluationRecord
        """
        record = EvaluationRecord(
            request_id=self.request.request_id,
            thread_id=self.request.thread_id,
            schema_version=self.request.schema_version,
            original_query=self.request.original_query,
            query_hash=self.request.query_hash,
            expected_decision=expected_decision,
            actual_decision=actual_decision,
            outcome=outcome,
            failure_type=failure_type,
            reason_codes=list(reason_codes),
            metadata=sanitize_value(metadata or {}),  # 【脫敏】清洗 metadata
        )
        self._evaluations.append(record)
        self._emit(record)  # 寫入 sink（fail-open）
        return record

    def metrics(self):
        """回傳當前的指標快照（MetricsSnapshot）。

        回傳:
            MetricsSnapshot（由 MetricsCollector.snapshot() 產生）
        """
        return self._metrics.snapshot()

    def close(self, *, status: TraceStatus = "COMPLETED", **fields: Any) -> None:
        """關閉請求級追蹤，寫入 SYSTEM/request 完成事件。

        參數:
            status: 完成狀態（預設 COMPLETED，例外時為 ERROR）
            **fields: 額外欄位（例如 error_type / error_message）

        行為：
        - 若已 _closed 則直接返回（冪等）
        - 標記 _closed=True，計算自 request.timestamp 起的總延遲
        - 寫入一筆 SYSTEM/request 事件（與構造時的 STARTED 配對）
        """
        if self._closed:
            return
        self._closed = True
        completed_at = utc_now()
        self.record(
            "SYSTEM",
            "request",
            status,
            started_at=self.request.timestamp,
            completed_at=completed_at,
            latency_ms=_elapsed_ms(self.request.timestamp, completed_at),
            **fields,
        )

    def __enter__(self) -> "TraceRecorder":
        """進入上下文管理器（直接回傳 self，供 with trace: 用法）。"""
        return self

    def __exit__(self, exc_type, exc_value, _traceback) -> bool:
        """離開上下文管理器：根據是否拋例外決定 close 狀態。

        參數:
            exc_type: 例外類型
            exc_value: 例外值
            _traceback: 追蹤資訊

        回傳:
            False（不吞例外）

        邏輯：
        - 無例外 → close()（COMPLETED）
        - 有例外 → close(status="ERROR", error_type, error_message=redact_text(...))
        """
        if exc_value is None:
            self.close()
        else:
            self.close(
                status="ERROR",
                error_type=exc_type.__name__ if exc_type else type(exc_value).__name__,
                error_message=redact_text(str(exc_value)),  # 錯誤訊息脫敏
            )
        return False  # 不吞例外

    def snapshot(self) -> dict[str, Any]:
        """產生完整快照（供持久化、匯出或 trajectory 渲染）。

        回傳:
            含 request / events / evaluations / metrics / sink_errors 的 dict
            （皆為 JSON 相容格式）
        """
        return {
            "request": self.request.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in self._events],
            "evaluations": [record.model_dump(mode="json") for record in self._evaluations],
            "metrics": self.metrics().model_dump(mode="json"),
            "sink_errors": list(self.sink_errors),
        }

    def _emit(self, record: TraceRecord) -> None:
        """寫入 sink（fail-open 邊界）。

        參數:
            record: 待寫出的 TraceRecord

        設計：
        - 以 try/except 包住 sink.emit()，任何例外僅記錄到 sink_errors
        - 不拋出、不覆蓋業務結果，確保 E 層永遠不影響 A-D 的答案
        - 錯誤訊息亦經 redact_text 脫敏
        """
        try:
            self.sink.emit(record)
        except Exception as exc:  # pragma: no cover - defensive sink boundary
            # 【fail-open】sink 錯誤不影響業務，僅記錄錯誤訊息（脫敏後）
            self.sink_errors.append(f"{type(exc).__name__}: {redact_text(str(exc))}")
