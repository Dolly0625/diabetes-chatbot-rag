from __future__ import annotations

from collections import defaultdict

from .schemas import LatencySummary, MetricsSnapshot, TraceEvent


class MetricsCollector:
    """Dependency-free counters and latency summaries for one recorder."""

    def __init__(self) -> None:
        self.request_count = 0
        self.event_count = 0
        self.error_count = 0
        self.fallback_count = 0
        self.blocked_count = 0
        self.by_component: dict[str, int] = defaultdict(int)
        self._latency_count: dict[str, int] = defaultdict(int)
        self._latency_total: dict[str, float] = defaultdict(float)

    def start_request(self) -> None:
        self.request_count += 1

    def observe(self, event: TraceEvent) -> None:
        self.event_count += 1
        self.by_component[event.component] += 1
        if event.status == "ERROR":
            self.error_count += 1
        if event.status in ("FALLBACK", "INSUFFICIENT"):
            self.fallback_count += 1
        if event.status == "BLOCKED":
            self.blocked_count += 1
        if event.latency_ms is not None:
            self._latency_count[event.component] += 1
            self._latency_total[event.component] += event.latency_ms

    def snapshot(self) -> MetricsSnapshot:
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
