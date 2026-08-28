"""E v0.1: observability and evaluation data collection.

E is intentionally cross-cutting and observational. It does not make medical
decisions, change A/B/C/D policy, or provide Agent execution authority.
"""

from .metrics import MetricsCollector
from .privacy import hash_text, redact_text, sanitize_value
from .schemas import (
    E_SCHEMA_VERSION,
    EvaluationRecord,
    MetricsSnapshot,
    RequestMetadata,
    TraceEvent,
)
from .sinks import InMemoryTraceSink, JsonlTraceSink
from .tracer import TraceRecorder
from .trajectory import format_trace_trajectory

__all__ = [
    "E_SCHEMA_VERSION",
    "EvaluationRecord",
    "InMemoryTraceSink",
    "JsonlTraceSink",
    "MetricsCollector",
    "MetricsSnapshot",
    "RequestMetadata",
    "TraceEvent",
    "TraceRecorder",
    "format_trace_trajectory",
    "hash_text",
    "redact_text",
    "sanitize_value",
]
