"""E 觀測層指標彙整（Metrics）— 無依賴的計數與延遲統計。

本模組為單一 TraceRecorder 的行程內指標收集器：
- 由 TraceRecorder 持有（self._metrics），每次 record() 後呼叫 observe(event)
- 彙整 request_count / event_count / error_count / fallback_count / blocked_count
- 按 component 分組計數（by_component）與延遲彙總（latency_by_component）
- snapshot() 產生 MetricsSnapshot 供持久化或匯出

與 tracer.py 的對應：
- TraceRecorder.__init__ 呼叫 start_request() 計數一次請求
- TraceRecorder.record() 每次寫入事件後呼叫 _metrics.observe(event)
- TraceRecorder.metrics() / snapshot() 對外暴露快照
"""

from __future__ import annotations

from collections import defaultdict

from .schemas import LatencySummary, MetricsSnapshot, TraceEvent


class MetricsCollector:
    """無依賴的計數與延遲彙總器（單一 recorder 內）。

    設計：
    - 所有計數器初始化為 0，by_component / 延遲字典以 defaultdict 實作
    - observe() 為唯一寫入入口，根據 TraceEvent.status 與 latency_ms 更新
    - snapshot() 產生不可變的 MetricsSnapshot，延遲平均值保留 3 位小數
    """

    def __init__(self) -> None:
        """初始化所有計數器與延遲累加器。"""
        self.request_count = 0  # 請求數（start_request 每次 +1）
        self.event_count = 0  # 事件總數
        self.error_count = 0  # ERROR 狀態數
        self.fallback_count = 0  # FALLBACK / INSUFFICIENT 數
        self.blocked_count = 0  # BLOCKED 數
        self.by_component: dict[str, int] = defaultdict(int)  # 按元件計數
        self._latency_count: dict[str, int] = defaultdict(int)  # 按元件有延遲的事件數
        self._latency_total: dict[str, float] = defaultdict(float)  # 按元件總延遲毫秒

    def start_request(self) -> None:
        """標記一個新請求開始（TraceRecorder 構造時呼叫，request_count +1）。"""
        self.request_count += 1

    def observe(self, event: TraceEvent) -> None:
        """觀測一筆 TraceEvent，更新計數與延遲。

        參數:
            event: 剛寫入的 TraceEvent（由 TraceRecorder.record 傳入）

        更新邏輯：
        - event_count 與 by_component 無條件 +1
        - status == ERROR → error_count +1
        - status in (FALLBACK, INSUFFICIENT) → fallback_count +1
        - status == BLOCKED → blocked_count +1
        - 若 latency_ms 非 None，按 component 累加延遲計數與總和
        """
        self.event_count += 1
        self.by_component[event.component] += 1  # 按元件計數
        if event.status == "ERROR":
            self.error_count += 1  # 錯誤計數
        if event.status in ("FALLBACK", "INSUFFICIENT"):
            self.fallback_count += 1  # 兜底/不足計數
        if event.status == "BLOCKED":
            self.blocked_count += 1  # 阻擋計數
        if event.latency_ms is not None:
            self._latency_count[event.component] += 1  # 有延遲的事件數
            self._latency_total[event.component] += event.latency_ms  # 累加總延遲

    def snapshot(self) -> MetricsSnapshot:
        """產生當前的指標快照（MetricsSnapshot）。

        回傳:
            MetricsSnapshot，包含：
            - 各類計數（request/event/error/fallback/blocked）
            - by_component（按元件排序）
            - latency_by_component（按元件排序，含 count/total_ms/average_ms，平均值 3 位小數）

        實作：
        - 遍歷 _latency_count 的所有 component，計算 LatencySummary
        - total_ms 與 average_ms 皆 round 到 3 位小數
        - by_component 與 latency 皆以 sorted 確保穩定輸出
        """
        latency = {
            component: LatencySummary(
                count=self._latency_count[component],
                total_ms=round(self._latency_total[component], 3),
                average_ms=round(
                    self._latency_total[component] / self._latency_count[component], 3
                ),
            )
            for component in sorted(self._latency_count)
        }
        return MetricsSnapshot(
            request_count=self.request_count,
            event_count=self.event_count,
            error_count=self.error_count,
            fallback_count=self.fallback_count,
            blocked_count=self.blocked_count,
            by_component=dict(sorted(self.by_component.items())),
            latency_by_component=latency,
        )
