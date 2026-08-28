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
    return max(0.0, (completed_at - started_at).total_seconds() * 1000)


class TraceSpan:
    """Context manager that records completion or exception without swallowing it."""

    def __init__(
        self,
        recorder: "TraceRecorder",
        component: str,
        node_name: str,
        fields: dict[str, Any],
    ) -> None:
        self.recorder = recorder
        self.component = component
        self.node_name = node_name
        self.started_at = utc_now()
        self.status: TraceStatus = "COMPLETED"
        self.fields = dict(fields)
        self._closed = False
        self._started_recorded = False

    def set(self, **fields: Any) -> "TraceSpan":
        """Set structured fields, including a final status, before exit."""

        if "status" in fields:
            self.status = fields.pop("status")
        self.fields.update(fields)
        return self

    def finish(self, *, status: TraceStatus | None = None, **fields: Any) -> TraceEvent:
        if self._closed:
            raise RuntimeError("trace span has already been finished")
        if not self._started_recorded:
            self.__enter__()
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
        if not self._started_recorded:
            self.recorder.record(
                self.component,
                self.node_name,
                "STARTED",
                started_at=self.started_at,
                completed_at=None,
                latency_ms=None,
                **self.fields,
            )
            self._started_recorded = True
        return self

    def __exit__(self, exc_type, exc_value, _traceback) -> bool:
        if self._closed:
            return False
        if exc_value is not None:
            self.finish(
                status="ERROR",
                error_type=exc_type.__name__ if exc_type else type(exc_value).__name__,
                error_message=redact_text(str(exc_value)),
            )
        else:
            self.finish()
        return False


class TraceRecorder:
    """Request-scoped E recorder for A/RAG/B/Agent/C/D nodes.

    Observability is fail-open: a sink serialization or filesystem error is
    captured in ``sink_errors`` and never replaces a component's business
    result with a new gate decision.
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
        redacted_query = redact_text(original_query) if original_query is not None else None
        self.request = RequestMetadata(
            request_id=request_id,
            trace_id=request_id,
            thread_id=thread_id,
            schema_version=schema_version,
            declared_role=declared_role,
            original_query=redacted_query,
            query_hash=hash_text(original_query),
        )
        self.sink = sink or InMemoryTraceSink()
        self._events: list[TraceEvent] = []
        self._evaluations: list[EvaluationRecord] = []
        self._metrics = MetricsCollector()
        self._metrics.start_request()
        self.sink_errors: list[str] = []
        self._closed = False
        self.record(
            "SYSTEM",
            "request",
            "STARTED",
            started_at=self.request.timestamp,
            completed_at=None,
            latency_ms=None,
        )

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    @property
    def evaluations(self) -> list[EvaluationRecord]:
        return list(self._evaluations)

    def span(self, component: str, node_name: str, **fields: Any) -> TraceSpan:
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
        started = started_at or utc_now()
        completed = completed_at
        if completed is None and status not in ("STARTED", "SKIPPED"):
            completed = utc_now()
        if latency_ms is None and completed is not None:
            latency_ms = _elapsed_ms(started, completed)
        payload = sanitize_value(fields)
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
        self._metrics.observe(event)
        self._emit(event)
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
            metadata=sanitize_value(metadata or {}),
        )
        self._evaluations.append(record)
        self._emit(record)
        return record

    def metrics(self):
        return self._metrics.snapshot()

    def close(self, *, status: TraceStatus = "COMPLETED", **fields: Any) -> None:
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
        return self

    def __exit__(self, exc_type, exc_value, _traceback) -> bool:
        if exc_value is None:
            self.close()
        else:
            self.close(
                status="ERROR",
                error_type=exc_type.__name__ if exc_type else type(exc_value).__name__,
                error_message=redact_text(str(exc_value)),
            )
        return False

    def snapshot(self) -> dict[str, Any]:
        return {
            "request": self.request.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in self._events],
            "evaluations": [record.model_dump(mode="json") for record in self._evaluations],
            "metrics": self.metrics().model_dump(mode="json"),
            "sink_errors": list(self.sink_errors),
        }

    def _emit(self, record: TraceRecord) -> None:
        try:
            self.sink.emit(record)
        except Exception as exc:  # pragma: no cover - defensive sink boundary
            self.sink_errors.append(f"{type(exc).__name__}: {redact_text(str(exc))}")
